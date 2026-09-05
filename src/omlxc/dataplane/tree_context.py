"""omlxc V5.0 — Hierarchical Tree Context over PagedKV (BET-Y2Q1-T3-04).

Builds a section-tree index over very long documents (0.5M+ chars) and pages
leaf text through the existing :class:`PagedKVMemoryManager` block allocator,
so that full-document memory stays bounded while chapter-level queries answer
in milliseconds. Pure-Python structural indexing: no LLM, no vector store.

Design:
- Heading-structure split (Markdown ``#`` and Chinese official ``一、/（一）/1.``
  patterns) into a hierarchical node tree with byte ranges into the source text.
- Every leaf node's text is registered with the paged allocator; body text is
  materialized lazily from the byte range on demand.
- ``detect_conflicts`` cross-checks numeric/date assertions that share a claim
  key across different sections (e.g. same policy clause quoting different
  amounts) — structural contradiction candidates, not semantic judgement.
- ``ttft_probe`` measures first-locate latency after build (target <= 50ms).
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from omlxc.dataplane.paged_kv import PagedKVMemoryManager

# ---------------------------------------------------------------------------
# heading patterns: Markdown ATX + Chinese official document numbering
_HEADING_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^(#{1,6})\s+(\S.*)$"), None),  # markdown, level = hash count
    (re.compile(r"^(第[一二三四五六七八九十百]+[章节篇][\s、.:：].*)$"), 2),
    (re.compile(r"^([一二三四五六七八九十]+、\S.*)$"), 2),
    (re.compile(r"^(（[一二三四五六七八九十]+）\S.*)$"), 3),
    (re.compile(r"^(\d{1,2}\.\d{1,2}(?:\.\d{1,2})*\s+\S.*)$"), 3),
]

_CLAIM_VALUE = re.compile(
    r"(?P<key>[\u4e00-\u9fffA-Za-z_]{2,24})"
    r"(?:率|金额|费用|预算|天数|时限|人次|床位数|覆盖|指标)?"
    r"[:：为不超超过约达到]\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|万元|亿元|天|个工作日|小时|分钟|人|张|家|GB|TB|ms|秒)?"
)


@dataclass(slots=True)
class TreeContextNode:
    """A node in the document section tree."""

    node_id: str
    title: str
    level: int
    byte_start: int
    byte_end: int
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)
    seq_id: str = ""  # paged-allocator sequence covering this node's text

    @property
    def is_leaf(self) -> bool:
        return not self.children


def _detect_heading(line: str) -> tuple[int, str] | None:
    stripped = line.strip()
    if not stripped:
        return None
    for pattern, fixed_level in _HEADING_PATTERNS:
        m = pattern.match(stripped)
        if m:
            level = fixed_level if fixed_level is not None else len(m.group(1))
            return level, m.group(2) if fixed_level is None else m.group(1)
    return None


class TreeContextIndex:
    """Hierarchical section tree with PagedKV-backed lazy leaf storage."""

    def __init__(
        self,
        memory: PagedKVMemoryManager | None = None,
        memory_budget_mb: float = 2048.0,
        source_name: str = "doc",
    ) -> None:
        self.memory = memory or PagedKVMemoryManager(total_vram_mb=memory_budget_mb)
        self.source_name = source_name
        self.roots: list[TreeContextNode] = []
        self.nodes: dict[str, TreeContextNode] = {}
        self._text: str = ""
        self._last_ttft_ms: float = -1.0

    # -- build ----------------------------------------------------------

    def build(self, text: str) -> TreeContextIndex:
        self._text = text
        self.roots = []
        self.nodes = {}
        lines = text.split("\n")
        offset = 0
        stack: list[TreeContextNode] = []
        counter = 0

        def _new_node(title: str, level: int, byte_start: int) -> TreeContextNode:
            nonlocal counter
            counter += 1
            nid = f"{self.source_name}-n{counter:05d}"
            node = TreeContextNode(
                node_id=nid,
                title=title[:120],
                level=level,
                byte_start=byte_start,
                byte_end=byte_start,
            )
            self.nodes[nid] = node
            return node

        root = _new_node(f"<doc:{self.source_name}>", 0, 0)
        self.roots.append(root)
        stack.append(root)

        for line in lines:
            line_len = len(line) + 1
            heading = _detect_heading(line)
            if heading:
                level, title = heading
                node = _new_node(title, level, offset)
                while stack and stack[-1].level >= level:
                    finished = stack.pop()
                    finished.byte_end = offset
                stack[-1].children.append(node.node_id)
                node.parent_id = stack[-1].node_id
                stack.append(node)
            offset += line_len

        while stack:
            finished = stack.pop()
            finished.byte_end = min(offset, finished.byte_end or offset)

        # close remaining ranges and register paged sequences for leaves
        for node in self.nodes.values():
            if node.byte_end < node.byte_start:
                node.byte_end = offset
        leaves = [n for n in self.nodes.values() if n.is_leaf]
        for leaf in leaves:
            leaf_text = self._text[leaf.byte_start : leaf.byte_end]
            est_tokens = max(1, len(leaf_text) // 2)
            leaf.seq_id = f"{leaf.node_id}"
            try:
                self.memory.allocate_sequence(leaf.seq_id, est_tokens)
            except Exception:
                leaf.seq_id = ""  # allocator full: leaf stays lazily served by range
        return self

    # -- access ----------------------------------------------------------

    def _node_by_id(self, node_id: str) -> TreeContextNode | None:
        return self.nodes.get(node_id)

    def node_text(self, node_id: str) -> str:
        node = self._node_by_id(node_id)
        if node is None:
            return ""
        start = node.byte_start
        end = node.byte_end
        if node.parent_id:
            parent = self.nodes[node.parent_id]
            child_starts = [
                self.nodes[c].byte_start for c in parent.children if c != node_id
            ]
            inner = [s for s in child_starts if start < s < end]
            if inner:
                end = min(inner)
        return self._text[start:end]

    def locate(self, query: str, limit: int = 10) -> list[TreeContextNode]:
        """Title/keyword match over the section tree (no body scan)."""
        q = query.strip().lower()
        if not q:
            return []
        hits = [
            n
            for n in self.nodes.values()
            if q in n.title.lower() or q in (self.node_text(n.node_id)[:0] or "")
        ]
        # keyword fallback inside leaf bodies is bounded to title-hit misses
        if not hits:
            hits = [
                n
                for n in self.nodes.values()
                if n.is_leaf and q in self.leaf_excerpt(n.node_id, 4096).lower()
            ]
        hits.sort(key=lambda n: (n.level, n.byte_start))
        return hits[:limit]

    def leaf_excerpt(self, node_id: str, max_chars: int = 4096) -> str:
        node = self._node_by_id(node_id)
        if node is None:
            return ""
        return self._text[node.byte_start : node.byte_end][:max_chars]

    def stats(self) -> dict[str, Any]:
        leaves = [n for n in self.nodes.values() if n.is_leaf]
        return {
            "total_nodes": len(self.nodes),
            "leaf_nodes": len(leaves),
            "doc_chars": len(self._text),
            "paged_blocks_allocated": self.memory.allocated_blocks_count,
            "paged_utilization": round(self.memory.memory_utilization_ratio, 4),
            "last_ttft_ms": round(self._last_ttft_ms, 2),
        }

    # -- conflict detection ------------------------------------------------

    def _collect_claims(self, node: TreeContextNode) -> list[dict[str, Any]]:
        body = self.leaf_excerpt(node.node_id)
        claims = []
        for m in _CLAIM_VALUE.finditer(body):
            claims.append(
                {
                    "key": m.group("key"),
                    "value": m.group("value"),
                    "unit": m.group("unit") or "",
                    "node_id": node.node_id,
                    "section": node.title,
                }
            )
        return claims

    def detect_conflicts(self) -> list[dict[str, Any]]:
        """Cross-section numeric assertion comparison (same claim key+unit,
        different values => contradiction candidate)."""
        claims: list[dict[str, Any]] = []
        for node in self.nodes.values():
            if node.is_leaf:
                claims.extend(self._collect_claims(node))
        by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for c in claims:
            by_key.setdefault((c["key"], c["unit"]), []).append(c)
        conflicts = []
        for (key, unit), group in sorted(by_key.items()):
            values = {c["value"] for c in group}
            if len(values) > 1:
                conflicts.append(
                    {
                        "claim_key": key,
                        "unit": unit,
                        "values": sorted(values),
                        "occurrences": [
                            {k: c[k] for k in ("value", "node_id", "section")}
                            for c in group
                        ],
                    }
                )
        conflicts.sort(key=lambda c: -len(c["occurrences"]))
        return conflicts

    # -- ttft ---------------------------------------------------------------

    def ttft_probe(self, probe_query: str | None = None) -> float:
        """First-locate latency after build, in ms (target <= 50ms)."""
        q = probe_query or (self.nodes[self.roots[0].children[0]].title if self.roots and self.roots[0].children else "概")
        t0 = time.perf_counter()
        self.locate(q)
        self._last_ttft_ms = (time.perf_counter() - t0) * 1000
        return self._last_ttft_ms


def fingerprint_text(text: str) -> str:
    """Stable short digest for manifest/audit use."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
