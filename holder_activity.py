#!/usr/bin/env python3
"""Generate holder_activity.json for the "Holders vs Users" tab.

KAT holder  = beneficial EOA with KAT exposure in ANY form: liquid KAT + locked vKAT
              + avKAT (held directly OR unwrapped from Morpho collateral / Uni-Sushi LP / Spectra PT).
Katana user = address that has done >= USER_TX_MIN transactions on Katana (nonce >= USER_TX_MIN).

Each venue also reports the KAT behind it, normalised to KAT (18 dp): liquid balance,
locked amount from VotingEscrow, or avKAT shares run through convertToAssets.

Usage:  python3 holder_activity.py [--refresh]
  --refresh re-pulls holder lists from Blockscout + replays avKAT + rescans Morpho
  + re-enumerates vKAT locks.
  Without it, cached pulls in .holder_cache/ are reused (only nonces, EOA checks and
  the avKAT conversion rate are refreshed).
"""
import json, os, sys, time, re, urllib.request, urllib.parse
from datetime import datetime, timezone

RPC      = "https://rpc.katana.network"
BS       = "https://explorer.katanarpc.com/api/v2"
KAT      = "0x7f1f4b4b29f5058fa32cc7a97141b8d7e5abdc2d"   # liquid KAT (circulating)
VKAT     = "0x106f7d67ea25cb9eff5064cf604ebf6259ff296d"   # vote-escrow NFT (locked KAT)
AVKAT    = "0x7231dbacdfc968e07656d12389ab20de82fbfceb"   # autocompounding vKAT
MORPHO   = "0xd50f2dfffd62f94ee4aed9ca05c61d0753268abc"
VE       = "0x4d6fc15ca6258b168225d283262743c623c13ead"   # VotingEscrow — holds each lock's KAT amount
AVKAT_DEPLOY  = 23368834
MORPHO_DEPLOY = 23368834   # avKAT markets can't predate avKAT
T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_CREATE   = "0xac4b2400f169220b0c0afdde7a0b32e775ba727ea1cb30b35f935cdaab8683ac"
T_SUPCOL   = "0xa3b9472a1399e17e123f3c2e6586c23e504184d504de59cdaa2b375e880c6184"  # SupplyCollateral
T_WDCOL    = "0xe80ebd7cc9223d7382aab2e0d1d6155c65651f83d53c8b9b06901d167e321142"  # WithdrawCollateral
S_TOTALSUPPLY  = "0x18160ddd"   # totalSupply()
S_TOKENBYINDEX = "0x4f6ccce7"   # tokenByIndex(uint256)
S_OWNEROF      = "0x6352211e"   # ownerOf(uint256)
S_LOCKED       = "0xb45a3c0e"   # locked(uint256) -> (amount, endTime)
S_CONVERT      = "0x07a2d13a"   # convertToAssets(uint256) — avKAT shares -> KAT
ZERO = "0x0000000000000000000000000000000000000000"
WEI  = 10 ** 18
USER_TX_MIN = 2

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".holder_cache")
HDRS = {"Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json"}
REFRESH = "--refresh" in sys.argv

def _post(payload, timeout=180, tries=6):
    body = json.dumps(payload).encode(); last = None
    for k in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(RPC, data=body, headers=HDRS), timeout=timeout).read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 413: raise
            time.sleep(0.6*(k+1))
        except Exception as e:
            last = e; time.sleep(0.6*(k+1))
    raise last
def rpc(m, p): return _post({"jsonrpc":"2.0","id":1,"method":m,"params":p})
def topic_addr(t): return "0x" + t[-40:].lower()

def _get(url, tries=6):
    last = None
    for k in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=HDRS), timeout=60).read())
        except Exception as e:
            last = e; time.sleep(0.5*(k+1))
    raise last

