"""Unit tests for the standalone SkipList.

SkipList is now its own data structure (no member->score dict, no Redis
semantics), so it's tested in isolation here. The caller contract is that an
existing member must be deleted at its old score before being re-inserted --
which is exactly what SortedSet does.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from miniredis.custom_data_structures import SkipList


class TestSkipList:
    def test_empty(self):
        sl = SkipList()
        assert sl.get_length() == 0
        assert sl.get_range_by_rank(0, -1) == []

    def test_ordering_and_rank(self):
        sl = SkipList()
        for i in range(5):
            sl.insert(float(i), str(i).encode())
        assert sl.get_length() == 5
        assert sl.get_range_by_rank(0, -1) == [b"0", b"1", b"2", b"3", b"4"]
        assert [sl.get_rank(float(i), str(i).encode()) for i in range(5)] == [0, 1, 2, 3, 4]

    def test_insertion_order_does_not_affect_sorted_order(self):
        sl = SkipList()
        for score, member in [(3.0, b"c"), (1.0, b"a"), (4.0, b"d"), (2.0, b"b")]:
            sl.insert(score, member)
        assert sl.get_range_by_rank(0, -1) == [b"a", b"b", b"c", b"d"]

    def test_equal_scores_break_ties_lexicographically(self):
        sl = SkipList()
        for member in [b"banana", b"apple", b"cherry"]:
            sl.insert(1.0, member)
        assert sl.get_range_by_rank(0, -1) == [b"apple", b"banana", b"cherry"]

    def test_range_by_rank_negative_indices(self):
        sl = SkipList()
        for i in range(5):
            sl.insert(float(i), str(i).encode())
        assert sl.get_range_by_rank(-2, -1) == [b"3", b"4"]
        assert sl.get_range_by_rank(1, 3) == [b"1", b"2", b"3"]

    def test_range_by_rank_out_of_bounds(self):
        sl = SkipList()
        for i in range(5):
            sl.insert(float(i), str(i).encode())
        all_members = [b"0", b"1", b"2", b"3", b"4"]
        # Regression: a start more negative than -length used to overrun the
        # list and crash; it must clamp to 0 (and here start>end => empty).
        assert sl.get_range_by_rank(-100, -50) == []
        # start clamps to 0, so this returns the whole set.
        assert sl.get_range_by_rank(-100, -1) == all_members
        assert sl.get_range_by_rank(10, 20) == []          # start past the end
        assert sl.get_range_by_rank(0, 100) == all_members  # end clamps to last
        assert sl.get_range_by_rank(3, 1) == []            # start > end

    def test_range_by_score_is_inclusive(self):
        sl = SkipList()
        for i in range(5):
            sl.insert(float(i), str(i).encode())
        assert sl.get_range_by_score(1.0, 3.0) == [b"1", b"2", b"3"]

    def test_infinity_scores(self):
        sl = SkipList()
        sl.insert(float("-inf"), b"lo")
        sl.insert(5.0, b"mid")
        sl.insert(float("inf"), b"hi")
        assert sl.get_range_by_rank(0, -1) == [b"lo", b"mid", b"hi"]
        assert sl.get_rank(float("-inf"), b"lo") == 0
        assert sl.get_rank(float("inf"), b"hi") == 2
        # +inf upper bound must terminate at the tail sentinel, not overrun it.
        assert sl.get_range_by_score(float("-inf"), float("inf")) == [b"lo", b"mid", b"hi"]
        assert sl.get_range_by_score(float("-inf"), 5.0) == [b"lo", b"mid"]

    def test_delete_updates_length_order_and_rank(self):
        sl = SkipList()
        for i in range(5):
            sl.insert(float(i), str(i).encode())
        sl.delete(2.0, b"2")
        assert sl.get_length() == 4
        assert sl.get_range_by_rank(0, -1) == [b"0", b"1", b"3", b"4"]
        assert sl.get_rank(3.0, b"3") == 2

    def test_delete_only_member(self):
        sl = SkipList()
        sl.insert(1.0, b"a")
        sl.delete(1.0, b"a")
        assert sl.get_length() == 0
        assert sl.get_range_by_rank(0, -1) == []


# --- property: the skip list always matches a sorted() reference ---------

_MEMBER_POOL = [bytes([i]) for i in range(6)]
_finite_floats = st.floats(allow_nan=False, allow_infinity=False)


@settings(max_examples=200)
@given(st.dictionaries(st.binary(min_size=1, max_size=4), _finite_floats, max_size=30))
def test_skiplist_matches_sorted_reference(members):
    sl = SkipList()
    for member, score in members.items():
        sl.insert(score, member)

    expected = [m for _, m in sorted((s, m) for m, s in members.items())]
    assert sl.get_length() == len(members)
    assert sl.get_range_by_rank(0, -1) == expected
    for i, member in enumerate(expected):
        assert sl.get_rank(members[member], member) == i


@settings(max_examples=200)
@given(
    st.lists(
        st.one_of(
            st.tuples(st.just("add"), st.sampled_from(_MEMBER_POOL), _finite_floats),
            st.tuples(st.just("rem"), st.sampled_from(_MEMBER_POOL), st.none()),
        ),
        max_size=50,
    )
)
def test_skiplist_under_add_update_remove(ops):
    sl = SkipList()
    live: dict[bytes, float] = {}

    for kind, member, score in ops:
        if kind == "add":
            if member in live:                 # update == remove-then-reinsert
                sl.delete(live[member], member)
            sl.insert(score, member)
            live[member] = score
        elif member in live:                   # remove
            sl.delete(live[member], member)
            del live[member]

    expected = [m for _, m in sorted((s, m) for m, s in live.items())]
    assert sl.get_length() == len(live)
    assert sl.get_range_by_rank(0, -1) == expected
    for i, member in enumerate(expected):
        assert sl.get_rank(live[member], member) == i
