"""Tests for the command handlers and the dispatcher.

The handlers operate on the module-level ``store`` singleton, so an autouse
fixture resets it before and after every test to keep them isolated.

The module is imported as ``commands`` (rather than importing the handlers by
name) so that handlers like ``set`` don't shadow Python builtins in this file.
"""
from unittest.mock import MagicMock

import pytest

import miniredis.commands as commands
from miniredis.client import ClientState
from miniredis.custom_data_structures import RandomDict
from miniredis.store import store


def _make_client() -> ClientState:
    """Build a ClientState with a mock StreamWriter for dispatch-only tests.

    write_to_socket() is never spawned here, so the writer never gets used.
    """
    return ClientState(MagicMock())


@pytest.fixture(autouse=True)
def reset_store():
    # `store` is the exact object the commands module holds a reference to, so
    # mutating it here is visible to the handlers under test.
    store._data.clear()
    store._ttl = RandomDict()
    yield
    store._data.clear()
    store._ttl = RandomDict()


@pytest.fixture(autouse=True)
def reset_pubsub_registry():
    """Same idea as `reset_store` but for the channel_registry singleton."""
    from miniredis.pubsub import channel_registry
    channel_registry.channel_subs.clear()
    channel_registry.pchannel_subs.clear()
    yield
    channel_registry.channel_subs.clear()
    channel_registry.pchannel_subs.clear()


@pytest.fixture
def client() -> ClientState:
    """A fresh per-connection ClientState for tests that go through dispatch."""
    return _make_client()


def _int(resp: bytes) -> int:
    """Decode a RESP integer reply (:<n>\\r\\n) into a Python int."""
    assert resp.startswith(b":"), f"expected integer reply, got {resp!r}"
    return int(resp[1:-2])


class TestDispatch:
    async def test_empty_command_is_an_error(self, client):
        assert (await commands.dispatch([], client)).startswith(b"-ERR")

    async def test_unknown_command_reports_the_name(self, client):
        resp = await commands.dispatch([b"NOPE"], client)
        assert resp.startswith(b"-ERR")
        assert b"NOPE" in resp

    async def test_command_name_is_case_insensitive(self, client):
        assert await commands.dispatch([b"ping"], client) == b"+PONG\r\n"


class TestPingEcho:
    async def test_ping_without_argument(self):
        assert await commands.ping([]) == b"+PONG\r\n"

    async def test_ping_echoes_its_argument(self):
        assert await commands.ping([b"hello"]) == b"$5\r\nhello\r\n"

    async def test_ping_with_too_many_args_errors(self, client):
        # Arity validation lives in dispatch now; PING is (0, 1).
        assert (await commands.dispatch([b"PING", b"a", b"b"], client)).startswith(b"-ERR")

    async def test_echo(self):
        assert await commands.echo([b"hi"]) == b"$2\r\nhi\r\n"

    async def test_echo_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"ECHO"], client)).startswith(b"-ERR")


class TestGetSet:
    async def test_set_then_get(self):
        assert await commands.set([b"k", b"v"]) == b"+OK\r\n"
        assert await commands.get([b"k"]) == b"$1\r\nv\r\n"

    async def test_get_missing_returns_nil(self):
        assert await commands.get([b"missing"]) == b"$-1\r\n"

    async def test_set_wrong_arity_errors(self):
        assert (await commands.set([b"only"])).startswith(b"-ERR")

    async def test_get_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"GET", b"a", b"b"], client)).startswith(b"-ERR")

    async def test_set_with_ex_sets_ttl_in_seconds(self):
        assert await commands.set([b"k", b"v", b"EX", b"100"]) == b"+OK\r\n"
        assert 90 <= _int(await commands.ttl([b"k"])) <= 100

    async def test_set_with_px_sets_ttl_in_millis(self):
        assert await commands.set([b"k", b"v", b"PX", b"100000"]) == b"+OK\r\n"
        assert 90 <= _int(await commands.ttl([b"k"])) <= 100

    async def test_set_with_zero_expiry_is_rejected(self):
        assert (await commands.set([b"k", b"v", b"EX", b"0"])).startswith(b"-ERR")

    async def test_set_with_negative_expiry_is_rejected(self):
        assert (await commands.set([b"k", b"v", b"EX", b"-5"])).startswith(b"-ERR")

    async def test_set_with_non_numeric_expiry_is_rejected(self):
        assert (await commands.set([b"k", b"v", b"EX", b"abc"])).startswith(b"-ERR")


