#!/usr/bin/env python3
"""
KAT Supply Circulation Tracker

Computes how much KAT has entered circulation:
  - Merkl rewards:  all Transfer events from the distributor (on-chain scan)
  - EtherFi vault:  hardcoded 0.2% of total supply
  - Lombard vault:  hardcoded 0.2% of total supply
  - Krates:         0.7% (70M vKAT, unlocked at TGE)
  - Ecosystem liq:  6% (600M KAT, unlocked at TGE)
  - POL airdrop:    1.4% (140M vKAT, immediate tranche at TGE)
  - Public sale:    1% (100M KAT, unlocked at TGE)
  Note: TVL program KAT went out via Merkl distributor — already in on-chain scan.

Also outputs a breakdown of the top source wallets by amount claimed.

Usage:
    python3 supply.py              # incremental (resumes from last scanned block)
    python3 supply.py --full       # rescan from genesis
    python3 supply.py --top N      # show top N wallets (default 20)
"""

import json, time, argparse
from datetime import datetime, timezone
from pathlib import Path
import urllib.request, urllib.error

SCRIPT_DIR        = Path(__file__).parent
SUPPLY_STATE_PATH = SCRIPT_DIR / 'supply_state.json'

# ── Merkl API ────────────────────────────────────────────────────────────────
MERKL_API = 'https://api.merkl.xyz/v3/userRewards'

# ── Chain ───────────────────────────────────────────────────────────────────────
KATANA_RPC   = 'https://rpc.katana.network/'
LOG_CHUNK    = 29_999
KAT_DECIMALS = 18

# ── Contracts ───────────────────────────────────────────────────────────────────
DISTRIBUTOR_ADDR = '0x3ef3d8ba38ebe18db133cec108f4d14ce00dd9ae'
TRANSFER_TOPIC   = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
DEPLOY_BLOCK     = 2_808_000
ZERO_ADDR        = '0x0000000000000000000000000000000000000000'
KAT_ADDR         = '0x6e9c1f88a960fe63387eb4b71bc525a9313d8461'

# KAT token addresses distributed via Merkl (18 decimals each)
KAT_TOKENS = {
    '0x6e9c1f88a960fe63387eb4b71bc525a9313d8461',  # KAT (main circulating token, ~431M supply)
    '0x3ba1fbc4c3aea775d335b31fb53778f46fd3a330',  # KAT wrapped (~19.8M supply)
    # '0x7f1f4b4b29f5058fa32cc7a97141b8d7e5abdc2d' excluded — KAT base (10B total supply,
    #   not distributed to end users; it's the underlying token that wraps into the above)
}

# Total KAT supply (base token, 10B)
TOTAL_KAT_SUPPLY = 10_000_000_000

# ── Fixed allocations (absolute amounts, already distributed) ──────────────────
# Source of truth: #multisig-operations-requests Slack channel + tokenomics.
# TVL program KAT went out via Merkl distributor — already in on-chain scan.
# Spectra rewards go through Merkl campaigns — already in on-chain scan.
# Employee/contributor tokens (~718M in nested SAFEs) are LOCKED per vesting.
# vKAT DAO (186.9M) is protocol treasury, not circulating.
# Ecosystem grants reserve (487M) still held by foundation.
# Each entry: (amount, description shown in frontend)
FIXED_ALLOCATIONS = {
    'EtherFi vault':       (  20_574_174, 'Vault rewards to depositors'),
    'Lombard vault':       (  15_000_000, 'Vault rewards to depositors'),
    'CEX deposits':        ( 308_000_000, 'Binance, Bitget, Kraken, KuCoin, others'),
    'Market makers':       ( 207_000_000, 'GSR, Selini, Lhava, FlowDesk'),
    'KOL payments':        (  12_550_000, 'ARK Point, Ethene Labs, Nebula, vendors'),
    'Ecosystem grants':    (   4_370_000, 'Foresight, Kensei, Jumper'),
    'Krates (vKAT)':       (  70_000_000, 'Pre-deposit Krates, unlocked at TGE'),
    'POL staker airdrop':  ( 140_000_000, 'Immediate tranche at TGE'),
    'Public sale':         ( 100_000_000, 'Binance Prime sale'),
}

