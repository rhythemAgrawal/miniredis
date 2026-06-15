from collections import deque

class ClientState:
    def __init__(self):
        self.in_transaction: bool = False
        self.queue: deque[list[bytes]] = deque()
        self.abort_transaction: bool = False

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
