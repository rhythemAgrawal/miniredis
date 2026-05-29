"""Unit tests for the Store data layer.

Each test gets a fresh ``Store`` instance (the module-level singleton is only
used by the command handlers, exercised in test_commands.py). Expiry is tested
deterministically by setting a TTL in the past via ``expire(key, -1)`` rather
than sleeping, so the suite stays fast and non-flaky.
"""
from collections import deque

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


class TestLists:
    def test_rpush_appends_in_order(self, store):
        assert store.rpush(b"l", [b"a", b"b", b"c"]) == 3
        assert store.lrange(b"l", 0, -1) == [b"a", b"b", b"c"]

    def test_lpush_prepends_so_batch_is_reversed(self, store):
        # Each element is pushed to the head in turn, so the batch ends reversed.
        assert store.lpush(b"l", [b"a", b"b", b"c"]) == 3
        assert store.lrange(b"l", 0, -1) == [b"c", b"b", b"a"]

    def test_push_returns_new_length(self, store):
        store.rpush(b"l", [b"a"])
        assert store.rpush(b"l", [b"b", b"c"]) == 3

    def test_lpop_default_count_returns_head(self, store):
        store.rpush(b"l", [b"a", b"b", b"c"])
        assert store.lpop(b"l") == [b"a"]

    def test_rpop_default_count_returns_tail(self, store):
        store.rpush(b"l", [b"a", b"b", b"c"])
        assert store.rpop(b"l") == [b"c"]

    def test_lpop_with_count(self, store):
        store.rpush(b"l", [b"a", b"b", b"c", b"d"])
        assert store.lpop(b"l", count=2) == [b"a", b"b"]

    def test_pop_count_exceeding_length_is_clamped(self, store):
        store.rpush(b"l", [b"a", b"b"])
        assert store.lpop(b"l", count=10) == [b"a", b"b"]

    def test_pop_on_missing_key_returns_none(self, store):
        assert store.lpop(b"missing") is None
        assert store.rpop(b"missing") is None

    def test_list_key_is_deleted_when_emptied(self, store):
        store.rpush(b"l", [b"a"])
        store.lpop(b"l")
        assert store.exists(b"l") == 0

    def test_llen(self, store):
        store.rpush(b"l", [b"a", b"b", b"c"])
        assert store.llen(b"l") == 3

    def test_llen_of_missing_key_is_zero(self, store):
        assert store.llen(b"missing") == 0

    def test_lrange_negative_indices(self, store):
        store.rpush(b"l", [b"a", b"b", b"c", b"d"])
        assert store.lrange(b"l", 0, -1) == [b"a", b"b", b"c", b"d"]
        assert store.lrange(b"l", -2, -1) == [b"c", b"d"]
        assert store.lrange(b"l", 0, -2) == [b"a", b"b", b"c"]

    def test_lrange_subrange(self, store):
        store.rpush(b"l", [b"a", b"b", b"c", b"d"])
        assert store.lrange(b"l", 1, 2) == [b"b", b"c"]

    def test_lrange_of_missing_key_is_empty(self, store):
        assert store.lrange(b"missing", 0, -1) == []


class TestHashes:
    def test_hset_and_hget(self, store):
        store.hset(b"h", [b"f", b"v"])
        assert store.hget(b"h", b"f") == b"v"

    def test_hset_returns_count_of_new_fields(self, store):
        assert store.hset(b"h", [b"a", b"1", b"b", b"2"]) == 2

    def test_hset_update_is_not_counted(self, store):
        store.hset(b"h", [b"a", b"1"])
        assert store.hset(b"h", [b"a", b"2", b"b", b"3"]) == 1  # only b is new
        assert store.hget(b"h", b"a") == b"2"

    def test_hget_missing_field_returns_none(self, store):
        store.hset(b"h", [b"a", b"1"])
        assert store.hget(b"h", b"missing") is None

    def test_hget_missing_key_returns_none(self, store):
        assert store.hget(b"missing", b"f") is None

    def test_hdel_returns_count_removed(self, store):
        store.hset(b"h", [b"a", b"1", b"b", b"2", b"c", b"3"])
        assert store.hdel(b"h", [b"a", b"b", b"missing"]) == 2

    def test_hash_key_is_deleted_when_emptied(self, store):
        store.hset(b"h", [b"a", b"1"])
        store.hdel(b"h", [b"a"])
        assert store.exists(b"h") == 0

    def test_hlen(self, store):
        store.hset(b"h", [b"a", b"1", b"b", b"2"])
        assert store.hlen(b"h") == 2

    def test_hlen_of_missing_key_is_zero(self, store):
        assert store.hlen(b"missing") == 0

    def test_hkeys_and_hvals_preserve_insertion_order(self, store):
        store.hset(b"h", [b"a", b"1", b"b", b"2"])
        assert store.hkeys(b"h") == [b"a", b"b"]
        assert store.hvals(b"h") == [b"1", b"2"]

    def test_hgetall_returns_flat_field_value_pairs(self, store):
        store.hset(b"h", [b"a", b"1", b"b", b"2"])
        assert store.hgetall(b"h") == [b"a", b"1", b"b", b"2"]

    def test_hgetall_of_missing_key_is_empty(self, store):
        assert store.hgetall(b"missing") == []


