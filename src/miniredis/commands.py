from collections import deque
from collections.abc import Awaitable, Callable
from typing import NamedTuple
import math

from miniredis.protocol import encode_error, encode_simple_string, encode_bulk_string, encode_integer, encode_array
from miniredis.store import store
from miniredis.custom_data_structures import SortedSet
from miniredis.client import ClientState


# ---------------------------------------------------------------------------
# Command handlers.
#
# Arity is validated by dispatch() before any handler runs (via the min_args/
# max_args fields on Command), so handlers no longer check argc themselves.
# Runtime-only validation (type parsing, value bounds, parity constraints
# that min/max can't express) stays in the handler.
# ---------------------------------------------------------------------------

async def ping(argv: list[bytes]) -> bytes:
    if len(argv) == 1:
        return encode_bulk_string(argv[0])
    return encode_simple_string("PONG")

async def echo(argv: list[bytes]) -> bytes:
    return encode_bulk_string(argv[0])

async def get(argv: list[bytes]) -> bytes:
    return encode_bulk_string(store.get(argv[0]))

async def set(argv: list[bytes]) -> bytes:
    if len(argv) == 2:
        store.set(argv[0], argv[1])
        return encode_simple_string("OK")
    if len(argv) == 4 and argv[2].upper() in [b"EX", b"PX"]:
        try:
            ttl = int(argv[3].decode())
        except ValueError:
            return encode_error("ERR Wrong ttl type")

        if argv[2].upper() == b"PX":
            ttl /= 1000

        if ttl <= 0:
            return encode_error("ERR invalid expire time in SET command")

        store.set(argv[0], argv[1], ttl)
        return encode_simple_string("OK")

    # argc is in [2, 4] (dispatch enforces) but the option-form (3 args) or a
    # bad option byte fell through. Not an arity problem -- it's syntax.
    return encode_error("ERR syntax error")

async def delete(argv: list[bytes]) -> bytes:
    return encode_integer(store.delete(*argv))

async def exists(argv: list[bytes]) -> bytes:
    return encode_integer(store.exists(*argv))

async def incrby(argv: list[bytes]) -> bytes:
    try:
        new_val = store.incr_by(argv[0], int(argv[1].decode()))
    except ValueError:
        return encode_error("ERR Wrong value type for this operation")

    return encode_integer(new_val)

async def decrby(argv: list[bytes]) -> bytes:
    try:
        new_val = store.decr_by(argv[0], int(argv[1].decode()))
    except ValueError:
        return encode_error("ERR Wrong value type for this operation")

    return encode_integer(new_val)

async def incr(argv: list[bytes]) -> bytes:
    try:
        new_val = store.incr(argv[0])
    except ValueError:
        return encode_error("ERR Wrong value type for this operation")

    return encode_integer(new_val)

async def decr(argv: list[bytes]) -> bytes:
    try:
        new_val = store.decr(argv[0])
    except ValueError:
        return encode_error("ERR Wrong value type for this operation")

    return encode_integer(new_val)

async def append(argv: list[bytes]) -> bytes:
    return encode_integer(store.append(argv[0], argv[1]))

async def strlen(argv: list[bytes]) -> bytes:
    return encode_integer(store.strlen(argv[0]))

async def expire(argv: list[bytes]) -> bytes:
    try:
        ttl = float(argv[1].decode())
    except ValueError:
        return encode_error("ERR Wrong ttl type")

    if ttl <= 0:
        return encode_error("ERR Invalid ttl value for EXPIRE command")

    return encode_integer(store.expire(argv[0], ttl))

async def ttl(argv: list[bytes]) -> bytes:
    return encode_integer(store.ttl(argv[0]))

async def persist(argv: list[bytes]) -> bytes:
    return encode_integer(store.persist(argv[0]))

async def lpush(argv: list[bytes]) -> bytes:
    return encode_integer(store.lpush(argv[0], argv[1:]))

async def rpush(argv: list[bytes]) -> bytes:
    return encode_integer(store.rpush(argv[0], argv[1:]))

async def lpop(argv: list[bytes]) -> bytes:
    count = 1

    if len(argv) == 2:
        try:
            count = int(argv[1].decode())

            if count <= 0:
                return encode_error("ERR Invalid value for count for LPOP command")
        except ValueError:
            return encode_error("ERR Invalid data type for count for LPOP command")

    popped = store.lpop(argv[0], count=count)

    if len(argv) == 2:
        return encode_array(popped if popped is None else [encode_bulk_string(element) for element in popped])
    return encode_bulk_string(popped if popped is None else popped[0])

