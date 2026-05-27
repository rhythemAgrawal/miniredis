"""Tests for the command handlers and the dispatcher.

The handlers operate on the module-level ``store`` singleton, so an autouse
fixture resets it before and after every test to keep them isolated.

The module is imported as ``commands`` (rather than importing the handlers by
name) so that handlers like ``set`` don't shadow Python builtins in this file.
"""
import pytest

import miniredis.commands as commands
from miniredis.custom_data_structures import RandomDict
from miniredis.store import store


@pytest.fixture(autouse=True)
def reset_store():
    # `store` is the exact object the commands module holds a reference to, so
    # mutating it here is visible to the handlers under test.
    store._data.clear()
    store._ttl = RandomDict()
    yield
    store._data.clear()
    store._ttl = RandomDict()


def _int(resp: bytes) -> int:
    """Decode a RESP integer reply (:<n>\\r\\n) into a Python int."""
    assert resp.startswith(b":"), f"expected integer reply, got {resp!r}"
    return int(resp[1:-2])


class TestDispatch:
    async def test_empty_command_is_an_error(self):
        assert (await commands.dispatch([])).startswith(b"-ERR")

    async def test_unknown_command_reports_the_name(self):
        resp = await commands.dispatch([b"NOPE"])
        assert resp.startswith(b"-ERR")
        assert b"NOPE" in resp

    async def test_command_name_is_case_insensitive(self):
        assert await commands.dispatch([b"ping"]) == b"+PONG\r\n"


class TestPingEcho:
    async def test_ping_without_argument(self):
        assert await commands.ping([]) == b"+PONG\r\n"

    async def test_ping_echoes_its_argument(self):
        assert await commands.ping([b"hello"]) == b"$5\r\nhello\r\n"

    async def test_ping_with_too_many_args_errors(self):
        assert (await commands.ping([b"a", b"b"])).startswith(b"-ERR")

    async def test_echo(self):
        assert await commands.echo([b"hi"]) == b"$2\r\nhi\r\n"

    async def test_echo_wrong_arity_errors(self):
        assert (await commands.echo([])).startswith(b"-ERR")


class TestGetSet:
    async def test_set_then_get(self):
        assert await commands.set([b"k", b"v"]) == b"+OK\r\n"
        assert await commands.get([b"k"]) == b"$1\r\nv\r\n"

    async def test_get_missing_returns_nil(self):
        assert await commands.get([b"missing"]) == b"$-1\r\n"

    async def test_set_wrong_arity_errors(self):
        assert (await commands.set([b"only"])).startswith(b"-ERR")

    async def test_get_wrong_arity_errors(self):
        assert (await commands.get([b"a", b"b"])).startswith(b"-ERR")

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

    async def test_del_wrong_arity_errors(self):
        assert (await commands.delete([])).startswith(b"-ERR")

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

    async def test_incrby_wrong_arity_errors(self):
        assert (await commands.incrby([b"c"])).startswith(b"-ERR")


class TestAppendStrlen:
    async def test_append_returns_new_length(self):
        assert _int(await commands.append([b"k", b"ab"])) == 2
        assert _int(await commands.append([b"k", b"cd"])) == 4
        assert await commands.get([b"k"]) == b"$4\r\nabcd\r\n"

    async def test_strlen(self):
        await commands.set([b"k", b"hello"])
        assert _int(await commands.strlen([b"k"])) == 5

    async def test_strlen_wrong_arity_errors(self):
        assert (await commands.strlen([])).startswith(b"-ERR")


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

    async def test_push_wrong_arity_errors(self):
        assert (await commands.rpush([b"l"])).startswith(b"-ERR")
        assert (await commands.lpush([b"l"])).startswith(b"-ERR")

    async def test_lrange_returns_array_of_bulk_strings(self):
        await commands.rpush([b"l", b"a", b"b", b"c"])
        assert await commands.lrange([b"l", b"0", b"-1"]) == \
            b"*3\r\n$1\r\na\r\n$1\r\nb\r\n$1\r\nc\r\n"

    async def test_lrange_of_missing_key_is_empty_array(self):
        assert await commands.lrange([b"missing", b"0", b"-1"]) == b"*0\r\n"

    async def test_lrange_non_integer_index_errors(self):
        assert (await commands.lrange([b"l", b"x", b"1"])).startswith(b"-ERR")

    async def test_lrange_wrong_arity_errors(self):
        assert (await commands.lrange([b"l", b"0"])).startswith(b"-ERR")

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

    async def test_lpop_too_many_args_errors(self):
        assert (await commands.lpop([b"l", b"1", b"extra"])).startswith(b"-ERR")

    async def test_llen(self):
        await commands.rpush([b"l", b"a", b"b"])
        assert _int(await commands.llen([b"l"])) == 2


class TestHashCommands:
    async def test_hset_returns_new_field_count(self):
        assert _int(await commands.hset([b"h", b"a", b"1", b"b", b"2"])) == 2

    async def test_hset_even_arg_count_errors(self):
        assert (await commands.hset([b"h", b"a"])).startswith(b"-ERR")  # missing value

    async def test_hset_too_few_args_errors(self):
        assert (await commands.hset([b"h"])).startswith(b"-ERR")

    async def test_hget(self):
        await commands.hset([b"h", b"f", b"v"])
        assert await commands.hget([b"h", b"f"]) == b"$1\r\nv\r\n"

    async def test_hget_missing_field_is_nil(self):
        await commands.hset([b"h", b"f", b"v"])
        assert await commands.hget([b"h", b"nope"]) == b"$-1\r\n"

    async def test_hget_wrong_arity_errors(self):
        assert (await commands.hget([b"h"])).startswith(b"-ERR")

    async def test_hdel_returns_count(self):
        await commands.hset([b"h", b"a", b"1", b"b", b"2"])
        assert _int(await commands.hdel([b"h", b"a", b"missing"])) == 1

    async def test_hdel_wrong_arity_errors(self):
        assert (await commands.hdel([b"h"])).startswith(b"-ERR")

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
    async def test_string_command_on_a_list_key(self):
        await commands.dispatch([b"RPUSH", b"l", b"a"])
        assert (await commands.dispatch([b"GET", b"l"])).startswith(b"-WRONGTYPE")

    async def test_list_command_on_a_string_key(self):
        await commands.dispatch([b"SET", b"s", b"v"])
        assert (await commands.dispatch([b"LPUSH", b"s", b"a"])).startswith(b"-WRONGTYPE")

    async def test_hash_command_on_a_string_key(self):
        await commands.dispatch([b"SET", b"s", b"v"])
        assert (await commands.dispatch([b"HSET", b"s", b"f", b"v"])).startswith(b"-WRONGTYPE")

    async def test_list_command_on_a_hash_key(self):
        await commands.dispatch([b"HSET", b"h", b"f", b"v"])
        assert (await commands.dispatch([b"LPUSH", b"h", b"a"])).startswith(b"-WRONGTYPE")

    async def test_type_agnostic_commands_work_across_types(self):
        await commands.dispatch([b"RPUSH", b"l", b"a"])
        assert await commands.dispatch([b"DEL", b"l"]) == b":1\r\n"
        await commands.dispatch([b"HSET", b"h", b"f", b"v"])
        assert await commands.dispatch([b"EXISTS", b"h"]) == b":1\r\n"

    async def test_missing_key_passes_the_type_check(self):
        # A command against a missing key should run, not raise WRONGTYPE.
        assert await commands.dispatch([b"GET", b"missing"]) == b"$-1\r\n"
        assert _int(await commands.dispatch([b"LPUSH", b"newlist", b"a"])) == 1
