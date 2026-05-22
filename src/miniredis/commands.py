from miniredis.protocol import encode_error, encode_simple_string, encode_bulk_string


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


COMMANDS = {
    b"PING": ping,
    b"ECHO": echo
}

async def dispatch(command_args: list[bytes]) -> bytes:
    if not command_args:
        return encode_error("ERR empty command")
    
    command_name = command_args[0].upper()
    command_handler = COMMANDS.get(command_name)

    if not command_handler:
        return encode_error("ERR Invalid command '{command_name}'")
    
    return await command_handler(command_args[1:])