async def rpop(argv: list[bytes]) -> bytes:
    count = 1

    if len(argv) == 2:
        try:
            count = int(argv[1].decode())

            if count <= 0:
                return encode_error("ERR Invalid value for count for RPOP command")
        except ValueError:
            return encode_error("ERR Invalid data type for count for RPOP command")

    popped = store.rpop(argv[0], count=count)

    if len(argv) == 2:
        return encode_array(popped if popped is None else [encode_bulk_string(element) for element in popped])
    return encode_bulk_string(popped if popped is None else popped[0])

async def lrange(argv: list[bytes]) -> bytes:
    try:
        start = int(argv[1].decode())
        end = int(argv[2].decode())
    except ValueError:
        return encode_error("ERR Invalid arguments for the LRANGE command")

    return encode_array([encode_bulk_string(element) for element in store.lrange(argv[0], start, end)])

async def llen(argv: list[bytes]) -> bytes:
    return encode_integer(store.llen(argv[0]))

async def hget(argv: list[bytes]) -> bytes:
    return encode_bulk_string(store.hget(argv[0], argv[1]))

async def hset(argv: list[bytes]) -> bytes:
    # Parity is a constraint min/max can't express: argv is
    # (key, f1, v1, f2, v2, ...) so its length must be odd.
    if len(argv) % 2 == 0:
        return encode_error("ERR wrong number of arguments for 'hset' command")

    return encode_integer(store.hset(argv[0], argv[1:]))

async def hdel(argv: list[bytes]) -> bytes:
    return encode_integer(store.hdel(argv[0], argv[1:]))

async def hgetall(argv: list[bytes]) -> bytes:
    return encode_array([encode_bulk_string(element) for element in store.hgetall(argv[0])])

async def hkeys(argv: list[bytes]) -> bytes:
    return encode_array([encode_bulk_string(element) for element in store.hkeys(argv[0])])

async def hvals(argv: list[bytes]) -> bytes:
    return encode_array([encode_bulk_string(element) for element in store.hvals(argv[0])])

async def hlen(argv: list[bytes]) -> bytes:
    return encode_integer(store.hlen(argv[0]))

async def zadd(argv: list[bytes]) -> bytes:
    # Parity: argv is (key, s1, m1, s2, m2, ...) -- length must be odd.
    if len(argv) % 2 == 0:
        return encode_error("ERR wrong number of arguments for 'zadd' command")

    key = argv[0]
    data = argv[1:]

    try:
        for i in range(0, len(data), 2):
            data[i] = float(data[i])

            if math.isnan(data[i]):
                raise ValueError
    except ValueError:
        return encode_error("ERR invalid arguments for ZADD command")

    return encode_integer(store.zadd(key, data))

async def zscore(argv: list[bytes]) -> bytes:
    score = store.zscore(argv[0], argv[1])

    if score is not None:
        score = str(score).encode()

    return encode_bulk_string(score)

async def zrank(argv: list[bytes]) -> bytes:
    rank = store.zrank(argv[0], argv[1])

    if rank is None:
        return encode_bulk_string(None)

    return encode_integer(rank)

async def zrange(argv: list[bytes]) -> bytes:
    key, start, end = argv

    try:
        start = int(start.decode())
        end = int(end.decode())
    except ValueError:
        return encode_error("ERR Invalid arguments for ZRANGE command")

    return encode_array([encode_bulk_string(element) for element in store.zrange(key, start, end)])

async def zrangebyscore(argv: list[bytes]) -> bytes:
    key, start, end = argv

    try:
        start = float(start.decode())
        end = float(end.decode())
    except ValueError:
        return encode_error("ERR Invalid arguments for ZRANGEBYSCORE command")

    return encode_array([encode_bulk_string(element) for element in store.zrange_by_score(key, start, end)])

async def zrem(argv: list[bytes]) -> bytes:
    return encode_integer(store.zrem(argv[0], argv[1:]))

async def multi(argv: list[bytes], client: ClientState) -> bytes:
    if client.in_transaction:
        return encode_error("ERR MULTI calls can not be nested")

    client.start_transaction()
    return encode_simple_string("OK")

async def exec(argv: list[bytes], client: ClientState) -> bytes:
    if not client.in_transaction:
        return encode_error("ERR EXEC without MULTI")

    if client.abort_transaction:
        return encode_error("EXECABORT Transaction discarded because of previous errors")

    commands = client.get_commands()
    client.clear_transaction()
    responses = []

    for command in commands:
        # NOTE: atomicity depends on handlers not yielding internally
        response = await dispatch(command, client)
        responses.append(response)

    return encode_array(responses)

async def discard(argv: list[bytes], client: ClientState) -> bytes:
    if not client.in_transaction:
        return encode_error("ERR DISCARD without MULTI")

    client.clear_transaction()
    return encode_simple_string("OK")


# ---------------------------------------------------------------------------
# Command registry + dispatch.
# ---------------------------------------------------------------------------

