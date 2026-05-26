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
