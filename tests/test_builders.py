"""Tests for build_stakers_output holder inclusion / display floor."""
from builders import build_stakers_output
from config import EXCLUDE_STAKERS

EXCLUDED = next(iter(EXCLUDE_STAKERS))  # a real excluded protocol contract


def _balances(**kw):
    return {a: {'kat': 0.0, 'avkat': v} for a, v in kw.items()}


def test_all_positive_holders_included_by_default():
    # A: 50 avKAT, C: 200 vKAT, E: dust avKAT  -> all are holders
    # B: zero balance -> excluded;  EXCLUDED: in EXCLUDE_STAKERS -> excluded
    stake_raw = {
        '0xa': {'stakedVKAT': False, 'stakedAvKAT': True},
        '0xb': {'stakedVKAT': False, 'stakedAvKAT': False},
        '0xe': {'stakedVKAT': False, 'stakedAvKAT': True},
        EXCLUDED: {'stakedVKAT': False, 'stakedAvKAT': True},
    }
    addr_balances = _balances(**{'0xa': 50.0, '0xb': 0.0, '0xe': 1e-6, EXCLUDED: 500.0})
    vkat_locks = {'0xc': {'amount': 200.0, 'endTime': 0}}
    out = build_stakers_output(stake_raw, addr_balances, vkat_locks, staker_min_kat=0)
    addrs = {s['address'] for s in out}
    assert addrs == {'0xa', '0xc', '0xe'}      # B (zero) and EXCLUDED dropped
    assert all(s['totalStaked'] > 0 for s in out)


def test_display_floor_still_works_when_set():
    stake_raw = {
        '0xa': {'stakedVKAT': False, 'stakedAvKAT': True},
        '0xe': {'stakedVKAT': False, 'stakedAvKAT': True},
    }
    addr_balances = _balances(**{'0xa': 50.0, '0xe': 1e-6})
    vkat_locks = {'0xc': {'amount': 200.0, 'endTime': 0}}
    out = build_stakers_output(stake_raw, addr_balances, vkat_locks, staker_min_kat=100)
    assert {s['address'] for s in out} == {'0xc'}   # only the >=100 position


def test_per_token_flags():
    stake_raw = {'0xa': {'stakedVKAT': False, 'stakedAvKAT': True}}
    addr_balances = _balances(**{'0xa': 10.0})
    vkat_locks = {'0xc': {'amount': 5.0, 'endTime': 0}}
    out = build_stakers_output(stake_raw, addr_balances, vkat_locks, staker_min_kat=0)
    by = {s['address']: s for s in out}
    assert by['0xa']['stakedAvKAT'] and not by['0xa']['stakedVKAT']
    assert by['0xc']['stakedVKAT'] and not by['0xc']['stakedAvKAT']