class TestTypeValidation:
    def test_missing_key_is_valid_for_any_type(self, store):
        assert store.is_valid_value_type(b"missing", bytes) is True
        assert store.is_valid_value_type(b"missing", deque) is True
        assert store.is_valid_value_type(b"missing", dict) is True

    def test_string_key_matches_bytes_only(self, store):
        store.set(b"s", b"v")
        assert store.is_valid_value_type(b"s", bytes) is True
        assert store.is_valid_value_type(b"s", deque) is False
        assert store.is_valid_value_type(b"s", dict) is False

    def test_list_key_matches_deque_only(self, store):
        store.rpush(b"l", [b"a"])
        assert store.is_valid_value_type(b"l", deque) is True
        assert store.is_valid_value_type(b"l", bytes) is False

    def test_hash_key_matches_dict_only(self, store):
        store.hset(b"h", [b"f", b"v"])
        assert store.is_valid_value_type(b"h", dict) is True
        assert store.is_valid_value_type(b"h", bytes) is False

    def test_empty_string_value_is_still_typed_as_bytes(self, store):
        # Regression: an empty-string value must NOT be treated as "any type".
        store.set(b"s", b"")
        assert store.is_valid_value_type(b"s", bytes) is True
        assert store.is_valid_value_type(b"s", deque) is False


class TestSortedSets:
    def test_zadd_and_zscore(self, store):
        assert store.zadd(b"z", [1.0, b"a", 2.0, b"b"]) == 2
        assert store.zscore(b"z", b"a") == 1.0

    def test_zadd_update_returns_zero_and_repositions(self, store):
        store.zadd(b"z", [1.0, b"a", 2.0, b"b"])
        assert store.zadd(b"z", [5.0, b"a"]) == 0          # a already exists
        assert store.zscore(b"z", b"a") == 5.0
        assert store.zrange(b"z", 0, -1) == [b"b", b"a"]   # b(2) now before a(5)

    def test_zscore_missing(self, store):
        assert store.zscore(b"missing", b"a") is None
        store.zadd(b"z", [1.0, b"a"])
        assert store.zscore(b"z", b"x") is None

    def test_zrank(self, store):
        store.zadd(b"z", [1.0, b"a", 2.0, b"b", 3.0, b"c"])
        assert store.zrank(b"z", b"a") == 0
        assert store.zrank(b"z", b"c") == 2

    def test_zrank_missing_returns_none(self, store):
        store.zadd(b"z", [1.0, b"a"])
        assert store.zrank(b"z", b"x") is None
        assert store.zrank(b"missing", b"a") is None

    def test_zrange_by_rank(self, store):
        store.zadd(b"z", [1.0, b"a", 2.0, b"b", 3.0, b"c"])
        assert store.zrange(b"z", 0, -1) == [b"a", b"b", b"c"]
        assert store.zrange(b"z", 0, 1) == [b"a", b"b"]
        assert store.zrange(b"z", -2, -1) == [b"b", b"c"]

    def test_zrange_missing_key_is_empty(self, store):
        assert store.zrange(b"missing", 0, -1) == []

    def test_zrange_by_score(self, store):
        store.zadd(b"z", [1.0, b"a", 2.0, b"b", 3.0, b"c", 4.0, b"d"])
        assert store.zrange_by_score(b"z", 2.0, 3.0) == [b"b", b"c"]

    def test_zrem(self, store):
        store.zadd(b"z", [1.0, b"a", 2.0, b"b", 3.0, b"c"])
        assert store.zrem(b"z", [b"a", b"missing"]) == 1
        assert store.zrange(b"z", 0, -1) == [b"b", b"c"]

    def test_zrem_missing_key(self, store):
        assert store.zrem(b"missing", [b"a"]) == 0

    def test_tie_break_is_lexicographic(self, store):
        store.zadd(b"z", [1.0, b"banana", 1.0, b"apple", 1.0, b"cherry"])
        assert store.zrange(b"z", 0, -1) == [b"apple", b"banana", b"cherry"]

    def test_infinity_scores(self, store):
        store.zadd(b"z", [float("-inf"), b"lo", 5.0, b"mid", float("inf"), b"hi"])
        assert store.zscore(b"z", b"hi") == float("inf")
        assert store.zscore(b"z", b"lo") == float("-inf")
        assert store.zrange(b"z", 0, -1) == [b"lo", b"mid", b"hi"]
        assert store.zrange_by_score(b"z", float("-inf"), float("inf")) == [b"lo", b"mid", b"hi"]
        assert store.zrange_by_score(b"z", float("-inf"), 5.0) == [b"lo", b"mid"]