class TestDelExists:
    async def test_del_counts_removed(self):
        await commands.set([b"a", b"1"])
        await commands.set([b"b", b"2"])
        assert _int(await commands.delete([b"a", b"b", b"missing"])) == 2

    async def test_del_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"DEL"], client)).startswith(b"-ERR")

    async def test_exists_counts_present(self):
        await commands.set([b"a", b"1"])
        assert _int(await commands.exists([b"a", b"missing"])) == 1


class TestCounters:
    async def test_incr_increments(self):
        assert _int(await commands.incr([b"c"])) == 1
        assert _int(await commands.incr([b"c"])) == 2

    async def test_decr_decrements(self):
        await commands.set([b"c", b"5"])
        assert _int(await commands.decr([b"c"])) == 4

    async def test_incrby_and_decrby(self):
        assert _int(await commands.incrby([b"c", b"10"])) == 10
        assert _int(await commands.decrby([b"c", b"4"])) == 6

    async def test_incr_on_non_integer_errors(self):
        await commands.set([b"c", b"abc"])
        assert (await commands.incr([b"c"])).startswith(b"-ERR")

    async def test_incrby_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"INCRBY", b"c"], client)).startswith(b"-ERR")


class TestAppendStrlen:
    async def test_append_returns_new_length(self):
        assert _int(await commands.append([b"k", b"ab"])) == 2
        assert _int(await commands.append([b"k", b"cd"])) == 4
        assert await commands.get([b"k"]) == b"$4\r\nabcd\r\n"

    async def test_strlen(self):
        await commands.set([b"k", b"hello"])
        assert _int(await commands.strlen([b"k"])) == 5

    async def test_strlen_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"STRLEN"], client)).startswith(b"-ERR")


class TestExpiry:
    async def test_expire_then_ttl(self):
        await commands.set([b"k", b"v"])
        assert _int(await commands.expire([b"k", b"100"])) == 1
        assert 90 <= _int(await commands.ttl([b"k"])) <= 100

    async def test_expire_on_missing_key_returns_zero(self):
        assert _int(await commands.expire([b"missing", b"100"])) == 0

    async def test_expire_with_non_positive_ttl_is_rejected(self):
        await commands.set([b"k", b"v"])
        assert (await commands.expire([b"k", b"0"])).startswith(b"-ERR")

    async def test_ttl_of_missing_key(self):
        assert _int(await commands.ttl([b"missing"])) == -2

    async def test_ttl_without_expiry(self):
        await commands.set([b"k", b"v"])
        assert _int(await commands.ttl([b"k"])) == -1

    async def test_persist_clears_ttl(self):
        await commands.set([b"k", b"v", b"EX", b"100"])
        assert _int(await commands.persist([b"k"])) == 1
        assert _int(await commands.ttl([b"k"])) == -1


class TestListCommands:
    async def test_rpush_returns_length(self):
        assert _int(await commands.rpush([b"l", b"a", b"b", b"c"])) == 3

    async def test_lpush_returns_length(self):
        assert _int(await commands.lpush([b"l", b"a", b"b"])) == 2

    async def test_push_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"RPUSH", b"l"], client)).startswith(b"-ERR")
        assert (await commands.dispatch([b"LPUSH", b"l"], client)).startswith(b"-ERR")

    async def test_lrange_returns_array_of_bulk_strings(self):
        await commands.rpush([b"l", b"a", b"b", b"c"])
        assert await commands.lrange([b"l", b"0", b"-1"]) == \
            b"*3\r\n$1\r\na\r\n$1\r\nb\r\n$1\r\nc\r\n"

    async def test_lrange_of_missing_key_is_empty_array(self):
        assert await commands.lrange([b"missing", b"0", b"-1"]) == b"*0\r\n"

    async def test_lrange_non_integer_index_errors(self):
        assert (await commands.lrange([b"l", b"x", b"1"])).startswith(b"-ERR")

    async def test_lrange_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"LRANGE", b"l", b"0"], client)).startswith(b"-ERR")

    async def test_lpop_without_count_returns_bulk_string(self):
        await commands.rpush([b"l", b"a", b"b"])
        assert await commands.lpop([b"l"]) == b"$1\r\na\r\n"

    async def test_rpop_without_count_returns_bulk_string(self):
        await commands.rpush([b"l", b"a", b"b"])
        assert await commands.rpop([b"l"]) == b"$1\r\nb\r\n"

    async def test_lpop_with_count_returns_array(self):
        await commands.rpush([b"l", b"a", b"b", b"c"])
        assert await commands.lpop([b"l", b"2"]) == b"*2\r\n$1\r\na\r\n$1\r\nb\r\n"

    async def test_lpop_missing_key_without_count_is_nil(self):
        assert await commands.lpop([b"missing"]) == b"$-1\r\n"

    async def test_lpop_missing_key_with_count_is_null_array(self):
        assert await commands.lpop([b"missing", b"2"]) == b"*-1\r\n"

    async def test_lpop_non_positive_count_errors(self):
        await commands.rpush([b"l", b"a"])
        assert (await commands.lpop([b"l", b"0"])).startswith(b"-ERR")

    async def test_lpop_non_integer_count_errors(self):
        await commands.rpush([b"l", b"a"])
        assert (await commands.lpop([b"l", b"x"])).startswith(b"-ERR")

    async def test_lpop_too_many_args_errors(self, client):
        assert (await commands.dispatch([b"LPOP", b"l", b"1", b"extra"], client)).startswith(b"-ERR")

    async def test_llen(self):
        await commands.rpush([b"l", b"a", b"b"])
        assert _int(await commands.llen([b"l"])) == 2


