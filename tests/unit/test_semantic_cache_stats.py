"""Round-trip coverage for semantic cache stats persistence.

load_cache_stats called json.loads without the module importing json, and its
except clause swallowed the resulting NameError — so every load silently
returned zeros no matter what had been written. Nothing covered these two
functions, which is why it survived.
"""

from __future__ import annotations

import pytest

from omlxc.dataplane import semantic_cache


@pytest.fixture
def stats_path(tmp_path, monkeypatch):
    path = tmp_path / "cache_stats.json"
    monkeypatch.setattr(semantic_cache, "_CACHE_STATS_PATH", path)
    return path


def _registry_with_counts(l1: int, l2: int, misses: int) -> semantic_cache.SemanticCacheRegistry:
    registry = semantic_cache.SemanticCacheRegistry()
    registry._l1_hits = l1
    registry._l2_hits = l2
    registry._misses = misses
    return registry


def test_saved_stats_are_read_back(stats_path):
    semantic_cache.save_cache_stats(_registry_with_counts(7, 3, 11))

    loaded = semantic_cache.load_cache_stats()

    assert loaded["l1_hits"] == 7
    assert loaded["l2_hits"] == 3
    assert loaded["misses"] == 11


def test_save_creates_the_parent_directory(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "dir" / "cache_stats.json"
    monkeypatch.setattr(semantic_cache, "_CACHE_STATS_PATH", path)

    semantic_cache.save_cache_stats(_registry_with_counts(1, 0, 0))

    assert path.exists()


def test_load_returns_zeros_when_no_stats_have_been_saved(stats_path):
    loaded = semantic_cache.load_cache_stats()

    assert loaded == {"l1_hits": 0, "l2_hits": 0, "misses": 0, "total_entries": 0}


def test_load_returns_zeros_on_corrupt_stats(stats_path):
    stats_path.write_text("{ not json")

    loaded = semantic_cache.load_cache_stats()

    assert loaded == {"l1_hits": 0, "l2_hits": 0, "misses": 0, "total_entries": 0}
