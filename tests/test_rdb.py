"""Unit tests for the _rdb C extension.

These exercise dump/load at the lowest layer -- no Store, no fork, no async.
They focus on:
  - byte-for-byte round-tripping of every supported value type
  - the contract on the returned types (RandomDict for ttl, etc.)
  - error surfaces (unsupported types, bad paths, corrupt headers)
  - atomic-write guarantees (a failed dump must not corrupt an existing file)
"""
from collections import deque
from pathlib import Path

import pytest

from miniredis import _rdb
from miniredis.custom_data_structures import RandomDict, SortedSet


@pytest.fixture
def rdb_path(tmp_path):
    """A per-test path inside pytest's tmpdir. Returns a str (what _rdb expects)."""
    return str(tmp_path / "test.rdb")


class TestDumpLoadRoundTrip:
    def test_empty_store(self, rdb_path):
        _rdb.dump({}, RandomDict(), rdb_path)
        data, ttl = _rdb.load(rdb_path)
        assert data == {}
        assert isinstance(ttl, RandomDict)
        assert ttl._data == {}

    def test_returned_ttl_is_a_random_dict(self, rdb_path):
        # Regression: the C extension previously returned a plain dict;
        # the store expects a RandomDict.
        _rdb.dump({b"k": b"v"}, RandomDict(), rdb_path)
        _, ttl = _rdb.load(rdb_path)
        assert isinstance(ttl, RandomDict)

    def test_string_value(self, rdb_path):
        _rdb.dump({b"foo": b"bar"}, RandomDict(), rdb_path)
        data, _ = _rdb.load(rdb_path)
        assert data == {b"foo": b"bar"}

    def test_empty_bytes_value(self, rdb_path):
        _rdb.dump({b"empty": b""}, RandomDict(), rdb_path)
        data, _ = _rdb.load(rdb_path)
        assert data == {b"empty": b""}

    def test_binary_safe_keys_and_values(self, rdb_path):
        # Length-prefix framing must hold for arbitrary bytes including
        # CRLF and NUL inside both the key and the value.
        payload = {b"\x00\xffkey\r\n": b"value\r\n\x00\xff"}
        _rdb.dump(payload, RandomDict(), rdb_path)
        data, _ = _rdb.load(rdb_path)
        assert data == payload

    def test_list_value(self, rdb_path):
        _rdb.dump({b"l": deque([b"a", b"b", b"c"])}, RandomDict(), rdb_path)
        data, _ = _rdb.load(rdb_path)
        assert isinstance(data[b"l"], deque)
        assert list(data[b"l"]) == [b"a", b"b", b"c"]

    def test_empty_list_value(self, rdb_path):
        _rdb.dump({b"l": deque()}, RandomDict(), rdb_path)
        data, _ = _rdb.load(rdb_path)
        assert isinstance(data[b"l"], deque)
        assert list(data[b"l"]) == []

    def test_hash_value(self, rdb_path):
        original = {b"f1": b"v1", b"f2": b"v2", b"f3": b"v3"}
        _rdb.dump({b"h": original}, RandomDict(), rdb_path)
        data, _ = _rdb.load(rdb_path)
        assert isinstance(data[b"h"], dict)
        assert data[b"h"] == original

    def test_sorted_set_value(self, rdb_path):
        ss = SortedSet()
        for score, member in [(1.0, b"a"), (2.5, b"b"), (3.0, b"c")]:
            ss.insert(score, member)
        _rdb.dump({b"z": ss}, RandomDict(), rdb_path)
        data, _ = _rdb.load(rdb_path)
        loaded = data[b"z"]
        assert isinstance(loaded, SortedSet)
        assert loaded.get_range_by_rank(0, -1) == [b"a", b"b", b"c"]
        assert loaded.get_score(b"a") == 1.0
        assert loaded.get_score(b"b") == 2.5
        assert loaded.get_score(b"c") == 3.0

    def test_sorted_set_with_infinity_scores(self, rdb_path):
        ss = SortedSet()
        ss.insert(float("-inf"), b"lo")
        ss.insert(0.0, b"mid")
        ss.insert(float("inf"), b"hi")
        _rdb.dump({b"z": ss}, RandomDict(), rdb_path)
        data, _ = _rdb.load(rdb_path)
        loaded = data[b"z"]
        assert loaded.get_score(b"lo") == float("-inf")
        assert loaded.get_score(b"hi") == float("inf")
        assert loaded.get_range_by_rank(0, -1) == [b"lo", b"mid", b"hi"]

    def test_ttl_roundtrip(self, rdb_path):
        ttl_in = RandomDict()
        ttl_in.set(b"foo", 1700000000.5)
        _rdb.dump({b"foo": b"bar"}, ttl_in, rdb_path)
        _, ttl_out = _rdb.load(rdb_path)
        # RDB stores ms granularity; allow for sub-ms quantization.
        assert ttl_out.get(b"foo") == pytest.approx(1700000000.5, abs=0.001)

    def test_ttl_only_emitted_for_keys_that_have_one(self, rdb_path):
        ttl_in = RandomDict()
        ttl_in.set(b"with_ttl", 100.0)
        _rdb.dump({b"with_ttl": b"v1", b"no_ttl": b"v2"}, ttl_in, rdb_path)
        _, ttl_out = _rdb.load(rdb_path)
        assert ttl_out.get(b"with_ttl") == pytest.approx(100.0, abs=0.001)
        assert ttl_out.get(b"no_ttl") is None

    def test_all_types_in_one_file(self, rdb_path):
        ss = SortedSet()
        ss.insert(1.0, b"m")
        original = {
            b"s": b"hello",
            b"l": deque([b"x", b"y"]),
            b"h": {b"f": b"v"},
            b"z": ss,
        }
        ttl_in = RandomDict()
        ttl_in.set(b"s", 12345.0)

        _rdb.dump(original, ttl_in, rdb_path)
        data, ttl_out = _rdb.load(rdb_path)

        assert data[b"s"] == b"hello"
        assert list(data[b"l"]) == [b"x", b"y"]
        assert data[b"h"] == {b"f": b"v"}
        assert data[b"z"].get_range_by_rank(0, -1) == [b"m"]
        assert ttl_out.get(b"s") == pytest.approx(12345.0, abs=0.001)


