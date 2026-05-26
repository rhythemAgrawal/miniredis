"""Unit tests for the RESP encoders and the command parser.

The parser is exercised with an in-memory ``asyncio.StreamReader`` fed raw
bytes, so no socket or server is involved.
"""
import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from miniredis.protocol import (
    ProtocolError,
    encode_array,
    encode_bulk_string,
    encode_error,
    encode_integer,
    encode_simple_string,
    read_command,
)


# --- helpers -------------------------------------------------------------

def _resp_command(*args: bytes) -> bytes:
    """Encode argv as a RESP array of bulk strings, the way a client sends it."""
    out = b"*" + str(len(args)).encode() + b"\r\n"
    for arg in args:
        out += b"$" + str(len(arg)).encode() + b"\r\n" + arg + b"\r\n"
    return out


def _reader_from(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


# --- encoders ------------------------------------------------------------

class TestEncoders:
    def test_simple_string(self):
        assert encode_simple_string("OK") == b"+OK\r\n"

    def test_error(self):
        assert encode_error("ERR bad request") == b"-ERR bad request\r\n"

    def test_integer(self):
        assert encode_integer(42) == b":42\r\n"

    def test_negative_integer(self):
        assert encode_integer(-2) == b":-2\r\n"

    def test_bulk_string(self):
        assert encode_bulk_string(b"hello") == b"$5\r\nhello\r\n"

    def test_empty_bulk_string(self):
        assert encode_bulk_string(b"") == b"$0\r\n\r\n"

    def test_nil_bulk_string(self):
        assert encode_bulk_string(None) == b"$-1\r\n"

    def test_bulk_string_is_binary_safe(self):
        # Length framing must hold even when the payload contains CRLF / NUL.
        payload = b"a\r\nb\x00c"
        assert encode_bulk_string(payload) == b"$6\r\n" + payload + b"\r\n"

    def test_array_concatenates_preencoded_items(self):
        items = [encode_bulk_string(b"a"), encode_bulk_string(b"bb")]
        assert encode_array(items) == b"*2\r\n$1\r\na\r\n$2\r\nbb\r\n"

    def test_empty_array(self):
        assert encode_array([]) == b"*0\r\n"


# --- parser --------------------------------------------------------------

class TestReadCommand:
    async def test_parses_multi_arg_command(self):
        argv = await read_command(_reader_from(_resp_command(b"SET", b"foo", b"bar")))
        assert argv == [b"SET", b"foo", b"bar"]

    async def test_parses_single_element(self):
        argv = await read_command(_reader_from(_resp_command(b"PING")))
        assert argv == [b"PING"]

    async def test_parses_empty_bulk_string_argument(self):
        argv = await read_command(_reader_from(_resp_command(b"SET", b"k", b"")))
        assert argv == [b"SET", b"k", b""]

    async def test_preserves_binary_payload(self):
        payload = b"\x00\x01\r\n\xff"
        argv = await read_command(_reader_from(_resp_command(b"SET", b"k", payload)))
        assert argv == [b"SET", b"k", payload]

    async def test_two_commands_back_to_back_on_one_stream(self):
        reader = _reader_from(_resp_command(b"PING") + _resp_command(b"GET", b"k"))
        assert await read_command(reader) == [b"PING"]
        assert await read_command(reader) == [b"GET", b"k"]

    async def test_raises_protocol_error_on_bad_trailing_crlf(self):
        # A bulk-string body that is not followed by \r\n is a protocol violation.
        malformed = b"*1\r\n$3\r\nfoo\x00\x00"
        with pytest.raises(ProtocolError):
            await read_command(_reader_from(malformed))

    async def test_raises_on_truncated_stream(self):
        # Declares 3 bytes of body but supplies 1, then hits EOF.
        truncated = b"*1\r\n$3\r\nf"
        with pytest.raises(asyncio.IncompleteReadError):
            await read_command(_reader_from(truncated))


# --- property: the parser is the exact inverse of the client encoding ----

@settings(max_examples=100)
@given(st.lists(st.binary(max_size=32), min_size=1, max_size=8))
def test_read_command_roundtrip(argv):
    """Any argv encoded as a RESP array must parse back to the same argv."""

    async def _parse() -> list[bytes]:
        return await read_command(_reader_from(_resp_command(*argv)))

    assert asyncio.run(_parse()) == argv
