"""Unit tests for PagedKVCache — BET-Y2Q1-T3-04."""

from __future__ import annotations

from omlxc.dataplane.paged_kv import PagedKVCache


class TestPagedKVCacheBasic:
    """Test basic put/get/evict operations."""

    def test_put_and_get(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)  # 1MB
        assert cache.put("key1", "value1") is True
        assert cache.get("key1") == "value1"

    def test_get_miss_returns_none(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        assert cache.get("nonexistent") is None

    def test_put_overwrites_existing(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        cache.put("key1", "old_value")
        cache.put("key1", "new_value")
        assert cache.get("key1") == "new_value"

    def test_contains(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        cache.put("k", "v")
        assert cache.contains("k") is True
        assert cache.contains("missing") is False

    def test_remove(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        cache.put("k", "v")
        assert cache.remove("k") is True
        assert cache.get("k") is None
        assert cache.remove("k") is False

    def test_clear(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.clear()
        assert cache.entry_count == 0
        assert cache.used_bytes == 0

    def test_keys_order(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        cache.put("c", "3")
        cache.put("a", "1")
        cache.put("b", "2")
        assert cache.keys() == ["c", "a", "b"]


class TestPagedKVCacheEviction:
    """Test LRU + priority eviction."""

    def test_lru_eviction_order(self) -> None:
        """Lowest priority + oldest entry evicted first."""
        cache = PagedKVCache(budget_bytes=4096)  # tiny budget
        cache.put("low1", "a" * 1000, priority=0)
        cache.put("high1", "b" * 1000, priority=10)
        cache.put("low2", "c" * 1000, priority=0)

        # Adding more should evict low priority entries
        cache.put("new1", "d" * 1000, priority=5)
        # low1 should be evicted first (lowest priority, oldest)
        assert cache.get("low1") is None

    def test_high_priority_survives(self) -> None:
        # Budget for exactly 4 blocks (16KB): 2 existing + space for evictions
        cache = PagedKVCache(budget_bytes=4 * PagedKVCache.BLOCK_SIZE)  # 16KB = 4 blocks
        cache.put("critical", "x" * 1000, priority=100)  # 1 block
        cache.put("normal", "y" * 1000, priority=5)  # 1 block

        # Fill with low priority — should trigger evictions
        for i in range(10):
            cache.put(f"filler{i}", "z" * 5000, priority=0)

        # Critical should survive (highest priority)
        assert cache.get("critical") == "x" * 1000

    def test_evict_returns_key(self) -> None:
        # Budget for 2 blocks (8KB): enough for 2 entries
        cache = PagedKVCache(budget_bytes=2 * PagedKVCache.BLOCK_SIZE)  # 8KB
        cache.put("a", "x" * 1000, priority=0)  # 1 block
        cache.put("b", "y" * 1000, priority=10)  # 1 block
        evicted = cache.evict()
        assert evicted == "a"  # lowest priority

    def test_evict_empty_cache(self) -> None:
        cache = PagedKVCache(budget_bytes=4096)
        assert cache.evict() is None


class TestPagedKVCacheMemory:
    """Test memory budget enforcement."""

    def test_budget_enforcement(self) -> None:
        cache = PagedKVCache(budget_bytes=8192)  # 8KB budget
        # Fill up to budget
        result = cache.put("key", "x" * 16000, priority=0)  # 16KB value
        # Should still succeed if evictions free enough space, or fail if value > budget
        # In this case 16KB > 8KB budget, so it might fail
        if result:
            assert cache.used_bytes <= cache.budget_bytes
        else:
            assert result is False

    def test_utilization_ratio(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        assert cache.utilization == 0.0
        cache.put("k", "v")
        assert 0.0 < cache.utilization <= 1.0

    def test_free_bytes(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        initial_free = cache.free_bytes
        cache.put("k", "v")
        assert cache.free_bytes < initial_free

    def test_4kb_block_alignment(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        cache.put("k", "v")  # small value
        # Size should be aligned to 4KB blocks
        assert cache.used_bytes % PagedKVCache.BLOCK_SIZE == 0


class TestPagedKVCachePriority:
    """Test priority-aware behavior."""

    def test_priority_affects_eviction(self) -> None:
        cache = PagedKVCache(budget_bytes=4096)
        cache.put("low", "a" * 1000, priority=0)
        cache.put("mid", "b" * 1000, priority=5)
        cache.put("high", "c" * 1000, priority=10)

        # Force eviction by adding more
        for i in range(5):
            cache.put(f"extra{i}", "d" * 500, priority=3)

        # low should be evicted first
        assert cache.get("low") is None


class TestPagedKVCachePerformance:
    """Test performance characteristics."""

    def test_many_entries(self) -> None:
        """Cache should handle 10K entries within budget."""
        cache = PagedKVCache(budget_bytes=16 * 1024 * 1024)  # 16MB
        for i in range(10000):
            cache.put(f"key-{i}", f"value-{i}", priority=i % 10)

        stats = cache.get_stats()
        assert stats["entry_count"] > 0
        assert stats["utilization_pct"] <= 100.0

    def test_get_stats(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        cache.put("a", "1")
        cache.get("a")
        cache.get("missing")
        stats = cache.get_stats()
        assert stats["hit_count"] == 1
        assert stats["miss_count"] == 1
        assert stats["hit_rate_pct"] == 50.0


class TestPagedKVCacheHitRate:
    """Test hit rate tracking."""

    def test_hit_rate_calculation(self) -> None:
        cache = PagedKVCache(budget_bytes=1024 * 1024)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.get("a")  # hit
        cache.get("a")  # hit
        cache.get("missing")  # miss
        stats = cache.get_stats()
        assert stats["hit_count"] == 2
        assert stats["miss_count"] == 1
        assert abs(stats["hit_rate_pct"] - 66.67) < 0.1