class TestHashCommands:
    async def test_hset_returns_new_field_count(self):
        assert _int(await commands.hset([b"h", b"a", b"1", b"b", b"2"])) == 2

    async def test_hset_even_arg_count_errors(self):
        assert (await commands.hset([b"h", b"a"])).startswith(b"-ERR")  # missing value

    async def test_hset_too_few_args_errors(self, client):
        assert (await commands.dispatch([b"HSET", b"h"], client)).startswith(b"-ERR")

    async def test_hget(self):
        await commands.hset([b"h", b"f", b"v"])
        assert await commands.hget([b"h", b"f"]) == b"$1\r\nv\r\n"

    async def test_hget_missing_field_is_nil(self):
        await commands.hset([b"h", b"f", b"v"])
        assert await commands.hget([b"h", b"nope"]) == b"$-1\r\n"

    async def test_hget_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"HGET", b"h"], client)).startswith(b"-ERR")

    async def test_hdel_returns_count(self):
        await commands.hset([b"h", b"a", b"1", b"b", b"2"])
        assert _int(await commands.hdel([b"h", b"a", b"missing"])) == 1

    async def test_hdel_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"HDEL", b"h"], client)).startswith(b"-ERR")

    async def test_hgetall_returns_flat_array(self):
        await commands.hset([b"h", b"a", b"1", b"b", b"2"])
        assert await commands.hgetall([b"h"]) == \
            b"*4\r\n$1\r\na\r\n$1\r\n1\r\n$1\r\nb\r\n$1\r\n2\r\n"

    async def test_hgetall_of_missing_key_is_empty_array(self):
        assert await commands.hgetall([b"missing"]) == b"*0\r\n"

    async def test_hkeys_and_hvals(self):
        await commands.hset([b"h", b"a", b"1", b"b", b"2"])
        assert await commands.hkeys([b"h"]) == b"*2\r\n$1\r\na\r\n$1\r\nb\r\n"
        assert await commands.hvals([b"h"]) == b"*2\r\n$1\r\n1\r\n$1\r\n2\r\n"

    async def test_hlen(self):
        await commands.hset([b"h", b"a", b"1", b"b", b"2"])
        assert _int(await commands.hlen([b"h"])) == 2


class TestWrongType:
    async def test_string_command_on_a_list_key(self, client):
        await commands.dispatch([b"RPUSH", b"l", b"a"], client)
        assert (await commands.dispatch([b"GET", b"l"], client)).startswith(b"-WRONGTYPE")

    async def test_list_command_on_a_string_key(self, client):
        await commands.dispatch([b"SET", b"s", b"v"], client)
        assert (await commands.dispatch([b"LPUSH", b"s", b"a"], client)).startswith(b"-WRONGTYPE")

    async def test_hash_command_on_a_string_key(self, client):
        await commands.dispatch([b"SET", b"s", b"v"], client)
        assert (await commands.dispatch([b"HSET", b"s", b"f", b"v"], client)).startswith(b"-WRONGTYPE")

    async def test_list_command_on_a_hash_key(self, client):
        await commands.dispatch([b"HSET", b"h", b"f", b"v"], client)
        assert (await commands.dispatch([b"LPUSH", b"h", b"a"], client)).startswith(b"-WRONGTYPE")

    async def test_type_agnostic_commands_work_across_types(self, client):
        await commands.dispatch([b"RPUSH", b"l", b"a"], client)
        assert await commands.dispatch([b"DEL", b"l"], client) == b":1\r\n"
        await commands.dispatch([b"HSET", b"h", b"f", b"v"], client)
        assert await commands.dispatch([b"EXISTS", b"h"], client) == b":1\r\n"

    async def test_missing_key_passes_the_type_check(self, client):
        # A command against a missing key should run, not raise WRONGTYPE.
        assert await commands.dispatch([b"GET", b"missing"], client) == b"$-1\r\n"
        assert _int(await commands.dispatch([b"LPUSH", b"newlist", b"a"], client)) == 1

    async def test_sorted_set_command_on_a_string_key(self, client):
        await commands.dispatch([b"SET", b"s", b"v"], client)
        assert (await commands.dispatch([b"ZADD", b"s", b"1", b"a"], client)).startswith(b"-WRONGTYPE")

    async def test_string_command_on_a_sorted_set_key(self, client):
        await commands.dispatch([b"ZADD", b"z", b"1", b"a"], client)
        assert (await commands.dispatch([b"GET", b"z"], client)).startswith(b"-WRONGTYPE")


