"""
BatchB 128k stress tests for PagedKVMemoryManager (BET-Y1Q3-T10-114).

Validates leak-freedom and fragmentation bounds at 128k-token scale:
131072 tokens / 32 tokens-per-block = 4096 blocks per full sequence.
"""

from __future__ import annotations

from omlxc.dataplane.paged_kv import PagedKVMemoryManager

TOKENS_128K = 131072
BLOCKS_128K = TOKENS_128K // 32  # 4096


def _manager_128k() -> PagedKVMemoryManager:
    # 280MB @ 64KB/block = 4480 blocks: one full 128k seq (4096) + 384 headroom
    # (8 children x (1 CoW-split + 32 appended) = 264 blocks worst case).
    mgr = PagedKVMemoryManager(total_vram_mb=280.0, block_size_tokens=32, bytes_per_token=2048)
    assert mgr.total_blocks >= BLOCKS_128K + 264
    return mgr


def test_128k_full_lifecycle_no_leak() -> None:
    """128k alloc → 8 CoW forks → append → free-all ⇒ full reclaim, audit clean."""
    mgr = _manager_128k()
    total = mgr.total_blocks

    mgr.allocate_sequence("main-128k", TOKENS_128K, model_id="qwen3-8b")
    assert len(mgr._seq_tables["main-128k"].physical_blocks) == BLOCKS_128K

    children = [f"sub-{i}" for i in range(8)]
    for child in children:
        mgr.fork_sequence_cow("main-128k", child)
    for child in children:
        mgr.append_tokens(child, 1024)  # 32 blocks each, CoW-splits shared tail

    assert mgr.audit_leaks() == []
    assert 0.0 <= mgr.fragmentation_ratio() <= 1.0

    for child in children:
        mgr.free_sequence(child)
    reclaimed = mgr.free_sequence("main-128k")
    assert reclaimed == BLOCKS_128K

    assert mgr.free_blocks_count == total
    assert mgr.allocated_blocks_count == 0
    assert mgr.audit_leaks() == []
    assert mgr.fragmentation_ratio() == 0.0


def test_fork_child_id_collision_no_leak() -> None:
    """Re-forking onto an existing child id must not leak the previous table."""
    mgr = _manager_128k()
    mgr.allocate_sequence("parent", 4096, model_id="qwen3-8b")  # 128 blocks
    free_after_alloc = mgr.free_blocks_count

    mgr.fork_sequence_cow("parent", "child")
    free_after_fork = mgr.free_blocks_count
    assert free_after_fork == free_after_alloc  # CoW shares, no new blocks

    mgr.fork_sequence_cow("parent", "child")  # collision: old child freed first
    assert mgr.free_blocks_count == free_after_fork
    assert mgr.audit_leaks() == []

    mgr.free_sequence("child")
    mgr.free_sequence("parent")
    assert mgr.free_blocks_count == mgr.total_blocks
    assert mgr.audit_leaks() == []


def test_fragmentation_ratio_bounds_and_reclaim() -> None:
    """fragmentation_ratio in [0,1]; fresh and fully-reclaimed managers read 0.0."""
    mgr = _manager_128k()
    assert mgr.fragmentation_ratio() == 0.0

    # Interleave allocations and frees to shatter free space.
    for i in range(64):
        mgr.allocate_sequence(f"seq-{i}", 1024)  # 32 blocks each
    for i in range(0, 64, 2):
        mgr.free_sequence(f"seq-{i}")
    r = mgr.fragmentation_ratio()
    assert 0.0 <= r <= 1.0
    assert mgr.audit_leaks() == []

    for i in range(1, 64, 2):
        mgr.free_sequence(f"seq-{i}")
    assert mgr.free_blocks_count == mgr.total_blocks
    assert mgr.fragmentation_ratio() == 0.0
    assert mgr.audit_leaks() == []