# ── RPC ─────────────────────────────────────────────────────────────────────────
_rpc_id = 0

def rpc_call(method, params, retries=3, timeout=30):
    global _rpc_id
    _rpc_id += 1
    for attempt in range(retries):
        try:
            body = json.dumps({
                'jsonrpc': '2.0', 'id': _rpc_id, 'method': method, 'params': params
            }).encode()
            req = urllib.request.Request(
                KATANA_RPC, data=body,
                headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                if 'error' in data:
                    raise ValueError(f"RPC error: {data['error']}")
                return data.get('result')
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
            else:
                print(f'  ⚠ {method} failed: {e}')
                return None

def get_block_number():
    r = rpc_call('eth_blockNumber', [])
    return int(r, 16) if r else 0

def eth_get_logs(params):
    r = rpc_call('eth_getLogs', [params])
    return r if isinstance(r, list) else []

def from_wei(hex_val):
    if not hex_val or hex_val in ('0x', '0x0'):
        return 0.0
    try:
        return int(hex_val, 16) / (10 ** KAT_DECIMALS)
    except Exception:
        return 0.0

# ── Scan KAT Transfer events FROM distributor → users ──────────────────────────
#
# We scan Transfer events from the KAT token contracts where `from = distributor`.
# This gives the ACTUAL tokens sent to each user — incremental amounts, not
# cumulative. This approach is immune to the "cumulative Claimed event" problem
# and handles multiple distributor contracts automatically.
#
def scan_merkl_transfers(from_block, latest, existing_by_addr):
    """
    Scan KAT Transfer events where from = DISTRIBUTOR_ADDR.
    existing_by_addr: dict addr -> amount (incremental sums, safe to accumulate).
    Returns updated existing_by_addr.
    """
    distributor_topic = '0x' + DISTRIBUTOR_ADDR[2:].lower().zfill(64)

    chunks = [
        (f, min(f + LOG_CHUNK - 1, latest))
        for f in range(from_block, latest + 1, LOG_CHUNK)
    ]
    print(f'  Scanning {len(chunks)} chunks ({from_block:,} → {latest:,})…')

    for i, (frm, to) in enumerate(chunks, 1):
        for token in KAT_TOKENS:
            logs = eth_get_logs({
                'fromBlock': hex(frm),
                'toBlock':   hex(to),
                'address':   token,
                'topics':    [TRANSFER_TOPIC, distributor_topic],
            })
            for log in logs:
                topics = log.get('topics', [])
                if len(topics) < 3:
                    continue
                dest   = '0x' + topics[2][26:].lower()
                amount = from_wei(log.get('data', '0x0'))
                if dest == ZERO_ADDR:
                    continue  # skip burns
                existing_by_addr[dest] = existing_by_addr.get(dest, 0.0) + amount
        if i % 10 == 0 or i == len(chunks):
            print(f'    {i}/{len(chunks)} chunks…', end='\r')

    print()
    return existing_by_addr

# ── Protocol breakdown via Merkl API ────────────────────────────────────────

def _classify_reason(key):
    """Map a Merkl reason key to a canonical protocol bucket."""
    # MultiLogProcessor wraps sub-campaigns: "MultiLogProcessor_XYZ~ActualProtocol_..."
    if '~' in key:
        return _classify_reason(key.split('~')[-1])
    k = key.upper()
    if 'SUSHI' in k:
        # Per-swap keys: SUSHI_SWAP_<pool>_<txhash> → 4+ underscore-delimited segments after SUSHI
        parts = k.split('_')
        if 'SWAP' in parts and len(parts) >= 4:
            return ('SushiSwap', 'swap fees')
        return ('SushiSwap', 'LP')
    if 'MORPHO' in k:   return ('Morpho', None)
    if 'STEER' in k:    return ('Steer', None)
    if 'CHARM' in k:    return ('Charm', None)
    if 'ICHI' in k:     return ('Ichi', None)
    if 'ERC20' in k:    return ('Yearn', None)
    if 'EPOCH' in k:    return ('Katana Vaults', None)
    if 'BONUS' in k or 'TOPUP' in k: return ('bonus/topup', None)
    if 'COMP' in k or 'MISSING' in k: return ('compensation', None)
    return ('other', None)

def _fetch_json(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            if i < retries - 1:
                time.sleep(0.5)
    return None

def scan_protocol_mix(transfers_by_addr, sample_per_tier=20):
    """
    Stratified sample of Merkl userRewards to estimate protocol mix.
    Returns dict: protocol -> {'total': float, 'subs': {sub -> float}}
    """
    ranked = sorted(transfers_by_addr.items(), key=lambda x: x[1], reverse=True)
    n      = len(ranked)

    # Three tiers weighted by their share of total on-chain KAT
    total_vol = sum(v for _, v in ranked)
    tiers = [
        ('top',  ranked[:sample_per_tier],                   sum(v for _, v in ranked[:100])    / total_vol),
        ('mid',  ranked[max(0, n//5):n//5 + sample_per_tier], sum(v for _, v in ranked[100:1000]) / total_vol),
        ('tail', ranked[max(0, n//2):n//2 + sample_per_tier], sum(v for _, v in ranked[1000:])   / total_vol),
    ]

    # proto -> {sub -> weighted_sum}, proto -> weighted_total
    proto_subs  = {}
    proto_total = {}

    for tier_name, sample, tier_weight in tiers:
        sample_onchain = sum(v for _, v in sample)
        if sample_onchain == 0:
            continue

        for addr, on_chain_amt in sample:
            data = _fetch_json(f'{MERKL_API}?chainId={747474}&user={addr}')
            time.sleep(0.08)
            if not data:
                continue

            user_by_proto = {}  # (proto, sub) -> amount
            user_total    = 0.0
            for tok_addr, tok_data in data.items():
                if tok_addr.lower() not in KAT_TOKENS:
                    continue
                for reason_key, rd in tok_data.get('reasons', {}).items():
                    amt = int(rd.get('accumulated', '0')) / 1e18
                    if amt <= 0:
                        continue
                    key = _classify_reason(reason_key)
                    user_by_proto[key] = user_by_proto.get(key, 0.0) + amt
                    user_total += amt

            if user_total == 0:
                continue

            # Scale API fractions by on-chain amount, then by tier weight
            scale = (on_chain_amt / sample_onchain) * tier_weight
            for (proto, sub), amt in user_by_proto.items():
                frac = amt / user_total
                proto_total[proto]          = proto_total.get(proto, 0.0)          + frac * scale
                proto_subs.setdefault(proto, {})
                if sub:
                    proto_subs[proto][sub]  = proto_subs[proto].get(sub, 0.0)      + frac * scale

    # Normalise to sum=1
    grand = sum(proto_total.values()) or 1.0
    result = {}
    for proto, w in proto_total.items():
        subs_raw = proto_subs.get(proto, {})
        sub_sum  = sum(subs_raw.values()) or 1.0
        result[proto] = {
            'frac': w / grand,
            'subs': {s: v / sub_sum for s, v in subs_raw.items()},
        }
    return result

def print_protocol_breakdown(protocol_mix, merkl_total):
    col = 22
    print('── MERKL PROTOCOL BREAKDOWN ──────────────────────────')
    print(f'  {"Protocol":<{col}} {"KAT (est)":>14}  {"% Merkl":>7}')
    sep = col - 1
    print(f'  {"─"*sep} {"─"*14}  {"─"*7}')

    ORDER = ['Katana Vaults', 'SushiSwap', 'Morpho', 'Yearn',
             'Steer', 'Charm', 'Ichi', 'bonus/topup', 'compensation', 'other']
    shown = set()

    for proto in ORDER + [p for p in protocol_mix if p not in ORDER]:
        if proto not in protocol_mix:
            continue
        shown.add(proto)
        info  = protocol_mix[proto]
        frac  = info['frac']
        amt   = frac * merkl_total
        subs  = info.get('subs', {})

        if subs:
            # Combined line + inline subdivision as "tooltip"
            sub_note = '  (' + ' + '.join(
                f'{s} {100*v:.0f}%' for s, v in sorted(subs.items(), key=lambda x: x[1], reverse=True)
            ) + ')'
            print(f'  {proto:<{col}} {amt:>14,.0f} KAT  {100*frac:>6.1f}%{sub_note}')
        else:
            print(f'  {proto:<{col}} {amt:>14,.0f} KAT  {100*frac:>6.1f}%')

    print('─' * 56)
    print(f'  {"(based on stratified":<{col}} {"sample — approx)":>22}')


# ── State ───────────────────────────────────────────────────────────────────────
def load_state():
    if SUPPLY_STATE_PATH.exists():
        try:
            s = json.loads(SUPPLY_STATE_PATH.read_text())
            # Migrate old formats to current transfersByAddr format
            if 'claimedByAddr' in s or 'maxByUserToken' in s:
                print('  ⚠ Old state format detected — clearing cache, will rescan from genesis')
                return {'scannedBlock': DEPLOY_BLOCK - 1, 'transfersByAddr': {}}
            return s
        except Exception:
            pass
    return {'scannedBlock': DEPLOY_BLOCK - 1, 'transfersByAddr': {}}

def save_state(state):
    SUPPLY_STATE_PATH.write_text(json.dumps(state, separators=(',', ':')))

# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='KAT Supply Circulation Tracker')
    parser.add_argument('--full',      action='store_true', help='Rescan from genesis')
    parser.add_argument('--top',       type=int, default=20, help='Top N wallets to show (default 20)')
    parser.add_argument('--protocols', action='store_true', help='Show Merkl protocol breakdown (makes API calls)')
    parser.add_argument('--json', action='store_true', help='Output supply_data.json for frontend')
    args = parser.parse_args()

    print('KAT Supply Circulation Tracker')
    print('=' * 56)

    total_supply = TOTAL_KAT_SUPPLY
    print(f'  Total supply: {total_supply:>20,.0f} KAT (hardcoded 10B base)\n')

    # Merkl — scan Transfer events FROM distributor (incremental, not cumulative)
    state = (
        {'scannedBlock': DEPLOY_BLOCK - 1, 'transfersByAddr': {}}
        if args.full else load_state()
    )

    latest = get_block_number()

    print('Scanning KAT Transfer events from Merkl distributor…')
    if state['scannedBlock'] >= latest:
        print(f'  Cache current at block {latest:,}')
        transfers_by_addr = state['transfersByAddr']
    else:
        transfers_by_addr = scan_merkl_transfers(
            state['scannedBlock'] + 1, latest, state['transfersByAddr']
        )
        state['scannedBlock'] = latest
        state['transfersByAddr'] = transfers_by_addr
        save_state(state)
        print(f'  State saved (block {latest:,})')

    claimed_by_addr = transfers_by_addr
    merkl_claimed   = sum(claimed_by_addr.values())

    print()

    # Fixed allocations (absolute amounts, already distributed)
    fixed_totals = {name: amt for name, (amt, _desc) in FIXED_ALLOCATIONS.items()}
    fixed_descs  = {name: desc for name, (_amt, desc) in FIXED_ALLOCATIONS.items()}

    # Totals
    total_circulating = merkl_claimed + sum(fixed_totals.values())
    pct_total         = 100 * total_circulating / total_supply

    # ── Supply breakdown ────────────────────────────────────────────────────────
    col = 26
    print('── CIRCULATION BREAKDOWN ─────────────────────────────')
    print(f'  {"Merkl rewards":<{col}} {merkl_claimed:>14,.0f} KAT   {100*merkl_claimed/total_supply:>5.2f}%')
    for name, amount in fixed_totals.items():
        pct = 100 * amount / total_supply
        print(f'  {name:<{col}} {amount:>14,.0f} KAT   {pct:>5.2f}%')
    print('─' * 60)
    print(f'  {"TOTAL circulating":<{col}} {total_circulating:>14,.0f} KAT   {pct_total:>5.2f}%')
    print(f'  {"of total supply":<{col}} {total_supply:>14,.0f} KAT')
    print()

    # ── Protocol breakdown (optional) ───────────────────────────────────────────
    protocol_mix = None
    if 'protocol_mix' in state:
        protocol_mix = state['protocol_mix']

    if args.protocols and claimed_by_addr:
        print('Sampling Merkl API for protocol breakdown (60 addresses)…')
        protocol_mix = scan_protocol_mix(claimed_by_addr)
        state['protocol_mix'] = protocol_mix
        state['protocol_mix_ts'] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        print()
        print_protocol_breakdown(protocol_mix, merkl_claimed)
        print()
    elif protocol_mix and claimed_by_addr:
        # Cached protocol_mix — still print it in terminal mode
        if not args.json:
            print_protocol_breakdown(protocol_mix, merkl_claimed)
            print()

    # ── JSON output ──────────────────────────────────────────────────────────────
    if args.json:
        # Build sources dict: merkl (dynamic) + all fixed allocations
        sources = {
            'merkl': {
                'label': 'Merkl Rewards',
                'amount': merkl_claimed,
                'pct': 100 * merkl_claimed / total_supply,
                'type': 'dynamic',
            },
        }
        for name, amount in fixed_totals.items():
            slug = name.lower().replace(' ', '_').replace('&', 'and').replace('(', '').replace(')', '')
            sources[slug] = {
                'label': name,
                'amount': amount,
                'pct': 100 * amount / total_supply,
                'type': 'fixed',
                'desc': fixed_descs[name],
            }

        output = {
            'meta': {
                'generatedAt': datetime.now(timezone.utc).isoformat(),
                'katanaBlock': latest,
            },
            'totalSupply': TOTAL_KAT_SUPPLY,
            'totalCirculating': total_circulating,
            'circulatingPct': pct_total,
            'sources': sources,
        }
        if protocol_mix:
            output['protocolMix'] = protocol_mix

        out_path = SCRIPT_DIR / 'supply_data.json'
        out_path.write_text(json.dumps(output, indent=2))
        print(f'  Wrote {out_path}')

    # ── Top claimants ───────────────────────────────────────────────────────────
    if not claimed_by_addr:
        print('No Merkl claim data yet.')
        return

    sorted_claimants = sorted(claimed_by_addr.items(), key=lambda x: x[1], reverse=True)
    top_n            = sorted_claimants[:args.top]
    top_total        = sum(amt for _, amt in top_n)
    claimant_count   = len(claimed_by_addr)

    print(f'── TOP {args.top} CLAIMANTS (of {claimant_count:,} total) ──────────────────────')
    print(f'  {"Rank":<5} {"Address":<44} {"KAT Claimed":>14}  {"% Merkl":>7}')
    print(f'  {"─"*4} {"─"*43} {"─"*14}  {"─"*7}')
    for rank, (addr, amt) in enumerate(top_n, 1):
        pct_merkl = 100 * amt / merkl_claimed if merkl_claimed else 0
        print(f'  {rank:<5} {addr:<44} {amt:>14,.0f}  {pct_merkl:>6.2f}%')
    print('─' * 56)
    pct_top = 100 * top_total / merkl_claimed if merkl_claimed else 0
    print(f'  Top {args.top} accounts for {top_total:,.0f} KAT ({pct_top:.1f}% of Merkl)')

if __name__ == '__main__':
    main()