class TestSortedSetCommands:
    async def test_zadd_returns_new_member_count(self):
        assert _int(await commands.zadd([b"z", b"1", b"a", b"2", b"b"])) == 2

    async def test_zadd_update_returns_zero(self):
        await commands.zadd([b"z", b"1", b"a"])
        assert _int(await commands.zadd([b"z", b"5", b"a"])) == 0

    async def test_zadd_wrong_arity_errors(self):
        assert (await commands.zadd([b"z", b"1"])).startswith(b"-ERR")  # missing member

    async def test_zadd_non_numeric_score_errors(self):
        assert (await commands.zadd([b"z", b"x", b"a"])).startswith(b"-ERR")

    async def test_zscore(self):
        await commands.zadd([b"z", b"2", b"a"])
        assert await commands.zscore([b"z", b"a"]) == b"$3\r\n2.0\r\n"

    async def test_zscore_missing_is_nil(self):
        assert await commands.zscore([b"z", b"a"]) == b"$-1\r\n"

    async def test_zscore_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"ZSCORE", b"z"], client)).startswith(b"-ERR")

    async def test_zrank(self):
        await commands.zadd([b"z", b"1", b"a", b"2", b"b"])
        assert _int(await commands.zrank([b"z", b"b"])) == 1

    async def test_zrank_missing_is_nil(self):
        await commands.zadd([b"z", b"1", b"a"])
        assert await commands.zrank([b"z", b"x"]) == b"$-1\r\n"

    async def test_zrange_returns_array_of_bulk_strings(self):
        await commands.zadd([b"z", b"1", b"a", b"2", b"b", b"3", b"c"])
        assert await commands.zrange([b"z", b"0", b"-1"]) == \
            b"*3\r\n$1\r\na\r\n$1\r\nb\r\n$1\r\nc\r\n"

    async def test_zrange_missing_key_is_empty_array(self):
        assert await commands.zrange([b"missing", b"0", b"-1"]) == b"*0\r\n"

    async def test_zrange_out_of_bounds_negative_start(self):
        # Regression: ZRANGE z -100 -50 on a small set must return an empty
        # array, not overrun the skip list.
        await commands.zadd([b"z", b"1", b"a", b"2", b"b", b"3", b"c"])
        assert await commands.zrange([b"z", b"-100", b"-50"]) == b"*0\r\n"

    async def test_zrange_non_integer_index_errors(self):
        assert (await commands.zrange([b"z", b"x", b"1"])).startswith(b"-ERR")

    async def test_zrangebyscore_returns_array(self):
        await commands.zadd([b"z", b"1", b"a", b"2", b"b", b"3", b"c"])
        assert await commands.zrangebyscore([b"z", b"2", b"3"]) == \
            b"*2\r\n$1\r\nb\r\n$1\r\nc\r\n"

    async def test_zrangebyscore_non_numeric_errors(self):
        assert (await commands.zrangebyscore([b"z", b"x", b"3"])).startswith(b"-ERR")

    async def test_zrem(self):
        await commands.zadd([b"z", b"1", b"a", b"2", b"b"])
        assert _int(await commands.zrem([b"z", b"a", b"missing"])) == 1

    async def test_zrem_wrong_arity_errors(self, client):
        assert (await commands.dispatch([b"ZREM", b"z"], client)).startswith(b"-ERR")

    async def test_zadd_and_zscore_with_infinity(self):
        await commands.zadd([b"z", b"+inf", b"a", b"-inf", b"b", b"5", b"c"])
        assert await commands.zscore([b"z", b"a"]) == b"$3\r\ninf\r\n"
        assert await commands.zscore([b"z", b"b"]) == b"$4\r\n-inf\r\n"

    async def test_zrangebyscore_with_infinite_bounds(self):
        await commands.zadd([b"z", b"+inf", b"a", b"-inf", b"b", b"5", b"c"])
        # -inf..+inf returns everything in score order (b=-inf, c=5, a=+inf)
        assert await commands.zrangebyscore([b"z", b"-inf", b"+inf"]) == \
            b"*3\r\n$1\r\nb\r\n$1\r\nc\r\n$1\r\na\r\n"
        assert await commands.zrangebyscore([b"z", b"-inf", b"5"]) == \
            b"*2\r\n$1\r\nb\r\n$1\r\nc\r\n"


