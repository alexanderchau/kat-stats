#!/usr/bin/env python3
"""
KAT Farmer Indexer
Scans Katana + ETH mainnet, writes data.json consumed by index.html.

Usage:
    python3 indexer.py              # incremental (only new blocks)
    python3 indexer.py --full       # ignore state.json, rescan from genesis

State is persisted in state.json so each run only fetches new blocks.
"""

import json, re, sys, time, threading, argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import urllib.request, urllib.error

SCRIPT_DIR = Path(__file__).parent
STATE_PATH = SCRIPT_DIR / 'state.json'
DATA_PATH  = SCRIPT_DIR / 'data.json'
INDEX_PATH = SCRIPT_DIR / 'index.html'
CONFIG_PATH = SCRIPT_DIR / 'config.json'

# ── Load config ───────────────────────────────────────────────────────────────
_cfg = json.loads(CONFIG_PATH.read_text())

# ── Chain config ───────────────────────────────────────────────────────────────
KATANA_RPC    = _cfg['katanaRpc']
ETH_RPC       = _cfg['ethRpc']
LOG_CHUNK     = 29_999    # Katana max getLogs range (30k fails)
ETH_LOG_CHUNK = 50_000    # ETH mainnet (~7 days per chunk, RPC limit)

# ── Contracts (loaded from config.json) ───────────────────────────────────────
DISTRIBUTOR_ADDR = _cfg['distributorAddr']
CLAIMED_TOPIC    = _cfg['claimedTopic']
TRANSFER_TOPIC   = _cfg['transferTopic']
SWAP_V3_TOPIC    = _cfg['swapV3Topic']
SWAP_V2_TOPIC    = _cfg['swapV2Topic']

DEPLOY_BLOCK  = _cfg['deployBlock']
KAT_ADDR      = _cfg['katAddr']
AVKAT_ADDR    = _cfg['avkatAddr']
KAT_DECIMALS  = _cfg['katDecimals']

KAT_TOKENS     = set(_cfg['katTokens'])
KAT_POOL_ADDRS = set(_cfg['poolAddresses'])

BRIDGE       = _cfg['bridges'][0]
BRIDGE2      = _cfg['bridges'][1]
BRIDGE_DESTS = set(_cfg['bridges'])
ZERO_ADDR    = _cfg['zeroAddress']

VOTING_ESCROW = _cfg['votingEscrow']
LOCK_NFT      = _cfg['lockNft']
STAKE_DESTS   = {VOTING_ESCROW, AVKAT_ADDR}

KAT_ETH_ADDR  = _cfg['katEthAddr']
ETH_KAT_START = _cfg['ethKatStart']

# ── Thread-safe RPC ────────────────────────────────────────────────────────────
_seq_lock = threading.Lock()
_seq      = 0

def _next_id():
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq

def rpc_call(url, method, params, retries=3, timeout=30):
    for attempt in range(retries):
        try:
            body = json.dumps({'jsonrpc': '2.0', 'id': _next_id(), 'method': method, 'params': params}).encode()
            req  = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}, method='POST')
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                if 'error' in data:
                    raise ValueError(f"RPC error: {data['error']}")
                return data.get('result')
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
            else:
                print(f'  ⚠ {method} failed after {retries} attempts: {e}')
                return None

def get_block_number(url):
    r = rpc_call(url, 'eth_blockNumber', [])
    return int(r, 16) if r else 0

def eth_get_logs(url, params):
    r = rpc_call(url, 'eth_getLogs', [params])
    return r if isinstance(r, list) else []

def eth_get_receipt(tx_hash):
    return rpc_call(KATANA_RPC, 'eth_getTransactionReceipt', [tx_hash])

def from_wei(hex_val):
    if not hex_val or hex_val in ('0x', '0x0'):
        return 0.0
    try:
        return int(hex_val, 16) / (10 ** KAT_DECIMALS)
    except Exception:
        return 0.0

def fmtM(n):
    if n >= 1e9: return f'{n/1e9:.2f}B'
    if n >= 1e6: return f'{n/1e6:.2f}M'
    if n >= 1e3: return f'{n/1e3:.1f}K'
    return f'{n:.0f}'

def balance_of(token, wallet, url=KATANA_RPC):
    padded = wallet.replace('0x', '').lower().zfill(64)
    data   = '0x70a08231' + padded
    r = rpc_call(url, 'eth_call', [{'to': token, 'data': data}, 'latest'])
    return from_wei(r) if r else 0.0

def total_assets(vault, url=KATANA_RPC):
    """Call totalAssets() on an ERC-4626 vault."""
    r = rpc_call(url, 'eth_call', [{'to': vault, 'data': '0x01e1d114'}, 'latest'])
    return from_wei(r) if r else 0.0

def is_contract(addr, url=KATANA_RPC):
    """Check if address is a contract (has code) vs EOA."""
    r = rpc_call(url, 'eth_getCode', [addr, 'latest'])
    return bool(r and len(r) > 2 and r != '0x')

