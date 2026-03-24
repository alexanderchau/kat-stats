"""Tests for rpc.py — pure helpers only (no network calls)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from rpc import from_wei, validate_hex, fmtM


class TestFromWei:
    def test_zero_hex(self):
        assert from_wei('0x0') == 0.0

    def test_empty_string(self):
        assert from_wei('') == 0.0

    def test_none(self):
        assert from_wei(None) == 0.0

    def test_bare_0x(self):
        assert from_wei('0x') == 0.0

    def test_one_ether(self):
        # 1e18 wei → 1.0
        assert from_wei('0xde0b6b3a7640000') == pytest.approx(1.0)

    def test_custom_decimals(self):
        # 1e6 with decimals=6 → 1.0
        assert from_wei(hex(10**6), decimals=6) == pytest.approx(1.0)

    def test_large_value(self):
        # 1_000_000 tokens with 18 decimals
        assert from_wei(hex(1_000_000 * 10**18)) == pytest.approx(1_000_000.0)

    def test_invalid_hex(self):
        assert from_wei('not_hex') == 0.0


class TestValidateHex:
    def test_valid_topic(self):
        topic = '0x' + 'a' * 64  # 66 chars
        assert validate_hex(topic) is True

    def test_short_hex(self):
        assert validate_hex('0xabcd') is False  # only 6 chars

    def test_none_input(self):
        assert validate_hex(None) is False

    def test_empty_string(self):
        assert validate_hex('') is False

    def test_custom_expected_len(self):
        val = '0x' + 'b' * 40  # 42 chars — valid Ethereum address length
        assert validate_hex(val, expected_len=42) is True
        assert validate_hex(val, expected_len=66) is False


class TestFmtM:
    def test_billions(self):
        assert fmtM(2_500_000_000) == '2.50B'

    def test_millions(self):
        assert fmtM(1_234_567) == '1.23M'

    def test_thousands(self):
        assert fmtM(5_678) == '5.7K'

    def test_sub_thousand(self):
        assert fmtM(999) == '999'

    def test_zero(self):
        assert fmtM(0) == '0'

    def test_exact_million(self):
        assert fmtM(1_000_000) == '1.00M'

    def test_exact_billion(self):
        assert fmtM(1_000_000_000) == '1.00B'
