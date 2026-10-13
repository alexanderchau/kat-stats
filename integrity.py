"""
KAT Farmer — integrity gate for holder counts.

Holder counts come from thousands of RPC reads. When the public RPC rate-limits
(HTTP 429), reads fail, and a failed read that turns into 0 silently drops the
holder. On 2026-10-13 that took the Stakers card from ~4,250 to 2,400 while the
KAT totals (single contract reads) still looked right. These checks decide
whether a run's counts are trustworthy enough to publish.
"""

LOCK_SUM_TOLERANCE = 0.01   # enumerated locks must match the escrow's KAT within 1%


def holder_count_problems(enumerated_lock_sum, ve_kat_total, unresolved_reads,
                          tolerance=LOCK_SUM_TOLERANCE):
    """Return a list of reasons the holder counts can't be trusted ([] = OK).

    enumerated_lock_sum: KAT summed over every Lock NFT we read, including the
        locks the escrow holds for the exit queue and the avKAT strategy's lock.
    ve_kat_total: the voting escrow's KAT balance (None if that read failed).
        Every locked KAT sits there, so the two must agree.
    unresolved_reads: lock or balance reads that never succeeded.
    """
    problems = []
    if unresolved_reads:
        problems.append(f'{unresolved_reads:,} reads unresolved')
    if ve_kat_total is None:
        problems.append('voting-escrow KAT balance unreadable')
    elif ve_kat_total <= 0:
        problems.append('voting-escrow KAT balance is 0')
    else:
        drift = abs(enumerated_lock_sum - ve_kat_total) / ve_kat_total
        if drift > tolerance:
            problems.append(f'enumerated locks {enumerated_lock_sum:,.0f} KAT vs escrow '
                            f'{ve_kat_total:,.0f} KAT ({drift:.1%} drift > {tolerance:.0%})')
    return problems


# Buyer metrics that come from balances/locks (the rest are log-derived).
BUYER_BALANCE_KEYS = ('staked', 'vkat', 'avkat')


def merge_snapshot(snapshots, day, totals, count, buyers, counts_ok, totals_ok):
    """Write today's snapshot without letting a bad run overwrite good data.

    Runs are hourly and each overwrites the day's entry, so a gated run keeps
    whatever an earlier good run that day wrote. With no earlier value the key
    stays absent and the history chart skips the day. Returns the new entry.
    """
    entry = dict(snapshots.get(day, {}))
    if totals_ok:
        entry.update(totals)
    new_buyers = dict(entry.get('buyers', {}))
    for k, v in buyers.items():
        if counts_ok or k not in BUYER_BALANCE_KEYS:
            new_buyers[k] = v
    entry['buyers'] = new_buyers
    if counts_ok:
        entry['count'] = count
    snapshots[day] = entry
    return entry
