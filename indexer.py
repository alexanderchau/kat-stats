#!/usr/bin/env python3
"""
KAT Farmer Indexer
Scans Katana + ETH mainnet, writes data.json consumed by index.html.

Usage:
    python3 indexer.py              # incremental (only new blocks)
    python3 indexer.py --full       # ignore state.json, rescan from genesis

State is persisted in state.json so each run only fetches new blocks.
"""

import json, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

from config import (
    KATANA_RPC, ETH_RPC, LOG_CHUNK, ETH_LOG_CHUNK,
    DISTRIBUTOR_ADDR, CLAIMED_TOPIC, TRANSFER_TOPIC,
    SWAP_V3_TOPIC, SWAP_V2_TOPIC,
    DEPLOY_BLOCK, KAT_ADDR, AVKAT_ADDR, KAT_DECIMALS,
    KAT_TOKENS, KAT_POOL_ADDRS, BRIDGE_DESTS, ZERO_ADDR,
    VOTING_ESCROW, LOCK_NFT, STAKE_DESTS,
    KAT_ETH_ADDR, ETH_KAT_START,
    KAT_BASE_ADDR, BUYER_MIN_KAT, STAKER_MIN_KAT,
)
import rpc
import fileio
from scanners import (
    scan_claimed, scan_transfers, scan_avkat_holders, scan_cex,
)
from balances import get_fresh_balances, get_eth_balances, enumerate_vkat_locks
from builders import build_output, build_buyers_output, build_stakers_output

SCRIPT_DIR = Path(__file__).parent
STATE_PATH = SCRIPT_DIR / 'state.json'
DATA_PATH  = SCRIPT_DIR / 'data.json'

# Full defaults dict — defines the state schema with all keys.
# load_state merges loaded JSON against this, so old state.json files
# with missing keys still work.
STATE_DEFAULTS = {
    'katanaBlock':     DEPLOY_BLOCK - 1,
    'ethBlock':        ETH_KAT_START - 1,
    'claimedByAddr':   {},
    'dumpRaw':         {},
    'cexByAddr':       {},
    'buyRaw':          {},
    'stakeRaw':        {},
    'avkatHolders':    [],
    'avkatHolderBlock': DEPLOY_BLOCK - 1,
}


def load_addresses():
    """Load the tracked address list from addresses.json."""
    path = SCRIPT_DIR / 'addresses.json'
    data = fileio.load_json(path)
    if not data or 'addresses' not in data:
        import sys
        sys.exit('ERROR: addresses.json missing or invalid')
    addrs = data['addresses']
    print(f'  Loaded {len(addrs)} addresses from addresses.json')
    return addrs