def enumerate_vkat_locks():
    """Enumerate all vKAT Lock NFTs → {owner: {amount, endTime}}."""
    # 1. Total supply
    r = rpc_call(KATANA_RPC, 'eth_call', [{'to': LOCK_NFT, 'data': '0x18160ddd'}, 'latest'])
    total = int(r, 16) if r else 0
    if total == 0:
        return {}
    print(f'  vKAT: enumerating {total:,} Lock NFTs…')

    # 2. Get all token IDs via tokenByIndex(i)
    def get_token_id(idx):
        data = '0x4f6ccce7' + hex(idx)[2:].zfill(64)
        r = rpc_call(KATANA_RPC, 'eth_call', [{'to': LOCK_NFT, 'data': data}, 'latest'])
        return int(r, 16) if r else None

    with ThreadPoolExecutor(max_workers=16) as ex:
        token_ids = list(ex.map(get_token_id, range(total)))
    token_ids = [t for t in token_ids if t is not None]

    # 3. For each token: ownerOf + locked
    def get_lock_info(token_id):
        tid_hex = hex(token_id)[2:].zfill(64)
        owner_r = rpc_call(KATANA_RPC, 'eth_call',
                           [{'to': LOCK_NFT, 'data': '0x6352211e' + tid_hex}, 'latest'])
        owner = ('0x' + owner_r[26:].lower()) if owner_r else None
        lock_r = rpc_call(KATANA_RPC, 'eth_call',
                          [{'to': VOTING_ESCROW, 'data': '0xb45a3c0e' + tid_hex}, 'latest'])
        amount, end_time = 0.0, 0
        if lock_r and len(lock_r) >= 130:
            amount = int(lock_r[2:66], 16) / (10 ** KAT_DECIMALS)
            end_time = int(lock_r[66:130], 16)
        return owner, amount, end_time

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(get_lock_info, token_ids))

    # 4. Aggregate per owner
    owner_locks = {}
    for owner, amount, end_time in results:
        if owner and amount > 0:
            if owner not in owner_locks:
                owner_locks[owner] = {'amount': 0.0, 'endTime': 0}
            owner_locks[owner]['amount'] += amount
            owner_locks[owner]['endTime'] = max(owner_locks[owner]['endTime'], end_time)

    total_locked = sum(v['amount'] for v in owner_locks.values())
    print(f'  vKAT: {len(owner_locks):,} owners, {fmtM(total_locked)} KAT locked')
    return owner_locks

# ── Address / CEX wallet loading ───────────────────────────────────────────────
def load_addresses():
    text = INDEX_PATH.read_text()
    m = re.search(r'const RAW\s*=\s*`(.*?)`', text, re.DOTALL)
    if not m:
        sys.exit('ERROR: RAW string not found in index.html')
    raw   = m.group(1)
    addrs = list(dict.fromkeys(a.lower() for a in re.findall(r'0x[0-9a-fA-F]{40}', raw)))
    print(f'  Loaded {len(addrs)} addresses from index.html')
    return addrs

def load_cex_wallets():
    text = INDEX_PATH.read_text()
    m = re.search(r'const CEX_WALLETS\s*=\s*new Set\(\[(.*?)\]\)', text, re.DOTALL)
    if not m:
        print('  ⚠ CEX_WALLETS not found in index.html — cross-chain CEX detection disabled')
        return set()
    wallets = set(re.findall(r"'(0x[0-9a-fA-F]{40})'", m.group(1)))
    print(f'  Loaded {len(wallets)} CEX wallets from index.html')
    return wallets

# ── State persistence ──────────────────────────────────────────────────────────
def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception as e:
            print(f'  ⚠ Corrupt state.json ({e}), starting fresh')
    return {
        'katanaBlock': DEPLOY_BLOCK - 1,
        'ethBlock':    ETH_KAT_START - 1,
        'claimedByAddr': {},
        'dumpRaw':       {},
        'cexByAddr':     {},
        'buyRaw':        {},
        'stakeRaw':      {},
    }

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))
    print(f'  Saved state.json (katana={state["katanaBlock"]}, eth={state["ethBlock"]})')

# ── Classification ─────────────────────────────────────────────────────────────
def disposed(d):
    """Legacy helper — only used for backward compat, not for classification."""
    return d.get('moved', 0) + d.get('transferred', 0) + d.get('bridged', 0)

