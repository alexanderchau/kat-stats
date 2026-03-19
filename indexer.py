#!/usr/bin/env python3
"""
KAT Dumpers Indexer
Scans Katana + ETH mainnet, writes data.json consumed by index.html.

Usage:
    python3 indexer.py              # incremental (only new blocks)
    python3 indexer.py --full       # ignore state.json, rescan from genesis

State is persisted in state.json so each run only fetches new blocks.
"""

import json, re, sys, time, threading, argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import urllib.request, urllib.error

SCRIPT_DIR = Path(__file__).parent
STATE_PATH = SCRIPT_DIR / 'state.json'
DATA_PATH  = SCRIPT_DIR / 'data.json'
INDEX_PATH = SCRIPT_DIR / 'index.html'

# ── Chain config ───────────────────────────────────────────────────────────────
KATANA_RPC    = 'https://rpc.katana.network/'
ETH_RPC       = 'https://ethereum.publicnode.com'
LOG_CHUNK     = 29_999    # Katana max getLogs range (30k fails)
ETH_LOG_CHUNK = 100_000   # ETH mainnet (~14 days per chunk)

# ── Contracts ──────────────────────────────────────────────────────────────────
DISTRIBUTOR_ADDR = '0x3ef3d8ba38ebe18db133cec108f4d14ce00dd9ae'
CLAIMED_TOPIC    = '0xf7a40077ff7a04c7e61f6f26fb13774259ddf1b6bce9ecf26a8276cdd3992683'
TRANSFER_TOPIC   = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
SWAP_V3_TOPIC    = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
SWAP_V2_TOPIC    = '0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822'

DEPLOY_BLOCK  = 2_808_000
KAT_ADDR      = '0x6e9c1f88a960fe63387eb4b71bc525a9313d8461'
AVKAT_ADDR    = '0x7231dbaCdFc968E07656D12389AB20De82FbfCeB'.lower()
KAT_DECIMALS  = 18

KAT_TOKENS = {
    '0x6e9c1f88a960fe63387eb4b71bc525a9313d8461',  # KAT wrapped v2 (primary Merkl-distributed)
    '0x3ba1fbc4c3aea775d335b31fb53778f46fd3a330',  # KAT wrapped
    '0x7f1f4b4b29f5058fa32cc7a97141b8d7e5abdc2d',  # KAT base
}

KAT_POOL_ADDRS = {
    '0x1d1f00a79bcd17e4ca05c0204a84a806c4417ced',  # V3 KAT/USDC 0.01%
    '0x10045367e619caae6f60cc80046c43c6cd55f629',  # V3 KAT/USDC 0.05%
    '0x6d8a30f4b2501de8f0b443cb11eb512f12d5355f',  # V3 KAT/USDC 0.3%
    '0xaa7bb0c80a30b61a5bb20a804f6cc96651bad37a',  # V3 KAT/USDC 1%
    '0x358004bf9ecd5128821bcd385e8018c403038f51',  # V3 KAT/USDT 1%
    '0xeb638f16ab412705fce43572cb5d0d251051ed32',  # V3 KAT/WETH 0.01%
    '0xfe4e52ccf659705141e6fa5dee01432a3e637904',  # V3 KAT/WETH 0.05%
    '0x74dde0376f6cb5633cc2a7f83b1d8c56161f59e5',  # V2 USDC/KAT
}

BRIDGE      = '0x2a3dd3eb832af982ec71669e178424b10dca2ede'
BRIDGE2     = '0x64b20eb25aed030fd510ef93b9125278b152f6a6'
BRIDGE_DESTS = {BRIDGE, BRIDGE2}
ZERO_ADDR   = '0x0000000000000000000000000000000000000000'

KAT_ETH_ADDR  = '0x8f051ca72a3440d83b18e71c3e59676203ab8f91'
ETH_KAT_START = 21_900_000

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
            req  = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'}, method='POST')
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

def balance_of(token, wallet, url=KATANA_RPC):
    padded = wallet.replace('0x', '').lower().zfill(64)
    data   = '0x70a08231' + padded
    r = rpc_call(url, 'eth_call', [{'to': token, 'data': data}, 'latest'])
    return from_wei(r) if r else 0.0

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
    }

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))
    print(f'  Saved state.json (katana={state["katanaBlock"]}, eth={state["ethBlock"]})')