class TestTransactionBasics:
    """The MULTI/EXEC/DISCARD state machine, exercised through dispatch.

    Going through dispatch (rather than calling multi/exec/discard directly)
    is what tests the *integration* with the dispatcher's queue/skip logic.
    """

    async def test_multi_returns_ok_and_enters_transaction(self, client):
        assert await commands.dispatch([b"MULTI"], client) == b"+OK\r\n"
        assert client.in_transaction is True

    async def test_command_inside_multi_returns_queued(self, client):
        await commands.dispatch([b"MULTI"], client)
        assert await commands.dispatch([b"SET", b"k", b"v"], client) == b"+QUEUED\r\n"
        assert await commands.dispatch([b"INCR", b"counter"], client) == b"+QUEUED\r\n"

    async def test_queue_preserves_insertion_order(self, client):
        await commands.dispatch([b"MULTI"], client)
        await commands.dispatch([b"SET", b"a", b"1"], client)
        await commands.dispatch([b"SET", b"b", b"2"], client)
        await commands.dispatch([b"SET", b"c", b"3"], client)
        assert client.get_commands() == [
            [b"SET", b"a", b"1"],
            [b"SET", b"b", b"2"],
            [b"SET", b"c", b"3"],
        ]

    async def test_exec_runs_queued_commands_in_order(self, client):
        await commands.dispatch([b"MULTI"], client)
        await commands.dispatch([b"SET", b"a", b"1"], client)
        await commands.dispatch([b"INCR", b"a"], client)
        await commands.dispatch([b"GET", b"a"], client)
        # EXEC returns RESP array: [+OK, :2, $1\r\n2]
        assert await commands.dispatch([b"EXEC"], client) == \
            b"*3\r\n+OK\r\n:2\r\n$1\r\n2\r\n"

    async def test_exec_clears_transaction_state(self, client):
        await commands.dispatch([b"MULTI"], client)
        await commands.dispatch([b"SET", b"k", b"v"], client)
        await commands.dispatch([b"EXEC"], client)
        assert client.in_transaction is False
        assert client.get_commands() == []
        assert client.abort_transaction is False

    async def test_discard_clears_state_and_returns_ok(self, client):
        await commands.dispatch([b"MULTI"], client)
        await commands.dispatch([b"SET", b"k", b"v"], client)
        assert await commands.dispatch([b"DISCARD"], client) == b"+OK\r\n"
        assert client.in_transaction is False
        assert client.get_commands() == []
        # And the SET was not applied (it was only queued, never executed).
        assert await commands.dispatch([b"GET", b"k"], client) == b"$-1\r\n"

    async def test_empty_multi_exec_returns_empty_array(self, client):
        await commands.dispatch([b"MULTI"], client)
        assert await commands.dispatch([b"EXEC"], client) == b"*0\r\n"


class TestTransactionControlOutsideMulti:
    async def test_exec_without_multi_returns_error(self, client):
        resp = await commands.dispatch([b"EXEC"], client)
        assert resp.startswith(b"-ERR")
        assert b"without MULTI" in resp

    async def test_discard_without_multi_returns_error(self, client):
        resp = await commands.dispatch([b"DISCARD"], client)
        assert resp.startswith(b"-ERR")
        assert b"without MULTI" in resp

    async def test_nested_multi_returns_error(self, client):
        await commands.dispatch([b"MULTI"], client)
        resp = await commands.dispatch([b"MULTI"], client)
        assert resp.startswith(b"-ERR")
        assert b"nested" in resp

    async def test_nested_multi_does_not_break_existing_transaction(self, client):
        # The outer MULTI should still be active; queued commands intact.
        await commands.dispatch([b"MULTI"], client)
        await commands.dispatch([b"SET", b"k", b"v"], client)
        await commands.dispatch([b"MULTI"], client)   # rejected with -ERR
        assert client.in_transaction is True
        assert client.get_commands() == [[b"SET", b"k", b"v"]]