def classify(d, stake_raw=None):
    """Classify address status. Matches frontend retainedPct() logic:
    held = balance + avkat + staker vKAT/avKAT + held defi positions."""
    # No Merkl claim = inactive (regardless of other activity)
    if d['claimed'] == 0:
        return 'inactive'
    # Balance-based: what fraction of claimed tokens are still held?
    held = d.get('balance', 0) + d.get('avkat', 0)
    # Add staker amounts (vKAT + avKAT from staker data)
    if stake_raw:
        staker = stake_raw.get(d['address'], {})
        held += staker.get('vkatAmount', 0) + staker.get('avkatAmount', 0)
    # Add held defi positions
    held += sum(p['amount'] for p in d.get('defiPositions', []) if p.get('held'))
    retained = max(0.0, min(1.0, held / d['claimed']))
    if retained < 0.5:  return 'farmer'
    if retained < 0.95: return 'partial'
    return 'hodler'

# ── Scan: Claimed events ───────────────────────────────────────────────────────
def scan_claimed(state, addr_set):
    scan_from = state['katanaBlock'] + 1
    latest    = get_block_number(KATANA_RPC)
    claimed   = state['claimedByAddr']

    if scan_from > latest:
        print(f'  Claimed: cache current at block {latest:,}')
        return claimed, latest

    chunks = [(f, min(f + LOG_CHUNK - 1, latest)) for f in range(scan_from, latest + 1, LOG_CHUNK)]
    print(f'  Claimed: {len(chunks)} chunks ({scan_from:,} → {latest:,})…')

    for i, (frm, to) in enumerate(chunks, 1):
        logs = eth_get_logs(KATANA_RPC, {
            'fromBlock': hex(frm),
            'toBlock':   hex(to),
            'address':   DISTRIBUTOR_ADDR,
            'topics':    [CLAIMED_TOPIC],
        })
        for log in logs:
            topics = log.get('topics', [])
            if len(topics) < 3:
                continue
            user  = '0x' + topics[1][26:].lower()
            token = '0x' + topics[2][26:].lower()
            if user not in addr_set or token not in KAT_TOKENS:
                continue
            claimed[user] = claimed.get(user, 0) + from_wei(log.get('data', '0x0'))
        if i % 20 == 0 or i == len(chunks):
            print(f'    {i}/{len(chunks)} chunks…', end='\r')

    print(f'  Claimed: {len(claimed):,} claimants total            ')
    return claimed, latest

