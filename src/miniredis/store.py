import time
import math
import asyncio

from miniredis.custom_data_structures import RandomDict

class Store:
    def __init__(self):
        self._data: dict[bytes, bytes] = {}
        self._ttl = RandomDict()
    
    def sample_and_expire(self) -> None:
        sample = self._ttl.get_sample(10)

        if sample:
            self.exists(*sample)
    
    def get(self, key: bytes) -> bytes | None:
        if self.exists(key):
            return self._data.get(key)
        
        return None

    def set(self, key: bytes, value: bytes, ttl: int | None = None) -> None:
        self._data[key] = value

        if ttl:
            self.expire(key, ttl/1000)
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
        self.set(key, str(new_val).encode())
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
        curr = self._data.get(key) or b""
        new_val = curr + value
        self.set(key, new_val)
        return len(new_val)
    
    def strlen(self, key: bytes) -> int:
        value = self._data.get(key) or b""
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
            return math.floor(self._ttl.get(key))
        
    def persist(self, key: bytes) -> int:
        if not self.exists(key) or not self._ttl.get(key):
            return 0
        
        self._ttl.delete(key)
        return 1

async def expiration_sweeper(store: Store) -> None:
    while True:
        await asyncio.sleep(1)
        store.sample_and_expire()

store = Store()