import random
from typing import NamedTuple

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
        index = self._pos.pop(key)
        self._keys[index], self._keys[-1] = self._keys[-1], self._keys[index]
        self._keys.pop()

        # Only relocate when we removed an interior element. If we removed the
        # tail, `index` now equals the new length (out of range) and there is
        # nothing to move.
        if index < len(self._keys):
            self._pos[self._keys[index]] = index

    def get_sample(self, count: int) -> list[bytes]:
        sample_size = min(count, len(self._keys))
        sample = random.sample(self._keys, sample_size)
        return sample
    
class SkipListNode:
    def __init__(self, member: bytes | str):
        self.member = member
        self.next: list[Next] = []
    
class Next(NamedTuple):
    node: SkipListNode
    span: int

class SortedSet:
    MAX_LEVEL = 32
    PROBABILITY = 0.25

    def __init__(self):
        self.score_table: dict[bytes | str, float] = {
            "head": float('-inf'),
            "tail": float('inf')
        }
        self.head = SkipListNode("head")
        self.tail = SkipListNode("tail")

        for _ in range(self.MAX_LEVEL):
            self.head.next.append(Next(self.tail, 1))
    
    def _is_lte(self, target: tuple[float, bytes], node: SkipListNode) -> bool:
        return target <= (self.score_table.get(node.member), node.member)
    
    def get_score(self, member: bytes) -> float | None:
        return self.score_table.get(member)
    
    def search_by_score(self, target_score: float, member: bytes):
        curr = self.head
        level = self.MAX_LEVEL - 1
        target = (target_score, member)
        prev_nodes = []
        skip_counts = []
        count = 0

        while level >= 0:
            if self._is_lte(target, curr.next[level].node):
                prev_nodes.append(curr)
                skip_counts.append(count)
                count = 0
                level -= 1
            else:
                count += curr.next[level].span
                curr = curr.next[level].node
        
        return(prev_nodes, skip_counts)
    
    def _heads(self) -> bool:
        return random.random() < self.PROBABILITY
    
    def insert(self, score: float, member: bytes) -> None:
        self.score_table[member] = score
        prev_nodes, skip_counts = self.search_by_score(score, member)
        prev_nodes.reverse()
        skip_counts.reverse()
        level_count = 1

        while self._heads() and level_count < self.MAX_LEVEL:
            level_count += 1

        new = SkipListNode(member)
        curr_count = 0

        for level in range(level_count):
            next_node = prev_nodes[level].next[level].node
            old_span = prev_nodes[level].next[level].span

            if level > 0:
                curr_count += skip_counts[level-1]
                new_span = old_span - curr_count
            else:
                curr_count = 1
                new_span = 1

            prev_nodes[level].next[level].node = new
            prev_nodes[level].next[level].span = curr_count
            new.next.append(Next(next_node, new_span))
    
    def delete(self, score: float, member: bytes) -> None:
        del self.score_table[member]
        prev_nodes, skip_counts = self.search_by_score(score, member)
        prev_nodes.reverse()
        to_delete = prev_nodes[0].next[0].node

        for level in range(len(to_delete.next)):
            prev_nodes[level].next[level].node = to_delete.next[level].node
            prev_nodes[level].next[level].span += to_delete.next[level].span
    
    def get_range_by_score(self, start: float, end: float) -> list[bytes]:
        prev_nodes, skip_counts = self.search_by_score(start, b"")
        curr = prev_nodes[-1].next[0].node
        member_range = []

        while self.score_table[curr.member] <= end:
            member_range.append(curr.member)
            curr = curr.next[0].node
        
        return member_range
    
    def get_rank(self, score: float, member: bytes) -> int:
        prev_nodes, skip_counts = self.search_by_score(score, member)
        return sum(skip_counts)
    
    def get_length(self) -> int:
        count = 0
        curr = self.head

        while curr != self.tail:
            count += curr.next[self.MAX_LEVEL-1].span
            curr = curr.next[self.MAX_LEVEL-1].node
        
        return count
    
    def get_range_by_rank(self, start: int, end: int) -> list[bytes]:
        length = self.get_length()

        if start < 0:
            start = length + start

        if end < 0:
            end = length + end
        
        if start > end or start >= length:
            return []
        
        end = min(end, length-1)

        curr = self.head
        level = self.MAX_LEVEL - 1
        target = start
        count = 0
        
        while count < target:
            if count + curr.next[level].span <= target:
                count += curr.next[level].span
                curr = curr.next[level].node
            else:
                level -= 1
        
        member_range = []

        for i in range(start, end+1):
            member_range.append(curr.member)
            curr = curr.next[0].node
        
        return member_range