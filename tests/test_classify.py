"""Tests for classify.py — status classification logic."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from classify import classify, retained_pct


ADDR = '0xabc'


def make_d(claimed, balance=0, avkat=0, defi=None):
    return {
        'address': ADDR,
        'claimed': claimed,
        'balance': balance,
        'avkat': avkat,
        'defiPositions': defi or [],
    }


class TestClassify:
    def test_inactive_never_claimed(self):
        assert classify(make_d(0)) == 'inactive'

    def test_hodler_all_held(self):
        d = make_d(1000, balance=1000)
        assert classify(d) == 'hodler'

    def test_hodler_95pct_held(self):
        d = make_d(1000, balance=950)
        assert classify(d) == 'hodler'

    def test_partial_just_below_hodler(self):
        d = make_d(1000, balance=940)
        assert classify(d) == 'partial'

    def test_partial_50pct_held(self):
        d = make_d(1000, balance=500)
        assert classify(d) == 'partial'

    def test_farmer_below_50pct(self):
        d = make_d(1000, balance=499)
        assert classify(d) == 'farmer'

    def test_farmer_nothing_held(self):
        d = make_d(1000, balance=0)
        assert classify(d) == 'farmer'

    def test_avkat_counts_as_held(self):
        # 800 balance + 200 avkat = 1000 = 100% of claimed
        d = make_d(1000, balance=800, avkat=200)
        assert classify(d) == 'hodler'

    def test_defi_position_held_counts(self):
        defi = [{'amount': 500, 'held': True}]
        d = make_d(1000, balance=500, defi=defi)
        assert classify(d) == 'hodler'

    def test_defi_position_not_held_doesnt_count(self):
        defi = [{'amount': 500, 'held': False}]
        d = make_d(1000, balance=0, defi=defi)
        assert classify(d) == 'farmer'

    def test_stake_raw_vkat_counts(self):
        stake_raw = {ADDR: {'vkatAmount': 950, 'avkatAmount': 0}}
        d = make_d(1000, balance=0)
        assert classify(d, stake_raw=stake_raw) == 'hodler'

    def test_stake_raw_avkat_counts(self):
        stake_raw = {ADDR: {'vkatAmount': 0, 'avkatAmount': 500}}
        d = make_d(1000, balance=500)
        assert classify(d, stake_raw=stake_raw) == 'hodler'

    def test_stake_raw_missing_address(self):
        stake_raw = {'0xother': {'vkatAmount': 1000, 'avkatAmount': 0}}
        d = make_d(1000, balance=0)
        assert classify(d, stake_raw=stake_raw) == 'farmer'

    def test_over_100pct_clamped_to_hodler(self):
        # Claimed 1000 but holds 2000 (bought more) → still hodler
        d = make_d(1000, balance=2000)
        assert classify(d) == 'hodler'


class TestRetainedPct:
    def test_zero_claimed(self):
        assert retained_pct(make_d(0)) == 0.0

    def test_full_retention(self):
        assert retained_pct(make_d(1000, balance=1000)) == 1.0

    def test_half_retention(self):
        assert retained_pct(make_d(1000, balance=500)) == 0.5

    def test_clamped_above_1(self):
        assert retained_pct(make_d(1000, balance=2000)) == 1.0

    def test_clamped_below_0(self):
        # Shouldn't be possible but guard: negative balance
        d = make_d(1000)
        d['balance'] = -100
        assert retained_pct(d) == 0.0