# ── Scan: Transfer events (dump + buyer + staking — single pass) ───────────────
def scan_transfers(state, addr_set, cex_wallets, latest_block):
    """One pass over all KAT Transfer events, building dump_raw, buy_raw, stake_raw."""
    scan_from  = state['katanaBlock'] + 1
    dump_raw   = state['dumpRaw']
    buy_raw    = state.get('buyRaw', {})
    stake_raw  = state.get('stakeRaw', {})

    if scan_from > latest_block:
        print(f'  Transfers: cache current at block {latest_block:,}')
        return dump_raw, buy_raw, stake_raw

    buy_sources = KAT_POOL_ADDRS | set(cex_wallets)
    chunks = [(f, min(f + LOG_CHUNK - 1, latest_block)) for f in range(scan_from, latest_block + 1, LOG_CHUNK)]
    print(f'  Transfers: {len(chunks)} chunks × {len(KAT_TOKENS)} tokens ({scan_from:,} → {latest_block:,})…')

    all_logs = []
    for i, (frm, to) in enumerate(chunks, 1):
        for token in KAT_TOKENS:
            logs = eth_get_logs(KATANA_RPC, {
                'fromBlock': hex(frm),
                'toBlock':   hex(to),
                'address':   token,
                'topics':    [TRANSFER_TOPIC],
            })
            all_logs.extend(logs)
        if i % 20 == 0 or i == len(chunks):
            print(f'    {i}/{len(chunks)} chunks…', end='\r')
    print(f'  Transfers: {len(all_logs):,} raw events collected            ')

    tx_map = {}  # for dump receipt detection
    for log in all_logs:
        topics = log.get('topics', [])
        if len(topics) < 3:
            continue
        sender = '0x' + topics[1][26:].lower()
        dest   = '0x' + topics[2][26:].lower()
        amount = from_wei(log.get('data', '0x0'))

        # ── Staking: to = vKAT escrow or avKAT vault ──
        if dest in STAKE_DESTS:
            if sender not in stake_raw:
                stake_raw[sender] = {'stakedVKAT': False, 'stakedAvKAT': False, 'vkatAmount': 0.0, 'avkatAmount': 0.0}
            # Backfill amount fields for old state entries
            if 'vkatAmount' not in stake_raw[sender]:
                stake_raw[sender]['vkatAmount'] = 0.0
                stake_raw[sender]['avkatAmount'] = 0.0
            if dest == VOTING_ESCROW:
                stake_raw[sender]['stakedVKAT'] = True
                stake_raw[sender]['vkatAmount'] += amount
            else:
                stake_raw[sender]['stakedAvKAT'] = True
                stake_raw[sender]['avkatAmount'] += amount

        # ── Buyers: from = pool or CEX → to = external wallet ──
        if (sender in buy_sources
                and dest != ZERO_ADDR
                and dest not in BRIDGE_DESTS
                and dest not in KAT_POOL_ADDRS):
            source = 'cex' if sender in cex_wallets else 'dex'
            if dest not in buy_raw:
                buy_raw[dest] = {'katReceived': 0.0, 'katSold': 0.0, 'txCount': 0, 'sources': []}
            buy_raw[dest]['katReceived'] += amount
            buy_raw[dest]['txCount'] += 1
            if source not in buy_raw[dest]['sources']:
                buy_raw[dest]['sources'].append(source)

        # ── Buyer sells: known buyer sending KAT back to a pool ──
        if sender in buy_raw and dest in KAT_POOL_ADDRS:
            buy_raw[sender]['katSold'] = buy_raw[sender].get('katSold', 0.0) + amount

        # ── Dump: from = airdrop address ──
        if sender in addr_set:
            tx = log.get('transactionHash', '')
            if not tx:
                continue
            if tx not in tx_map:
                tx_map[tx] = {'from': sender, 'entries': [], 'is_swap': False}
            tx_map[tx]['entries'].append({'to': dest, 'amount': amount})

    # Swap detection for dump data
    receipts_needed = []
    for tx_hash, v in tx_map.items():
        entries = v['entries']
        all_bridge = all(e['to'] == ZERO_ADDR or e['to'] in BRIDGE_DESTS for e in entries)
        if all_bridge:
            continue
        if all(e['to'] in KAT_POOL_ADDRS for e in entries):
            v['is_swap'] = True
            continue
        receipts_needed.append(tx_hash)

    print(f'  Transfers: {len(receipts_needed):,} receipts needed for swap detection…')
    receipt_results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(eth_get_receipt, h): h for h in receipts_needed}
        done = 0
        for fut in as_completed(futs):
            receipt_results[futs[fut]] = fut.result()
            done += 1
            if done % 100 == 0 or done == len(receipts_needed):
                print(f'    {done}/{len(receipts_needed)} receipts…', end='\r')
    if receipts_needed:
        print()

    for tx_hash in receipts_needed:
        rec = receipt_results.get(tx_hash)
        if not rec:
            continue
        logs = rec.get('logs', [])
        if any((l.get('topics') or [''])[0] in (SWAP_V3_TOPIC, SWAP_V2_TOPIC) for l in logs):
            tx_map[tx_hash]['is_swap'] = True

    for v in tx_map.values():
        sender = v['from']
        if sender not in dump_raw:
            dump_raw[sender] = {'sold': 0.0, 'bridged': 0.0, 'dests': {}}
        r = dump_raw[sender]
        for entry in v['entries']:
            dest, amount = entry['to'], entry['amount']
            if v['is_swap']:
                r['sold'] += amount
            elif dest == ZERO_ADDR or dest in BRIDGE_DESTS:
                r['bridged'] += amount
            else:
                r['dests'][dest] = r['dests'].get(dest, 0.0) + amount

    print(f'  Transfers: {len(dump_raw):,} airdrop addrs · {len(buy_raw):,} buyers · {len(stake_raw):,} stakers')
    return dump_raw, buy_raw, stake_raw

# ── Scan: aVKAT holders (discover all depositors via share token transfers) ────
def scan_avkat_holders(state, stake_raw):
    """Scan aVKAT token Transfer events to discover all holders.
    Addresses that received aVKAT shares via zaps/routers/transfers won't appear
    in stake_raw from the KAT transfer scan. This closes that gap."""
    avkat_holders = state.get('avkatHolders', set())
    if isinstance(avkat_holders, list):
        avkat_holders = set(avkat_holders)
    scan_from = state.get('avkatHolderBlock', DEPLOY_BLOCK - 1) + 1
    latest    = get_block_number(KATANA_RPC)

    if scan_from > latest:
        print(f'  aVKAT holders: cache current at block {latest:,}')
        return avkat_holders, latest

    chunks = [(f, min(f + LOG_CHUNK - 1, latest)) for f in range(scan_from, latest + 1, LOG_CHUNK)]
    print(f'  aVKAT holders: {len(chunks)} chunks ({scan_from:,} → {latest:,})…')

    new_found = 0
    for i, (frm, to) in enumerate(chunks, 1):
        logs = eth_get_logs(KATANA_RPC, {
            'fromBlock': hex(frm),
            'toBlock':   hex(to),
            'address':   AVKAT_ADDR,
            'topics':    [TRANSFER_TOPIC],
        })
        for log in logs:
            topics = log.get('topics', [])
            if len(topics) < 3:
                continue
            # Track both sender and recipient of aVKAT shares
            sender = '0x' + topics[1][26:].lower()
            dest   = '0x' + topics[2][26:].lower()
            for addr in (sender, dest):
                if addr != ZERO_ADDR and addr != AVKAT_ADDR and addr not in avkat_holders:
                    avkat_holders.add(addr)
                    new_found += 1
        if i % 20 == 0 or i == len(chunks):
            print(f'    {i}/{len(chunks)} chunks…', end='\r')

    # Merge into stake_raw so they get balance-fetched
    merged = 0
    for addr in avkat_holders:
        if addr not in stake_raw:
            stake_raw[addr] = {'stakedVKAT': False, 'stakedAvKAT': True, 'vkatAmount': 0.0, 'avkatAmount': 0.0}
            merged += 1
        elif not stake_raw[addr].get('stakedAvKAT'):
            stake_raw[addr]['stakedAvKAT'] = True

    print(f'  aVKAT holders: {len(avkat_holders):,} total, {new_found:,} new, {merged:,} merged into stakeRaw')
    return avkat_holders, latest