class Command(NamedTuple):
    # Handlers come in two shapes:
    #   async def cmd(argv) -> bytes
    #   async def cmd(argv, client) -> bytes
    # The latter is used only by the transaction-control commands listed in
    # SKIP_QUEUE; dispatch() branches on that to decide which shape to call.
    # Callable[..., Awaitable[bytes]] accepts both.
    handler: Callable[..., Awaitable[bytes]]
    # The value type a key must already hold for this command to run. None
    # means the command is type-agnostic (DEL, EXISTS, TTL, ...) or takes no key.
    value_type: type | None = None
    # Allowed argc range, NOT counting the command name itself. max_args=None
    # means unbounded. Dispatch enforces this before queueing/executing.
    min_args: int = 0
    max_args: int | None = None


COMMANDS: dict[bytes, Command] = {
    b"PING":          Command(ping, None, 0, 1),
    b"ECHO":          Command(echo, None, 1, 1),
    b"GET":           Command(get, bytes, 1, 1),
    b"SET":           Command(set, bytes, 2, 4),
    b"DEL":           Command(delete, None, 1, None),
    b"EXISTS":        Command(exists, None, 1, None),
    b"INCRBY":        Command(incrby, bytes, 2, 2),
    b"DECRBY":        Command(decrby, bytes, 2, 2),
    b"INCR":          Command(incr, bytes, 1, 1),
    b"DECR":          Command(decr, bytes, 1, 1),
    b"APPEND":        Command(append, bytes, 2, 2),
    b"STRLEN":        Command(strlen, bytes, 1, 1),
    b"EXPIRE":        Command(expire, None, 2, 2),
    b"TTL":           Command(ttl, None, 1, 1),
    b"PERSIST":       Command(persist, None, 1, 1),
    b"LPUSH":         Command(lpush, deque, 2, None),
    b"RPUSH":         Command(rpush, deque, 2, None),
    b"LPOP":          Command(lpop, deque, 1, 2),
    b"RPOP":          Command(rpop, deque, 1, 2),
    b"LRANGE":        Command(lrange, deque, 3, 3),
    b"LLEN":          Command(llen, deque, 1, 1),
    b"HGET":          Command(hget, dict, 2, 2),
    b"HSET":          Command(hset, dict, 3, None),
    b"HDEL":          Command(hdel, dict, 2, None),
    b"HGETALL":       Command(hgetall, dict, 1, 1),
    b"HKEYS":         Command(hkeys, dict, 1, 1),
    b"HVALS":         Command(hvals, dict, 1, 1),
    b"HLEN":          Command(hlen, dict, 1, 1),
    b"ZADD":          Command(zadd, SortedSet, 3, None),
    b"ZSCORE":        Command(zscore, SortedSet, 2, 2),
    b"ZRANK":         Command(zrank, SortedSet, 2, 2),
    b"ZRANGE":        Command(zrange, SortedSet, 3, 3),
    b"ZRANGEBYSCORE": Command(zrangebyscore, SortedSet, 3, 3),
    b"ZREM":          Command(zrem, SortedSet, 2, None),
    b"MULTI":         Command(multi, None, 0, 0),
    b"EXEC":          Command(exec, None, 0, 0),
    b"DISCARD":       Command(discard, None, 0, 0),
}

SKIP_QUEUE = {b"MULTI", b"EXEC", b"DISCARD"}

async def dispatch(command_args: list[bytes], client: ClientState) -> bytes:
    if not command_args:
        return encode_error("ERR empty command")

    command_name = command_args[0].upper()
    command = COMMANDS.get(command_name)

    if command is None:
        if client.in_transaction:
            client.mark_transaction_as_aborted()
        return encode_error(f"ERR Invalid command '{command_name.decode()}'")

    argc = len(command_args) - 1   # not counting the command name itself
    # NOTE: `command.max_args is not None` (vs. just `command.max_args`) so
    # max_args=0 commands (MULTI/EXEC/DISCARD) still reject excess args.
    if argc < command.min_args or (command.max_args is not None and argc > command.max_args):
        if client.in_transaction:
            client.mark_transaction_as_aborted()
        return encode_error(
            f"ERR wrong number of arguments for '{command_name.decode().lower()}' command"
        )

    if client.in_transaction and command_name not in SKIP_QUEUE:
        client.add_command(command_args)
        return encode_simple_string("QUEUED")

    if (command.value_type is not None
            and argc >= 1
            and not store.is_valid_value_type(command_args[1], command.value_type)):
        return encode_error("WRONGTYPE Operation against a key holding the wrong kind of value")

    # Transaction-control handlers take (argv, client); everything else just (argv).
    if command_name in SKIP_QUEUE:
        return await command.handler(command_args[1:], client)
    return await command.handler(command_args[1:])
