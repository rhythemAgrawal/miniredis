import asyncio

from miniredis.protocol import read_command
from miniredis.commands import dispatch


async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            command_args = await read_command(reader)
            response = await dispatch(command_args)
            writer.write(response)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()

async def main(host="127.0.0.1", port=6380):
    server = await asyncio.start_server(handle_request, host, port)

    async with server:
        await server.serve_forever()