class TestTransactionAbortSemantics:
    async def test_unknown_command_during_multi_aborts_transaction(self, client):
        await commands.dispatch([b"MULTI"], client)
        await commands.dispatch([b"SET", b"k", b"v"], client)
        # Unknown command during MULTI: returns error AND flags the txn aborted.
        resp = await commands.dispatch([b"NOPE"], client)
        assert resp.startswith(b"-ERR")
        assert client.abort_transaction is True

        # EXEC must now return EXECABORT and run nothing.
        exec_resp = await commands.dispatch([b"EXEC"], client)
        assert exec_resp.startswith(b"-EXECABORT")

        # And the SET was *not* applied. The client is cleanly out of the
        # transaction after EXECABORT, so we can reuse it to inspect.
        assert await commands.dispatch([b"GET", b"k"], client) == b"$-1\r\n"

    async def test_execabort_clears_transaction_state(self, client):
        # Regression for the EXECABORT cleanup bug: after a failed
        # transaction, the client must be cleanly out of transaction state
        # so a fresh MULTI works without an intervening DISCARD.
        await commands.dispatch([b"MULTI"], client)
        await commands.dispatch([b"NOPE"], client)         # marks abort
        assert (await commands.dispatch([b"EXEC"], client)).startswith(b"-EXECABORT")

        assert client.in_transaction is False
        assert client.abort_transaction is False
        assert client.get_commands() == []

        # And a fresh MULTI should succeed immediately.
        assert await commands.dispatch([b"MULTI"], client) == b"+OK\r\n"
        assert await commands.dispatch([b"SET", b"k", b"v"], client) == b"+QUEUED\r\n"
        assert await commands.dispatch([b"EXEC"], client) == b"*1\r\n+OK\r\n"
        assert await commands.dispatch([b"GET", b"k"], client) == b"$1\r\nv\r\n"

    async def test_queue_time_arity_error_aborts_transaction(self, client):
        await commands.dispatch([b"MULTI"], client)
        await commands.dispatch([b"SET", b"k", b"v"], client)
        # GET with no key: arity violation -- caught at queue time by dispatch.
        resp = await commands.dispatch([b"GET"], client)
        assert resp.startswith(b"-ERR")
        assert client.abort_transaction is True
        # EXEC -> EXECABORT, queued SET not applied.
        assert (await commands.dispatch([b"EXEC"], client)).startswith(b"-EXECABORT")

    async def test_discard_clears_abort_flag(self, client):
        await commands.dispatch([b"MULTI"], client)
        await commands.dispatch([b"NOPE"], client)   # marks abort
        await commands.dispatch([b"DISCARD"], client)
        assert client.abort_transaction is False
        assert client.in_transaction is False


class TestTransactionRuntimeErrors:
    """Runtime errors inside a queued command appear in the EXEC result array;
    they do NOT abort the rest of the batch. This is real-Redis semantics."""

    async def test_wrongtype_inside_multi_is_a_runtime_error(self, client):
        # WRONGTYPE check is deferred to EXEC time; the command queues fine.
        await commands.dispatch([b"RPUSH", b"l", b"a"], client)  # `l` is now a list
        await commands.dispatch([b"MULTI"], client)
        # Queue a GET on a list-typed key -- queue-time succeeds.
        assert await commands.dispatch([b"GET", b"l"], client) == b"+QUEUED\r\n"
        # Queue a valid LRANGE that comes after.
        assert await commands.dispatch([b"LRANGE", b"l", b"0", b"-1"], client) == b"+QUEUED\r\n"

        resp = await commands.dispatch([b"EXEC"], client)
        # Two replies: first is -WRONGTYPE, second is the LRANGE array.
        assert b"-WRONGTYPE" in resp
        assert b"$1\r\na\r\n" in resp   # LRANGE still ran

    async def test_hset_parity_error_at_exec_does_not_abort_batch(self, client):
        await commands.dispatch([b"MULTI"], client)
        # HSET with even argv (missing value for second field) -- min_args=3
        # passes dispatch, parity check fires inside the handler at EXEC.
        assert await commands.dispatch([b"HSET", b"h", b"f1", b"v1", b"f2"], client) \
            == b"+QUEUED\r\n"
        assert await commands.dispatch([b"SET", b"k", b"v"], client) == b"+QUEUED\r\n"

        resp = await commands.dispatch([b"EXEC"], client)
        # Two replies in the array: first is the parity error, second is +OK.
        assert b"-ERR wrong number of arguments" in resp
        assert b"+OK" in resp
        # The SET DID get applied -- runtime errors don't abort the batch.
        assert await commands.dispatch([b"GET", b"k"], client) == b"$1\r\nv\r\n"


