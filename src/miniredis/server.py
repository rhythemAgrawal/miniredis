import asyncio

from miniredis.protocol import read_command, ProtocolError, encode_error
from miniredis.commands import dispatch
from miniredis.store import store, expiration_sweeper


async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            command_args = await read_command(reader)
            response = await dispatch(command_args)
            writer.write(response)
            await writer.drain()
    except ProtocolError as e:
        writer.write(encode_error(f"ERR Protocol error: {e}"))
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()

async def main(host="127.0.0.1", port=6380):
    server = await asyncio.start_server(handle_request, host, port)
    asyncio.create_task(expiration_sweeper(store))

    async with server:
        await server.serve_forever()
