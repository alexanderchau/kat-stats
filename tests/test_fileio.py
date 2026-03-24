"""Tests for fileio.py — atomic I/O helpers."""
import sys, json, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fileio import atomic_write, load_json, save_json, load_state


class TestAtomicWrite:
    def test_creates_file(self, tmp_path):
        p = tmp_path / 'out.json'
        atomic_write(p, '{"key": 1}')
        assert p.exists()
        assert p.read_text() == '{"key": 1}'

    def test_no_tmp_file_left(self, tmp_path):
        p = tmp_path / 'out.json'
        atomic_write(p, 'hello')
        tmp = p.with_suffix('.tmp')
        assert not tmp.exists()

    def test_overwrites_existing(self, tmp_path):
        p = tmp_path / 'out.txt'
        p.write_text('old')
        atomic_write(p, 'new')
        assert p.read_text() == 'new'


class TestLoadJson:
    def test_loads_valid_json(self, tmp_path):
        p = tmp_path / 'data.json'
        p.write_text('{"a": 1}')
        assert load_json(p) == {'a': 1}

    def test_missing_file_returns_default(self, tmp_path):
        p = tmp_path / 'nonexistent.json'
        assert load_json(p) is None
        assert load_json(p, {}) == {}
        assert load_json(p, []) == []

    def test_corrupt_json_returns_default(self, tmp_path):
        p = tmp_path / 'bad.json'
        p.write_text('{not valid json')
        assert load_json(p, 'fallback') == 'fallback'


class TestSaveJson:
    def test_compact_mode(self, tmp_path):
        p = tmp_path / 'out.json'
        save_json(p, {'k': 1}, compact=True)
        assert p.read_text() == '{"k":1}'

    def test_pretty_mode(self, tmp_path):
        p = tmp_path / 'out.json'
        save_json(p, {'k': 1}, compact=False)
        loaded = json.loads(p.read_text())
        assert loaded == {'k': 1}
        assert '\n' in p.read_text()  # indent=2 adds newlines

    def test_roundtrip(self, tmp_path):
        p = tmp_path / 'rt.json'
        data = {'addresses': ['0xabc'], 'count': 42}
        save_json(p, data, compact=True)
        assert load_json(p) == data


class TestLoadState:
    def test_missing_file_returns_defaults(self, tmp_path):
        p = tmp_path / 'state.json'
        defaults = {'katanaBlock': 0, 'buyRaw': {}}
        result = load_state(p, defaults)
        assert result == defaults

    def test_merges_with_defaults(self, tmp_path):
        p = tmp_path / 'state.json'
        p.write_text('{"katanaBlock": 999}')
        defaults = {'katanaBlock': 0, 'newKey': 'default_val'}
        result = load_state(p, defaults)
        assert result['katanaBlock'] == 999  # loaded value wins
        assert result['newKey'] == 'default_val'  # missing key gets default

    def test_corrupt_file_returns_defaults(self, tmp_path):
        p = tmp_path / 'state.json'
        p.write_text('{{invalid')
        defaults = {'x': 1}
        result = load_state(p, defaults)
        assert result == {'x': 1}

    def test_loaded_values_win_over_defaults(self, tmp_path):
        p = tmp_path / 'state.json'
        saved = {'katanaBlock': 12345, 'ethBlock': 999, 'claimedByAddr': {'0xabc': 100.0}}
        p.write_text(json.dumps(saved))
        defaults = {'katanaBlock': 0, 'ethBlock': 0, 'claimedByAddr': {}, 'buyRaw': {}}
        result = load_state(p, defaults)
        assert result['katanaBlock'] == 12345
        assert result['claimedByAddr'] == {'0xabc': 100.0}
        assert result['buyRaw'] == {}  # from defaults (missing in file)