# ── Scan: ETH mainnet CEX transfers ────────────────────────────────────────────
def scan_cex(state, addr_set, cex_wallets, dump_raw=None):
    if not cex_wallets:
        return state['cexByAddr'], state['ethBlock']

    # Build reverse map: intermediary dest -> original claimer
    # Dumpers often transfer KAT to an intermediary wallet on KAT chain, which
    # then bridges to ETH and deposits to a CEX.  Without this map the ETH-side
    # scan only catches direct claimers, missing the majority of CEX deposits.
    dest_to_claimer = {}
    if dump_raw:
        for claimer, raw in dump_raw.items():
            for dest_addr in raw.get('dests', {}):
                dest_to_claimer[dest_addr] = claimer
    eth_senders = addr_set | set(dest_to_claimer)

    scan_from   = state['ethBlock'] + 1
    latest      = get_block_number(ETH_RPC)
    cex_by_addr = state['cexByAddr']

    if scan_from > latest:
        print(f'  CEX: cache current at ETH block {latest:,}')
        return cex_by_addr, latest

    chunks = [(f, min(f + ETH_LOG_CHUNK - 1, latest)) for f in range(scan_from, latest + 1, ETH_LOG_CHUNK)]
    print(f'  CEX: {len(chunks)} ETH chunks ({scan_from:,} → {latest:,}), tracking {len(eth_senders):,} senders…')

    for i, (frm, to) in enumerate(chunks, 1):
        logs = eth_get_logs(ETH_RPC, {
            'fromBlock': hex(frm),
            'toBlock':   hex(to),
            'address':   KAT_ETH_ADDR,
            'topics':    [TRANSFER_TOPIC],
        })
        for log in logs:
            topics = log.get('topics', [])
            if len(topics) < 3:
                continue
            sender = '0x' + topics[1][26:].lower()
            dest   = '0x' + topics[2][26:].lower()
            if sender not in eth_senders or dest not in cex_wallets:
                continue
            # Attribute to original claimer if sender is an intermediary
            owner = dest_to_claimer.get(sender, sender)
            amount = from_wei(log.get('data', '0x0'))
            if owner not in cex_by_addr:
                cex_by_addr[owner] = {'cexSent': 0.0, 'cexDests': []}
            cex_by_addr[owner]['cexSent'] += amount
            if dest not in cex_by_addr[owner]['cexDests']:
                cex_by_addr[owner]['cexDests'].append(dest)
        if i % 5 == 0 or i == len(chunks):
            print(f'    {i}/{len(chunks)} chunks…', end='\r')

    print(f'  CEX: {len(cex_by_addr):,} addresses with cross-chain CEX activity            ')
    return cex_by_addr, latest