# ── Classification ─────────────────────────────────────────────────────────────
def disposed(d):
    return d.get('moved', 0) + d.get('transferred', 0) + d.get('bridged', 0)

def classify(d):
    if d['claimed'] == 0:
        return 'inactive'
    ratio = min(1.0, disposed(d) / d['claimed'])
    if ratio > 0.5:  return 'dumper'
    if ratio > 0.05: return 'partial'
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

# ── Scan: Transfer events (dump detection) ─────────────────────────────────────
def scan_dump(state, addr_set, latest_block):
    scan_from = state['katanaBlock'] + 1
    dump_raw  = state['dumpRaw']

    if scan_from > latest_block:
        print(f'  Dump: cache current at block {latest_block:,}')
        return dump_raw

    chunks = [(f, min(f + LOG_CHUNK - 1, latest_block)) for f in range(scan_from, latest_block + 1, LOG_CHUNK)]
    print(f'  Dump: {len(chunks)} chunks × {len(KAT_TOKENS)} tokens…')

    # Collect all Transfer events across all 3 KAT tokens
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
    print(f'  Dump: {len(all_logs):,} raw Transfer events collected            ')

    # Group by txHash, filter to our address set as sender
    tx_map = {}
    for log in all_logs:
        topics = log.get('topics', [])
        if len(topics) < 3:
            continue
        sender = '0x' + topics[1][26:].lower()
        if sender not in addr_set:
            continue
        dest   = '0x' + topics[2][26:].lower()
        amount = from_wei(log.get('data', '0x0'))
        tx     = log.get('transactionHash', '')
        if not tx:
            continue
        if tx not in tx_map:
            tx_map[tx] = {'from': sender, 'entries': [], 'is_swap': False}
        tx_map[tx]['entries'].append({'to': dest, 'amount': amount})

    # Swap detection: pool-address shortcut first, receipt fallback for unknowns
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

    print(f'  Dump: {len(receipts_needed):,} receipts needed for swap detection…')
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

    # Accumulate into dump_raw
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

    print(f'  Dump: {len(dump_raw):,} addresses with KAT transfer activity')
    return dump_raw

# ── Scan: ETH mainnet CEX transfers ────────────────────────────────────────────
def scan_cex(state, addr_set, cex_wallets):
    if not cex_wallets:
        return state['cexByAddr'], state['ethBlock']

    scan_from   = state['ethBlock'] + 1
    latest      = get_block_number(ETH_RPC)
    cex_by_addr = state['cexByAddr']

    if scan_from > latest:
        print(f'  CEX: cache current at ETH block {latest:,}')
        return cex_by_addr, latest

    chunks = [(f, min(f + ETH_LOG_CHUNK - 1, latest)) for f in range(scan_from, latest + 1, ETH_LOG_CHUNK)]
    print(f'  CEX: {len(chunks)} ETH chunks ({scan_from:,} → {latest:,})…')

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
            if sender not in addr_set or dest not in cex_wallets:
                continue
            amount = from_wei(log.get('data', '0x0'))
            if sender not in cex_by_addr:
                cex_by_addr[sender] = {'cexSent': 0.0, 'cexDests': []}
            cex_by_addr[sender]['cexSent'] += amount
            if dest not in cex_by_addr[sender]['cexDests']:
                cex_by_addr[sender]['cexDests'].append(dest)
        if i % 5 == 0 or i == len(chunks):
            print(f'    {i}/{len(chunks)} chunks…', end='\r')

    print(f'  CEX: {len(cex_by_addr):,} addresses with cross-chain CEX activity            ')
    return cex_by_addr, latest

# ── Fresh balances ─────────────────────────────────────────────────────────────
def get_fresh_balances(addresses, dump_raw):
    all_dest_tokens = list(KAT_TOKENS) + [AVKAT_ADDR]
    all_dests = set()
    for r in dump_raw.values():
        all_dests.update(r['dests'].keys())

    total_tasks = len(addresses) + len(all_dests)
    print(f'  Balances: {len(addresses):,} addresses + {len(all_dests):,} destinations = {total_tasks:,} tasks…')

    addr_balances = {}  # addr  -> { kat, avkat }
    dest_balances = {}  # dest  -> total (all KAT tokens + avkat)
    done_count = 0
    lock = threading.Lock()

    def fetch_addr(addr):
        kat   = balance_of(KAT_ADDR,  addr)
        avkat = balance_of(AVKAT_ADDR, addr)
        return addr, kat, avkat

    def fetch_dest(dest):
        total = sum(balance_of(t, dest) for t in all_dest_tokens)
        return dest, total

    with ThreadPoolExecutor(max_workers=16) as ex:
        addr_futs = {ex.submit(fetch_addr, a): a for a in addresses}
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

    return addr_balances, dest_balances

