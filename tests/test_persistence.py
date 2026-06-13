"""Store-level persistence tests: snapshot via fork + load via get_store().

These actually fork the test process for each ``await store.snapshot()`` call:
the child writes ``dump.rdb``, the parent reaps via the async cleaner. We
target a per-test path under ``tmp_path`` so nothing escapes the test sandbox,
and use ``monkeypatch`` to point ``config.snapshot_path`` at it.
"""
import time

import miniredis.store as store_module
import pytest

from miniredis import _rdb
from miniredis.config import config
from miniredis.custom_data_structures import RandomDict
from miniredis.store import Store, get_store


@pytest.fixture
def snapshot_path(tmp_path, monkeypatch):
    """Per-test snapshot file under pytest's tmpdir."""
    path = tmp_path / "dump.rdb"
    monkeypatch.setattr(config, "snapshot_path", str(path))
    return path


# -- get_store() ---------------------------------------------------------


class TestGetStore:
    def test_returns_empty_store_when_no_snapshot_exists(self, snapshot_path):
        assert not snapshot_path.exists()
        store = get_store()
        assert isinstance(store, Store)
        assert store._data == {}
        assert isinstance(store._ttl, RandomDict)

    def test_loads_data_when_snapshot_exists(self, snapshot_path):
        # Pre-write a snapshot directly via the C extension. TTL is an
        # absolute unix-seconds timestamp; use a value in the future so the
        # key isn't immediately evicted as expired on read.
        future_expiry = time.time() + 100
        ttl = RandomDict()
        ttl.set(b"foo", future_expiry)
        _rdb.dump({b"foo": b"bar", b"baz": b"qux"}, ttl, str(snapshot_path))

        store = get_store()
        assert store.get(b"foo") == b"bar"
        assert store.get(b"baz") == b"qux"
        assert store._ttl.get(b"foo") == pytest.approx(future_expiry, abs=0.001)
        # And the TTL container is still the right type after load.
        assert isinstance(store._ttl, RandomDict)

    def test_corrupt_snapshot_returns_empty_store_and_prints_to_stderr(
        self, snapshot_path, capsys
    ):
        snapshot_path.write_bytes(b"NOTREDIS_corrupt_blob")
        store = get_store()
        assert store._data == {}
        assert "snapshot load failed" in capsys.readouterr().err


# -- snapshot() round-trips per value type ------------------------------


class TestSnapshotRoundTrip:
    async def test_string(self, snapshot_path):
        store = Store()
        store.set(b"foo", b"bar")
        store.set(b"baz", b"qux")
        await store.snapshot()

        reloaded = get_store()
        assert reloaded.get(b"foo") == b"bar"
        assert reloaded.get(b"baz") == b"qux"

    async def test_list(self, snapshot_path):
        store = Store()
        store.rpush(b"l", [b"a", b"b", b"c"])
        await store.snapshot()

        reloaded = get_store()
        assert reloaded.lrange(b"l", 0, -1) == [b"a", b"b", b"c"]
        assert reloaded.llen(b"l") == 3

    async def test_hash(self, snapshot_path):
        store = Store()
        store.hset(b"h", [b"f1", b"v1", b"f2", b"v2"])
        await store.snapshot()

        reloaded = get_store()
        assert reloaded.hget(b"h", b"f1") == b"v1"
        assert reloaded.hget(b"h", b"f2") == b"v2"

    async def test_sorted_set(self, snapshot_path):
        store = Store()
        store.zadd(b"z", [1.0, b"a", 2.0, b"b", 3.0, b"c"])
        await store.snapshot()

        reloaded = get_store()
        assert reloaded.zrange(b"z", 0, -1) == [b"a", b"b", b"c"]
        assert reloaded.zscore(b"z", b"b") == 2.0

    async def test_ttl_is_preserved(self, snapshot_path):
        store = Store()
        store.set(b"will_expire", b"v", ttl=100)
        await store.snapshot()

        reloaded = get_store()
        # Persisted as absolute ms, restored as absolute seconds. Same key
        # has a TTL after reload, and it's within the original window.
        assert 0 < reloaded.ttl(b"will_expire") <= 100

    async def test_all_types_in_one_snapshot(self, snapshot_path):
        store = Store()
        store.set(b"s", b"hello")
        store.rpush(b"l", [b"x", b"y"])
        store.hset(b"h", [b"f", b"v"])
        store.zadd(b"z", [1.0, b"member"])
        await store.snapshot()

        reloaded = get_store()
        assert reloaded.get(b"s") == b"hello"
        assert reloaded.lrange(b"l", 0, -1) == [b"x", b"y"]
        assert reloaded.hget(b"h", b"f") == b"v"
        assert reloaded.zrange(b"z", 0, -1) == [b"member"]

    async def test_empty_store(self, snapshot_path):
        # Snapshotting an empty store must produce a loadable file.
        store = Store()
        await store.snapshot()
        assert snapshot_path.exists()

        reloaded = get_store()
        assert reloaded._data == {}


# -- snapshot lifecycle ------------------------------------------------


class TestSnapshotLifecycle:
    async def test_snapshot_creates_the_file(self, snapshot_path):
        store = Store()
        store.set(b"k", b"v")
        assert not snapshot_path.exists()
        await store.snapshot()
        assert snapshot_path.exists()

    async def test_snapshot_pid_is_cleared_after_completion(self, snapshot_path):
        # Regression: in earlier versions snapshot_pid was never cleared,
        # which left the "save already in progress" lock latched forever.
        store = Store()
        store.set(b"k", b"v")
        await store.snapshot()
        assert store_module.snapshot_pid is None

    async def test_multiple_snapshots_in_sequence(self, snapshot_path):
        # If the lifecycle is correct, snapshot N+1 must work and reflect
        # mutations made between the two calls. This is the test that the
        # never-clear-the-lock bug would fail.
        store = Store()
        store.set(b"k1", b"v1")
        await store.snapshot()

        store.set(b"k2", b"v2")
        await store.snapshot()

        reloaded = get_store()
        assert reloaded.get(b"k1") == b"v1"
        assert reloaded.get(b"k2") == b"v2"

    async def test_snapshot_overwrites_previous_file(self, snapshot_path):
        # The atomic .tmp + rename pattern means each snapshot fully
        # replaces the previous one; deleted keys are gone from disk.
        store = Store()
        store.set(b"keep", b"yes")
        store.set(b"drop", b"yes")
        await store.snapshot()

        store.delete(b"drop")
        await store.snapshot()

        reloaded = get_store()
        assert reloaded.get(b"keep") == b"yes"
        assert reloaded.get(b"drop") is None

    async def test_unfreeze_runs_after_snapshot(self, snapshot_path):
        # gc.freeze() pre-fork + gc.unfreeze() in the cleaner's finally.
        # After a snapshot completes, the GC's permanent generation should
        # be empty again so normal collection resumes.
        import gc

        store = Store()
        store.set(b"k", b"v")
        await store.snapshot()
        # The permanent generation -- gc.get_freeze_count() returns its size.
        assert gc.get_freeze_count() == 0