# ── Fresh balances ─────────────────────────────────────────────────────────────
def get_fresh_balances(addresses, dump_raw, extra_addrs=None):
    all_dest_tokens = list(KAT_TOKENS) + [AVKAT_ADDR]
    all_dests = set()
    for r in dump_raw.values():
        all_dests.update(r['dests'].keys())

    # Extra addresses (buyers/stakers) that also need balance fetches
    extra = set(extra_addrs or []) - set(addresses)
    all_fetch = list(addresses) + list(extra)

    total_tasks = len(all_fetch) + len(all_dests)
    print(f'  Balances: {len(all_fetch):,} addresses + {len(all_dests):,} destinations = {total_tasks:,} tasks…')

    addr_balances = {}  # addr  -> { kat, avkat }
    dest_balances = {}  # dest  -> total (all KAT tokens + avkat)
    done_count = 0
    lock = threading.Lock()

    def fetch_addr(addr):
        kat   = sum(balance_of(t, addr) for t in KAT_TOKENS)
        avkat = balance_of(AVKAT_ADDR, addr)
        return addr, kat, avkat

    def fetch_dest(dest):
        total = sum(balance_of(t, dest) for t in all_dest_tokens)
        return dest, total

    with ThreadPoolExecutor(max_workers=16) as ex:
        addr_futs = {ex.submit(fetch_addr, a): a for a in all_fetch}
        dest_futs = {ex.submit(fetch_dest, d): d for d in all_dests}
        all_futs  = list(addr_futs.keys()) + list(dest_futs.keys())

        for fut in as_completed(all_futs):
            with lock:
                done_count += 1
                n = done_count
            if n % 100 == 0 or n == total_tasks:
                print(f'    {n}/{total_tasks} balance fetches done…', end='\r')

    print()
    for fut, addr in addr_futs.items():
        try:
            _, kat, avkat = fut.result()
        except Exception:
            kat, avkat = 0.0, 0.0
        addr_balances[addr] = {'kat': kat, 'avkat': avkat}

    for fut, dest in dest_futs.items():
        try:
            _, total = fut.result()
        except Exception:
            total = 0.0
        dest_balances[dest] = total

    # Classify each destination address
    print(f'  Classifying {len(all_dests):,} destination addresses…')
    dest_types = {}
    # Known types first (no RPC needed)
    unknown_dests = []
    for dest in all_dests:
        if dest in KAT_POOL_ADDRS:
            dest_types[dest] = 'LP Pool'
        elif dest in STAKE_DESTS:
            dest_types[dest] = 'Staking'
        elif dest in BRIDGE_DESTS:
            dest_types[dest] = 'Bridge'
        elif dest == DISTRIBUTOR_ADDR:
            dest_types[dest] = 'Merkl Distributor'
        else:
            unknown_dests.append(dest)

    # Batch is_contract checks for unknown destinations
    contract_results = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(is_contract, d): d for d in unknown_dests}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                contract_results[d] = fut.result()
            except Exception:
                contract_results[d] = False

    for dest in unknown_dests:
        if contract_results.get(dest, False):
            dest_types[dest] = 'Contract'
        else:
            dest_types[dest] = 'Wallet'

    contracts = sum(1 for v in dest_types.values() if v not in ('Wallet',))
    print(f'  Classified: {contracts} contracts, {len(dest_types) - contracts} wallets')

    return addr_balances, dest_balances, dest_types

# ── Build output ───────────────────────────────────────────────────────────────
def build_output(addresses, claimed_by_addr, dump_raw, cex_by_addr, addr_balances, dest_balances, dest_types, stake_raw=None):
    out = []
    for addr in addresses:
        claimed = claimed_by_addr.get(addr, 0.0)
        r = dump_raw.get(addr, {'sold': 0.0, 'bridged': 0.0, 'dests': {}})

        transferred, transferred_to = 0.0, []
        sent_held,   sent_held_to   = 0.0, []
        defi_positions = []
        for dest, amount in r['dests'].items():
            dtype = dest_types.get(dest, 'Wallet')
            if dtype != 'Wallet':
                # Contract destination = productive DeFi use, not a dump
                defi_positions.append({
                    'address': dest,
                    'amount':  round(amount, 6),
                    'type':    dtype,
                    'held':    dest_balances.get(dest, 0) >= amount * 0.95,
                })
            elif dest_balances.get(dest, 0.0) >= amount * 0.95:
                sent_held += amount
                sent_held_to.append(dest)
            else:
                transferred += amount
                transferred_to.append(dest)

        xchain = cex_by_addr.get(addr, {'cexSent': 0.0, 'cexDests': []})
        bals   = addr_balances.get(addr, {'kat': 0.0, 'avkat': 0.0})

        d = {
            'address':        addr,
            'claimed':        round(claimed, 6),
            'claimable':      0,
            'total':          round(claimed, 6),
            'balance':        round(bals['kat'], 6),
            'avkat':          round(bals['avkat'], 6),
            'moved':          round(r['sold'], 6),
            'transferred':    round(transferred, 6),
            'transferredTo':  transferred_to,
            'sentHeld':       round(sent_held, 6),
            'sentHeldTo':     sent_held_to,
            'defiPositions':  defi_positions,
            'bridged':        round(r['bridged'], 6),
            'cexSent':        round(xchain['cexSent'], 6),
            'cexDests':       xchain['cexDests'],
        }
        d['status'] = classify(d, stake_raw=stake_raw)
        out.append(d)
    return out

# ── Build buyers output ────────────────────────────────────────────────────────
def build_buyers_output(buy_raw, stake_raw, addr_set, addr_balances, vkat_locks):
    out = []
    for addr, b in buy_raw.items():
        sources = b.get('sources', [])
        if len(sources) >= 2:
            buy_source = 'both'
        elif sources:
            buy_source = sources[0]
        else:
            buy_source = 'dex'
        kat_net = b['katReceived'] - b.get('katSold', 0.0)
        bals = addr_balances.get(addr, {'kat': 0.0, 'avkat': 0.0})
        kat_balance = bals['kat'] + bals['avkat']  # Include avKAT in held total
        # Derive staking booleans from on-chain state, not stale events
        has_vkat  = addr in vkat_locks and vkat_locks[addr]['amount'] > 0
        has_avkat = bals['avkat'] > 0
        # Include only if net purchased via DEX/CEX >= 1000
        if kat_net < 1000:
            continue
        lock_info = vkat_locks.get(addr, {'amount': 0.0})
        vkat_amt  = lock_info['amount'] if has_vkat else 0.0
        avkat_amt = bals['avkat']
        out.append({
            'address':     addr,
            'category':    'airdrop_buyer' if addr in addr_set else 'pure_buyer',
            'buySource':   buy_source,
            'katHeld':     round(kat_balance, 6),
            'katNet':      round(kat_net, 6),
            'katReceived': round(b['katReceived'], 6),
            'katSold':     round(b.get('katSold', 0.0), 6),
            'txCount':     b['txCount'],
            'stakedVKAT':  has_vkat,
            'stakedAvKAT': has_avkat,
            'staked':      has_vkat or has_avkat,
            'vkatAmount':  round(vkat_amt, 6),
            'avkatAmount': round(avkat_amt, 6),
        })
    out.sort(key=lambda x: x['katNet'], reverse=True)
    return out