class TestTransactionIsolation:
    async def test_two_clients_have_independent_transactions(self):
        # Two separate connections (ClientStates) -- A's MULTI must not
        # affect B, and vice versa. The shared `store` is module-global,
        # but transaction state is per-connection.
        a = _make_client()
        b = _make_client()

        await commands.dispatch([b"MULTI"], a)
        await commands.dispatch([b"SET", b"shared", b"from_a"], a)

        # B is not in a transaction; a normal SET goes through immediately.
        assert await commands.dispatch([b"SET", b"shared", b"from_b"], b) == b"+OK\r\n"
        # The store now has "from_b" (B's SET applied; A's still queued).
        assert await commands.dispatch([b"GET", b"shared"], b) == b"$6\r\nfrom_b\r\n"

        # Now run A's EXEC: it applies the queued SET, overwriting.
        await commands.dispatch([b"EXEC"], a)
        assert await commands.dispatch([b"GET", b"shared"], b) == b"$6\r\nfrom_a\r\n"


class TestTransactionMaxArgs:
    async def test_multi_with_extra_args_is_arity_error(self, client):
        # max_args=0 was the bug-fix from earlier; verify it still bites.
        resp = await commands.dispatch([b"MULTI", b"extra"], client)
        assert resp.startswith(b"-ERR")
        assert client.in_transaction is False

    async def test_exec_with_extra_args_is_arity_error(self, client):
        await commands.dispatch([b"MULTI"], client)
        resp = await commands.dispatch([b"EXEC", b"extra"], client)
        # arity error fires; since we're still in a transaction, this also
        # flags abort. EXEC's actual error path doesn't execute.
        assert resp.startswith(b"-ERR")


class TestPubSubSubscribeReplies:
    """SUBSCRIBE/UNSUBSCRIBE/PSUBSCRIBE/PUNSUBSCRIBE return None from
    dispatch (the handler writes ack frames directly to client.write_buffer)."""

    async def test_subscribe_returns_none_and_writes_ack_to_buffer(self, client):
        resp = await commands.dispatch([b"SUBSCRIBE", b"news"], client)
        assert resp is None
        ack = client.write_buffer.get_nowait()
        assert ack.startswith(b"*3\r\n")
        assert b"subscribe" in ack
        assert b"news" in ack
        # Subscription count = 1.
        assert b":1\r\n" in ack
        # And the client is now in subscribe mode.
        assert client.is_subscribed is True

    async def test_psubscribe_returns_none_and_writes_pack_to_buffer(self, client):
        resp = await commands.dispatch([b"PSUBSCRIBE", b"news.*"], client)
        assert resp is None
        ack = client.write_buffer.get_nowait()
        assert b"psubscribe" in ack
        assert b"news.*" in ack
        assert client.is_subscribed is True

    async def test_unsubscribe_with_no_args_unsubs_all(self, client):
        # Regression: this used to crash via "set changed size during iteration."
        await commands.dispatch([b"SUBSCRIBE", b"a", b"b", b"c"], client)
        # drain the three subscribe acks
        for _ in range(3):
            client.write_buffer.get_nowait()
        resp = await commands.dispatch([b"UNSUBSCRIBE"], client)
        assert resp is None
        # Three unsubscribe acks follow, in some order.
        msgs = [client.write_buffer.get_nowait() for _ in range(3)]
        # Each is a 3-element array with "unsubscribe" + channel + dwindling count.
        assert all(b"unsubscribe" in m for m in msgs)
        assert client.is_subscribed is False