class TestDumpErrors:
    def test_unsupported_value_type_raises(self, rdb_path):
        with pytest.raises(TypeError, match="unsupported value type"):
            _rdb.dump({b"k": 42}, RandomDict(), rdb_path)

    def test_path_argument_must_be_str(self, tmp_path):
        # The C extension uses PyArg_ParseTuple("s", ...), which rejects Path.
        with pytest.raises(TypeError):
            _rdb.dump({}, RandomDict(), tmp_path / "x.rdb")

    def test_atomic_write_preserves_existing_file_on_failure(self, tmp_path):
        # An existing dump.rdb must survive a failed dump call -- the C code
        # writes to <path>.tmp and only rename()s on success, then unlinks
        # the tmp on failure.
        path = str(tmp_path / "test.rdb")
        _rdb.dump({b"orig": b"value"}, RandomDict(), path)
        original_bytes = Path(path).read_bytes()

        with pytest.raises(TypeError):
            _rdb.dump({b"k": 42}, RandomDict(), path)

        assert Path(path).read_bytes() == original_bytes
        assert not (tmp_path / "test.rdb.tmp").exists()


class TestLoadErrors:
    def test_missing_file_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            _rdb.load(str(tmp_path / "nonexistent.rdb"))

    def test_corrupt_magic_raises_valueerror(self, tmp_path):
        bad = tmp_path / "bad.rdb"
        bad.write_bytes(b"NOTREDIS0011" + b"\x00" * 16)
        with pytest.raises(ValueError, match="missing REDIS magic"):
            _rdb.load(str(bad))

    def test_empty_file_raises(self, tmp_path):
        empty = tmp_path / "empty.rdb"
        empty.write_bytes(b"")
        with pytest.raises(ValueError, match="unexpected EOF"):
            _rdb.load(str(empty))

    def test_truncated_after_header_raises(self, tmp_path):
        # Magic + version present, but no body / EOF marker.
        bad = tmp_path / "trunc.rdb"
        bad.write_bytes(b"REDIS0011")
        with pytest.raises(ValueError, match="unexpected EOF"):
            _rdb.load(str(bad))

    def test_path_argument_must_be_str(self, tmp_path):
        fpath = tmp_path / "test.rdb"
        _rdb.dump({}, RandomDict(), str(fpath))
        with pytest.raises(TypeError):
            _rdb.load(fpath)  # Path object, not str
