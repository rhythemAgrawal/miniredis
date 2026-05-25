import time
import math

class Store:
    def __init__(self):
        self._data: dict[bytes, bytes] = {}
        self._ttl: dict[bytes, float] = {}
    
    def get(self, key: bytes) -> bytes | None:
        if self.exists(key):
            return self._data.get(key)
        
        return None

    def set(self, key: bytes, value: bytes, ttl: int | None = None) -> None:
        self._data[key] = value

        if ttl:
            self.expire(key, ttl/1000)
    
    def delete(self, *keys: bytes) -> int:
        count = 0

        for key in keys:
            if self.exists(key):
                del self._data[key]
                self._ttl.pop(key, None)
                count += 1

        return count

    def exists(self, *keys: bytes) -> int:
        count = 0

        for key in keys:
            if key in self._data:
                if self._ttl.get(key, float('inf')) < time.time():
                    del self._data[key]
                    del self._ttl[key]
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
        
        self._ttl[key] = time.time() + ttl
        return 1
    
    def ttl(self, key: bytes) -> int:
        if not self.exists(key):
            return -2
        elif key not in self._ttl:
            return -1
        else:
            return math.floor(self._ttl[key])
        
    def persist(self, key: bytes) -> int:
        if not self.exists(key) or key not in self._ttl:
            return 0
        
        del self._ttl[key]
        return 1


store = Store()