def main():
    parser = argparse.ArgumentParser(description='KAT Farmer Indexer')
    parser.add_argument('--full', action='store_true',
                        help='Rescan from genesis (ignore state.json)')
    args = parser.parse_args()

    print('KAT Farmer Indexer')
    print('=' * 50)

    addresses   = load_addresses()
    addr_set    = set(addresses)
    addr_data   = fileio.load_json(SCRIPT_DIR / 'addresses.json', {})
    cex_wallets = set(addr_data.get('cex_wallets', []))
    print(f'  Loaded {len(cex_wallets)} CEX wallets from addresses.json')
    print()

    if args.full:
        state = dict(STATE_DEFAULTS)
        print('  --full: starting from genesis')
    else:
        state = fileio.load_state(STATE_PATH, STATE_DEFAULTS)
        print(f'  State: katana={state["katanaBlock"]:,}, eth={state["ethBlock"]:,}')
    print()

    print('1/6 Scanning Claimed events…')
    claimed_by_addr, latest_katana = scan_claimed(
        state, addr_set, KATANA_RPC,
        DISTRIBUTOR_ADDR, CLAIMED_TOPIC, KAT_TOKENS, LOG_CHUNK,
    )
    state['claimedByAddr'] = claimed_by_addr
    print()

    print('2/6 Scanning Transfer events (dump + buyers + staking)…')
    dump_raw, buy_raw, stake_raw = scan_transfers(
        state, addr_set, cex_wallets, latest_katana, KATANA_RPC,
        KAT_TOKENS, KAT_POOL_ADDRS, TRANSFER_TOPIC,
        SWAP_V3_TOPIC, SWAP_V2_TOPIC,
        STAKE_DESTS, VOTING_ESCROW, BRIDGE_DESTS, ZERO_ADDR, LOG_CHUNK,
    )
    state['dumpRaw']  = dump_raw
    state['buyRaw']   = buy_raw
    state['stakeRaw'] = stake_raw
    print()

    print('3/6 Scanning aVKAT holders (share token transfers)…')
    avkat_holders, _ = scan_avkat_holders(
        state, stake_raw, KATANA_RPC,
        AVKAT_ADDR, TRANSFER_TOPIC, ZERO_ADDR, DEPLOY_BLOCK, LOG_CHUNK,
    )
    state['avkatHolders']     = list(avkat_holders)
    state['avkatHolderBlock'] = latest_katana
    print()

    print('4/6 Scanning ETH mainnet CEX transfers…')
    cex_by_addr, latest_eth = scan_cex(
        state, addr_set, cex_wallets, dump_raw, ETH_RPC,
        KAT_ETH_ADDR, TRANSFER_TOPIC, ETH_LOG_CHUNK,
    )
    state['cexByAddr'] = cex_by_addr
    print()

    state['katanaBlock'] = latest_katana
    state['ethBlock']    = latest_eth

    extra_addrs = set(buy_raw.keys()) | set(stake_raw.keys())
    print('5/6 Fetching fresh balances…')
    addr_balances, dest_balances, dest_types = get_fresh_balances(
        addresses, dump_raw, extra_addrs, KATANA_RPC,
        KAT_TOKENS, AVKAT_ADDR, KAT_POOL_ADDRS, STAKE_DESTS,
        BRIDGE_DESTS, DISTRIBUTOR_ADDR,
    )
    bridged_addrs = [a for a in addresses if dump_raw.get(a, {}).get('bridged', 0) > 0]
    eth_balances = get_eth_balances(bridged_addrs, ETH_RPC, KAT_ETH_ADDR)
    print()

    fileio.save_json(STATE_PATH, state, compact=True)
    print(f'  Saved state.json (katana={state["katanaBlock"]}, eth={state["ethBlock"]})')
    print()

    print('6/6 Building data.json…')
    vkat_locks    = enumerate_vkat_locks(KATANA_RPC, LOCK_NFT, VOTING_ESCROW, KAT_DECIMALS)
    address_data  = build_output(
        addresses, claimed_by_addr, dump_raw, cex_by_addr,
        addr_balances, dest_balances, dest_types, stake_raw=stake_raw,
        eth_balances=eth_balances,
    )
    buyers_data   = build_buyers_output(
        buy_raw, stake_raw, addr_set, addr_balances, vkat_locks, BUYER_MIN_KAT,
    )
    stakers_data  = build_stakers_output(stake_raw, addr_balances, vkat_locks, STAKER_MIN_KAT)

    total    = len(address_data)
    farmers  = sum(1 for d in address_data if d['status'] == 'farmer')
    hodlers  = sum(1 for d in address_data if d['status'] == 'hodler')
    partials = sum(1 for d in address_data if d['status'] == 'partial')
    inactive = sum(1 for d in address_data if d['status'] == 'inactive')
    print(f'  {total:,} airdrop addrs: {farmers} farmers, {hodlers} hodlers, {partials} partial, {inactive} inactive')

    b_total  = len(buyers_data)
    b_pure   = sum(1 for b in buyers_data if b['category'] == 'pure_buyer')
    b_staked = sum(1 for b in buyers_data if b['staked'])
    if b_total:
        print(f'  {b_total:,} buyers: {b_pure} pure · {b_total - b_pure} airdrop+ · {b_staked} staked ({100*b_staked/b_total:.1f}%)')
    else:
        print('  0 buyers found')

    s_total        = len(stakers_data)
    s_total_staked = sum(s['totalStaked'] for s in stakers_data)
    print(f'  {s_total:,} stakers (≥100 KAT): {rpc.fmtM(s_total_staked)} KAT total staked')

    # Read circulating supply from supply_data.json
    circ_supply      = 0.0
    supply_data_path = SCRIPT_DIR / 'supply_data.json'
    if supply_data_path.exists():
        try:
            sd = json.loads(supply_data_path.read_text())
            circ_supply = sd.get('totalCirculating', 0.0)
        except Exception:
            pass

    on_chain_vkat  = rpc.balance_of(KAT_BASE_ADDR, VOTING_ESCROW, KATANA_RPC)
    on_chain_avkat = rpc.total_assets(AVKAT_ADDR, KATANA_RPC)
    on_chain_total = on_chain_vkat + on_chain_avkat
    print(f'  On-chain: vKAT={rpc.fmtM(on_chain_vkat)}, aVKAT={rpc.fmtM(on_chain_avkat)}, total={rpc.fmtM(on_chain_total)}')

    enumerated_vkat = sum(v['amount'] for v in vkat_locks.values())
    drift_pct = abs(enumerated_vkat - on_chain_vkat) / on_chain_vkat * 100 if on_chain_vkat > 0 else 0
    if drift_pct > 0.1:
        print(f'  ⚠ vKAT drift: enumerated={rpc.fmtM(enumerated_vkat)} vs on-chain={rpc.fmtM(on_chain_vkat)} ({drift_pct:.2f}%)')
    else:
        print(f'  ✓ vKAT cross-check OK: enumerated={rpc.fmtM(enumerated_vkat)} ≈ on-chain={rpc.fmtM(on_chain_vkat)}')

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

    fileio.save_json(DATA_PATH, output, compact=True)
    size_kb = DATA_PATH.stat().st_size / 1024
    print(f'\n✓ Wrote {DATA_PATH.name} ({size_kb:.1f} KB)')

    # ── Save daily snapshot for growth stats ──────────────────────────────
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
    cutoff    = (datetime.now(timezone.utc) - timedelta(days=90)).strftime('%Y-%m-%d')
    snapshots = {k: v for k, v in snapshots.items() if k >= cutoff}
    fileio.save_json(snap_path, snapshots, compact=False)
    print(f'  Saved {snap_path.name} ({len(snapshots)} snapshots)')

    print('Done.')


if __name__ == '__main__':
    main()
