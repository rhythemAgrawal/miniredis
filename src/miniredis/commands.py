from collections import deque
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from miniredis.protocol import encode_error, encode_simple_string, encode_bulk_string, encode_integer, encode_array
from miniredis.store import store


async def ping(argv: list[bytes]) -> bytes:
    if len(argv) == 1:
        return encode_bulk_string(argv[0])
    elif len(argv) == 0:
        return encode_simple_string("PONG")
    
    return encode_error("ERR Wrong number of arguements for PING command")

async def echo(argv: list[bytes]) -> bytes:
    if len(argv) == 1:
        return encode_bulk_string(argv[0])
    
    return encode_error("ERR Wrong number of arguements for ECHO command")

async def get(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for GET command")
    
    return encode_bulk_string(store.get(argv[0]))

async def set(argv: list[bytes]) -> bytes:
    if len(argv) == 2:
        store.set(argv[0], argv[1])
        return encode_simple_string("OK")
    elif len(argv) == 4 and argv[2].upper() in [b"EX", b"PX"]:
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
    
    return encode_error("ERR Wrong number of arguements for SET command")

async def delete(argv: list[bytes]) -> bytes:
    if len(argv) == 0:
        return encode_error("ERR Wrong number of arguements for DEL command")
    
    return encode_integer(store.delete(*argv))

async def exists(argv: list[bytes]) -> bytes:
    if len(argv) == 0:
        return encode_error("ERR Wrong number of arguements for EXISTS command")
    
    return encode_integer(store.exists(*argv))

async def incrby(argv: list[bytes]) -> bytes:
    if len(argv) != 2:
        return encode_error("ERR Wrong number of arguements for INCRBY command")
    
    try:
        new_val = store.incr_by(argv[0], int(argv[1].decode()))
    except ValueError:
        return encode_error("ERR Wrong value type for this operation")
    
    return encode_integer(new_val)

async def decrby(argv: list[bytes]) -> bytes:
    if len(argv) != 2:
        return encode_error("ERR Wrong number of arguements for DECRBY command")
    
    try:
        new_val = store.decr_by(argv[0], int(argv[1].decode()))
    except ValueError:
        return encode_error("ERR Wrong value type for this operation")
    
    return encode_integer(new_val)

async def incr(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for INCR command")
    
    try:
        new_val = store.incr(argv[0])
    except ValueError:
        return encode_error("ERR Wrong value type for this operation")
    
    return encode_integer(new_val)

async def decr(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for DECR command")
    
    try:
        new_val = store.decr(argv[0])
    except ValueError:
        return encode_error("ERR Wrong value type for this operation")
    
    return encode_integer(new_val)

async def append(argv: list[bytes]) -> bytes:
    if len(argv) != 2:
        return encode_error("ERR Wrong number of arguements for APPEND command")
    
    return encode_integer(store.append(argv[0], argv[1]))

async def strlen(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for STRLEN command")
    
    return encode_integer(store.strlen(argv[0]))

async def expire(argv: list[bytes]) -> bytes:
    if len(argv) != 2:
        return encode_error("ERR Wrong number of arguements for EXPIRE command")
    
    try:
        ttl = float(argv[1].decode())
    except ValueError:
        return encode_error("ERR Wrong ttl type")
    
    if ttl <= 0:
        return encode_error("ERR Invalid ttl value for EXPIRE command")
    
    return encode_integer(store.expire(argv[0], ttl))

async def ttl(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for TTL command")
    
    return encode_integer(store.ttl(argv[0]))

async def persist(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for PERSIST command")
    
    return encode_integer(store.persist(argv[0]))

async def lpush(argv: list[bytes]) -> bytes:
    if len(argv) < 2:
        return encode_error("ERR Wrong number of arguements for LPUSH command")
    
    return encode_integer(store.lpush(argv[0], argv[1:]))

async def rpush(argv: list[bytes]) -> bytes:
    if len(argv) < 2:
        return encode_error("ERR Wrong number of arguements for RPUSH command")
    
    return encode_integer(store.rpush(argv[0], argv[1:]))

async def lpop(argv: list[bytes]) -> bytes:
    if len(argv) > 2:
        return encode_error("ERR Wrong number of arguements for LPOP command")
    
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
    else:
        return encode_bulk_string(popped if popped is None else popped[0])

async def rpop(argv: list[bytes]) -> bytes:
    if len(argv) > 2:
        return encode_error("ERR Wrong number of arguements for RPOP command")
    
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
    else:
        return encode_bulk_string(popped if popped is None else popped[0])

async def lrange(argv: list[bytes]) -> bytes:
    if len(argv) != 3:
        return encode_error("ERR Wrong number of arguements for LRANGE command")
    
    try:
        start = int(argv[1].decode())
        end = int(argv[2].decode())
    except ValueError:
        return encode_error("ERR Invalid arguments for the LRANGE command")
    
    return encode_array([encode_bulk_string(element) for element in store.lrange(argv[0], start, end)])

async def llen(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for LLEN command")
    
    return encode_integer(store.llen(argv[0]))

async def hget(argv: list[bytes]) -> bytes:
    if len(argv) != 2:
        return encode_error("ERR Wrong number of arguements for HGET command")
    
    return encode_bulk_string(store.hget(argv[0], argv[1]))

async def hset(argv: list[bytes]) -> bytes:
    if len(argv) < 3 or len(argv) % 2 == 0:
        return encode_error("ERR Wrong number of arguements for HSET command")
    
    return encode_integer(store.hset(argv[0], argv[1:]))

async def hdel(argv: list[bytes]) -> bytes:
    if len(argv) < 2:
        return encode_error("ERR Wrong number of arguements for HDEL command")
    
    return encode_integer(store.hdel(argv[0], argv[1:]))

async def hgetall(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for HGETALL command")
    
    return encode_array([encode_bulk_string(element) for element in store.hgetall(argv[0])])

async def hkeys(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for HKEYS command")
    
    return encode_array([encode_bulk_string(element) for element in store.hkeys(argv[0])])

async def hvals(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for HVALS command")
    
    return encode_array([encode_bulk_string(element) for element in store.hvals(argv[0])])

async def hlen(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for HLEN command")
    
    return encode_integer(store.hlen(argv[0]))


class Command(NamedTuple):
    handler: Callable[[list[bytes]], Awaitable[bytes]]
    # The value type a key must already hold for this command to run. None means
    # the command is type-agnostic (DEL, EXISTS, TTL, ...) or takes no key.
    value_type: type | None = None


COMMANDS: dict[bytes, Command] = {
    b"PING": Command(ping),
    b"ECHO": Command(echo),
    b"GET": Command(get, bytes),
    b"SET": Command(set, bytes),
    b"DEL": Command(delete),
    b"EXISTS": Command(exists),
    b"INCRBY": Command(incrby, bytes),
    b"DECRBY": Command(decrby, bytes),
    b"INCR": Command(incr, bytes),
    b"DECR": Command(decr, bytes),
    b"APPEND": Command(append, bytes),
    b"STRLEN": Command(strlen, bytes),
    b"EXPIRE": Command(expire),
    b"TTL": Command(ttl),
    b"PERSIST": Command(persist),
    b"LPUSH": Command(lpush, deque),
    b"RPUSH": Command(rpush, deque),
    b"LPOP": Command(lpop, deque),
    b"RPOP": Command(rpop, deque),
    b"LRANGE": Command(lrange, deque),
    b"LLEN": Command(llen, deque),
    b"HGET": Command(hget, dict),
    b"HSET": Command(hset, dict),
    b"HDEL": Command(hdel, dict),
    b"HGETALL": Command(hgetall, dict),
    b"HKEYS": Command(hkeys, dict),
    b"HVALS": Command(hvals, dict),
    b"HLEN": Command(hlen, dict),
}

async def dispatch(command_args: list[bytes]) -> bytes:
    if not command_args:
        return encode_error("ERR empty command")
    
    command_name = command_args[0].upper()
    command = COMMANDS.get(command_name)

    if command is None:
        return encode_error(f"ERR Invalid command '{command_name.decode()}'")

    if (command.value_type is not None and
        len(command_args) >= 2 and
        not store.is_valid_value_type(command_args[1], command.value_type)):
        return encode_error("WRONGTYPE Operation against a key holding the wrong kind of value")

    return await command.handler(command_args[1:])