class TestSubscribeMode:
    """Once a client is in subscribe mode, only the whitelisted commands work."""

    async def test_non_pubsub_command_in_subscribe_mode_is_rejected(self, client):
        await commands.dispatch([b"SUBSCRIBE", b"x"], client)
        client.write_buffer.get_nowait()    # drain the ack
        resp = await commands.dispatch([b"GET", b"k"], client)
        assert resp.startswith(b"-ERR")
        assert b"in this context" in resp

    async def test_ping_is_allowed_in_subscribe_mode(self, client):
        # PING is the customary heartbeat for subscribers; must not error.
        await commands.dispatch([b"SUBSCRIBE", b"x"], client)
        client.write_buffer.get_nowait()
        resp = await commands.dispatch([b"PING"], client)
        assert resp == b"+PONG\r\n"

    async def test_subscribe_commands_themselves_are_allowed_in_subscribe_mode(self, client):
        await commands.dispatch([b"SUBSCRIBE", b"x"], client)
        client.write_buffer.get_nowait()
        # Can SUBSCRIBE to more, UNSUBSCRIBE, PSUBSCRIBE, PUNSUBSCRIBE freely.
        assert await commands.dispatch([b"SUBSCRIBE", b"y"], client) is None
        assert await commands.dispatch([b"PSUBSCRIBE", b"p.*"], client) is None
        assert await commands.dispatch([b"UNSUBSCRIBE", b"x"], client) is None
        assert await commands.dispatch([b"PUNSUBSCRIBE", b"p.*"], client) is None


class TestSubscribeInsideMulti:
    """SUBSCRIBE/PSUBSCRIBE/UNSUBSCRIBE/PUNSUBSCRIBE are *not* allowed inside
    MULTI (a deliberate scope decision; see the long discussion of RESP2
    EXEC array malformation when subscribe-mode commands run inside MULTI)."""

    @pytest.mark.parametrize("cmd", [b"SUBSCRIBE", b"PSUBSCRIBE",
                                      b"UNSUBSCRIBE", b"PUNSUBSCRIBE"])
    async def test_subscribe_family_rejected_in_multi(self, client, cmd):
        await commands.dispatch([b"MULTI"], client)
        resp = await commands.dispatch([cmd, b"x"], client)
        assert resp.startswith(b"-ERR")
        assert b"not allowed in transactions" in resp
        # And the transaction is now flagged for EXECABORT.
        assert client.abort_transaction is True
        assert (await commands.dispatch([b"EXEC"], client)).startswith(b"-EXECABORT")

    async def test_publish_is_queueable_in_multi(self, client):
        # PUBLISH is NOT a pubsub-state-change command and is allowed in MULTI.
        await commands.dispatch([b"MULTI"], client)
        queued = await commands.dispatch([b"PUBLISH", b"ch", b"msg"], client)
        assert queued == b"+QUEUED\r\n"
        # EXEC runs it; with no subscribers the count is 0.
        result = await commands.dispatch([b"EXEC"], client)
        assert result == b"*1\r\n:0\r\n"


class TestPublishDispatch:
    async def test_publish_with_no_subscribers_returns_zero(self, client):
        resp = await commands.dispatch([b"PUBLISH", b"ch", b"hi"], client)
        assert resp == b":0\r\n"

    async def test_publish_delivers_to_subscriber_and_returns_count(self, client):
        sub = _make_client()
        # `sub` subscribes; `client` publishes.
        await commands.dispatch([b"SUBSCRIBE", b"ch"], sub)
        sub.write_buffer.get_nowait()    # drain ack

        resp = await commands.dispatch([b"PUBLISH", b"ch", b"hello"], client)
        assert resp == b":1\r\n"
        msg = sub.write_buffer.get_nowait()
        assert b"message" in msg
        assert b"ch" in msg
        assert b"hello" in msg

    async def test_publish_count_includes_pattern_subscribers(self, client):
        direct = _make_client()
        pattern_sub = _make_client()
        await commands.dispatch([b"SUBSCRIBE", b"news.tech"], direct)
        await commands.dispatch([b"PSUBSCRIBE", b"news.*"], pattern_sub)
        # Drain acks
        direct.write_buffer.get_nowait()
        pattern_sub.write_buffer.get_nowait()

        resp = await commands.dispatch([b"PUBLISH", b"news.tech", b"story"], client)
        # 1 direct + 1 pattern = 2 receivers
        assert resp == b":2\r\n"
        # And the right frame went to each.
        assert b"message" in direct.write_buffer.get_nowait()
        assert b"pmessage" in pattern_sub.write_buffer.get_nowait()

    async def test_publish_wrong_arity_errors(self, client):
        # PUBLISH has min=2, max=2 (channel + message).
        assert (await commands.dispatch([b"PUBLISH", b"ch"], client)).startswith(b"-ERR")
        assert (await commands.dispatch([b"PUBLISH"], client)).startswith(b"-ERR")
