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
    if prefix[:1] != b"*":
        raise ProtocolError(f"Expected prefix character for array but instead got '{prefix[0:1].decode()}'.")
    array_count = int(prefix[1:-2].decode())
    decoded = []

    for _ in range(array_count):
        prefix = await reader.readuntil(separator=b"\r\n")
        if prefix[:1] != b"$":
            raise ProtocolError(f"Expected prefix character for bulk string but instead got '{prefix[0:1].decode()}'.")
        byte_length = int(prefix[1:-2].decode())
        cmd_arg = await reader.readexactly(byte_length)
        decoded.append(cmd_arg)
        crlf = await reader.readexactly(2)

        if crlf != b"\r\n":
            raise ProtocolError(f"expected CRLF after the bulk string, got {crlf!r}")

    return decoded

def resp_encode_command(command_args: list[bytes]) -> bytes:
    encoded_args = []

    for arg in command_args:
        encoded_args.append(encode_bulk_string(arg))
    
    return encode_array(encoded_args)

class FileStreamReader:
    CHUNK_SIZE = 1 << 16

    def __init__(self, file):
        self.file = file
        self.buf = bytearray()
        self.last_good_offset = 0
    
    def _fill(self) -> bool:
        chunk = self.file.read(self.CHUNK_SIZE)
        if not chunk:
            return False
        self.buf += chunk
        return True
    
    def _at_eof(self) -> bool:
        return not self.buf and not self._fill()
    
    def read_exactly(self, n: int) -> bytes:
        while len(self.buf) < n:
            if not self._fill():
                raise EOFError(f"Expected {n} bytes but only got {len(self.buf)} bytes.")
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data
    
    def read_until(self, separator: bytes = b"\r\n") -> bytes:
        start = 0
        while True:
            index = self.buf.find(separator, start)
            if index != -1:
                data = bytes(self.buf[:index+len(separator)])
                del self.buf[:index+len(separator)]
                return data
            start = max(0, len(self.buf) - len(separator) + 1)
            if not self._fill():
                raise EOFError(f"Reached EOF before finding separator the '{separator.decode()}'.")
    
    def set_last_good_offset(self) -> None:
        self.last_good_offset = self.file.tell() - len(self.buf)
    
    def get_last_good_offset(self) -> int:
        return self.last_good_offset

def read_aof_command(f: FileStreamReader) -> list[bytes] | None:
    if f._at_eof():
        return None

    prefix = f.read_until(b"\r\n")
    if prefix[:1] != b"*":
        raise ProtocolError(f"Expected prefix character for array but instead got '{prefix[0:1].decode()}'.")
    array_count = int(prefix[1:-2].decode())
    command_args = []

    for _ in range(array_count):
        bulk_string_prefix = f.read_until(b"\r\n")
        if bulk_string_prefix[:1] != b"$":
            raise ProtocolError(f"Expected prefix character for bulk string but instead got '{bulk_string_prefix[0:1].decode()}'.")
        string_length = int(bulk_string_prefix[1:-2])
        arg = f.read_exactly(string_length)
        command_args.append(arg)
        crlf = f.read_exactly(2)

        if crlf != b"\r\n":
            raise ProtocolError(f"expected CRLF after the bulk string, got {crlf!r}")
    
    f.set_last_good_offset()
    return command_args
