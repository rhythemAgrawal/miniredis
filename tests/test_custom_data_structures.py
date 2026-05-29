"""Unit tests for RandomDict.

These are deliberately white-box: RandomDict's whole reason to exist is O(1)
random sampling backed by a parallel list + position index, so the tests assert
on the internal ``_keys`` / ``_pos`` / ``_data`` invariants that make that work.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from miniredis.custom_data_structures import RandomDict, SortedSet


def _assert_invariants(rd: RandomDict) -> None:
    """The three containers must always describe the same key set consistently."""
    assert set(rd._keys) == set(rd._data) == set(rd._pos)
    assert len(rd._keys) == len(set(rd._keys))            # no duplicates in _keys
    assert len(rd._keys) == len(rd._data) == len(rd._pos)  # all same size
    for key, idx in rd._pos.items():                       # _pos inverts _keys
        assert rd._keys[idx] == key


class TestBasics:
    def test_set_and_get(self):
        rd = RandomDict()
        rd.set(b"k", 1.5)
        assert rd.get(b"k") == 1.5

    def test_get_missing_returns_default(self):
        rd = RandomDict()
        assert rd.get(b"missing") is None
        assert rd.get(b"missing", 9.0) == 9.0

    def test_overwrite_updates_value_without_duplicating_key(self):
        rd = RandomDict()
        rd.set(b"k", 1.0)
        rd.set(b"k", 2.0)
        assert rd.get(b"k") == 2.0
        assert rd._keys.count(b"k") == 1
        _assert_invariants(rd)


class TestDelete:
    def test_delete_only_key(self):
        # Regression: deleting the sole (== last) key used to raise IndexError.
        rd = RandomDict()
        rd.set(b"only", 1.0)
        rd.delete(b"only")
        assert rd.get(b"only") is None
        _assert_invariants(rd)

    def test_delete_last_of_many(self):
        rd = RandomDict()
        for i in range(5):
            rd.set(bytes([i]), float(i))
        rd.delete(bytes([4]))  # last inserted -> last slot in _keys
        assert rd.get(bytes([4])) is None
        _assert_invariants(rd)

    def test_delete_middle_key_relocates_tail(self):
        rd = RandomDict()
        for i in range(5):
            rd.set(bytes([i]), float(i))
        rd.delete(bytes([2]))
        assert rd.get(bytes([2])) is None
        for i in (0, 1, 3, 4):
            assert rd.get(bytes([i])) == float(i)
        _assert_invariants(rd)

    def test_delete_missing_key_is_noop(self):
        rd = RandomDict()
        rd.set(b"k", 1.0)
        rd.delete(b"absent")
        assert rd.get(b"k") == 1.0
        _assert_invariants(rd)

    def test_delete_every_key_one_by_one(self):
        rd = RandomDict()
        keys = [bytes([i]) for i in range(10)]
        for i, key in enumerate(keys):
            rd.set(key, float(i))
        for key in keys:
            rd.delete(key)
            _assert_invariants(rd)
        assert rd._keys == []


class TestSampling:
    def test_sample_of_empty_dict_is_empty(self):
        assert RandomDict().get_sample(10) == []

    def test_sample_caps_at_available_keys(self):
        rd = RandomDict()
        for i in range(3):
            rd.set(bytes([i]), float(i))
        sample = rd.get_sample(10)
        assert len(sample) == 3
        assert set(sample) == {bytes([0]), bytes([1]), bytes([2])}

    def test_sample_returns_distinct_subset(self):
        rd = RandomDict()
        for i in range(20):
            rd.set(bytes([i]), float(i))
        sample = rd.get_sample(5)
        assert len(sample) == 5
        assert len(set(sample)) == 5  # no repeats
        assert set(sample).issubset(set(rd._keys))


# --- property: invariants survive arbitrary set/delete sequences ---------

_KEY_POOL = [bytes([i]) for i in range(8)]


@settings(max_examples=200)
@given(
    st.lists(
        st.tuples(st.sampled_from(["set", "delete"]), st.sampled_from(_KEY_POOL)),
        max_size=50,
    )
)
def test_invariants_hold_under_random_ops(ops):
    rd = RandomDict()
    for op, key in ops:
        if op == "set":
            rd.set(key, 1.0)
        else:
            rd.delete(key)
        _assert_invariants(rd)


class TestSortedSet:
    """SortedSet coordinates a member->score dict with a SkipList.

    The heavy skip-list correctness (ordering, ranks, ranges, edge cases, and
    the property tests) lives in test_skiplist.py. These tests cover SortedSet's
    own responsibility: keeping the two structures in sync and delegating
    queries to the skip list.
    """

    def test_get_score(self):
        ss = SortedSet()
        ss.insert(3.5, b"a")
        assert ss.get_score(b"a") == 3.5
        assert ss.get_score(b"missing") is None

    def test_insert_updates_both_structures(self):
        ss = SortedSet()
        ss.insert(1.0, b"a")
        assert ss.get_score(b"a") == 1.0          # the member->score dict
        assert ss.get_length() == 1               # the skip list
        assert ss.get_range_by_rank(0, -1) == [b"a"]

    def test_delete_removes_from_both_structures(self):
        ss = SortedSet()
        ss.insert(1.0, b"a")
        ss.delete(1.0, b"a")
        assert ss.get_score(b"a") is None
        assert ss.get_length() == 0
        assert ss.get_range_by_rank(0, -1) == []

    def test_query_methods_delegate_to_skiplist(self):
        ss = SortedSet()
        for i, member in enumerate([b"a", b"b", b"c"]):
            ss.insert(float(i), member)
        assert ss.get_rank(1.0, b"b") == 1
        assert ss.get_range_by_rank(0, -1) == [b"a", b"b", b"c"]
        assert ss.get_range_by_score(0.0, 1.0) == [b"a", b"b"]
        assert ss.get_length() == 3
