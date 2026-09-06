"""
Radix Tree / Trie-Based Dynamic Prefix Cache for omlxc (ADR-0197/ADR-0203/ADR-0204).

Enables SGLang / vLLM-grade dynamic prefix matching, multi-turn conversation KV cache reuse,
subagent fork branch sharing, and LRU eviction under memory pressure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PrefixMatchResult:
    """Result of a prefix lookup against the Radix Tree."""

    matched_tokens: int
    total_requested_tokens: int
    reuse_ratio: float
    matched_blocks: list[str]
    is_exact_match: bool


@dataclass
class RadixTreeNode:
    """Node in the Radix Trie representing a contiguous sequence of tokens."""

    tokens: tuple[int, ...]
    block_ids: list[str] = field(default_factory=list[str])
    children: dict[int, RadixTreeNode] = field(default_factory=dict[int, "RadixTreeNode"])
    parent: RadixTreeNode | None = None
    ref_count: int = 0
    last_accessed: float = field(default_factory=time.monotonic)

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    def touch(self) -> None:
        self.last_accessed = time.monotonic()


class RadixPrefixCache:
    """
    Dynamic Radix Tree Prefix Cache for zero-redundancy LLM prefilling.
    """

    def __init__(self, max_cached_tokens: int = 262144) -> None:
        self.max_cached_tokens = max_cached_tokens
        self.root = RadixTreeNode(tokens=())
        self._total_cached_tokens: int = 0
        self._hit_count: int = 0
        self._miss_count: int = 0
        self._tokens_saved: int = 0

    @property
    def total_cached_tokens(self) -> int:
        return self._total_cached_tokens

    def match_prefix(self, token_seq: list[int] | tuple[int, ...]) -> PrefixMatchResult:
        """Find the longest matching prefix in the Radix Tree."""
        tokens = tuple(token_seq)
        matched_tokens = 0
        matched_blocks: list[str] = []
        curr = self.root
        idx = 0

        while idx < len(tokens):
            first_tok = tokens[idx]
            if first_tok not in curr.children:
                break

            child = curr.children[first_tok]
            child_tokens = child.tokens
            match_len = 0
            while (
                match_len < len(child_tokens)
                and idx + match_len < len(tokens)
                and child_tokens[match_len] == tokens[idx + match_len]
            ):
                match_len += 1

            matched_tokens += match_len
            matched_blocks.extend(child.block_ids[:match_len])
            child.touch()

            if match_len < len(child_tokens):
                # Partial match on this node's branch
                break

            idx += match_len
            curr = child

        total_req = len(tokens)
        ratio = (matched_tokens / total_req) if total_req > 0 else 0.0

        if matched_tokens > 0:
            self._hit_count += 1
            self._tokens_saved += matched_tokens
        else:
            self._miss_count += 1

        return PrefixMatchResult(
            matched_tokens=matched_tokens,
            total_requested_tokens=total_req,
            reuse_ratio=ratio,
            matched_blocks=matched_blocks,
            is_exact_match=(matched_tokens == total_req),
        )

    def insert_sequence(
        self,
        token_seq: list[int] | tuple[int, ...],
        block_ids: list[str] | None = None,
    ) -> int:
        """
        Insert a sequence of tokens into the Radix Tree.
        Splits existing nodes if a branching prefix is detected.
        Returns the number of newly inserted tokens.
        """
        tokens = tuple(token_seq)
        if not tokens:
            return 0

        blocks = block_ids or [f"blk_{i}" for i in range(len(tokens))]
        curr = self.root
        idx = 0
        newly_inserted = 0

        while idx < len(tokens):
            first_tok = tokens[idx]

            if first_tok not in curr.children:
                # Direct new child branch
                remaining_tokens = tokens[idx:]
                remaining_blocks = blocks[idx:]
                new_node = RadixTreeNode(
                    tokens=remaining_tokens,
                    block_ids=remaining_blocks,
                    parent=curr,
                )
                curr.children[first_tok] = new_node
                self._total_cached_tokens += len(remaining_tokens)
                newly_inserted += len(remaining_tokens)
                break

            child = curr.children[first_tok]
            child_tokens = child.tokens
            match_len = 0
            while (
                match_len < len(child_tokens)
                and idx + match_len < len(tokens)
                and child_tokens[match_len] == tokens[idx + match_len]
            ):
                match_len += 1

            if match_len == len(child_tokens):
                # Full match of child node, descend further
                idx += match_len
                curr = child
                curr.touch()
            else:
                # Partial match -> Split child node
                # 1. Common prefix split node
                split_tokens = child_tokens[:match_len]
                split_blocks = child.block_ids[:match_len]
                split_node = RadixTreeNode(
                    tokens=split_tokens,
                    block_ids=split_blocks,
                    parent=curr,
                )

                # 2. Existing child becomes grandchild with remaining suffix
                suffix_tokens = child_tokens[match_len:]
                suffix_blocks = child.block_ids[match_len:]
                child.tokens = suffix_tokens
                child.block_ids = suffix_blocks
                child.parent = split_node
                split_node.children[suffix_tokens[0]] = child

                # 3. Replace child in current node's children table
                curr.children[first_tok] = split_node

                # 4. If new sequence has remaining tokens, create branch
                idx += match_len
                if idx < len(tokens):
                    new_branch_tokens = tokens[idx:]
                    new_branch_blocks = blocks[idx:]
                    new_node = RadixTreeNode(
                        tokens=new_branch_tokens,
                        block_ids=new_branch_blocks,
                        parent=split_node,
                    )
                    split_node.children[new_branch_tokens[0]] = new_node
                    self._total_cached_tokens += len(new_branch_tokens)
                    newly_inserted += len(new_branch_tokens)
                break

        # Check and enforce maximum capacity via LRU eviction
        if self._total_cached_tokens > self.max_cached_tokens:
            self.evict_lru(self._total_cached_tokens - self.max_cached_tokens)

        return newly_inserted

    def evict_lru(self, tokens_to_free: int) -> int:
        """Evict least-recently-used leaf nodes until target tokens are freed."""
        freed = 0
        while freed < tokens_to_free:
            leaves: list[RadixTreeNode] = []
            self._collect_unreferenced_leaves(self.root, leaves)
            if not leaves:
                break

            # Sort by oldest access time
            leaves.sort(key=lambda n: n.last_accessed)
            oldest = leaves[0]

            node_tokens = len(oldest.tokens)
            if oldest.parent and oldest.tokens:
                first_tok = oldest.tokens[0]
                oldest.parent.children.pop(first_tok, None)

            freed += node_tokens
            self._total_cached_tokens = max(0, self._total_cached_tokens - node_tokens)

        return freed

    def _collect_unreferenced_leaves(
        self,
        node: RadixTreeNode,
        leaves: list[RadixTreeNode],
    ) -> None:
        if node.is_leaf and node != self.root and node.ref_count == 0:
            leaves.append(node)
        for child in node.children.values():
            self._collect_unreferenced_leaves(child, leaves)

    def get_metrics(self) -> dict[str, Any]:
        """Return operational cache telemetry."""
        total_queries = self._hit_count + self._miss_count
        hit_rate = (self._hit_count / total_queries) if total_queries > 0 else 0.0
        return {
            "total_cached_tokens": self._total_cached_tokens,
            "max_cached_tokens": self.max_cached_tokens,
            "hit_count": self._hit_count,
            "miss_count": self._miss_count,
            "hit_rate": round(hit_rate, 4),
            "tokens_saved": self._tokens_saved,
        }
