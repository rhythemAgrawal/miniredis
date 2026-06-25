from functools import cache
import os
import threading
import atexit

from miniredis.config import get_settings


class AOF:
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        settings = get_settings()
        self.fsync_policy = settings.fsync_policy

        if self.fsync_policy == "EVERYSEC":
            atexit.register(self.close)
    
    def periodic_fsync(self) -> None:
        while not self.event.wait(self.interval):
            with self.lock:
                if not self.is_dirty:
                    continue
                self.file.flush()
                os.fsync(self.file.fileno())
                self.is_dirty = False

    def open(self) -> None:
        self.file = open(self.file_path, 'ab')

        if self.fsync_policy == "EVERYSEC":
            self.lock = threading.Lock()
            self.is_dirty = False
            self.event = threading.Event()
            self.interval = 1
            self.thread = threading.Thread(target=self.periodic_fsync, daemon=True)
            self.thread.start()

    def write(self, data: bytes) -> None:
        if self.fsync_policy == "EVERYSEC":
            with self.lock:
                self.file.write(data)
                self.is_dirty = True
        else:
            self.file.write(data)

        if self.fsync_policy == "ALWAYS":
            self.file.flush()
            os.fsync(self.file.fileno())

    def close(self) -> None:
        if getattr(self, "file", None) is None or self.file.closed:
            return
        if self.fsync_policy == "EVERYSEC":
            self.event.set()
            self.thread.join()
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()

@cache
def get_main_aof() -> AOF:
    settings = get_settings()
    return AOF(settings.aof_main_file_path)

@cache
def get_temp_aof() -> AOF:
    settings = get_settings()
    return AOF(settings.aof_temp_file_path)

def append_to_aof(data: bytes) -> None:
    from miniredis.store import is_dump_in_progress

    main_aof = get_main_aof()
    temp_aof = get_temp_aof()

    main_aof.write(data)
    if is_dump_in_progress():
        temp_aof.write(data)
