#!/usr/bin/env python3
"""
One-off: rebuild daily KAT-in-exit-queue history from on-chain events.

A vKAT lock enters the exit queue when beginWithdrawal() transfers its Lock NFT
to the voting escrow, and leaves when withdraw() burns it (escrow → 0x0) or the
exit is cancelled (escrow → holder). The lock amount can't change while queued,
so each entry is valued with an archive locked(tokenId) call at its entry block.

Writes exit_queue_history.json ({YYYY-MM-DD: KAT at end of UTC day}), keeping
any dates already present (the indexer appends today's live value each run).

    python3 backfill_exit_queue.py
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import fileio
import rpc
from config import KATANA_RPC, LOCK_NFT, VOTING_ESCROW, KAT_DECIMALS, TRANSFER_TOPIC, LOG_CHUNK

LOCK_NFT_DEPLOY = 23_368_000          # first block with Lock NFT code (binary-searched)
HIST_PATH = Path(__file__).resolve().parent / 'exit_queue_history.json'


def _topic(addr):
    return '0x' + addr.lower()[2:].zfill(64)


def _retry(fn, *args):
    for i in range(6):
        r = fn(*args)
        if r is not None:
            return r
        time.sleep(1 + i)
    raise RuntimeError(f'{fn.__name__}{args} failed')


def _logs(frm, to, topics):
    # rpc_call (not eth_get_logs) so a failed chunk is None, not a silent [].
    return rpc.rpc_call(KATANA_RPC, 'eth_getLogs', [{
        'address': LOCK_NFT, 'fromBlock': hex(frm), 'toBlock': hex(to), 'topics': topics,
    }])


def _scan(frm, to):
    ve = _topic(VOTING_ESCROW)
    ins  = _retry(_logs, frm, to, [TRANSFER_TOPIC, None, ve])
    outs = _retry(_logs, frm, to, [TRANSFER_TOPIC, ve])
    return ins + outs


def _locked_at(token_id, block):
    r = rpc.rpc_call(KATANA_RPC, 'eth_call', [
        {'to': VOTING_ESCROW, 'data': '0xb45a3c0e' + hex(token_id)[2:].zfill(64)}, hex(block)])
    return int(r[2:66], 16) / 10 ** KAT_DECIMALS if r and len(r) >= 66 else None


def _block_ts(block):
    r = rpc.rpc_call(KATANA_RPC, 'eth_getBlockByNumber', [hex(block), False])
    return int(r['timestamp'], 16) if r else None


def main():
    latest = rpc.get_block_number(KATANA_RPC)
    chunks = [(b, min(b + LOG_CHUNK - 1, latest)) for b in range(LOCK_NFT_DEPLOY, latest + 1, LOG_CHUNK)]
    print(f'Scanning {len(chunks)} chunks of Lock NFT transfers to/from the escrow…')
    with ThreadPoolExecutor(max_workers=6) as ex:
        logs = [l for part in ex.map(lambda c: _scan(*c), chunks) for l in part]

    ve = VOTING_ESCROW.lower()
    events = []   # (block, logIndex, tokenId, +1 in / -1 out)
    for l in logs:
        frm = '0x' + l['topics'][1][26:]
        to  = '0x' + l['topics'][2][26:]
        if frm == to:
            continue
        tid = int(l['topics'][3], 16)
        events.append((int(l['blockNumber'], 16), int(l['logIndex'], 16), tid, 1 if to == ve else -1))
    events.sort()
    print(f'  {sum(e[3] == 1 for e in events):,} queue entries, {sum(e[3] == -1 for e in events):,} exits')

    entries = [(b, t) for b, _, t, d in events if d == 1]
    with ThreadPoolExecutor(max_workers=6) as ex:
        amounts = list(ex.map(lambda e: _retry(_locked_at, e[1], e[0]), entries))
    blocks = sorted({e[0] for e in events})
    with ThreadPoolExecutor(max_workers=6) as ex:
        ts = dict(zip(blocks, ex.map(lambda b: _retry(_block_ts, b), blocks)))

    amount_by_entry = dict(zip(entries, amounts))
    queued, total = {}, 0.0
    by_day = {}
    for b, _, tid, d in events:
        if d == 1:
            queued[tid] = amount_by_entry[(b, tid)]
            total += queued[tid]
        else:
            total -= queued.pop(tid, 0.0)
        day = datetime.fromtimestamp(ts[b], timezone.utc).strftime('%Y-%m-%d')
        by_day[day] = total

    # Carry the running total across days with no events.
    hist, running = {}, 0.0
    if by_day:
        d = datetime.strptime(min(by_day), '%Y-%m-%d').replace(tzinfo=timezone.utc)
        end = datetime.now(timezone.utc)
        while d <= end:
            key = d.strftime('%Y-%m-%d')
            running = by_day.get(key, running)
            hist[key] = round(running, 2)
            d = d.fromtimestamp(d.timestamp() + 86400, timezone.utc)

    print(f'  Reconstructed now: {rpc.fmtM(total)} KAT in {len(queued):,} positions')
    existing = json.loads(HIST_PATH.read_text()) if HIST_PATH.exists() else {}
    hist.update(existing)          # live indexer values win over reconstruction
    fileio.save_json(HIST_PATH, dict(sorted(hist.items())), compact=False)
    print(f'✓ Wrote {HIST_PATH.name} ({len(hist)} days, {min(hist)} → {max(hist)})')


if __name__ == '__main__':
    main()
