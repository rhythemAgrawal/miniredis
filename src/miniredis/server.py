import asyncio
import structlog

from miniredis.protocol import read_command, ProtocolError, encode_error
from miniredis.commands import dispatch
from miniredis.store import store, expiration_sweeper, schedule_snapshot
from miniredis.client import ClientState
from miniredis.config import get_settings


logger = structlog.get_logger()
settings = get_settings()

async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    logger.info("Connection estabilished with client")
    client = ClientState(writer)
    writer_task = asyncio.create_task(client.write_to_socket())

    try:
        while True:
            command_args = await read_command(reader)
            response = await dispatch(command_args, client)
            if response is not None:
                client.write_to_buffer(response)
    except ProtocolError as e:
        client.write_to_buffer(encode_error(f"ERR Protocol error: {e}"))
    except (asyncio.IncompleteReadError, ConnectionError): # Will exception handling still work now that we are first writing to the buffer?
        pass
    finally:
        client.channels.unsub_channel([])
        client.channels.unsub_pchannel([])
        client.write_to_buffer(None)
        try:
            await asyncio.wait_for(writer_task, timeout=settings.buffer_drain_timeout)
        except TimeoutError:
            writer_task.cancel()
        writer.close()
        await writer.wait_closed()
        logger.info("Connection closed with client")

async def main(host="127.0.0.1", port=6380):
    server = await asyncio.start_server(handle_request, host, port)
    logger.info("Server started")
    asyncio.create_task(expiration_sweeper(store))
    asyncio.create_task(schedule_snapshot(store))

    async with server:
        await server.serve_forever()
