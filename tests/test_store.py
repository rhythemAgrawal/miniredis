"""Unit tests for the Store data layer.

Each test gets a fresh ``Store`` instance (the module-level singleton is only
used by the command handlers, exercised in test_commands.py). Expiry is tested
deterministically by setting a TTL in the past via ``expire(key, -1)`` rather
than sleeping, so the suite stays fast and non-flaky.
"""
import pytest

from miniredis.store import Store


@pytest.fixture
def store() -> Store:
    return Store()


class TestStrings:
    def test_set_and_get(self, store):
        store.set(b"k", b"v")
        assert store.get(b"k") == b"v"

    def test_get_missing_returns_none(self, store):
        assert store.get(b"missing") is None

    def test_set_overwrites(self, store):
        store.set(b"k", b"a")
        store.set(b"k", b"b")
        assert store.get(b"k") == b"b"

    def test_set_without_ttl_clears_existing_ttl(self, store):
        store.set(b"k", b"v", ttl=100)
        store.set(b"k", b"v2")          # plain SET must drop the prior TTL
        assert store.ttl(b"k") == -1


class TestDeleteExists:
    def test_delete_counts_only_removed_keys(self, store):
        store.set(b"a", b"1")
        store.set(b"b", b"2")
        assert store.delete(b"a", b"b", b"missing") == 2
        assert store.get(b"a") is None

    def test_exists_counts_present_keys(self, store):
        store.set(b"a", b"1")
        assert store.exists(b"a", b"missing") == 1

    def test_exists_counts_repeated_keys(self, store):
        store.set(b"a", b"1")
        assert store.exists(b"a", b"a") == 2  # real Redis counts repeats


class TestCounters:
    def test_incr_from_missing_starts_at_one(self, store):
        assert store.incr(b"c") == 1

    def test_incr_then_decr(self, store):
        store.incr(b"c")
        store.incr(b"c")
        assert store.decr(b"c") == 1

    def test_incr_by_and_decr_by(self, store):
        assert store.incr_by(b"c", 10) == 10
        assert store.decr_by(b"c", 4) == 6

    def test_incr_on_non_integer_value_raises(self, store):
        store.set(b"c", b"notanumber")
        with pytest.raises(ValueError):
            store.incr(b"c")

    def test_incr_preserves_existing_ttl(self, store):
        # Regression: INCR must not wipe a key's TTL.
        store.set(b"c", b"5", ttl=100)
        assert store.incr(b"c") == 6
        assert 90 <= store.ttl(b"c") <= 100


class TestAppendStrlen:
    def test_append_to_missing_key(self, store):
        assert store.append(b"k", b"abc") == 3
        assert store.get(b"k") == b"abc"

    def test_append_extends_existing_value(self, store):
        store.set(b"k", b"ab")
        assert store.append(b"k", b"cd") == 4
        assert store.get(b"k") == b"abcd"

    def test_append_preserves_existing_ttl(self, store):
        store.set(b"k", b"ab", ttl=100)
        store.append(b"k", b"cd")
        assert 90 <= store.ttl(b"k") <= 100

    def test_append_treats_expired_key_as_empty(self, store):
        # An expired-but-unswept key must not be appended onto: APPEND should
        # start fresh, not extend the stale value.
        store.set(b"k", b"old")
        store.expire(b"k", -1)             # TTL in the past
        assert store.append(b"k", b"new") == 3
        assert store.get(b"k") == b"new"

    def test_strlen(self, store):
        store.set(b"k", b"hello")
        assert store.strlen(b"k") == 5

    def test_strlen_of_missing_key_is_zero(self, store):
        assert store.strlen(b"missing") == 0

    def test_strlen_of_expired_key_is_zero(self, store):
        store.set(b"k", b"value")
        store.expire(b"k", -1)             # TTL in the past
        assert store.strlen(b"k") == 0
        assert store.get(b"k") is None     # and the key is gone afterwards


class TestExpiry:
    def test_expire_sets_ttl(self, store):
        store.set(b"k", b"v")
        assert store.expire(b"k", 100) == 1
        assert 90 <= store.ttl(b"k") <= 100

    def test_expire_on_missing_key_returns_zero(self, store):
        assert store.expire(b"missing", 100) == 0

    def test_ttl_of_missing_key_is_minus_two(self, store):
        assert store.ttl(b"missing") == -2

    def test_ttl_without_expiry_is_minus_one(self, store):
        store.set(b"k", b"v")
        assert store.ttl(b"k") == -1

    def test_expired_key_is_evicted_on_read(self, store):
        store.set(b"k", b"v")
        store.expire(b"k", -1)             # TTL in the past
        assert store.get(b"k") is None
        assert store.exists(b"k") == 0

    def test_persist_removes_ttl(self, store):
        store.set(b"k", b"v", ttl=100)
        assert store.persist(b"k") == 1
        assert store.ttl(b"k") == -1

    def test_persist_without_ttl_returns_zero(self, store):
        store.set(b"k", b"v")
        assert store.persist(b"k") == 0

    def test_persist_missing_key_returns_zero(self, store):
        assert store.persist(b"missing") == 0


class TestActiveExpiry:
    def test_sample_and_expire_evicts_dead_keys(self, store):
        store.set(b"k", b"v")
        store.expire(b"k", -1)             # already expired, but body still resident
        assert b"k" in store._data
        store.sample_and_expire()
        assert b"k" not in store._data

    def test_sample_and_expire_keeps_live_keys(self, store):
        store.set(b"k", b"v", ttl=100)
        store.sample_and_expire()
        assert store.get(b"k") == b"v"
