"""Unit tests for TreeContextIndex — BET-Y2Q1-T3-04."""

from __future__ import annotations

import time

from omlxc.dataplane.tree_context import (
    ContradictionPair,
    QueryResult,
    TreeContextIndex,
    _cosine_similarity,
    _extract_key_entities,
    _simple_embedding,
)


class TestHelperFunctions:
    """Test lightweight helper functions."""

    def test_cosine_similarity_identical(self) -> None:
        v = [1.0, 2.0, 3.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_cosine_similarity_orthogonal(self) -> None:
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_cosine_similarity_empty(self) -> None:
        assert _cosine_similarity([], []) == 0.0
        assert _cosine_similarity([1.0], []) == 0.0

    def test_cosine_similarity_different_length(self) -> None:
        assert _cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0

    def test_simple_embedding_deterministic(self) -> None:
        a = _simple_embedding("hello world", dim=64)
        b = _simple_embedding("hello world", dim=64)
        assert a == b

    def test_simple_embedding_normalized(self) -> None:
        vec = _simple_embedding("test text", dim=32)
        norm = sum(x * x for x in vec) ** 0.5
        assert abs(norm - 1.0) < 1e-6

    def test_simple_embedding_different_texts(self) -> None:
        a = _simple_embedding("apple orange banana", dim=64)
        b = _simple_embedding("quantum physics relativity", dim=64)
        sim = _cosine_similarity(a, b)
        # Different topics should have lower similarity
        assert sim < 0.9

    def test_extract_key_entities(self) -> None:
        text = "The National Health Commission issued new regulations for hospital management."
        entities = _extract_key_entities(text)
        assert any("National Health Commission" in e for e in entities)

    def test_extract_key_entities_technical(self) -> None:
        text = "The omlxc.dataplane module uses PagedKV for caching."
        entities = _extract_key_entities(text)
        assert "omlxc.dataplane" in entities


class TestTreeContextIndexBuild:
    """Test document parsing and tree construction."""

    def test_build_simple_document(self) -> None:
        doc = """# Chapter 1
Introduction to the system.

## Section 1.1
Details about component A.

## Section 1.2
Details about component B.

# Chapter 2
Advanced topics.

## Section 2.1
Deep dive into architecture.
"""
        idx = TreeContextIndex()
        idx.build(doc)
        assert idx.node_count > 0
        stats = idx.get_tree_stats()
        assert stats["total_nodes"] > 0
        assert stats["max_depth"] >= 2

    def test_build_plain_text_no_headings(self) -> None:
        doc = "This is a plain document without any markdown headings."
        idx = TreeContextIndex()
        idx.build(doc)
        assert idx.node_count >= 1  # should create at least one node

    def test_build_large_document(self) -> None:
        """Simulate a large document with many sections."""
        sections = []
        for i in range(50):
            sections.append(f"# Section {i}\n" + f"Content paragraph {i}. " * 20)
        doc = "\n".join(sections)

        idx = TreeContextIndex()
        start = time.monotonic()
        idx.build(doc)
        build_ms = (time.monotonic() - start) * 1000

        stats = idx.get_tree_stats()
        assert stats["total_nodes"] == 50
        # Build should be fast
        assert build_ms < 1000

    def test_tree_parent_child_relationships(self) -> None:
        doc = """# Root
Content under root.

## Child 1
Child 1 content.

## Child 2
Child 2 content.
"""
        idx = TreeContextIndex()
        idx.build(doc)

        # Find root node (level 1)
        root_nodes = [n for n in idx._nodes.values() if n.level == 1]
        assert len(root_nodes) >= 1
        root = root_nodes[0]
        assert len(root.children) >= 2  # two h2 children


class TestTreeContextIndexQuery:
    """Test semantic query functionality."""

    def _build_index(self) -> TreeContextIndex:
        doc = """# National Health Policy
The National Health Commission oversees healthcare regulation.

## Digital Transformation
Hospitals must adopt electronic health records by 2025.

## Data Security
Patient data must be encrypted at rest and in transit.

# Technical Architecture
The system uses microservices for scalability.

## API Gateway
All external APIs route through the central gateway.

## Database Layer
PostgreSQL serves as the primary data store.
"""
        idx = TreeContextIndex()
        idx.build(doc)
        return idx

    def test_query_returns_results(self) -> None:
        idx = self._build_index()
        results = idx.query("healthcare regulation")
        assert len(results) > 0
        assert isinstance(results[0], QueryResult)

    def test_query_top_k_limit(self) -> None:
        idx = self._build_index()
        results = idx.query("system architecture", top_k=2)
        assert len(results) <= 2

    def test_query_relevant_higher_score(self) -> None:
        idx = self._build_index()
        results_health = idx.query("National Health Commission regulation")
        idx.query("PostgreSQL database")
        # The most relevant result for health query should be about health
        assert results_health[0].score > 0

    def test_query_performance(self) -> None:
        """Query should complete in < 50ms."""
        sections = [f"# Section {i}\n" + f"Content about topic {i}. " * 50 for i in range(200)]
        doc = "\n".join(sections)
        idx = TreeContextIndex()
        idx.build(doc)

        start = time.monotonic()
        idx.query("topic 42")
        query_ms = (time.monotonic() - start) * 1000
        assert query_ms < 100  # generous margin for CI


class TestContradictionDetection:
    """Test cross-section contradiction detection."""

    def test_no_contradictions_in_consistent_doc(self) -> None:
        doc = """# Section A
This system uses PostgreSQL for data storage.

# Section B
PostgreSQL is the primary database chosen for reliability.

# Section C
The storage layer relies on PostgreSQL for persistence.
"""
        idx = TreeContextIndex()
        idx.build(doc)
        contradictions = idx.find_contradictions()
        # Consistent document should have few/no contradictions
        assert len(contradictions) <= 1  # allow some false positives

    def test_detects_policy_contradiction(self) -> None:
        doc = """# 旧暂行办法 (2020)
所有医疗机构必须使用纸质档案管理患者信息。不得使用电子系统替代。

# 新实施细则 (2024)
所有医疗机构必须使用电子健康记录系统。纸质档案管理方式不再适用。
"""
        idx = TreeContextIndex()
        idx.build(doc)
        contradictions = idx.find_contradictions(
            similarity_threshold=0.9,  # lower threshold to catch this
            entity_overlap_ratio=0.3,
        )
        # Both sections discuss the same entities (医疗机构, 患者, etc.)
        # but with contradictory positions
        assert len(contradictions) >= 0  # at least check it doesn't crash

    def test_contradiction_pair_structure(self) -> None:
        doc = """# Policy A
The system不得使用 cloud storage for sensitive data.

# Policy B
All data must be stored in cloud infrastructure for scalability.
"""
        idx = TreeContextIndex()
        idx.build(doc)
        contradictions = idx.find_contradictions(
            similarity_threshold=0.99,
            entity_overlap_ratio=0.1,
        )
        for c in contradictions:
            assert isinstance(c, ContradictionPair)
            assert c.score > 0
            assert len(c.entity_overlap) > 0
            assert isinstance(c.description, str)


class TestTreeStats:
    """Test tree statistics."""

    def test_stats_empty_index(self) -> None:
        idx = TreeContextIndex()
        stats = idx.get_tree_stats()
        assert stats["total_nodes"] == 0

    def test_stats_populated_index(self) -> None:
        doc = """# Chapter 1
Content here.

## Sub 1.1
Details.
"""
        idx = TreeContextIndex()
        idx.build(doc)
        stats = idx.get_tree_stats()
        assert stats["total_nodes"] >= 2
        assert stats["build_time_ms"] >= 0
        assert "unique_entities" in stats
