import asyncio


class ProtocolError(Exception):
    pass

def encode_simple_string(s: str) -> bytes:
    encoded = "+" + s + "\r\n"
    return encoded.encode()

def encode_error(msg: str) -> bytes:
    encoded = "-" + msg + "\r\n"
    return encoded.encode()

def encode_integer(n: int) -> bytes:
    encoded = ":" + str(n) + "\r\n"
    return encoded.encode()

def encode_bulk_string(s: bytes | None) -> bytes:
    if s is None:
        return "$-1\r\n".encode()

    size = str(len(s))
    prefix = "$" + size + "\r\n"
    suffix = "\r\n"
    return prefix.encode() + s + suffix.encode()

def encode_array(items: list[bytes] | None) -> bytes:
    if items is None:
        return "*-1\r\n".encode()

    prefix = "*" + str(len(items)) + "\r\n"
    return prefix.encode() + b"".join(items)

async def read_command(reader: asyncio.StreamReader) -> list[bytes]:
    prefix = await reader.readuntil(separator=b"\r\n")
    array_count = int(prefix[1:-2].decode())
    decoded = []

    for _ in range(array_count):
        prefix = await reader.readuntil(separator=b"\r\n")
        byte_length = int(prefix[1:-2].decode())
        cmd_arg = await reader.readexactly(byte_length)
        decoded.append(cmd_arg)
        crlf = await reader.readexactly(2)

        if crlf != b"\r\n":
            raise ProtocolError(f"expected CRLF after the bulk string, got {crlf!r}")

    return decoded
