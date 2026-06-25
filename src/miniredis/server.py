import asyncio
import structlog

from miniredis.protocol import read_command, ProtocolError, encode_error, FileStreamReader, read_aof_command
from miniredis.commands import dispatch, replay_dispatch
from miniredis.store import store, expiration_sweeper, schedule_snapshot
from miniredis.client import ClientState
from miniredis.config import get_settings
from miniredis.aof import get_main_aof, append_to_aof


logger = structlog.get_logger()
settings = get_settings()

async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    logger.info("Connection estabilished with client")
    client = ClientState(writer)
    writer_task = asyncio.create_task(client.write_to_socket())

    try:
        while True:
            command_args = await read_command(reader)
            response, append_cmd = await dispatch(command_args, client)
            if response is not None:
                client.write_to_buffer(response)
                if append_cmd:
                    append_to_aof(append_cmd)
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
    await replay_commands()
    main_aof = get_main_aof()
    main_aof.open()
    server = await asyncio.start_server(handle_request, host, port)
    logger.info("Server started")
    asyncio.create_task(expiration_sweeper(store))
    asyncio.create_task(schedule_snapshot(store))

    async with server:
        await server.serve_forever()

async def replay_commands() -> None:
    settings = get_settings()
    try:
        with open(settings.aof_main_file_path, 'r+b') as aof:
            reader = FileStreamReader(aof)
            state = {
                "in_transaction": False,
                "queue": []
            }
            while True:
                try:
                    command_args = read_aof_command(reader)
                    if command_args is None:
                        return
                except EOFError as e:
                    logger.error(f"Reached EOF in-between reading a command: {e}")
                    aof.truncate(reader.get_last_good_offset())
                    break
                await replay_dispatch(command_args, state)
    except FileNotFoundError:
        logger.error("Skipped AOF load since AOF file wasn't found at the configured path.")
        pass
    except Exception as e:
        logger.error(f"AOF load failed because of error: {e}")
        raise
