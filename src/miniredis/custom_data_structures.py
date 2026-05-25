import random

class RandomDict:
    def __init__(self):
        self._data: dict[bytes, float] = {}
        self._keys: list[bytes] = []
        self._pos: dict[bytes, int] = {}
    
    def get(self, key: bytes, default=None) -> float | None:
        return self._data.get(key, default)
    
    def set(self, key: bytes, value: float) -> None:
        if key in self._data:
            self._data[key] = value
            return
        
        self._data[key] = value
        self._keys.append(key)
        self._pos[key] = len(self._keys) - 1

    def delete(self, key: bytes) -> None:
        if key not in self._data:
            return
        
        del self._data[key]
        index = self._pos[key]
        self._keys[index], self._keys[-1] = self._keys[-1], self._keys[index]
        self._keys.pop()
        self._pos[self._keys[index]] = index
        del self._pos[key]

    def get_sample(self, count: int) -> list[bytes]:
        sample_size = min(count, len(self._keys))
        sample = random.sample(self._keys, sample_size)
        return sample