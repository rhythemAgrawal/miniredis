from miniredis.protocol import encode_error, encode_simple_string, encode_bulk_string, encode_integer
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
        
        if argv[2].upper() == "EX":
            ttl *= 1000
        
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
    
    return encode_integer(store.expire(argv[0], float(argv[1].decode())))

async def ttl(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for TTL command")
    
    return encode_integer(store.ttl(argv[0]))

async def persist(argv: list[bytes]) -> bytes:
    if len(argv) != 1:
        return encode_error("ERR Wrong number of arguements for PERSIST command")
    
    return encode_integer(store.persist(argv[0]))


COMMANDS = {
    b"PING": ping,
    b"ECHO": echo,
    b"GET": get,
    b"SET": set,
    b"DEL": delete,
    b"EXISTS": exists,
    b"INCRBY": incrby,
    b"DECRBY": decrby,
    b"INCR": incr,
    b"DECR": decr,
    b"APPEND": append,
    b"STRLEN": strlen,
    b"EXPIRE": expire,
    b"TTL": ttl,
    b"PERSIST": persist
}

async def dispatch(command_args: list[bytes]) -> bytes:
    if not command_args:
        return encode_error("ERR empty command")
    
    command_name = command_args[0].upper()
    command_handler = COMMANDS.get(command_name)

    if not command_handler:
        return encode_error("ERR Invalid command '{command_name}'")
    
    return await command_handler(command_args[1:])