# ── Build output ───────────────────────────────────────────────────────────────
def build_output(addresses, claimed_by_addr, dump_raw, cex_by_addr, addr_balances, dest_balances):
    out = []
    for addr in addresses:
        claimed = claimed_by_addr.get(addr, 0.0)
        r = dump_raw.get(addr, {'sold': 0.0, 'bridged': 0.0, 'dests': {}})

        transferred, transferred_to = 0.0, []
        sent_held,   sent_held_to   = 0.0, []
        for dest, amount in r['dests'].items():
            if dest_balances.get(dest, 0.0) >= amount * 0.95:
                sent_held += amount
                sent_held_to.append(dest)
            else:
                transferred += amount
                transferred_to.append(dest)

        xchain = cex_by_addr.get(addr, {'cexSent': 0.0, 'cexDests': []})
        bals   = addr_balances.get(addr, {'kat': 0.0, 'avkat': 0.0})

        d = {
            'address':       addr,
            'claimed':       round(claimed, 6),
            'claimable':     0,
            'total':         round(claimed, 6),
            'balance':       round(bals['kat'], 6),
            'avkat':         round(bals['avkat'], 6),
            'moved':         round(r['sold'], 6),
            'transferred':   round(transferred, 6),
            'transferredTo': transferred_to,
            'sentHeld':      round(sent_held, 6),
            'sentHeldTo':    sent_held_to,
            'bridged':       round(r['bridged'], 6),
            'cexSent':       round(xchain['cexSent'], 6),
            'cexDests':      xchain['cexDests'],
        }
        d['status'] = classify(d)
        out.append(d)
    return out

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='KAT Dumpers Indexer')
    parser.add_argument('--full', action='store_true', help='Rescan from genesis (ignore state.json)')
    args = parser.parse_args()

    print('KAT Dumpers Indexer')
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
    }
    if args.full:
        print('  --full: starting from genesis')
    else:
        print(f'  State: katana={state["katanaBlock"]:,}, eth={state["ethBlock"]:,}')
    print()

    print('1/5 Scanning Claimed events…')
    claimed_by_addr, latest_katana = scan_claimed(state, addr_set)
    state['claimedByAddr'] = claimed_by_addr
    print()

    print('2/5 Scanning Transfer events (dump detection)…')
    dump_raw = scan_dump(state, addr_set, latest_katana)
    state['dumpRaw'] = dump_raw
    print()

    print('3/5 Scanning ETH mainnet CEX transfers…')
    cex_by_addr, latest_eth = scan_cex(state, addr_set, cex_wallets)
    state['cexByAddr'] = cex_by_addr
    print()

    state['katanaBlock'] = latest_katana
    state['ethBlock']    = latest_eth

    print('4/5 Fetching fresh balances…')
    addr_balances, dest_balances = get_fresh_balances(addresses, dump_raw)
    print()

    save_state(state)
    print()

    print('5/5 Building data.json…')
    address_data = build_output(addresses, claimed_by_addr, dump_raw, cex_by_addr, addr_balances, dest_balances)

    total    = len(address_data)
    dumpers  = sum(1 for d in address_data if d['status'] == 'dumper')
    hodlers  = sum(1 for d in address_data if d['status'] == 'hodler')
    partials = sum(1 for d in address_data if d['status'] == 'partial')
    inactive = sum(1 for d in address_data if d['status'] == 'inactive')
    print(f'  {total:,} addresses: {dumpers} dumpers, {hodlers} hodlers, {partials} partial, {inactive} inactive')

    output = {
        'meta': {
            'generatedAt':  datetime.now(timezone.utc).isoformat(),
            'katanaBlock':  latest_katana,
            'ethBlock':     latest_eth,
            'addressCount': total,
        },
        'addresses': address_data,
    }

    DATA_PATH.write_text(json.dumps(output, separators=(',', ':')))
    size_kb = DATA_PATH.stat().st_size / 1024
    print(f'\n✓ Wrote {DATA_PATH.name} ({size_kb:.1f} KB)')
    print('Done.')

if __name__ == '__main__':
    main()
