import pytest
import asyncio

from miniredis.protocol import read_command

@pytest.mark.asyncio
async def test_parses_set_command():
    raw = b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n"
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    argv = await read_command(reader)
    assert argv == [b"SET", b"foo", b"bar"]
