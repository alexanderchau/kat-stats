#!/usr/bin/env python3
"""
Recompute snapshot holder counts for past days from archive state.

From ~2026-09-29 the public RPC's 429s made reads fail, the old code counted
each failure as a zero balance, and snapshots.json recorded counts as low as
2,384 when the true figure was ~4,250. This reads the chain as of 23:59:59 UTC
on each given day and rebuilds `count` (unique vKAT ∪ avKAT holders, the same
number indexer.py writes). Days whose reads don't all resolve, or whose lock
sum doesn't match the escrow, are reported and left unchanged.

The avKAT candidate set is every address in state.json that ever held avKAT,
a superset of the holders on any past day.

Usage:
    python3 backfill_holder_counts.py STATE_JSON DAY [DAY ...]          # dry run
    python3 backfill_holder_counts.py STATE_JSON DAY [DAY ...] --write
"""
import json, sys
from datetime import datetime, timezone
from pathlib import Path

import rpc
import fileio
from config import (KATANA_RPC, LOCK_NFT, VOTING_ESCROW, KAT_DECIMALS,
                    KAT_BASE_ADDR, AVKAT_ADDR, EXCLUDE_STAKERS)
from integrity import holder_count_problems

SNAP_PATH = Path(__file__).parent / 'snapshots.json'


def block_at(ts):
    """Last block with timestamp <= ts (binary search)."""
    def stamp(n):
        b = rpc.rpc_call(KATANA_RPC, 'eth_getBlockByNumber', [hex(n), False], retries=8)
        return int(b['timestamp'], 16)
    lo, hi = 1, rpc.get_block_number(KATANA_RPC)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if stamp(mid) <= ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


def counts_at(block, avkat_candidates):
    tag = hex(block)
    pad = rpc._word
    r = rpc.rpc_call(KATANA_RPC, 'eth_call', [{'to': LOCK_NFT, 'data': '0x18160ddd'}, tag], retries=8)
    total = int(r, 16)
    ids = rpc.multicall(KATANA_RPC, [(LOCK_NFT, '0x4f6ccce7' + pad(i)) for i in range(total)], block=tag)
    unresolved = ids.count(None)
    token_ids = [rpc.decode_uint(x) for x in ids if x is not None and x[0]]
    calls = []
    for tid in token_ids:
        calls += [(LOCK_NFT, '0x6352211e' + pad(tid)), (VOTING_ESCROW, '0xb45a3c0e' + pad(tid))]
    res = rpc.multicall(KATANA_RPC, calls, block=tag)
    owners, lock_sum = set(), 0.0
    for k in range(len(token_ids)):
        o, l = res[2 * k], res[2 * k + 1]
        if o is None or l is None:
            unresolved += 1
            continue
        if not o[0] or not l[0] or len(l[1]) < 66:
            continue
        amount = int(l[1][2:66], 16) / 10 ** KAT_DECIMALS
        lock_sum += amount
        if amount > 0:
            owners.add('0x' + o[1][26:66].lower())

    cands = sorted(avkat_candidates)
    bals = rpc.multicall(KATANA_RPC, [rpc.balance_of_call(AVKAT_ADDR, a) for a in cands], block=tag)
    vals = [rpc.decode_uint(x) for x in bals]
    unresolved += vals.count(None)
    av = {a for a, v in zip(cands, vals) if v}

    ve = rpc.decode_uint(rpc.multicall(KATANA_RPC, [rpc.balance_of_call(KAT_BASE_ADDR, VOTING_ESCROW)],
                                       block=tag)[0], 18)
    vk = owners - EXCLUDE_STAKERS
    av -= EXCLUDE_STAKERS
    problems = holder_count_problems(lock_sum, ve, unresolved)
    return {'vkat': len(vk), 'avkat': len(av), 'count': len(vk | av)}, problems


def main():
    args = [a for a in sys.argv[1:] if a != '--write']
    write = '--write' in sys.argv
    state = json.loads(Path(args[0]).read_text())
    cands = set(state.get('avkatHolders', [])) | {
        a for a, v in state.get('stakeRaw', {}).items() if v.get('stakedAvKAT')}
    snaps = json.loads(SNAP_PATH.read_text())
    for day in args[1:]:
        ts = int(datetime.strptime(day + ' 23:59:59', '%Y-%m-%d %H:%M:%S')
                 .replace(tzinfo=timezone.utc).timestamp())
        block = block_at(ts)
        got, problems = counts_at(block, cands)
        old = snaps.get(day, {}).get('count')
        if problems:
            print(f'{day} block {block:,}: SKIPPED ({"; ".join(problems)})', flush=True)
            continue
        print(f'{day} block {block:,}: {got["vkat"]:,} vKAT / {got["avkat"]:,} avKAT / '
              f'{got["count"]:,} unique (snapshot had {old})', flush=True)
        if write and day in snaps:
            snaps[day]['count'] = got['count']
    if write:
        fileio.save_json(SNAP_PATH, snaps, compact=False)
        print(f'wrote {SNAP_PATH.name}')


if __name__ == '__main__':
    main()
