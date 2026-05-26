import time
import math
import asyncio
from collections import deque

from miniredis.custom_data_structures import RandomDict

class Store:
    def __init__(self):
        self._data: dict[bytes, bytes | deque[bytes] | dict[bytes, bytes]] = {}
        self._ttl = RandomDict()
    
    def sample_and_expire(self) -> None:
        sample = self._ttl.get_sample(10)

        if sample:
            self.exists(*sample)
    
    def is_valid_value_type(self, key: bytes, allowed_type: type) -> bool:
        value = self._data.get(key) if self.exists(key) else None

        if not value:
            return True
        
        return type(value) == allowed_type
    
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

        if not curr:
            self._data[key] = deque()
            curr = self._data[key]

        curr.extendleft(elements)
        return len(curr)
    
    def rpush(self, key: bytes, elements: list[bytes]) -> int:
        curr = self._list_get(key)

        if not curr:
            self._data[key] = deque()
            curr = self._data[key]

        curr.extend(elements)
        return len(curr)
    
    def lpop(self, key: bytes, count: int=1) -> list[bytes]:
        curr = self._list_get(key)
        popped = []

        if not curr:
            return popped
        
        pop_count = min(count, len(curr))

        for _ in range(pop_count):
            popped.append(curr.popleft())
        
        if not len(curr):
            self.delete(key)
        
        return popped
    
    def rpop(self, key: bytes, count: int=1) -> list[bytes]:
        curr = self._list_get(key)
        popped = []

        if not curr:
            return popped
        
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
    
    def hget(self, key: bytes, field: bytes) -> bytes | None:
        if not self.exists(key):
            return
        
        value = self._data.get(key)

        if field not in value:
            return
        
        return value.get(field)


async def expiration_sweeper(store: Store) -> None:
    while True:
        await asyncio.sleep(1)
        store.sample_and_expire()

store = Store()