# ── Build stakers output ──────────────────────────────────────────────────
def build_stakers_output(stake_raw, addr_balances, vkat_locks):
    # Merge vKAT NFT holders into stake_raw so they appear even if they
    # never sent a KAT Transfer to the escrow (e.g. received NFT via transfer)
    for addr in vkat_locks:
        if addr not in stake_raw:
            stake_raw[addr] = {'stakedVKAT': True, 'stakedAvKAT': False, 'vkatAmount': 0.0, 'avkatAmount': 0.0}
        else:
            stake_raw[addr]['stakedVKAT'] = True

    out = []
    for addr, s in stake_raw.items():
        bals = addr_balances.get(addr, {'kat': 0.0, 'avkat': 0.0})
        avkat_bal = bals['avkat']
        # vKAT: on-chain locked amount from NFT enumeration (replaces stale cumulative)
        lock_info = vkat_locks.get(addr, {'amount': 0.0, 'endTime': 0})
        vkat_amt  = lock_info['amount']
        total     = vkat_amt + avkat_bal
        if total < 100:
            continue
        out.append({
            'address':     addr,
            'vkatAmount':  round(vkat_amt, 6),
            'avkatAmount': round(avkat_bal, 6),
            'totalStaked': round(total, 6),
            'stakedVKAT':  vkat_amt > 0,
            'stakedAvKAT': avkat_bal > 0,
        })
    out.sort(key=lambda x: x['totalStaked'], reverse=True)
    return out

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='KAT Farmer Indexer')
    parser.add_argument('--full', action='store_true', help='Rescan from genesis (ignore state.json)')
    args = parser.parse_args()

    print('KAT Farmer Indexer')
    print('=' * 50)

    addresses   = load_addresses()
    addr_set    = set(addresses)
    cex_wallets = load_cex_wallets()
    print()

    state = load_state() if not args.full else {
        'katanaBlock': DEPLOY_BLOCK - 1,
        'ethBlock':    ETH_KAT_START - 1,
        'claimedByAddr': {},
        'dumpRaw':       {},
        'cexByAddr':     {},
        'buyRaw':        {},
        'stakeRaw':      {},
    }
    if args.full:
        print('  --full: starting from genesis')
    else:
        print(f'  State: katana={state["katanaBlock"]:,}, eth={state["ethBlock"]:,}')
    print()

    print('1/6 Scanning Claimed events…')
    claimed_by_addr, latest_katana = scan_claimed(state, addr_set)
    state['claimedByAddr'] = claimed_by_addr
    print()

    print('2/6 Scanning Transfer events (dump + buyers + staking)…')
    dump_raw, buy_raw, stake_raw = scan_transfers(state, addr_set, cex_wallets, latest_katana)
    state['dumpRaw']  = dump_raw
    state['buyRaw']   = buy_raw
    state['stakeRaw'] = stake_raw
    print()

    print('3/6 Scanning aVKAT holders (share token transfers)…')
    avkat_holders, _ = scan_avkat_holders(state, stake_raw)
    state['avkatHolders']    = list(avkat_holders)
    state['avkatHolderBlock'] = latest_katana
    print()

    print('4/6 Scanning ETH mainnet CEX transfers…')
    cex_by_addr, latest_eth = scan_cex(state, addr_set, cex_wallets, dump_raw)
    state['cexByAddr'] = cex_by_addr
    print()

    state['katanaBlock'] = latest_katana
    state['ethBlock']    = latest_eth

    # Collect buyer + staker addresses for balance fetching
    extra_addrs = set(buy_raw.keys()) | set(stake_raw.keys())
    print('5/6 Fetching fresh balances…')
    addr_balances, dest_balances, dest_types = get_fresh_balances(addresses, dump_raw, extra_addrs)
    print()

    save_state(state)
    print()

    print('6/6 Building data.json…')
    vkat_locks    = enumerate_vkat_locks()
    address_data  = build_output(addresses, claimed_by_addr, dump_raw, cex_by_addr, addr_balances, dest_balances, dest_types, stake_raw=stake_raw)
    buyers_data   = build_buyers_output(buy_raw, stake_raw, addr_set, addr_balances, vkat_locks)
    stakers_data  = build_stakers_output(stake_raw, addr_balances, vkat_locks)

    total    = len(address_data)
    farmers  = sum(1 for d in address_data if d['status'] == 'farmer')
    hodlers  = sum(1 for d in address_data if d['status'] == 'hodler')
    partials = sum(1 for d in address_data if d['status'] == 'partial')
    inactive = sum(1 for d in address_data if d['status'] == 'inactive')
    print(f'  {total:,} airdrop addrs: {farmers} farmers, {hodlers} hodlers, {partials} partial, {inactive} inactive')

    b_total  = len(buyers_data)
    b_pure   = sum(1 for b in buyers_data if b['category'] == 'pure_buyer')
    b_staked = sum(1 for b in buyers_data if b['staked'])
    print(f'  {b_total:,} buyers: {b_pure} pure · {b_total - b_pure} airdrop+ · {b_staked} staked ({100*b_staked/b_total:.1f}%)' if b_total else '  0 buyers found')

    s_total = len(stakers_data)
    s_total_staked = sum(s['totalStaked'] for s in stakers_data)
    print(f'  {s_total:,} stakers (≥100 KAT): {fmtM(s_total_staked)} KAT total staked')

    # Read circulating supply from supply_data.json (full circ, not just Merkl)
    circ_supply = 0.0
    supply_data_path = SCRIPT_DIR / 'supply_data.json'
    if supply_data_path.exists():
        try:
            sd = json.loads(supply_data_path.read_text())
            circ_supply = sd.get('totalCirculating', 0.0)
        except Exception:
            pass

    # On-chain contract totals (ground truth, not limited to tracked addresses)
    # vKAT escrow holds KAT base token (not the wrapped variant)
    KAT_BASE = '0x7f1f4b4b29f5058fa32cc7a97141b8d7e5abdc2d'
    on_chain_vkat  = balance_of(KAT_BASE, VOTING_ESCROW)
    on_chain_avkat = total_assets(AVKAT_ADDR)
    on_chain_total = on_chain_vkat + on_chain_avkat
    print(f'  On-chain: vKAT={fmtM(on_chain_vkat)}, aVKAT={fmtM(on_chain_avkat)}, total={fmtM(on_chain_total)}')
    # Cross-check: enumerated vKAT vs on-chain KAT held by escrow
    enumerated_vkat = sum(v['amount'] for v in vkat_locks.values())
    drift_pct = abs(enumerated_vkat - on_chain_vkat) / on_chain_vkat * 100 if on_chain_vkat > 0 else 0
    if drift_pct > 0.1:
        print(f'  ⚠ vKAT drift: enumerated={fmtM(enumerated_vkat)} vs on-chain={fmtM(on_chain_vkat)} ({drift_pct:.2f}%)')
    else:
        print(f'  ✓ vKAT cross-check OK: enumerated={fmtM(enumerated_vkat)} ≈ on-chain={fmtM(on_chain_vkat)}')

    output = {
        'meta': {
            'generatedAt':  datetime.now(timezone.utc).isoformat(),
            'katanaBlock':  latest_katana,
            'ethBlock':     latest_eth,
            'addressCount': total,
            'buyerCount':   b_total,
            'stakerCount':  s_total,
            'circSupply':   round(circ_supply, 6),
            'onChainVkat':  round(on_chain_vkat, 6),
            'onChainAvkat': round(on_chain_avkat, 6),
            'onChainTotal': round(on_chain_total, 6),
        },
        'addresses': address_data,
        'buyers':    buyers_data,
        'stakers':   stakers_data,
    }

    DATA_PATH.write_text(json.dumps(output, separators=(',', ':')))
    size_kb = DATA_PATH.stat().st_size / 1024
    print(f'\n✓ Wrote {DATA_PATH.name} ({size_kb:.1f} KB)')

    # ── Save daily snapshot for growth stats ──────────────────────────────────
    snap_path = SCRIPT_DIR / 'snapshots.json'
    try:
        snapshots = json.loads(snap_path.read_text()) if snap_path.exists() else {}
    except Exception:
        snapshots = {}
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    snapshots[today] = {
        'totalStaked': round(on_chain_total, 2),
        'pctCirc': round(on_chain_total / circ_supply * 100, 2) if circ_supply else 0,
        'count': s_total,
        'vkat': round(on_chain_vkat, 2),
        'avkat': round(on_chain_avkat, 2),
    }
    # Prune entries older than 90 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime('%Y-%m-%d')
    snapshots = {k: v for k, v in snapshots.items() if k >= cutoff}
    snap_path.write_text(json.dumps(snapshots, indent=2))
    print(f'  Saved {snap_path.name} ({len(snapshots)} snapshots)')

    print('Done.')

if __name__ == '__main__':
    main()
