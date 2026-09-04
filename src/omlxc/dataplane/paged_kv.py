"""
Paged KV Cache Block Allocator & Copy-on-Write Manager (ADR-0197/ADR-0203).

Provides block-level memory virtualization (block_size=32 tokens), eliminating VRAM
fragmentation and enabling zero-copy subagent conversation branching via CoW.
"""

from __future__ import annotations

import math
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
        bytes_per_token: int = 2048,     # 2KB per token for 4-bit KV
    ) -> None:
        self.block_size_tokens = block_size_tokens
        self.bytes_per_token = bytes_per_token
        self.bytes_per_block = block_size_tokens * bytes_per_token
        self.total_blocks = int((total_vram_mb * 1024 * 1024) // self.bytes_per_block)

        self._blocks: list[PhysicalBlock] = [
            PhysicalBlock(block_id=i, size_tokens=block_size_tokens)
            for i in range(self.total_blocks)
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

        # BatchB leak guard: re-forking onto an existing child id must release
        # the previous child table first, otherwise its blocks leak (ref_counts
        # never decremented). At 128k scale one leaked table = 4096 blocks.
        if child_seq_id in self._seq_tables:
            self.free_sequence(child_seq_id)

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
            "fragmentation_ratio": round(self.fragmentation_ratio(), 4),
        }

    def audit_leaks(self) -> list[int]:
        """
        BatchB 128k leak detector (diagnostic-only, O(tables + blocks)).

        Recomputes the expected ref_count of every block from live sequence
        tables and returns ids whose stored state disagrees:
        - allocated block referenced by zero tables (leaked, never reclaimed)
        - block whose stored ref_count != number of referencing tables
        - free-list membership contradicting is_allocated
        Empty list means no leaks.
        """
        expected_ref: dict[int, int] = {}
        for table in self._seq_tables.values():
            for b_id in table.physical_blocks:
                expected_ref[b_id] = expected_ref.get(b_id, 0) + 1

        leaked: list[int] = []
        for block in self._blocks:
            exp = expected_ref.get(block.block_id, 0)
            in_free = block.block_id in self._free_block_ids
            if exp == 0:
                if block.is_allocated or block.ref_count != 0 or not in_free:
                    leaked.append(block.block_id)
            else:
                if in_free or not block.is_allocated or block.ref_count != exp:
                    leaked.append(block.block_id)
        return sorted(leaked)

    def fragmentation_ratio(self) -> float:
        """
        BatchB fragmentation guard (diagnostic-only, O(total_blocks)).

        0.0 = all free blocks form one contiguous run (fresh manager or full
        reclaim after 128k lifecycle); approaches 1.0 as free space shatters
        into isolated singletons. Computed as 1 - largest_free_run/free_count.
        """
        free_count = len(self._free_block_ids)
        if free_count == 0:
            return 0.0
        if free_count == self.total_blocks:
            return 0.0
        free_sorted = sorted(self._free_block_ids)
        longest = 1
        run = 1
        for prev, cur in zip(free_sorted, free_sorted[1:]):
            if cur == prev + 1:
                run += 1
                longest = max(longest, run)
            else:
                run = 1
        return 1.0 - (longest / free_count)
