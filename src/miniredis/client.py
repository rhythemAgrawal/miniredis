from collections import deque
from asyncio import Queue, StreamWriter

from miniredis.pubsub import ClientChannels


class ClientState:
    def __init__(self, writer: StreamWriter):
        self.in_transaction: bool = False
        self.queue: deque[list[bytes]] = deque()
        self.abort_transaction: bool = False
        self.write_buffer: Queue[bytes] = Queue()
        self.channels = ClientChannels(self)
        self.writer: StreamWriter = writer

    def add_command(self, command: list[bytes]) -> None:
        self.queue.append(command)
    
    def get_commands(self) -> list[list[bytes]]:
        return list(self.queue)
    
    def clear_transaction(self) -> None:
        self.in_transaction = False
        self.abort_transaction = False
        self.queue.clear()
    
    def start_transaction(self) -> None:
        self.in_transaction = True
        self.abort_transaction = False
        self.queue.clear()

    def mark_transaction_as_aborted(self) -> None:
        self.abort_transaction = True

    @property
    def is_subscribed(self) -> bool:
        return bool(self.channels.get_channel_count())
    
    def write_to_buffer(self, message: bytes | None) -> None:
        self.write_buffer.put_nowait(message)

    async def write_to_socket(self) -> None:
        while True:
            response = await self.write_buffer.get()
            if response is None:
                return
            self.writer.write(response)
            await self.writer.drain()