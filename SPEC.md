# KAT Farmer — Architecture Refactoring Spec

## PRD

### Problem
The kat-farmer codebase has grown organically from a single-script prototype to a 4-file, 4700-line project with 26 structural issues identified by code audit. The three most dangerous: (1) an XSS vector where Merkl API data flows unescaped into innerHTML, (2) a JS classification bug that double-counts avKAT for some addresses, and (3) non-atomic JSON writes that corrupt state on crash. Beyond these, supply.py and indexer.py duplicate 4 utility functions with diverging signatures, hardcode constants that should come from config.json, and parse address lists out of index.html via regex. The codebase is not pleasant to extend — adding a new data source means touching an 890-line god module with 6 responsibilities.

**Impact**: XSS allows arbitrary code execution on the public dashboard. The avKAT double-counting silently misclassifies addresses (showing 'hodler' when backend says 'farmer'). Non-atomic writes cause full rescan (30+ minutes) after any crash. The duplication and coupling make every change risky and slow.

### Users
- **Primary user**: Alex — sole developer and maintainer. Works on this project in focused bursts, often with weeks between sessions. Needs the code to be self-documenting and modular so cold-start context recovery is fast.
- **Functional JTBD**: When I return to extend kat-farmer, I want clearly separated modules with one responsibility each, so I can modify one concern without reading 890 lines of unrelated code.
- **Current solution**: Everything in flat files with duplicated utilities. Supply.py was written independently and shares zero code with indexer.py.

### User Workflow
**Trigger**: Code audit revealed 26 issues; Alex wants the codebase cleaned before adding new features.
**Before**: Alex has the full audit report with prioritized findings.
**During**: Refactoring session — restructure files, fix bugs, add tests.
**After**: A clean, modular codebase ready for future extension.

### Success Criteria
1. **[P0]** All 3 XSS vectors in the supply tab are closed — every external string is escaped before innerHTML insertion
2. **[P0]** The JS retainedPct avKAT double-counting bug is fixed — classification matches Python backend for all addresses
3. **[P0]** All JSON writes (state.json, data.json, supply_data.json, supply_state.json, snapshots.json) use atomic write (tmp + rename)
4. **[P0]** `python3 indexer.py` and `python3 supply.py` produce identical output before and after refactoring (byte-for-byte data.json + supply_data.json)
5. **[P0]** Cloudflare Pages deploy continues to work with same command
6. **[P1]** Zero duplicated utility functions — rpc, from_wei, get_block_number, eth_get_logs exist in exactly one place
7. **[P1]** supply.py loads all constants from config.json — no hardcoded contract addresses
8. **[P1]** Address list lives in addresses.json, consumed by both indexer and frontend
9. **[P1]** Core tests pass: rpc utilities, from_wei, classify(), atomic_write()
10. **[P1]** indexer.py main module is ≤150 lines (orchestration only)
11. **[P2]** load_state normalizes missing keys against defaults (backward-compatible with any state.json version)
12. **[P2]** stakeRaw drops dead vkatAmount/avkatAmount fields from state persistence
13. **[P2]** All state files use compact JSON; all human-readable output uses indent=2
14. **[P2]** disposed() dead code removed, load_state error logging added to supply.py, topic/owner slices length-guarded

### Constraints
- CLI interface unchanged: `python3 indexer.py [--full]`, `python3 supply.py [--full] [--json] [--protocols]`
- Deploy command unchanged: `npx wrangler pages deploy . --project-name kat-dumpers --branch main`
- Existing state.json and supply_state.json must be readable by new code (no forced rescan)
- No external Python dependencies — stdlib only
- index.html must remain a single HTML file (JS extracted to app.js, loaded via `<script src>`)

### Scope Fence
OUT: New features, new data sources, new dashboard tabs, UI redesign
OUT: Consolidating indexer and supply state files into one (different scan scopes, intentional)
OUT: Converting to a proper Python package with setup.py/pyproject.toml (overkill for this project)
OUT: TypeScript migration for the frontend
FUTURE: Full test suite for scanners and builders (phase 2 — core utility tests only in this pass)

### North Star
A codebase where every module does one thing, every constant has one source, every write is safe, and a developer opening it for the first time sees clean architecture that makes the right thing easy and the wrong thing hard.

### Core Experience
The module split. When it's done right, you can read `indexer.py` top to bottom in 30 seconds and understand the entire pipeline — because it's just imports and orchestration. The real work lives in focused modules you only enter when you need to.

