"""
Tree-structured Document Context Index & Contradiction Detector (ADR-0203).

Builds a hierarchical tree from long-form documents (500K+ chars) partitioned by
heading levels, supporting sub-50ms semantic queries and cross-node contradiction
detection for policy/architecture documents.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TreeNode:
    """A node in the hierarchical document tree."""

    node_id: str
    heading_path: list[str]
    level: int  # heading depth (1=h1, 2=h2, etc.)
    text: str  # full text under this heading
    summary: str  # condensed summary (<200 tokens approx)
    chunk_start: int  # char offset in original doc
    chunk_end: int
    embedding: list[float] = field(default_factory=list[float])
    children: list[str] = field(default_factory=list[str])  # node_ids of children
    parent_id: str | None = None
    entities: set[str] = field(default_factory=set[str])  # extracted key entities


@dataclass(slots=True)
class QueryResult:
    """Result of a semantic query against the tree."""

    node_id: str
    heading_path: list[str]
    score: float  # similarity score
    text_snippet: str
    summary: str


@dataclass(slots=True)
class ContradictionPair:
    """A detected contradiction between two document sections."""

    node_a_id: str
    heading_a: list[str]
    node_b_id: str
    heading_b: list[str]
    score: float  # conflict severity (0-1)
    entity_overlap: set[str]
    description: str


def _simple_tokenize(text: str) -> list[str]:
    """Whitespace + punctuation tokenizer for lightweight embedding."""
    return re.findall(r"\w+", text.lower())


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _extract_key_entities(text: str, min_length: int = 3) -> set[str]:
    """Extract capitalized multi-word entities and technical terms."""
    entities: set[str] = set()
    # Match capitalized word sequences (proper nouns / titles)
    for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text):
        entities.add(match.group(1))
    # Match technical identifiers (kebab-case, snake_case, dotted)
    for match in re.finditer(r"\b([a-zA-Z][a-zA-Z0-9_-]*(?:\.[a-zA-Z][a-zA-Z0-9_-]*)+)\b", text):
        entities.add(match.group(1))
    # Filter short entities
    return {e for e in entities if len(e) >= min_length}


def _simple_embedding(text: str, dim: int = 128) -> list[float]:
    """
    Deterministic bag-of-words embedding (no ML dependency).
    Uses hash-based projection for lightweight semantic representation.
    """
    tokens = _simple_tokenize(text)
    vec = [0.0] * dim
    for token in tokens:
        h = int(hashlib.md5(token.encode(), usedforsecurity=False).hexdigest(), 16)
        idx = h % dim
        vec[idx] += 1.0
    # Normalize
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


class TreeContextIndex:
    """
    Hierarchical document context index for ultra-long documents.

    Partitions a document by heading structure into a tree, computes lightweight
    embeddings per node, and supports fast semantic queries and contradiction
    detection.
    """

    def __init__(
        self,
        summary_max_chars: int = 800,
        embedding_dim: int = 128,
    ) -> None:
        self.summary_max_chars = summary_max_chars
        self.embedding_dim = embedding_dim
        self._nodes: dict[str, TreeNode] = {}
        self._root_ids: list[str] = []
        self._doc_text: str = ""
        self._build_time_ms: float = 0.0

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    def build(self, document: str) -> None:
        """Parse a document into a hierarchical tree of indexed nodes."""
        self._doc_text = document
        self._nodes.clear()
        self._root_ids.clear()
        start = time.monotonic()

        # Split by markdown-style headings
        lines = document.split("\n")
        heading_stack: list[tuple[int, str]] = []  # (level, heading_text)
        char_offset = 0
        node_counter = 0

        # Phase 1: Parse headings and build raw chunks
        raw_sections: list[tuple[int, str, str, int, int]] = []  # (level, heading, text, start, end)
        section_start = 0
        current_heading = "root"
        current_level = 0

        for line in lines:
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading_match:
                # Save previous section
                if section_start < char_offset:
                    section_text = document[section_start:char_offset].strip()
                    if section_text:
                        raw_sections.append((current_level, current_heading, section_text, section_start, char_offset))

                level = len(heading_match.group(1))
                current_heading = heading_match.group(2).strip()
                current_level = level
                section_start = char_offset

            char_offset += len(line) + 1  # +1 for \n

        # Save last section
        if section_start < len(document):
            section_text = document[section_start:].strip()
            if section_text:
                raw_sections.append((current_level, current_heading, section_text, section_start, len(document)))

        # If no headings found, treat the whole document as a single section
        if not raw_sections:
            raw_sections = [(1, "Document", document[: self.summary_max_chars * 4], 0, len(document))]

        # Phase 2: Build tree nodes
        for level, heading, text, start, end in raw_sections:
            node_id = f"node-{node_counter}"
            node_counter += 1
            heading_path = [h for _, h in heading_stack if _ < level] + [heading]
            summary = text[: self.summary_max_chars] + ("..." if len(text) > self.summary_max_chars else "")
            embedding = _simple_embedding(text, self.embedding_dim)
            entities = _extract_key_entities(text)

            node = TreeNode(
                node_id=node_id,
                heading_path=heading_path,
                level=level,
                text=text,
                summary=summary,
                chunk_start=start,
                chunk_end=end,
                embedding=embedding,
                entities=entities,
            )
            self._nodes[node_id] = node

            # Update heading stack
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, heading))

        # Phase 3: Wire parent-child relationships
        stack: list[str] = []
        for node_id, node in self._nodes.items():
            # Pop stack until we find a parent with lower level
            while stack and self._nodes[stack[-1]].level >= node.level:
                stack.pop()
            if stack:
                parent_id = stack[-1]
                node.parent_id = parent_id
                self._nodes[parent_id].children.append(node_id)
            else:
                self._root_ids.append(node_id)
            stack.append(node_id)

        self._build_time_ms = (time.monotonic() - start) * 1000

    def query(self, text: str, top_k: int = 5) -> list[QueryResult]:
        """
        Semantic search: find the most relevant tree nodes for a query.
        Target: <50ms on CPU for 500K-char documents.
        """
        query_embedding = _simple_embedding(text, self.embedding_dim)
        results: list[QueryResult] = []

        for node in self._nodes.values():
            score = _cosine_similarity(query_embedding, node.embedding)
            # Boost score for entity overlap
            query_entities = _extract_key_entities(text)
            if query_entities and node.entities:
                overlap = query_entities & node.entities
                score += 0.1 * len(overlap) / max(len(query_entities), 1)

            if score > 0.01:  # minimum threshold
                snippet_start = max(0, node.chunk_start)
                snippet_end = min(len(self._doc_text), node.chunk_start + 500)
                results.append(
                    QueryResult(
                        node_id=node.node_id,
                        heading_path=node.heading_path,
                        score=score,
                        text_snippet=self._doc_text[snippet_start:snippet_end],
                        summary=node.summary,
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def find_contradictions(
        self,
        similarity_threshold: float = 0.3,
        entity_overlap_ratio: float = 0.5,
    ) -> list[ContradictionPair]:
        """
        Detect potential contradictions between document sections.

        A pair is flagged as contradictory when:
        1. Embedding cosine similarity is LOW (< threshold) — sections discuss similar
           topics but diverge in content.
        2. Entity overlap is HIGH (> ratio) — sections reference the same entities.
        This combination suggests the sections disagree about the same topic.
        """
        nodes = list(self._nodes.values())
        contradictions: list[ContradictionPair] = []
        seen_pairs: set[tuple[str, str]] = set()

        for i, node_a in enumerate(nodes):
            for node_b in nodes[i + 1 :]:
                if node_a.level != node_b.level:
                    continue  # only compare same-level sections
                if not node_a.entities or not node_b.entities:
                    continue

                # Entity overlap check
                common_entities = node_a.entities & node_b.entities
                overlap_count = len(common_entities)
                max_entities = max(len(node_a.entities), len(node_b.entities))
                if max_entities == 0:
                    continue
                overlap_ratio = overlap_count / max_entities

                if overlap_ratio < entity_overlap_ratio:
                    continue

                # Low similarity on same-entity sections = potential contradiction
                sim = _cosine_similarity(node_a.embedding, node_b.embedding)
                if sim >= similarity_threshold:
                    continue  # too similar, probably consistent

                first_id, second_id = sorted([node_a.node_id, node_b.node_id])
                pair_key = (first_id, second_id)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                # Extract negation keywords as contradiction evidence
                negation_words = {"不", "不得", "禁止", "废止", "不再", "not", "shall not", "prohibited", "repealed"}
                a_negations = {w for w in negation_words if w in node_a.text.lower()}
                b_negations = {w for w in negation_words if w in node_b.text.lower()}

                severity = 1.0 - sim
                description = (
                    f"Sections share {overlap_count} entities but diverge in content "
                    f"(similarity={sim:.3f}). "
                    f"Negation signals: A={a_negations or 'none'}, B={b_negations or 'none'}."
                )

                contradictions.append(
                    ContradictionPair(
                        node_a_id=node_a.node_id,
                        heading_a=node_a.heading_path,
                        node_b_id=node_b.node_id,
                        heading_b=node_b.heading_path,
                        score=severity,
                        entity_overlap=common_entities,
                        description=description,
                    )
                )

        contradictions.sort(key=lambda c: c.score, reverse=True)
        return contradictions

    def get_tree_stats(self) -> dict[str, Any]:
        """Return tree statistics for verification."""
        total_nodes = len(self._nodes)
        max_depth = max((n.level for n in self._nodes.values()), default=0)
        total_text_chars = sum(len(n.text) for n in self._nodes.values())
        total_entities: set[str] = set()
        for n in self._nodes.values():
            total_entities |= n.entities

        return {
            "total_nodes": total_nodes,
            "root_count": len(self._root_ids),
            "max_depth": max_depth,
            "total_text_chars": total_text_chars,
            "unique_entities": len(total_entities),
            "build_time_ms": round(self._build_time_ms, 2),
            "avg_entities_per_node": round(len(total_entities) / max(total_nodes, 1), 1),
        }
