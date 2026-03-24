"""
KAT Farmer — address classification logic.
Single source of truth for classify() and retained_pct().
disposed() is NOT here — it was dead code and has been deleted.
"""

def classify(d, stake_raw=None):
    """Classify address status based on how much of their claimed KAT they still hold.

    Thresholds:
      claimed == 0  → 'inactive'  (never claimed from Merkl)
      retained < 50%  → 'farmer'   (dumped most of their airdrop)
      50% ≤ retained < 95% → 'partial' (mixed behaviour)
      retained ≥ 95%  → 'hodler'   (held essentially all)

    d: address dict with keys: claimed, balance, avkat, address, defiPositions
    stake_raw: optional dict addr → staker data (has vkatAmount, avkatAmount)
    Returns: 'inactive' | 'farmer' | 'partial' | 'hodler'
    """
    if d['claimed'] == 0:
        return 'inactive'
    held = d.get('balance', 0) + d.get('avkat', 0)
    if stake_raw:
        staker = stake_raw.get(d['address'], {})
        held += staker.get('vkatAmount', 0) + staker.get('avkatAmount', 0)
    held += sum(p['amount'] for p in d.get('defiPositions', []) if p.get('held'))
    retained = max(0.0, min(1.0, held / d['claimed']))
    if retained < 0.5:  return 'farmer'
    if retained < 0.95: return 'partial'
    return 'hodler'


def retained_pct(d, stake_raw=None):
    """Return retained fraction as a float in [0.0, 1.0].

    Same held calculation as classify(). Returns 0.0 if claimed == 0.
    """
    if d['claimed'] == 0:
        return 0.0
    held = d.get('balance', 0) + d.get('avkat', 0)
    if stake_raw:
        staker = stake_raw.get(d['address'], {})
        held += staker.get('vkatAmount', 0) + staker.get('avkatAmount', 0)
    held += sum(p['amount'] for p in d.get('defiPositions', []) if p.get('held'))
    return max(0.0, min(1.0, held / d['claimed']))
