"""Tests for the RPC-failure handling behind the holder counts (no network).

A failed read must never count as a zero balance, and a run whose reads don't
add up must not publish counts. See integrity.py for the 2026-10-13 incident.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import rpc
import balances
from integrity import holder_count_problems, merge_snapshot

NFT = '0x106f7d67ea25cb9eff5064cf604ebf6259ff296d'
VE  = '0x4d6fc15ca6258b168225d283262743c623c13ead'
W = rpc._word


def encode_result(entries):
    """ABI-encode an aggregate3 return value: (bool, bytes)[]."""
    tuples = []
    for ok, data in entries:
        raw = data[2:]
        padded = raw + '0' * ((-len(raw)) % 64)
        tuples.append(W(1 if ok else 0) + W(0x40) + W(len(raw) // 2) + padded)
    offsets, pos = [], 32 * len(tuples)
    for t in tuples:
        offsets.append(W(pos))
        pos += len(t) // 2
    return '0x' + W(0x20) + W(len(entries)) + ''.join(offsets) + ''.join(tuples)


class TestAggregate3Codec:
    def test_decode_round_trip(self):
        entries = [(True, '0x' + W(5)), (False, '0x08c379a0' + W(1)), (True, '0x')]
        assert rpc.decode_aggregate3(encode_result(entries)) == entries

    def test_encode_layout(self):
        data = rpc.encode_aggregate3([(NFT, '0x18160ddd')])
        assert data.startswith('0x82ad56cb' + W(0x20) + W(1) + W(0x20))
        assert NFT[2:] in data


class TestDecodeUint:
    def test_unresolved_stays_none(self):
        assert rpc.decode_uint(None) is None
        assert rpc.decode_uint(None, 18) is None

    def test_revert_is_a_real_zero(self):
        assert rpc.decode_uint((False, '0x')) == 0

    def test_value(self):
        assert rpc.decode_uint((True, '0x' + W(3 * 10**18)), 18) == pytest.approx(3.0)


class TestMulticallFailures:
    def test_rpc_failure_is_none_not_zero(self, monkeypatch):
        monkeypatch.setattr(rpc, 'rpc_call', lambda *a, **k: None)
        monkeypatch.setattr(rpc.time, 'sleep', lambda s: None)
        out = rpc.multicall('x', [(NFT, '0x18160ddd')] * 3, attempts=2)
        assert out == [None, None, None]

    def test_failed_chunk_is_retried(self, monkeypatch):
        calls = {'n': 0}
        def flaky(url, method, params, **k):
            calls['n'] += 1
            return None if calls['n'] == 1 else encode_result([(True, '0x' + W(7))])
        monkeypatch.setattr(rpc, 'rpc_call', flaky)
        monkeypatch.setattr(rpc.time, 'sleep', lambda s: None)
        assert rpc.multicall('x', [(NFT, '0x18160ddd')]) == [(True, '0x' + W(7))]

    def test_strict_balance_of(self, monkeypatch):
        monkeypatch.setattr(rpc, 'rpc_call', lambda *a, **k: None)
        assert rpc.balance_of(NFT, VE, 'x', strict=True) is None
        assert rpc.total_assets(NFT, 'x', strict=True) is None


def fake_chain(locks, fail_ids=()):
    """Stub rpc for enumerate_vkat_locks. locks: {token_id: (owner, amount)}."""
    ids = list(locks)

    def rpc_call(url, method, params, **k):
        return '0x' + W(len(ids))   # Lock NFT totalSupply

    def multicall(url, calls, **k):
        out = []
        for target, data in calls:
            sel, arg = data[:10], int(data[10:], 16)
            if sel == '0x4f6ccce7':                      # tokenByIndex
                out.append((True, '0x' + W(ids[arg])))
            elif arg in fail_ids:                         # RPC never answered
                out.append(None)
            elif sel == '0x6352211e':                    # ownerOf
                out.append((True, '0x' + locks[arg][0][2:].zfill(64)))
            else:                                         # locked
                out.append((True, '0x' + W(locks[arg][1] * 10**18) + W(2_000_000_000)))
        return out
    return rpc_call, multicall


class TestEnumerateVkatLocks:
    LOCKS = {1: ('0x' + 'a' * 40, 100), 2: ('0x' + 'b' * 40, 50), 3: ('0x' + 'a' * 40, 25)}

    def test_all_reads_ok(self, monkeypatch):
        rc, mc = fake_chain(self.LOCKS)
        monkeypatch.setattr(rpc, 'rpc_call', rc)
        monkeypatch.setattr(rpc, 'multicall', mc)
        locks, unresolved = balances.enumerate_vkat_locks('x', NFT, VE, 18)
        assert unresolved == 0
        assert locks['0x' + 'a' * 40]['amount'] == pytest.approx(125)
        assert locks['0x' + 'b' * 40]['amount'] == pytest.approx(50)

    def test_failed_read_is_counted_not_dropped(self, monkeypatch):
        rc, mc = fake_chain(self.LOCKS, fail_ids={2})
        monkeypatch.setattr(rpc, 'rpc_call', rc)
        monkeypatch.setattr(rpc, 'multicall', mc)
        locks, unresolved = balances.enumerate_vkat_locks('x', NFT, VE, 18)
        assert unresolved == 1
        assert '0x' + 'b' * 40 not in locks

    def test_unreadable_total_supply(self, monkeypatch):
        monkeypatch.setattr(rpc, 'rpc_call', lambda *a, **k: None)
        locks, unresolved = balances.enumerate_vkat_locks('x', NFT, VE, 18)
        assert locks == {} and unresolved > 0


class TestFreshBalances:
    def test_unresolved_address_is_flagged(self, monkeypatch):
        good, bad = '0x' + '1' * 40, '0x' + '2' * 40

        def multicall(url, calls, **k):
            return [None if bad[2:] in data else (True, '0x' + W(10**18)) for _, data in calls]
        monkeypatch.setattr(rpc, 'multicall', multicall)
        monkeypatch.setattr(rpc, 'is_contract', lambda d, u: False)
        addr_bal, _, _, unresolved = balances.get_fresh_balances(
            [good, bad], {}, set(), 'x', ['0x' + 'k' * 40], '0x' + 'v' * 40,
            set(), set(), set(), None)
        assert unresolved == {bad}
        assert addr_bal[good] == {'kat': 1.0, 'avkat': 1.0}


class TestHolderCountGate:
    def test_ok_within_tolerance(self):
        assert holder_count_problems(99.5e6, 100e6, 0) == []

    def test_incident_numbers_are_blocked(self):
        # 2026-10-13: 34.38M enumerated vs ~378M in the escrow
        problems = holder_count_problems(34.38e6, 378e6, 0)
        assert problems and 'drift' in problems[0]

    def test_unresolved_reads_block_even_if_sums_match(self):
        assert holder_count_problems(100e6, 100e6, 3) == ['3 reads unresolved']

    def test_unreadable_escrow_blocks(self):
        assert holder_count_problems(100e6, None, 0)
        assert holder_count_problems(100e6, 0.0, 0)


class TestMergeSnapshot:
    TOTALS = {'totalStaked': 10.0, 'vkat': 4.0, 'avkat': 6.0}
    BUYERS = {'total': 9}

    def test_good_run_writes_everything(self):
        snaps = {}
        merge_snapshot(snaps, 'd', self.TOTALS, 4252, self.BUYERS, True, True)
        assert snaps['d'] == {**self.TOTALS, 'count': 4252, 'buyers': self.BUYERS}

    def test_gated_run_keeps_earlier_good_count(self):
        snaps = {'d': {'count': 4252, 'buyers': {'total': 8, 'staked': 48}, 'totalStaked': 1.0}}
        merge_snapshot(snaps, 'd', self.TOTALS, 2400, {'total': 9, 'staked': 29}, False, True)
        assert snaps['d']['count'] == 4252
        # log-derived buyer total updates; the balance-derived one is held
        assert snaps['d']['buyers'] == {'total': 9, 'staked': 48}
        assert snaps['d']['totalStaked'] == 10.0

    def test_gated_run_with_no_history_writes_no_count(self):
        snaps = {}
        merge_snapshot(snaps, 'd', self.TOTALS, 2400, self.BUYERS, False, True)
        assert 'count' not in snaps['d']

    def test_unreadable_totals_not_written(self):
        snaps = {'d': {'totalStaked': 5.0}}
        merge_snapshot(snaps, 'd', {'totalStaked': -97e6}, 4252, self.BUYERS, True, False)
        assert snaps['d']['totalStaked'] == 5.0
