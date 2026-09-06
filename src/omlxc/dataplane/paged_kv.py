"""
Paged KV Cache Block Allocator & Copy-on-Write Manager (ADR-0197/ADR-0203).

Provides block-level memory virtualization (block_size=32 tokens), eliminating VRAM
fragmentation and enabling zero-copy subagent conversation branching via CoW.

Also includes PagedKVCache — a general-purpose LRU cache with fixed memory budget
for document context compression (BET-Y2Q1-T3-04).
"""

from __future__ import annotations

import math
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PhysicalBlock:
    """A physical block in unified memory storing KV state."""

    block_id: int
    size_tokens: int = 32
    ref_count: int = 0
    model_id: str = ""
    is_allocated: bool = False


@dataclass(slots=True)
class SequenceBlockTable:
    """Logical sequence to physical block mapping with Copy-on-Write tracking."""

    seq_id: str
    physical_blocks: list[int] = field(default_factory=list)
    num_tokens: int = 0


class PagedKVMemoryManager:
    """
    Paged KV Cache allocator inspired by vLLM PagedAttention and Apple Silicon Unified Memory.
    """

    def __init__(
        self,
        total_vram_mb: float = 98304.0,  # 96 GB (75% default safe quota)
        block_size_tokens: int = 32,
        bytes_per_token: int = 2048,  # 2KB per token for 4-bit KV
    ) -> None:
        self.block_size_tokens = block_size_tokens
        self.bytes_per_token = bytes_per_token
        self.bytes_per_block = block_size_tokens * bytes_per_token
        self.total_blocks = int((total_vram_mb * 1024 * 1024) // self.bytes_per_block)

        self._blocks: list[PhysicalBlock] = [
            PhysicalBlock(block_id=i, size_tokens=block_size_tokens) for i in range(self.total_blocks)
        ]
        self._free_block_ids: set[int] = set(range(self.total_blocks))
        self._seq_tables: dict[str, SequenceBlockTable] = {}

    @property
    def free_blocks_count(self) -> int:
        return len(self._free_block_ids)

    @property
    def allocated_blocks_count(self) -> int:
        return self.total_blocks - len(self._free_block_ids)

    @property
    def memory_utilization_ratio(self) -> float:
        return (self.allocated_blocks_count / self.total_blocks) if self.total_blocks > 0 else 0.0

    def allocate_sequence(self, seq_id: str, initial_tokens: int, model_id: str = "") -> SequenceBlockTable:
        """Allocate physical blocks for a new sequence."""
        if seq_id in self._seq_tables:
            self.free_sequence(seq_id)

        blocks_needed = max(1, math.ceil(initial_tokens / self.block_size_tokens))
        if blocks_needed > len(self._free_block_ids):
            raise MemoryError(
                f"Cannot allocate {blocks_needed} blocks for seq {seq_id}. "
                f"Only {len(self._free_block_ids)} free blocks remaining."
            )

        allocated_ids: list[int] = []
        for _ in range(blocks_needed):
            b_id = self._free_block_ids.pop()
            block = self._blocks[b_id]
            block.ref_count = 1
            block.model_id = model_id
            block.is_allocated = True
            allocated_ids.append(b_id)

        table = SequenceBlockTable(
            seq_id=seq_id,
            physical_blocks=allocated_ids,
            num_tokens=initial_tokens,
        )
        self._seq_tables[seq_id] = table
        return table

    def fork_sequence_cow(self, parent_seq_id: str, child_seq_id: str) -> SequenceBlockTable:
        """
        Fork a child sequence sharing all existing KV blocks with Copy-On-Write semantics.
        Enables instant subagent spawning without duplicate KV allocation.
        """
        parent_table = self._seq_tables.get(parent_seq_id)
        if not parent_table:
            raise KeyError(f"Parent sequence {parent_seq_id} not found")

        # Increment reference count on all shared physical blocks
        for b_id in parent_table.physical_blocks:
            self._blocks[b_id].ref_count += 1

        child_table = SequenceBlockTable(
            seq_id=child_seq_id,
            physical_blocks=list(parent_table.physical_blocks),
            num_tokens=parent_table.num_tokens,
        )
        self._seq_tables[child_seq_id] = child_table
        return child_table

    def append_tokens(self, seq_id: str, new_tokens: int) -> None:
        """Append tokens to an active sequence, allocating or CoW-splitting blocks as needed."""
        table = self._seq_tables.get(seq_id)
        if not table:
            raise KeyError(f"Sequence {seq_id} not found")

        total_tokens = table.num_tokens + new_tokens
        blocks_needed = math.ceil(total_tokens / self.block_size_tokens)

        # Check last block for CoW split if shared
        if table.physical_blocks:
            last_block_id = table.physical_blocks[-1]
            last_block = self._blocks[last_block_id]
            if last_block.ref_count > 1:
                # CoW Split: Allocate independent physical block for mutation
                if not self._free_block_ids:
                    raise MemoryError("Out of free blocks for Copy-on-Write split")
                new_block_id = self._free_block_ids.pop()
                new_block = self._blocks[new_block_id]
                new_block.ref_count = 1
                new_block.is_allocated = True
                new_block.model_id = last_block.model_id
                last_block.ref_count -= 1
                table.physical_blocks[-1] = new_block_id

        # Allocate additional blocks if capacity exceeded
        while len(table.physical_blocks) < blocks_needed:
            if not self._free_block_ids:
                raise MemoryError(f"Out of memory allocating new blocks for seq {seq_id}")
            b_id = self._free_block_ids.pop()
            block = self._blocks[b_id]
            block.ref_count = 1
            block.is_allocated = True
            table.physical_blocks.append(b_id)

        table.num_tokens = total_tokens

    def free_sequence(self, seq_id: str) -> int:
        """Free a sequence and reclaim any physical blocks whose ref_count drops to 0."""
        table = self._seq_tables.pop(seq_id, None)
        if not table:
            return 0

        reclaimed = 0
        for b_id in table.physical_blocks:
            block = self._blocks[b_id]
            block.ref_count -= 1
            if block.ref_count <= 0:
                block.ref_count = 0
                block.is_allocated = False
                block.model_id = ""
                self._free_block_ids.add(b_id)
                reclaimed += 1

        return reclaimed

    def get_stats(self) -> dict[str, Any]:
        """Return memory layout and utilization metrics."""
        return {
            "total_blocks": self.total_blocks,
            "allocated_blocks": self.allocated_blocks_count,
            "free_blocks": self.free_blocks_count,
            "utilization_pct": round(self.memory_utilization_ratio * 100.0, 2),
            "block_size_tokens": self.block_size_tokens,
            "allocated_memory_mb": round((self.allocated_blocks_count * self.bytes_per_block) / (1024 * 1024), 2),
            "active_sequences": len(self._seq_tables),
        }


# ---------------------------------------------------------------------------
# PagedKVCache — General-purpose LRU cache with fixed memory budget
# (BET-Y2Q1-T3-04: 50 万字文档上下文压缩用)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CacheEntry:
    """Internal entry in the PagedKVCache."""

    key: str
    value: Any
    priority: int  # higher = more important, evicted last when tied
    size_bytes: int
    created_at: float
    last_access: float
    access_count: int = 0


class PagedKVCache:
    """
    Fixed-memory-budget LRU cache with priority-aware eviction.

    Designed for 50 万字级 document context compression: stores pre-computed
    embeddings, chunk summaries, and KV pairs within a strict memory ceiling.

    Features:
    - Fixed memory budget (default 16 GB, configurable)
    - 4KB block granularity for allocation efficiency
    - Priority-aware LRU eviction: low-priority items evicted first
    - O(1) put/get via OrderedDict + size tracking
    """

    DEFAULT_BUDGET_BYTES = 16 * 1024 * 1024 * 1024  # 16 GB
    BLOCK_SIZE = 4096  # 4KB blocks

    def __init__(self, budget_bytes: int | None = None) -> None:
        self._budget = budget_bytes or self.DEFAULT_BUDGET_BYTES
        self._used_bytes: int = 0
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._hit_count: int = 0
        self._miss_count: int = 0
        self._evict_count: int = 0

    @property
    def used_bytes(self) -> int:
        return self._used_bytes

    @property
    def free_bytes(self) -> int:
        return self._budget - self._used_bytes

    @property
    def budget_bytes(self) -> int:
        return self._budget

    @property
    def utilization(self) -> float:
        """Memory utilization ratio (0.0 - 1.0)."""
        return self._used_bytes / self._budget if self._budget > 0 else 0.0

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def _aligned_size(self, size_bytes: int) -> int:
        """Round up to next 4KB block boundary."""
        return ((size_bytes + self.BLOCK_SIZE - 1) // self.BLOCK_SIZE) * self.BLOCK_SIZE

    def put(self, key: str, value: Any, priority: int = 0) -> bool:
        """
        Insert or update a cache entry.

        Returns True if successfully cached, False if value exceeds budget.
        Lower priority entries are evicted first when budget is tight.
        """
        # Estimate size (use sys.getsizeof for Python objects, plus alignment)
        value_size = sys.getsizeof(value)
        if isinstance(value, (list, dict, set)):
            value_size += sum(sys.getsizeof(item) for item in value) if hasattr(value, "__iter__") else 0
        aligned = self._aligned_size(value_size)

        # If this key already exists, remove old entry first
        if key in self._entries:
            old = self._entries.pop(key)
            self._used_bytes -= old.size_bytes

        # Evict LRU entries (lowest priority first) until we have space
        while self._used_bytes + aligned > self._budget and self._entries:
            self._evict_one()

        # If still no room after evicting everything, this value is too large
        if self._used_bytes + aligned > self._budget:
            return False

        now = time.monotonic()
        entry = _CacheEntry(
            key=key,
            value=value,
            priority=priority,
            size_bytes=aligned,
            created_at=now,
            last_access=now,
        )
        self._entries[key] = entry
        self._used_bytes += aligned
        return True

    def get(self, key: str) -> Any | None:
        """
        Retrieve a cached value by key. Returns None on miss.
        Access refreshes LRU position.
        """
        entry = self._entries.get(key)
        if entry is None:
            self._miss_count += 1
            return None

        # Move to end (most recently used)
        self._entries.move_to_end(key)
        entry.last_access = time.monotonic()
        entry.access_count += 1
        self._hit_count += 1
        return entry.value

    def evict(self) -> str | None:
        """
        Manually evict the lowest-priority LRU entry.
        Returns the evicted key, or None if cache is empty.
        """
        return self._evict_one()

    def _evict_one(self) -> str | None:
        """Evict the single lowest-priority LRU entry."""
        if not self._entries:
            return None

        # Find entry with lowest priority (ties broken by LRU = first in OrderedDict)
        min_priority = min(e.priority for e in self._entries.values())
        for key, entry in self._entries.items():
            if entry.priority == min_priority:
                self._used_bytes -= entry.size_bytes
                del self._entries[key]
                self._evict_count += 1
                return key
        return None

    def contains(self, key: str) -> bool:
        return key in self._entries

    def remove(self, key: str) -> bool:
        """Remove a specific key. Returns True if it existed."""
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._used_bytes -= entry.size_bytes
            return True
        return False

    def clear(self) -> None:
        """Remove all entries."""
        self._entries.clear()
        self._used_bytes = 0

    def keys(self) -> list[str]:
        """Return all keys in LRU order (oldest first)."""
        return list(self._entries.keys())

    def get_stats(self) -> dict[str, Any]:
        """Return cache statistics for verification and telemetry."""
        total_access = self._hit_count + self._miss_count
        return {
            "entry_count": self.entry_count,
            "used_bytes": self._used_bytes,
            "free_bytes": self.free_bytes,
            "budget_bytes": self._budget,
            "utilization_pct": round(self.utilization * 100, 2),
            "hit_count": self._hit_count,
            "miss_count": self._miss_count,
            "hit_rate_pct": round(self._hit_count / total_access * 100, 2) if total_access > 0 else 0.0,
            "evict_count": self._evict_count,
        }