def batch(method, args, label=""):
    out = [None]*len(args); B = 200; i = 0
    while i < len(args):
        chunk = args[i:i+B]
        pl = [{"jsonrpc":"2.0","id":j,"method":method,"params":p} for j,p in enumerate(chunk)]
        try:
            resp = _post(pl)
        except urllib.error.HTTPError as e:
            if e.code == 413 and B > 1: B = max(1, B//2); continue
            raise
        for r in resp: out[i + r["id"]] = r.get("result") if "error" not in r else None
        i += len(chunk)
        if label and (i // B) % 20 == 0: print(f"  {label} {i}/{len(args)}", file=sys.stderr)
    return out

# ── cached/fetched holder lists ──────────────────────────────────────────────
def fetch_holders(token):
    out, npp, page = {}, None, 0
    while True:
        url = f"{BS}/tokens/{token}/holders" + ("?" + urllib.parse.urlencode(npp) if npp else "")
        d = _get(url); items = d.get("items", [])
        if not items: break
        for it in items:
            a = it.get("address", {})
            h = ((a.get("hash") if isinstance(a, dict) else a) or "").lower()
            # keep `value` — the holder's raw balance (KAT/avKAT wei, or NFT count
            # for vKAT). Needed for the per-venue KAT totals further down.
            if h: out[h] = {"is_contract": a.get("is_contract") if isinstance(a, dict) else None,
                            "value": it.get("value")}
        page += 1; print(f"  {token[:8]} page {page} ({len(out)})", file=sys.stderr)
        npp = d.get("next_page_params")
        if not npp: break
    return out

def cached(name, builder, valid=None):
    """Cached builder. `valid(val)` rejects caches written by an older schema
    (e.g. holder pulls with no `value`, unwrap scans with no amounts) so a format
    change rebuilds itself instead of silently serving fields that aren't there."""
    path = os.path.join(CACHE, name)
    if not REFRESH and os.path.exists(path):
        val = json.load(open(path))
        if valid is None or valid(val):
            return val
        print(f"  {name}: stale cache schema → rebuilding", file=sys.stderr)
    os.makedirs(CACHE, exist_ok=True)
    val = builder()
    json.dump(val, open(path, "w"))
    return val

def has_values(d):  return bool(d) and all("value" in v for v in d.values())
def has_keys(*ks):  return lambda d: all(k in d for k in ks)

def get_logs(address, topics, start):
    logs = []; latest = int(rpc("eth_blockNumber", [])["result"], 16); lo = start; span = latest-start+1
    while lo <= latest:
        hi = min(latest, lo+span-1); ok = False; batch_ = None
        params = [{"fromBlock":hex(lo),"toBlock":hex(hi),"address":address,"topics":topics}]
        try:
            r = rpc("eth_getLogs", params)
            if "error" in r:
                msg = r["error"].get("data","") or r["error"].get("message","")
                m = re.search(r"0x[0-9a-fA-F]+\]", msg)
                if m:
                    sug = int(m.group(0)[:-1],16); span = max(1, min(span//2, sug-lo+1)) if sug<=lo else (sug-lo+1)
                else: span = max(1, span//2)
            else: ok, batch_ = True, r["result"]
        except urllib.error.HTTPError as e:
            if e.code == 413: span = max(1, span//2)
            else: raise
        if ok:
            logs.extend(batch_); lo = hi+1
            if len(batch_) < 20000: span = min(latest-lo+1, span*2) if span < (latest-lo+1) else span
    return logs

WRAPPERS = {  # avKAT held on behalf of users → unwrap to funders (verified EOA only)
    "0x8640e1867bd563b2ab865160e77cb7b875243b13": "UniV3Pool",
    "0xbda5995c8ffeb294f8f681253acee2ab87a0435e": "Spectra-PT",
    "0xbd91b400194ae150cc3c375e977dfd25901ad80c": "Spectra-PT/IBT",
}
def build_avkat_unwrap():
    print("replaying avKAT transfers…", file=sys.stderr)
    logs = get_logs(AVKAT, [T_TRANSFER], AVKAT_DEPLOY)
    from collections import defaultdict
    # NET flow per (wrapper, funder): deposits in − withdrawals out. Keeping only
    # net-positive funders drops anyone who later fully exited the LP/PT position.
    net = {w: defaultdict(int) for w in WRAPPERS}
    for lg in logs:
        frm = topic_addr(lg["topics"][1]); to = topic_addr(lg["topics"][2])
        amt = int(lg["data"], 16) if lg.get("data") not in (None, "0x", "") else 0
        if to in WRAPPERS and frm not in WRAPPERS and frm != ZERO:
            net[to][frm] += amt
        elif frm in WRAPPERS and to not in WRAPPERS and to != ZERO:
            net[frm][to] -= amt
    return {"wrapper_funders": {WRAPPERS[w]: sorted(a for a, v in net[w].items() if v > 0)
                                for w in WRAPPERS},
            # same net flows, kept as avKAT wei per funder (str — JSON can't hold uint256)
            "wrapper_funder_amounts": {WRAPPERS[w]: {a: str(v) for a, v in net[w].items() if v > 0}
                                       for w in WRAPPERS}}

def _dword(data, i):  # i-th 32-byte word of log data as int
    h = data[2:][i*64:(i+1)*64]
    return int(h, 16) if h else 0

def build_morpho():
    print("scanning Morpho avKAT collateral (supply − withdraw)…", file=sys.stderr)
    cm = get_logs(MORPHO, [T_CREATE], MORPHO_DEPLOY)
    avkat_mkts = []
    for lg in cm:
        words = [lg["data"][2:][i:i+64] for i in range(0, len(lg["data"][2:]), 64)]
        if len(words) >= 2 and "0x"+words[1][-40:].lower() == AVKAT:
            avkat_mkts.append(lg["topics"][1])
    # net collateral per onBehalf; keep only positions still open (net > 0).
    from collections import defaultdict
    net = defaultdict(int)
    if avkat_mkts:
        for lg in get_logs(MORPHO, [T_SUPCOL, avkat_mkts], MORPHO_DEPLOY):
            net[topic_addr(lg["topics"][3])] += _dword(lg["data"], 0)   # +assets, onBehalf=topics[3]
        for lg in get_logs(MORPHO, [T_WDCOL, avkat_mkts], MORPHO_DEPLOY):
            net[topic_addr(lg["topics"][2])] -= _dword(lg["data"], 1)   # −assets, onBehalf=topics[2]
    return {"avkat_markets": avkat_mkts,
            "avkat_collateral_suppliers": sorted(a for a, v in net.items() if v > 0),
            "avkat_collateral_amounts": {a: str(v) for a, v in net.items() if v > 0}}

def _word(n): return f"{n:064x}"
def build_vkat_locks():
    """Enumerate every vKAT lock NFT → {owner: locked KAT wei}.

    The NFT holder list gives counts only (vKAT is ERC-721, so Blockscout's
    `value` is a lock count, not an amount), so the KAT behind each lock has to
    come from VotingEscrow.locked(tokenId)."""
    r = rpc("eth_call", [{"to": VKAT, "data": S_TOTALSUPPLY}, "latest"])
    total = int(r["result"], 16) if r.get("result") not in (None, "0x", "") else 0
    print(f"enumerating {total} vKAT locks…", file=sys.stderr)
    if not total:
        return {"totalLocks": 0, "owner_locked": {}, "totalLockedWei": "0"}
    ids = batch("eth_call", [[{"to": VKAT, "data": S_TOKENBYINDEX + _word(i)}, "latest"] for i in range(total)], "lockId")
    ids = [x for x in ids if x not in (None, "0x", "")]
    owners = batch("eth_call", [[{"to": VKAT, "data": S_OWNEROF + x[-64:]}, "latest"] for x in ids], "lockOwner")
    amts   = batch("eth_call", [[{"to": VE,   "data": S_LOCKED  + x[-64:]}, "latest"] for x in ids], "lockAmt")
    from collections import defaultdict
    per = defaultdict(int)
    dropped = 0
    for o, a in zip(owners, amts):
        # a None on either leg = RPC error → skip the lock rather than attribute 0
        if not o or len(o) < 66 or not a or len(a) < 66: dropped += 1; continue
        per["0x" + o[-40:].lower()] += int(a[2:66], 16)
    if dropped: print(f"  WARNING: {dropped}/{len(ids)} locks unreadable (excluded)", file=sys.stderr)
    return {"totalLocks": total,
            "unreadableLocks": dropped,
            "owner_locked": {k: str(v) for k, v in per.items()},
            "totalLockedWei": str(sum(per.values()))}

# ── build universe ───────────────────────────────────────────────────────────
liquid = cached("kat_liquid_holders.json", lambda: fetch_holders(KAT),  has_values)
vkat   = cached("vkat_holders.json",       lambda: fetch_holders(VKAT), has_values)
avkat  = cached("avkat_holders.json",      lambda: fetch_holders(AVKAT), has_values)
unwrap = cached("avkat_unwrap.json",       build_avkat_unwrap, has_keys("wrapper_funder_amounts"))
morpho = cached("morpho_suppliers.json",   build_morpho,       has_keys("avkat_collateral_amounts"))
locks  = cached("vkat_locks.json",         build_vkat_locks,   has_keys("owner_locked"))

def eoas(d):  return {h for h, v in d.items() if v.get("is_contract") is False}  # strict: drop unknown(None)
def conts(d): return {h for h, v in d.items() if v.get("is_contract") is True}

morpho_sup = {a.lower() for a in morpho.get("avkat_collateral_suppliers", [])}
lp      = {a.lower() for a in unwrap["wrapper_funders"].get("UniV3Pool", [])}
spectra = {a.lower() for a in unwrap["wrapper_funders"].get("Spectra-PT", [])} | \
          {a.lower() for a in unwrap["wrapper_funders"].get("Spectra-PT/IBT", [])}
unwrapped = (morpho_sup | lp | spectra) - (conts(liquid) | conts(vkat) | conts(avkat))

# eth_getCode every address we're about to call a wallet — Blockscout's
# is_contract flag mislabels proxies and tokenised positions (e.g. the Spectra PT)
# as EOAs. It's ~1% of addresses but ~16% of the KAT they hold, so the venue
# totals below are meaningless without this. A None result = RPC error → treat as
# UNKNOWN and exclude (never count as EOA).
bs_claimed = eoas(liquid) | eoas(vkat) | eoas(avkat)
cand = sorted(bs_claimed | unwrapped)
codes = batch("eth_getCode", [[a, "latest"] for a in cand], "code") if cand else []
codemap = dict(zip(cand, codes))
NO_CODE = ("0x", "0x0", "")
verified  = {a for a, c in codemap.items() if c in NO_CODE}
code_errs = sum(1 for c in codes if c is None)
bs_mislabeled = sum(1 for a in bs_claimed if codemap.get(a) not in NO_CODE and codemap.get(a) is not None)

liquid_eoa, vkat_eoa, avkat_eoa = eoas(liquid) & verified, eoas(vkat) & verified, eoas(avkat) & verified
core = liquid_eoa | vkat_eoa | avkat_eoa
new_eoa = (unwrapped & verified) - core

universe = sorted(core | new_eoa)
uset = set(universe)
print(f"universe = {len(universe)} beneficial EOA holders ({len(new_eoa)} via unwrap, "
      f"{bs_mislabeled} Blockscout-'EOA' contracts dropped)", file=sys.stderr)

# ── classify by tx count (nonce) ─────────────────────────────────────────────
latest_block = int(rpc("eth_blockNumber", [])["result"], 16)
nonces = batch("eth_getTransactionCount", [[a, "latest"] for a in universe], "nonce")
def _nonce(x):  # "0x" (some nodes' zero), "", None(error) → 0; else hex int
    return int(x, 16) if (x and x not in ("0x", "")) else 0
counts = [_nonce(x) for x in nonces]
nonce_errs = sum(1 for x in nonces if x is None)

def bucket(n):
    if n == 0:  return "0"
    if n == 1:  return "1"
    if n <= 4:  return "2-4"
    if n <= 9:  return "5-9"
    if n <= 49: return "10-49"
    return "50+"
order = ["0", "1", "2-4", "5-9", "10-49", "50+"]
b = {k: 0 for k in order}
for n in counts: b[bucket(n)] += 1

users    = sum(1 for n in counts if n >= USER_TX_MIN)   # >=2 tx ("active user")
nonusers = len(counts) - users
total    = len(counts)
nonce0   = b["0"]
holders_transacted = total - nonce0                     # holders with >=1 tx

# component venue sets — GROSS beneficial-EOA participation per venue (a wallet
# can appear in several). EOA-clean: a venue address counts only if it made it
# into the verified-EOA universe (contracts dropped).
morpho_eoa = morpho_sup & uset
lpspec_eoa = (lp | spectra) & uset

# ── KAT held per venue ───────────────────────────────────────────────────────
# All five figures are normalised to KAT (18 dp) so they're comparable and, unlike
# the wallet counts, they do NOT overlap: each venue custodies a distinct pile.
#   liquid  — KAT balance in the wallet
#   vKAT    — KAT inside the wallet's own lock NFTs (VotingEscrow.locked)
#   avKAT   — vault shares in the wallet, converted at the vault's own rate
#   Morpho / LP+Spectra — net avKAT the wallet put in, likewise converted
# avKAT sitting in Morpho / the LP / Spectra is held by *those* contracts, so it
# never appears in the avKAT-direct figure; the avKAT strategy's own 283M-KAT lock
# is owned by a contract, so it never appears in the vKAT figure either.
def av_to_kat(shares):
    """avKAT shares → underlying KAT, at the vault's live exchange rate."""
    if shares <= 0: return 0
    r = rpc("eth_call", [{"to": AVKAT, "data": S_CONVERT + _word(shares)}, "latest"])
    res = r.get("result")
    if res in (None, "0x", ""):
        print(f"WARNING: convertToAssets failed ({r.get('error')}) — venue KAT undercounted", file=sys.stderr)
        return 0
    return int(res, 16)

def sum_vals(holders, addrs):  return sum(int(holders[a].get("value") or 0) for a in addrs)
def sum_amts(amts, addrs):     return sum(int(amts.get(a, 0)) for a in addrs)

locked_by_owner = locks.get("owner_locked", {})
wfa = unwrap.get("wrapper_funder_amounts", {})
lpspec_amts = {}
for w in ("UniV3Pool", "Spectra-PT", "Spectra-PT/IBT"):
    for a, v in wfa.get(w, {}).items():
        lpspec_amts[a] = lpspec_amts.get(a, 0) + int(v)

kat_liquid = sum_vals(liquid, liquid_eoa)
kat_vkat   = sum_amts(locked_by_owner, vkat_eoa)
kat_avkat  = av_to_kat(sum_vals(avkat, avkat_eoa))
kat_morpho = av_to_kat(sum_amts(morpho.get("avkat_collateral_amounts", {}), morpho_eoa))
kat_lpspec = av_to_kat(sum_amts(lpspec_amts, lpspec_eoa))
kat_total  = kat_liquid + kat_vkat + kat_avkat + kat_morpho + kat_lpspec

# vKAT sanity: locks owned by addresses outside the verified-EOA holder list are
# contract-held (avKAT strategy, treasury) and correctly excluded — log the split.
locked_all = int(locks.get("totalLockedWei") or 0)
print(f"vKAT locks: {locks.get('totalLocks')} total, {locked_all/WEI:,.0f} KAT locked overall, "
      f"{kat_vkat/WEI:,.0f} KAT ({len(vkat_eoa)} wallets) attributed to EOAs", file=sys.stderr)

# ── Katana-wide context (>=1 tx definition, matches Blockscout "Total accounts") ─
ctx = None
try:
    cn = {x["id"]: x["value"] for x in _get("https://explorer.katanarpc.com/api/v1/counters")["counters"]}
    total_addresses   = int(cn["totalAddresses"])      # all addresses ever seen
    transacting_users = int(cn["totalAccounts"])       # EOAs that sent >=1 tx
    ctx = {
        "userDef": ">=1 tx (EOA that has sent at least one transaction; Blockscout 'Total accounts')",
        "totalAddresses":    total_addresses,
        "transactingUsers":  transacting_users,
        "holdersTransacted": holders_transacted,                            # KAT holders with >=1 tx
        "usersNoKat":        max(0, transacting_users - holders_transacted), # users that hold no KAT (clamped)
        "holderShareOfUsers":     round(min(100.0, 100*holders_transacted/transacting_users), 1),
        "holderShareOfAddresses": round(100*total/total_addresses, 1),
    }
except Exception as e:
    print("counters fetch failed:", e, file=sys.stderr)

if not total:
    print("ERROR: 0 holders resolved (pull likely failed) — aborting without touching holder_activity.json", file=sys.stderr)
    sys.exit(1)

out = {
    "generatedAt": datetime.now(timezone.utc).isoformat(),
    "katanaBlock": latest_block,
    "userTxMin": USER_TX_MIN,
    "totalHolders": total,
    "users": users,
    "nonUsers": nonusers,
    "usersPct": round(100*users/total, 1),
    "nonUsersPct": round(100*nonusers/total, 1),
    "buckets": [{"label": k, "count": b[k]} for k in order],
    "components": [
        {"label": "Liquid KAT",              "count": len(liquid_eoa), "kat": round(kat_liquid/WEI, 2)},
        {"label": "Staked · vKAT (locked)",  "count": len(vkat_eoa),   "kat": round(kat_vkat/WEI, 2)},
        {"label": "avKAT (direct)",          "count": len(avkat_eoa),  "kat": round(kat_avkat/WEI, 2)},
        {"label": "Morpho avKAT collateral", "count": len(morpho_eoa), "kat": round(kat_morpho/WEI, 2)},
        {"label": "Sushi/Uni LP + Spectra",  "count": len(lpspec_eoa), "kat": round(kat_lpspec/WEI, 2)},
    ],
    "componentsKatTotal": round(kat_total/WEI, 2),   # venues don't overlap → safe to add up
    "chainAddresses": (ctx or {}).get("totalAddresses"),
    "katanaContext": ctx,
    "diagnostics": {"unwrapNewEoas": len(new_eoa), "getCodeErrors": code_errs, "nonceErrors": nonce_errs,
                    "blockscoutMislabeledContracts": bs_mislabeled,
                    "vkatLocksTotal": locks.get("totalLocks"),
                    "vkatLockedAllOwnersKat": round(locked_all/WEI, 2)},
}
if code_errs or nonce_errs:
    print(f"WARNING: {code_errs} getCode + {nonce_errs} nonce RPC errors (excluded, not miscounted)", file=sys.stderr)

_out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holder_activity.json")
_tmp_path = _out_path + ".tmp"
with open(_tmp_path, "w") as _f:
    json.dump(out, _f, indent=2)
os.replace(_tmp_path, _out_path)  # atomic swap — never leave a truncated/partial file if killed mid-write
print(json.dumps({k: v for k, v in out.items() if k not in ("buckets", "components")}, indent=2))
print("buckets:", out["buckets"])
print("components:", out["components"])