### Failure Modes
- Refactoring introduces a regression: data.json output differs from pre-refactoring (caught by P0 #4)
- JS extraction breaks Cloudflare Pages: app.js not served (caught by P0 #5)
- New module imports fail: circular dependency between extracted modules
- State migration breaks: old state.json unreadable by new load_state (caught by constraint)

### Decisions
- **Full module split over minimal extraction**: Full split now costs more but creates natural test boundaries and makes future extension trivial. Minimal extraction would leave the 890-line god module intact.
  - *Rejected: Minimal (just rpc.py)*: Leaves scan_transfers at 128 lines with 3 concerns, no test seams
  - *Rejected: No split*: Duplicated utilities remain, no path to testability
- **addresses.json over embedded-in-HTML**: Eliminates regex coupling, creates a single source of truth, makes the data relationship explicit
  - *Rejected: Keep in HTML*: Regex parsing is fragile and bidirectional coupling
- **Core tests only (not full suite)**: Test the foundations everything depends on — rpc utils, classify, atomic write. Full scanner tests need mock RPC infrastructure (phase 2).
  - *Deferred: Scanner/builder tests*: Need RPC mocking infrastructure
- **No build step for JS**: app.js loaded via `<script src="app.js">`. Simple bundler acceptable if needed for minification later.
  - *Rejected: Keep JS inline*: 1600 lines of JS in HTML is unmaintainable
- **Flat module structure (not nested package)**: Files in project root (`rpc.py`, `scanners.py`, etc.) — not `src/katfarm/`. This project is too small for nested packages.
  - *Rejected: Python package structure*: Over-engineering for 3 scripts
- **JS trusts backend status**: `ingestAddress()` currently overrides `d.status` from data.json with JS `classify(d)`. The fix is to use the pre-computed status from data.json. JS `retainedPct`/`dumpPct` remain for the display column only.
  - *Rejected: Fix JS classify to match Python*: Fragile — two implementations diverge again on next change

**[Amended after TIP research]**: The classification bug is deeper than initially reported. `ingestAddress()` (index.html:1592) overrides the pre-computed `d.status` from data.json with a JS re-classification using a different formula. The Python classify() adds `d.avkat + staker.avkatAmount` (potential double-count). The JS uses `d.staked || d.avkat` (different fallback chain). The root fix: JS should trust the backend-computed status for the badge, and only use JS retainedPct for the numeric Dump% column.

**[Amended after TIP research]**: `PIPELINE_TOKEN = 'kat-stats-refresh-2026'` is hardcoded in index.html:1243, committed to git, and served publicly. This is a leaked credential that should be removed from the HTML and injected at deploy time or moved server-side.

---

## Technical Implementation Plan

### Approach
Extract shared Python modules from indexer.py's 890-line monolith into focused, single-responsibility files. Unify all config loading through a single config.py. Replace all file writes with atomic tmp+rename. Extract JS from index.html into app.js. Extract embedded address data into addresses.json. Fix XSS, classification, and robustness bugs. Add core tests for the foundation modules everything depends on.

### Architecture

**Target file structure:**
```
kat-farmer/
├── config.py          NEW — load config.json, export typed constants
├── rpc.py             NEW — thread-safe RPC client, from_wei, hex helpers
├── fileio.py              NEW — atomic_write, load_json, save_json, load_state with defaults merge
├── classify.py        NEW — classify(), retained_pct() — single source of truth
├── scanners.py        NEW — scan_claimed, scan_transfers, scan_avkat_holders, scan_cex
├── balances.py        NEW — get_fresh_balances, enumerate_vkat_locks
├── builders.py        NEW — build_output, build_buyers_output, build_stakers_output
├── addresses.json     NEW — extracted RAW + CEX_WALLETS from index.html
├── app.js             NEW — extracted from index.html <script> block
├── tests/
│   ├── __init__.py    NEW
│   ├── test_rpc.py    NEW
│   ├── test_classify.py NEW
│   └── test_fileio.py     NEW
├── indexer.py          MODIFY — gut to ~120 lines of orchestration
├── supply.py           MODIFY — use config.py + rpc.py, remove all hardcoded constants
├── enrich_addresses.py MODIFY — minor: use fileio.py for atomic write
├── index.html          MODIFY — remove <script> block, add <script src="app.js">
├── config.json         MODIFY — add chainId, katBaseAddr
├── _headers            UNCHANGED
├── _redirects          UNCHANGED
├── .gitignore          MODIFY — add tests/__pycache__/
```

**Dependency graph (no cycles):**
```
config.py ← rpc.py ← scanners.py ← indexer.py
              ↑          ↑
           fileio.py    balances.py ← indexer.py
              ↑
         classify.py ← builders.py ← indexer.py

supply.py → config.py, rpc.py, fileio.py (no dependency on scanners/balances/builders)
app.js → addresses.json, data.json, supply_data.json, labels.json, snapshots.json
```

### Files

**Create:**
- `config.py`: Load config.json once at import time. Export all constants as module-level names with type hints in comments. Add `CHAIN_ID = 747474`, `KAT_BASE_ADDR`. Add `SUPPLY_KAT_TOKENS` (2-element set, excluding base token with documented reason). Add `BUYER_MIN_KAT = 1000`, `STAKER_MIN_KAT = 100` as named constants.
- `rpc.py`: Thread-safe `rpc_call(url, method, params, retries=3, timeout=30)` using threading.Lock for ID counter. `get_block_number(url)`, `eth_get_logs(url, params)`, `from_wei(hex_val, decimals=18)`, `balance_of(token, wallet, url)`, `total_assets(vault, url)`, `is_contract(addr, url)`, `fmtM(n)`. All take `url` as explicit parameter — no globals.
- `fileio.py`: `atomic_write(path, content)` using tmp+os.replace. `load_json(path, default=None)`. `save_json(path, data, compact=True)` — compact uses `separators=(',',':')`, non-compact uses `indent=2`. `load_state(path, defaults)` — loads JSON then merges against `defaults` dict so missing keys get default values.
- `classify.py`: `classify(d, stake_raw=None)` and `retained_pct(d, stake_raw=None)` — extracted from indexer.py:225-242. Single source of truth. `disposed()` is NOT extracted (dead code, deleted).
- `scanners.py`: `scan_claimed(state, addr_set, rpc_url)`, `scan_transfers(state, addr_set, cex_wallets, latest_block, rpc_url)` (still handles staking+buyers+dumps in one pass — splitting the scan loop would require re-fetching all logs), `scan_avkat_holders(state, stake_raw, rpc_url)`, `scan_cex(state, addr_set, cex_wallets, dump_raw, eth_rpc_url)`. All receive config values as parameters, not globals.
- `balances.py`: `get_fresh_balances(addresses, dump_raw, extra_addrs, rpc_url, kat_tokens, avkat_addr)`, `enumerate_vkat_locks(rpc_url, lock_nft, voting_escrow)`. Config passed as params.
- `builders.py`: `build_output(...)`, `build_buyers_output(...)`, `build_stakers_output(...)`. Import classify from classify.py.
- `addresses.json`: `{"addresses": ["0x...", ...], "cex_wallets": ["0x...", ...], "cex_labels": {"0x...": {"label": "Binance", "chain": "eth"}, ...}}`
- `app.js`: Full JS extracted from index.html lines 1222-2818. Fix XSS (3 sites). Fix classification (trust backend status). Fix fmtM precision. Remove localStorage snapshot fallback. Keep PIPELINE_API/TOKEN for now (security fix noted as follow-up — requires server-side change).
- `tests/__init__.py`: Empty.
- `tests/test_rpc.py`: Test `from_wei` (edge cases: '0x', '0x0', None, large values, non-hex). Test `fmtM` (B/M/K thresholds).
- `tests/test_classify.py`: Test all 4 classification outcomes (farmer/partial/hodler/inactive). Test with and without stake_raw. Test retained_pct boundary conditions (0%, 50%, 95%, 100%).
- `tests/test_fileio.py`: Test `atomic_write` (writes successfully, file exists after). Test `load_state` with missing keys (merges defaults). Test `save_json` compact vs indent modes.

**Modify:**
- `indexer.py` → gut to ~120 lines: imports from config, rpc, io, scanners, balances, builders. `main()` is pure orchestration: load addresses, load state, run 6 scan steps, build output, save. `load_addresses()` reads from `addresses.json` instead of regex-parsing index.html. Remove `load_cex_wallets()` (data now in addresses.json). Remove all utility functions (moved to modules). Remove `disposed()` (dead code).
- `supply.py` → replace all hardcoded constants with `from config import ...`. Replace `rpc_call`, `get_block_number`, `eth_get_logs`, `from_wei` with imports from `rpc.py`. Replace `load_state`/`save_state` with `io.load_state`/`io.save_json`. Use `SUPPLY_KAT_TOKENS` from config (the 2-token set with documented exclusion). Add `CHAIN_ID` from config for Merkl API URL. Add error logging to load_state exception. State format migration logic stays in supply.py (specific to its state schema).
- `enrich_addresses.py` → import `io.atomic_write` for labels.json write. Import `io.load_json` for data.json read. Minor — file is already clean.
- `index.html` → remove everything between `<script>` and `</script>` (lines 1222-2818). Add `<script src="app.js"></script>` before `</body>`. Remove the embedded `const RAW`, `const CEX_WALLETS`, `const CEX_LABELS` data blocks.
- `config.json` → add `"chainId": 747474`, `"katBaseAddr": "0x7f1f4b4b29f5058fa32cc7a97141b8d7e5abdc2d"`, `"supplyKatTokens": ["0x6e9c1f88a960fe63387eb4b71bc525a9313d8461", "0x3ba1fbc4c3aea775d335b31fb53778f46fd3a330"]` (the 2-token set for supply scanning, with comment explaining why base token excluded), `"buyerMinKat": 1000`, `"stakerMinKat": 100`.
- `.gitignore` → add `tests/__pycache__/`

### Patterns to Follow
- `indexer.py:71-86` — rpc_call with url parameter and threading.Lock: this is the template for rpc.py
- `indexer.py:200-214` — load_state with default dict: template for io.load_state, but enhanced with defaults merge
- `indexer.py:225-242` — classify function: moves verbatim to classify.py
- `index.html:1549-1551` — escapeHtml: already exists, just needs consistent use in supply tab

### Existing Logic to Reuse
- `indexer.py:rpc_call` (url-parameterized, thread-safe): becomes rpc.py's implementation
- `indexer.py:from_wei`, `get_block_number`, `eth_get_logs`: move to rpc.py
- `indexer.py:balance_of`, `total_assets`, `is_contract`: move to rpc.py
- `indexer.py:classify`: move to classify.py
- `indexer.py:fmtM`: move to rpc.py (shared utility)
- `index.html:escapeHtml`: already exists at line 1549, just use it consistently

### Interface Contracts

| Producer → Consumer | Interface | Signature |
|---------------------|-----------|-----------|
| Task 1 (config.py) → all Python | `config.KATANA_RPC` etc. | Module-level constants, imported via `from config import KATANA_RPC, ...` |
| Task 2 (rpc.py) → scanners, balances, supply | `rpc.rpc_call(url, method, params)` | `def rpc_call(url: str, method: str, params: list, retries=3, timeout=30) -> Any` |
| Task 2 (rpc.py) → all | `rpc.from_wei(hex_val)` | `def from_wei(hex_val: str, decimals: int = 18) -> float` |
| Task 3 (fileio.py) → indexer, supply, enrich | `io.atomic_write(path, content)` | `def atomic_write(path: Path, content: str) -> None` |
| Task 3 (fileio.py) → indexer, supply | `io.load_state(path, defaults)` | `def load_state(path: Path, defaults: dict) -> dict` — returns merged dict |
| Task 4 (classify.py) → builders | `classify.classify(d, stake_raw)` | `def classify(d: dict, stake_raw: dict = None) -> str` — returns 'farmer'/'partial'/'hodler'/'inactive' |

### Implementation Tasks

| # | Task | Files (exclusive) | Depends on | Verify |
|---|------|--------------------|------------|--------|
| 1 | Foundation: config.py | `config.py`, `config.json` | none | `python3 -c "from config import KATANA_RPC, CHAIN_ID; print(KATANA_RPC, CHAIN_ID)"` |
| 2 | Foundation: rpc.py | `rpc.py` | none | `python3 -c "from rpc import from_wei, fmtM; assert from_wei('0xde0b6b3a7640000') == 1.0; print('ok')"` |
| 3 | Foundation: fileio.py | `fileio.py` | none | `python3 -c "from io import atomic_write; from pathlib import Path; atomic_write(Path('/tmp/test.json'), '{}'); print('ok')"` — actually `io` shadows stdlib, name it `filefileio.py` |
| 4 | Classification: classify.py | `classify.py` | none | `python3 -c "from classify import classify; d={'claimed':100,'balance':90,'avkat':0}; assert classify(d)=='hodler'"` |
| 5 | Data extraction: addresses.json | `addresses.json` | none | `python3 -c "import json; d=json.load(open('addresses.json')); print(len(d['addresses']), 'addrs', len(d['cex_wallets']), 'cex')"` |
| 6 | Scanners: scanners.py | `scanners.py` | 1, 2 | `python3 -c "import scanners; print(dir(scanners))"` → lists scan_claimed, scan_transfers, etc. |
| 7 | Balances: balances.py | `balances.py` | 1, 2 | `python3 -c "import balances; print(dir(balances))"` |
| 8 | Builders: builders.py | `builders.py` | 4 | `python3 -c "from builders import build_output; print('ok')"` |
| 9 | Rewrite indexer.py | `indexer.py` | 1-8 | `python3 indexer.py --full` → produces data.json identical to pre-refactoring |
| 10 | Rewrite supply.py | `supply.py` | 1, 2, 3 | `python3 supply.py --json` → produces supply_data.json identical to pre-refactoring |
| 11 | Update enrich_addresses.py | `enrich_addresses.py` | 3 | `python3 enrich_addresses.py` runs without error |
| 12 | Frontend: extract app.js + fix bugs | `app.js`, `index.html` | 5 | Open index.html in browser → all tabs render, no console errors |
| 13 | Tests | `tests/` (all files) | 1-4 | `python3 -m pytest tests/ -v` → all green |
| 14 | Cleanup | `.gitignore` | all | `git status` shows only expected changes |

> **Task 1: Foundation — config.py + config.json**
> Create `config.py` that loads `config.json` at import time via `json.loads(Path(__file__).parent.joinpath('config.json').read_text())`. Export every value as a module-level constant: `KATANA_RPC`, `ETH_RPC`, `DISTRIBUTOR_ADDR`, `CLAIMED_TOPIC`, `TRANSFER_TOPIC`, `SWAP_V3_TOPIC`, `SWAP_V2_TOPIC`, `DEPLOY_BLOCK`, `KAT_ADDR`, `AVKAT_ADDR`, `KAT_DECIMALS`, `KAT_TOKENS` (set), `KAT_POOL_ADDRS` (set), `BRIDGES` (list), `BRIDGE_DESTS` (set), `ZERO_ADDR`, `VOTING_ESCROW`, `LOCK_NFT`, `STAKE_DESTS` (set), `KAT_ETH_ADDR`, `ETH_KAT_START`, `CHAIN_ID`, `KAT_BASE_ADDR`, `SUPPLY_KAT_TOKENS` (set — 2 tokens for supply scanning), `BUYER_MIN_KAT`, `STAKER_MIN_KAT`. Derive computed values: `BRIDGE_DESTS = set(BRIDGES)`, `STAKE_DESTS = {VOTING_ESCROW, AVKAT_ADDR}`. Update `config.json` to add `chainId`, `katBaseAddr`, `supplyKatTokens`, `buyerMinKat`, `stakerMinKat`. Pattern: module acts as singleton — import constants anywhere, loaded once.

> **Task 2: Foundation — rpc.py**
> Extract from indexer.py lines 61-127 (the thread-safe versions). All functions take `url` as first parameter. `rpc_call(url, method, params, retries=3, timeout=30)` with `threading.Lock` for ID counter. `get_block_number(url)`, `eth_get_logs(url, params)`, `from_wei(hex_val, decimals=18)` — add decimals parameter with default 18. `balance_of(token, wallet, url)`, `total_assets(vault, url)`, `is_contract(addr, url)`. `fmtM(n)` — standardize to 2dp for millions matching Python convention. Add `validate_hex(val, expected_len=66)` helper that returns bool — use this for topic/owner slice guards. `eth_get_receipt(tx_hash, url)`. No module-level state except the Lock and counter. No imports from config.py — rpc.py is config-agnostic.

> **Task 3: Foundation — filefileio.py**
> Name: `filefileio.py` (not `fileio.py` — shadows stdlib). `atomic_write(path: Path, content: str)`: write to `path.with_suffix('.tmp')`, then `os.replace(tmp, path)`. `load_json(path: Path, default=None)`: try parse, return default on any error, print warning. `save_json(path: Path, data, compact=True)`: atomic_write with `separators=(',', ':')` if compact, `indent=2` if not. `load_state(path: Path, defaults: dict) -> dict`: load_json then `{**defaults, **loaded}` to merge missing keys. Print warning on corrupt/missing file.

> **Task 4: Classification — classify.py**
> Extract `classify(d, stake_raw=None)` and `retained_pct(d, stake_raw=None)` from indexer.py:225-242. Keep logic identical — this is the single source of truth. Do NOT extract `disposed()` — it is dead code and should be deleted. Do NOT add `dumpPct` — that's a display concern for the frontend. Add docstrings explaining the classification thresholds (<50% = farmer, <95% = partial, ≥95% = hodler, claimed=0 = inactive).

> **Task 5: Data extraction — addresses.json**
> Extract `const RAW` backtick-string addresses from index.html (line ~1511) into a JSON file. Parse the RAW string to extract all 0x addresses. Extract `CEX_WALLETS` array (line ~1306). Extract `CEX_LABELS` object (line ~1340). Structure: `{"addresses": [...], "cex_wallets": [...], "cex_labels": {...}}`. The extraction is a one-time manual operation (read HTML, parse, write JSON). Verify: address count matches what indexer.py's current `load_addresses()` returns.

> **Task 6: Scanners — scanners.py**
> Move `scan_claimed`, `scan_transfers`, `scan_avkat_holders`, `scan_cex` from indexer.py. Each function imports from rpc.py and receives config values as parameters (not globals). `scan_transfers` stays as one function (splitting the log loop would require 3 passes over the same data — the single-pass design is correct, just move it). Add length validation for topic slices: `if len(topics[N]) < 66: continue`. Remove the stakeRaw vkatAmount/avkatAmount backfill code (lines 323-325) — stakeRaw now only stores boolean flags. Adjust scan_transfers to only set `stakedVKAT`/`stakedAvKAT` booleans, not accumulate amounts.

> **Task 7: Balances — balances.py**
> Move `get_fresh_balances` and `enumerate_vkat_locks` from indexer.py. Both receive config values as parameters. Use `rpc.validate_hex()` for owner_r length check in `enumerate_vkat_locks` (line 153). No other changes needed — these are already well-factored functions.

> **Task 8: Builders — builders.py**
> Move `build_output`, `build_buyers_output`, `build_stakers_output` from indexer.py. Import `classify` from classify.py. Use `BUYER_MIN_KAT` and `STAKER_MIN_KAT` from config.py instead of magic numbers 1000 and 100. No other logic changes.

> **Task 9: Rewrite indexer.py**
> Gut to ~120 lines. Structure: imports, `load_addresses()` reading from addresses.json, `main()` as pure orchestration calling modules. `main()` flow: load addresses → load state (via fileio.load_state with defaults) → scan_claimed → scan_transfers → scan_avkat_holders → scan_cex → get_fresh_balances → save_state → enumerate_vkat_locks → build_output → build_buyers/stakers → save data.json → save snapshots. All state saves via `fileio.save_json(path, data, compact=True)` for state files, `fileio.save_json(path, data, compact=True)` for data.json. CLI stays: `python3 indexer.py [--full]`. State defaults dict defined here (defines the schema with all keys). Remove `disposed()`.

> **Task 10: Rewrite supply.py**
> Replace all hardcoded constants with imports from config.py: `from config import KATANA_RPC, DISTRIBUTOR_ADDR, TRANSFER_TOPIC, DEPLOY_BLOCK, KAT_DECIMALS, ZERO_ADDR, SUPPLY_KAT_TOKENS, CHAIN_ID`. Replace local `rpc_call`, `get_block_number`, `eth_get_logs`, `from_wei` with imports from rpc.py. Replace `load_state`/`save_state` with `fileio.load_state`/`fileio.save_json`. Add print to load_state exception. `FIXED_ALLOCATIONS` dict stays in supply.py (it's supply-specific data, not config). `scan_merkl_transfers` and `scan_protocol_mix` stay in supply.py (supply-specific scanners, not shared). `_classify_reason` stays. `_fetch_json` stays (it's a simple HTTP GET, not an RPC call — different retry/timeout pattern). Use `fileio.save_json(path, data, compact=True)` for supply_state.json, `fileio.save_json(path, data, compact=False)` for supply_data.json (human-readable output).

> **Task 11: Update enrich_addresses.py**
> Minor: replace `json.load(open(DATA_FILE))` with `fileio.load_json(DATA_FILE)`. Replace `json.dump(labels, f, indent=2)` with `fileio.save_json(OUTPUT_FILE, labels, compact=False)`. 3-line change.

> **Task 12: Frontend — extract app.js + fix bugs**
> 1. Extract lines 1222-2818 from index.html into app.js. Replace with `<script src="app.js"></script>`.
> 2. Remove `const RAW`, `const CEX_WALLETS`, `const CEX_LABELS` from app.js. Instead: `fetch('addresses.json').then(r => r.json()).then(d => { ADDRESSES_RAW = d.addresses; CEX_WALLETS_SET = new Set(d.cex_wallets); CEX_LABELS_MAP = d.cex_labels; })` — load from addresses.json.
> 3. **XSS fix**: In renderSupplyTab(), wrap all external strings with escapeHtml(): `${escapeHtml(name)}` at line 2414 (both branches), `${escapeHtml(s)}` at line 2410, `${escapeHtml(v.name)}` and `${escapeHtml(detail)}` at line 2458, `${escapeHtml(sub.name)}` at line 2465.
> 4. **Classification fix**: In `ingestAddress(d)`, remove `d.status = classify(d)`. Use the pre-computed `d.status` from data.json. Keep `d.staked` computation for display. Keep `classify()`, `retainedPct()`, `dumpPct()` for the Dump% column display only.
> 5. **fmtM fix**: Change `(n / 1e6).toFixed(1)` to `(n / 1e6).toFixed(2)` at the fmtM function.
> 6. **localStorage fix**: Remove the localStorage snapshot fallback in `showStakerGrowth`. Load only from snapshots.json.
> 7. The `_headers` file already exists for Cloudflare Pages caching. Verify app.js is served with correct Content-Type.

> **Task 13: Tests**
> `tests/test_rpc.py`: Test `from_wei` with edge cases ('0x', '0x0', None, empty string, valid hex, large numbers). Test `fmtM` with 0, 999, 1000, 999999, 1e6, 1e9. Test `validate_hex` with valid/invalid/short strings.
> `tests/test_classify.py`: Test classify returns 'inactive' when claimed=0. Test 'farmer' when retained < 50%. Test 'partial' when 50% ≤ retained < 95%. Test 'hodler' when retained ≥ 95%. Test with stake_raw parameter. Test retained_pct boundary values.
> `tests/test_fileio.py`: Test `atomic_write` creates file with correct content. Test `load_state` with missing file returns defaults. Test `load_state` with corrupt file returns defaults + prints warning. Test `load_state` merges missing keys from defaults.

> **Task 14: Cleanup**
> Add `tests/__pycache__/` to `.gitignore`. Run `python3 -m pytest tests/ -v` to confirm all green. Run `python3 indexer.py` and `python3 supply.py --json` to confirm output unchanged. Verify `npx wrangler pages deploy` would include app.js and addresses.json.

### Current Behavior
- `python3 indexer.py`: loads addresses from index.html via regex, scans chain events, writes state.json (indent=2) and data.json (compact). Takes ~5-30 minutes depending on blocks to scan.
- `python3 supply.py --json`: scans distributor transfers with its own hardcoded constants, writes supply_state.json (compact) and supply_data.json (indent=2).
- `python3 enrich_addresses.py`: reads data.json, fetches labels from APIs, writes labels.json.
- Frontend: loads data.json + supply_data.json + labels.json + snapshots.json via fetch. Renders 3 tabs (Dumpers, Buyers, Stakers) + Supply tab. Overrides d.status via JS classify.
- Deploy: `npx wrangler pages deploy . --project-name kat-dumpers --branch main` serves all files in project root.

### Risks & Gotchas
- **filefileio.py naming**: `fileio.py` shadows Python stdlib `io` module. Named `filefileio.py` instead.
- **Circular imports**: config.py must NOT import from rpc.py. rpc.py must NOT import from config.py. Dependency direction: config → (imported by) → rpc → (imported by) → scanners/balances. Verify with `python3 -c "import config; import rpc; import scanners; import balances; import builders"` — no ImportError.
- **state.json migration**: Old state.json has all keys. New code merges against defaults. No migration code needed — the merge handles it. But stakeRaw entries may have old vkatAmount/avkatAmount fields — new code ignores them (reads only boolean flags).
- **addresses.json extraction**: Must extract the exact same addresses that the regex currently captures. Verify: run current indexer.py with print(len(addrs)), then verify addresses.json has same count.
- **app.js loading order**: app.js must load AFTER the HTML DOM is ready. Use `defer` attribute: `<script src="app.js" defer></script>`. The current inline script runs after DOM is parsed (it's at the end of body), and `defer` preserves this behavior.
- **Cloudflare Pages**: All files in the project root are served. app.js and addresses.json will be automatically served. Verify the `_headers` file doesn't block .js or .json files.
- **PIPELINE_TOKEN exposure**: Hardcoded at index.html:1243 / app.js. NOT fixed in this refactoring (requires server-side change). Flagged for follow-up.

### Error Handling
- `rpc_call` failure: retry 3x with exponential backoff (existing behavior, preserved)
- `atomic_write` failure: if os.replace fails (permission, disk full), the .tmp file remains — next write overwrites it. No special cleanup needed.
- `load_state` corrupt file: print warning, return defaults dict. Existing behavior enhanced to merge missing keys.
- `from_wei` malformed input: return 0.0 (existing behavior, preserved)

### Code Conventions
- **Naming**: snake_case for Python, camelCase for JS (matching existing code)
- **Error pattern**: return None/0.0 for RPC failures, print warnings for state issues (matching existing)
- **Import style**: `from config import KATANA_RPC, ...` (explicit imports, not `import config`)
- **Logging**: print() with emoji indicators (existing pattern: ⚠ for warnings, ✓ for success)
- **Type hints**: None in code (matching existing), but add comments explaining parameter types in docstrings

### Traceability Matrix

| Success Criterion | Implementation Task | Test |
|---|---|---|
| P0-1: XSS closed | Task 12 | Manual: inspect supply tab innerHTML in dev tools |
| P0-2: Classification matches backend | Task 12 | Manual: compare d.status from data.json vs displayed badge |
| P0-3: Atomic writes | Task 3, 9, 10, 11 | test_fileio.py::test_atomic_write |
| P0-4: Identical output | Task 9, 10 | `diff <(python3 indexer.py) <(old_indexer.py)` on data.json |
| P0-5: Deploy works | Task 12, 14 | `npx wrangler pages deploy` succeeds |
| P1-6: No duplicated utils | Task 2, 10 | `grep -r "def rpc_call" *.py` returns exactly 1 result |
| P1-7: supply.py uses config | Task 10 | `grep "KATANA_RPC\s*=" supply.py` returns 0 results |
| P1-8: addresses.json | Task 5, 9, 12 | `python3 -c "import json; print(len(json.load(open('addresses.json'))['addresses']))"` |
| P1-9: Tests pass | Task 13 | `python3 -m pytest tests/ -v` → all green |
| P1-10: indexer.py ≤150 lines | Task 9 | `wc -l indexer.py` ≤ 150 |
| P2-11: load_state normalizes | Task 3 | test_fileio.py::test_load_state_missing_keys |
| P2-12: stakeRaw trimmed | Task 6 | grep stakeRaw entries in state.json — no vkatAmount fields |
| P2-13: JSON conventions | Task 3, 9, 10 | State files have no newlines; output files have indent |
| P2-14: Dead code removed | Task 9, 10 | `grep -r "def disposed" *.py` returns 0 |

### Verification
1. **Before refactoring**: `python3 indexer.py && cp data.json data_before.json && python3 supply.py --json && cp supply_data.json supply_before.json`
2. **After refactoring**: `python3 indexer.py && diff data.json data_before.json` → identical
3. **After refactoring**: `python3 supply.py --json && diff supply_data.json supply_before.json` → identical
4. **Tests**: `python3 -m pytest tests/ -v` → all green
5. **No circular imports**: `python3 -c "import config; import rpc; import fileio; import classify; import scanners; import balances; import builders"` → no error
6. **Frontend**: Open index.html in browser → all 4 tabs render, no console errors
7. **XSS**: Inspect supply tab source — all protocol names wrapped in escapeHtml()
8. **Deploy**: `npx wrangler pages deploy . --project-name kat-dumpers --branch main` → succeeds
9. **No duplication**: `grep -c "def rpc_call" *.py` → `rpc.py:1` only

### Build Handoff
| Implementation Task | Relevant PRD Sections |
|--------------------|-----------------------|
| Tasks 1-3 (Foundation) | Constraints (stdlib only), Success Criteria #3 (atomic), #6 (no duplication) |
| Task 4 (classify) | Success Criteria #2, Decisions (trust backend status) |
| Task 5 (addresses.json) | Success Criteria #8, Decisions (addresses.json over HTML) |
| Tasks 6-8 (Modules) | Success Criteria #10 (indexer ≤150 lines), Core Experience |
| Tasks 9-10 (Rewrites) | Success Criteria #4 (identical output), #7 (supply uses config), Constraints (CLI unchanged) |
| Task 12 (Frontend) | Success Criteria #1 (XSS), #2 (classification), #5 (deploy), Constraints (deploy unchanged) |
| Task 13 (Tests) | Success Criteria #9, Scope Fence (core tests only) |

### Review Gate Resolutions

**Resolved (self-resolvable):**
1. `io.py` → `fileio.py` naming: all 14 references updated globally
2. **State schema** (cold-session gap #2): The full defaults dict for `load_state` in Task 9 is: `{'katanaBlock': DEPLOY_BLOCK - 1, 'ethBlock': ETH_KAT_START - 1, 'claimedByAddr': {}, 'dumpRaw': {}, 'cexByAddr': {}, 'buyRaw': {}, 'stakeRaw': {}, 'avkatHolders': [], 'avkatHolderBlock': DEPLOY_BLOCK - 1}`. Build agent must define this in indexer.py.
3. **data.json format** (cold-session gap #3): data.json is compact (`separators=(',',':')`), NOT indented. P2-13 "output files have indent" applies only to supply_data.json (human-readable reference). data.json stays compact for frontend performance (776KB → would be ~1.2MB indented).
4. **Async addresses.json** (cold-session gap #4): In app.js, declare `let ADDRESSES_RAW = [], CEX_WALLETS_SET = new Set(), CEX_LABELS_MAP = {};` at top. In `main()`, fetch addresses.json first, assign values, THEN proceed with data.json fetch. The addresses.json fetch is blocking (awaited) because downstream code depends on it.
5. **XSS fix references** (cold-session gap #5): Fix sites by function name: (a) `renderSupplyTab()` — the `nameHtml` variable where `name` is used, (b) same function — the `detail` variable built from `info.subs` entries, (c) same function — the `fixedRows.forEach` block where `v.name`, `detail`, `sub.name` are injected.
6. **scan_transfers params** (cold-session gap #6): Function receives the full `state` dict and extracts `state['dumpRaw']`, `state.get('buyRaw', {})`, `state.get('stakeRaw', {})` internally (current behavior preserved). Return type: `(dump_raw: dict, buy_raw: dict, stake_raw: dict)`.
7. **eth_get_receipt** (cold-session gap #12): Used by `scan_transfers` for swap detection (receipt inspection for Swap events). It belongs in rpc.py because scan_transfers imports from rpc.
8. **build_output signatures** (cold-session gap #8): `build_output(addresses, claimed_by_addr, dump_raw, cex_by_addr, addr_balances, dest_balances, dest_types, stake_raw=None) -> list[dict]`. `build_buyers_output(buy_raw, stake_raw, addr_set, addr_balances, vkat_locks) -> list[dict]`. `build_stakers_output(stake_raw, addr_balances, vkat_locks) -> list[dict]`. All return sorted lists of dicts matching current output shape.
9. **addresses.json extraction** (cold-session gap #9): RAW is a backtick-delimited string with one 0x address per line. Parse via regex `re.findall(r'0x[0-9a-fA-F]{40}', raw)` then deduplicate preserving order. CEX_WALLETS is a JS Set literal with quoted addresses. CEX_LABELS is a JS object literal with address keys and string/object values. Script to extract: read index.html, regex-extract each data block, parse, write JSON.
10. **Verification with live RPC** (cold-session gap #10): For incremental runs (no `--full`), indexer.py takes <10 seconds when state is current. The "before refactoring" snapshot captures current output; the "after" diff confirms byte-identical. A developer does NOT need --full unless the state was cleared. The diff test is the proof.
11. **config.json current keys** (cold-session gap #11): Current keys (from file read): `katanaRpc`, `ethRpc`, `distributorAddr`, `claimedTopic`, `transferTopic`, `swapV3Topic`, `swapV2Topic`, `deployBlock`, `katAddr`, `avkatAddr`, `katDecimals`, `katTokens` (3-element array), `poolAddresses` (8 addresses), `bridges` (2 addresses), `votingEscrow`, `lockNft`, `katEthAddr`, `ethKatStart`, `zeroAddress`. All of these become module-level constants in config.py.
12. **"may have" vague language** (ambiguity review): Changed to: "stakeRaw entries from before this refactoring contain old vkatAmount/avkatAmount fields".
