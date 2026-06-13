import time
import math
import asyncio
import sys
import os
import gc
import signal
from collections import deque
from pathlib import Path

from miniredis.custom_data_structures import RandomDict, SortedSet
from miniredis import _rdb
from miniredis.config import config


snapshot_pid = None

class Store:
    def __init__(self):
        self._data: dict[bytes, bytes | deque[bytes] | dict[bytes, bytes] | SortedSet] = {}
        self._ttl = RandomDict()
    
    def sample_and_expire(self) -> None:
        sample = self._ttl.get_sample(10)

        if sample:
            self.exists(*sample)
    
    async def snapshot(self) -> None:
        async def child_cleaner():
            global snapshot_pid
            try:
                start = time.monotonic()
                while time.monotonic() < start + config.max_save_timeout:
                    await asyncio.sleep(0.1)
                    pid, status = os.waitpid(snapshot_pid, os.WNOHANG)

                    if pid == 0:
                        continue

                    if os.WIFEXITED(status):
                        print(f"child {pid} exited, code {os.WEXITSTATUS(status)}")
                    elif os.WIFSIGNALED(status):
                        print(f"child {pid} killed by signal {os.WTERMSIG(status)}")
                    
                    return
                
                os.kill(snapshot_pid, signal.SIGKILL)
                os.waitpid(snapshot_pid, 0)
            except Exception as e:
                print("Error while running snapshot process cleaner: %s" % e)
            finally:
                snapshot_pid = None
                gc.unfreeze()

        global snapshot_pid
        
        if snapshot_pid:
            print("Snapshot save already in progress")
            return

        path = config.snapshot_path
        gc.freeze()
        pid = os.fork()

        if pid == 0:
            # Child process
            gc.disable()

            try:
                _rdb.dump(self._data, self._ttl, path)
            except Exception as e:
                print("Snapshot save gave error: %s" % e)
                os._exit(1)

            os._exit(0)
        else:
            snapshot_pid = pid
            await child_cleaner()

    def is_valid_value_type(self, key: bytes, allowed_type: type) -> bool:
        if not self.exists(key):
            return True
        
        return type(self._data.get(key)) is allowed_type
    
    def get(self, key: bytes) -> bytes | None:
        if self.exists(key):
            return self._data.get(key)
        
        return None

    def set(self, key: bytes, value: bytes, ttl: float | None = None) -> None:
        self._data[key] = value

        if ttl is not None:
            self.expire(key, ttl)
        else:
            self._ttl.delete(key)
    
    def delete(self, *keys: bytes) -> int:
        count = 0

        for key in keys:
            if self.exists(key):
                del self._data[key]
                self._ttl.delete(key)
                count += 1

        return count

    def exists(self, *keys: bytes) -> int:
        count = 0

        for key in keys:
            if key in self._data:
                if self._ttl.get(key, float('inf')) < time.time():
                    del self._data[key]
                    self._ttl.delete(key)
                else:
                    count += 1
        
        return count
    
    def _change_by(self, key: bytes, delta: int) -> int:
        curr = self.get(key)
        curr_val = 0 if curr is None else int(curr.decode())
        new_val = curr_val + delta
        self._data[key] = str(new_val).encode()
        return new_val
    
    def incr_by(self, key: bytes, incr_count: int) -> int:
        return self._change_by(key, incr_count)
    
    def decr_by(self, key: bytes, decr_count: int) -> int:
        return self._change_by(key, -1 * decr_count)
    
    def incr(self, key: bytes) -> int:
        return self.incr_by(key, 1)
    
    def decr(self, key: bytes) -> int:
        return self.decr_by(key, 1)
    
    def append(self, key: bytes, value: bytes) -> int:
        curr = self._data.get(key) if self.exists(key) else b""
        new_val = curr + value
        self._data[key] = new_val
        return len(new_val)
    
    def strlen(self, key: bytes) -> int:
        value = self._data.get(key) if self.exists(key) else b""
        return len(value)
    
    def expire(self, key: bytes, ttl: int | float) -> int:
        if not self.exists(key):
            return 0
        
        self._ttl.set(key, time.time() + ttl)
        return 1
    
    def ttl(self, key: bytes) -> int:
        if not self.exists(key):
            return -2
        elif not self._ttl.get(key):
            return -1
        else:
            return math.floor(self._ttl.get(key) - time.time())
        
    def persist(self, key: bytes) -> int:
        if not self.exists(key) or not self._ttl.get(key):
            return 0
        
        self._ttl.delete(key)
        return 1
    
    def _list_get(self, key: bytes) -> deque | None:
        curr = None

        if self.exists(key):
            curr = self._data.get(key)

        return curr
    
    def lpush(self, key: bytes, elements: list[bytes]) -> int:
        curr = self._list_get(key)

        if curr is None:
            self._data[key] = deque()
            curr = self._data[key]

        curr.extendleft(elements)
        return len(curr)
    
    def rpush(self, key: bytes, elements: list[bytes]) -> int:
        curr = self._list_get(key)

        if curr is None:
            self._data[key] = deque()
            curr = self._data[key]

        curr.extend(elements)
        return len(curr)
    
    def lpop(self, key: bytes, count: int=1) -> list[bytes] | None:
        curr = self._list_get(key)
        popped = []

        if curr is None:
            return None
        
        pop_count = min(count, len(curr))

        for _ in range(pop_count):
            popped.append(curr.popleft())
        
        if not len(curr):
            self.delete(key)
        
        return popped
    
    def rpop(self, key: bytes, count: int=1) -> list[bytes] | None:
        curr = self._list_get(key)
        popped = []

        if curr is None:
            return None
        
        pop_count = min(count, len(curr))

        for _ in range(pop_count):
            popped.append(curr.pop())
        
        if not len(curr):
            self.delete(key)
        
        return popped
    
    def lrange(self, key: bytes, start: int, end: int) -> list[bytes]:
        curr = list(self._list_get(key) or deque())
        end = len(curr) if end == -1 else end+1
        return curr[start:end]
    
    def llen(self, key: bytes) -> int:
        curr = self._list_get(key) or deque()
        return len(curr)
    
    def _get_hash(self, key) -> dict | None:
        return self._data.get(key) if self.exists(key) else None
    
    def hget(self, key: bytes, field: bytes) -> bytes | None:
        hash = self._get_hash(key)

        if not hash or field not in hash:
            return
        
        return hash.get(field)
    
    def hset(self, key: bytes, field_value_pairs: list[bytes]) -> int:
        hash = self._get_hash(key)

        if hash is None:
            hash = {}
            self._data[key] = hash

        count = 0
        
        for i in range(0, len(field_value_pairs), 2):
            field = field_value_pairs[i]
            value = field_value_pairs[i+1]

            if field not in hash:
                count += 1
            
            hash[field] = value
        
        return count
    
    def hdel(self, key: bytes, fields: list[bytes]) -> int:
        hash = self._get_hash(key)

        if not hash:
            return 0

        count = 0

        for field in fields:
            if field in hash:
                del hash[field]
                count += 1
        
        if not len(hash):
            self.delete(key)

        return count
    
    def hgetall(self, key: bytes) -> list[bytes]:
        hash = self._get_hash(key)

        if not hash:
            return []
        
        all_data = []

        for field, value in hash.items():
            all_data.extend([field, value])
        
        return all_data
    
    def hkeys(self, key: bytes) -> list[bytes]:
        hash = self._get_hash(key)

        if not hash:
            return []
        
        return list(hash.keys())
    
    def hvals(self, key: bytes) -> list[bytes]:
        hash = self._get_hash(key)

        if not hash:
            return []
        
        return list(hash.values())
    
    def hlen(self, key: bytes) -> int:
        hash = self._get_hash(key)

        if not hash:
            return 0
        
        return len(hash)
    
    def _get_sorted_set(self, key: bytes) -> SortedSet | None:
        return self._data.get(key) if self.exists(key) else None
    
    def zadd(self, key: bytes, data: list) -> int:
        sorted_set = self._get_sorted_set(key)

        if sorted_set is None:
            sorted_set = SortedSet()
            self._data[key] = sorted_set
        
        count = 0

        for i in range(0, len(data), 2):
            score, member = data[i], data[i+1]
            existing_score = sorted_set.get_score(member)

            if existing_score is not None:
                sorted_set.delete(existing_score, member)
            else:
                count += 1
            
            sorted_set.insert(score, member)
        
        return count
    
    def zscore(self, key: bytes, member: bytes) -> float | None:
        sorted_set = self._get_sorted_set(key)

        if sorted_set is None:
            return None
        
        return sorted_set.get_score(member)
    
    def zrank(self, key: bytes, member: bytes) -> int | None:
        score = self.zscore(key, member)

        if score is None:
            return None
        
        sorted_set = self._get_sorted_set(key)
        return sorted_set.get_rank(score, member)
    
    def zrange(self, key: bytes, start: int, end: int) -> list[bytes]:
        sorted_set = self._get_sorted_set(key)

        if sorted_set is None:
            return []
        
        return sorted_set.get_range_by_rank(start, end)
    
    def zrange_by_score(self, key: bytes, start: float, end: float) -> list[bytes]:
        sorted_set = self._get_sorted_set(key)

        if sorted_set is None:
            return []
        
        return sorted_set.get_range_by_score(start, end)
    
    def zrem(self, key: bytes, members: list[bytes]) -> int:
        sorted_set = self._get_sorted_set(key)

        if sorted_set is None:
            return 0
        
        count = 0
        
        for member in members:
            score = sorted_set.get_score(member)

            if score is not None:
                sorted_set.delete(score, member)
                count += 1

        return count

async def expiration_sweeper(store: Store) -> None:
    while True:
        await asyncio.sleep(1)
        store.sample_and_expire()

async def schedule_snapshot(store: Store) -> None:
    while True:
        await asyncio.sleep(3600)

        try:
            await store.snapshot()
        except Exception as e:
            print("Error while running snapshot: %s" % e)

def get_store() -> Store:
    path = Path(config.snapshot_path)
    store = Store()

    if path.is_file():
        try:
            data, ttl = _rdb.load(str(path))
            store._data = data
            store._ttl = ttl
        except Exception as e:
            # To-do: Log this exception after logging is enabled
            print(f"snapshot load failed: {e}", file=sys.stderr)
    
    return store

store = get_store()
