"""
Hierarchical 3-Tier Cache Coordinator (L1 Semantic -> L2 Paged Radix KV -> L3 Persistent Snapshot).

Unifies:
1. L1 Semantic & Exact Cache (Zero compute prompt hit)
2. L2 In-Memory Paged Radix KV Cache (Longest prefix match & CoW branch sharing)
3. L3 Disk / NVMe Persistent Snapshots (0ms cold start pre-warming)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from .adaptive_kv_quant import AdaptiveKVQuantizer, CompressedKVPlan
from .context_compressor import ContextOptimizationResult, ContextOptimizer
from .paged_kv import PagedKVMemoryManager
from .prefix_snapshot import StaticPrefixSnapshotManager
from .radix_cache import PrefixMatchResult, RadixPrefixCache
from .semantic_cache import SemanticCacheRegistry


class CacheResolutionTier(enum.StrEnum):
    L1_EXACT_OR_SEMANTIC = "l1_exact_or_semantic"
    L2_RADIX_PREFIX_KV = "l2_radix_prefix_kv"
    L3_DISK_SNAPSHOT_KV = "l3_disk_snapshot_kv"
    COLD_PREFILL_REQUIRED = "cold_prefill_required"


@dataclass(slots=True)
class HierarchicalResolutionPlan:
    """The resolved execution plan after querying all cache layers."""

    resolution_tier: CacheResolutionTier
    instant_response: str | None
    matched_prefix_tokens: int
    total_tokens: int
    reuse_ratio: float
    quantization_plan: CompressedKVPlan
    context_opt: ContextOptimizationResult
    estimated_ttft_ms: float
    telemetry: dict[str, Any] = field(default_factory=dict[str, Any])


class HierarchicalCacheCoordinator:
    """
    Coordinates 3-tier caching and context compression for omlxc.
    """

    def __init__(
        self,
        semantic_registry: SemanticCacheRegistry | None = None,
        radix_cache: RadixPrefixCache | None = None,
        paged_kv: PagedKVMemoryManager | None = None,
        snapshot_mgr: StaticPrefixSnapshotManager | None = None,
        quantizer: AdaptiveKVQuantizer | None = None,
        optimizer: ContextOptimizer | None = None,
    ) -> None:
        self.semantic_registry = semantic_registry or SemanticCacheRegistry()
        self.radix_cache = radix_cache or RadixPrefixCache()
        self.paged_kv = paged_kv or PagedKVMemoryManager()
        self.snapshot_mgr = snapshot_mgr or StaticPrefixSnapshotManager()
        self.quantizer = quantizer or AdaptiveKVQuantizer()
        self.optimizer = optimizer or ContextOptimizer()

    def resolve_inference_cache(
        self,
        prompt_text: str,
        token_seq: list[int] | tuple[int, ...] | None = None,
        snapshot_id: str | None = None,
        model_id: str = "qwen-3.8-27b",
    ) -> HierarchicalResolutionPlan:
        """
        Evaluate L1 -> L2 -> L3 cache hierarchy to produce an optimal execution plan.
        """
        # Step 0: Optimize context text
        opt_res = self.optimizer.optimize_text(prompt_text)
        cleaned_text = opt_res.optimized_text

        # Approximate tokens if not provided
        tokens = token_seq or tuple(range(max(1, len(cleaned_text) // 4)))
        total_tokens = len(tokens)
        quant_plan = self.quantizer.plan_compression(total_tokens)

        # Tier 1: Check L1 Semantic / Exact Cache
        l1_entry = self.semantic_registry.lookup_exact(cleaned_text)
        if not l1_entry:
            l1_entry = self.semantic_registry.lookup_semantic(cleaned_text)

        if l1_entry and not l1_entry.is_expired():
            return HierarchicalResolutionPlan(
                resolution_tier=CacheResolutionTier.L1_EXACT_OR_SEMANTIC,
                instant_response=l1_entry.response_content,
                matched_prefix_tokens=total_tokens,
                total_tokens=total_tokens,
                reuse_ratio=1.0,
                quantization_plan=quant_plan,
                context_opt=opt_res,
                estimated_ttft_ms=0.0,
                telemetry={"l1_cache_key": l1_entry.cache_key, "hit_tier": "L1"},
            )

        # Tier 2: Check L2 In-Memory Radix Prefix Tree
        radix_match: PrefixMatchResult = self.radix_cache.match_prefix(tokens)
        if radix_match.matched_tokens > 0:
            reused = radix_match.matched_tokens
            unmatched = total_tokens - reused
            # Each remaining unmatched token requires prefill (~0.1ms/token on M5 Max)
            est_ttft = max(0.5, unmatched * 0.1)

            return HierarchicalResolutionPlan(
                resolution_tier=CacheResolutionTier.L2_RADIX_PREFIX_KV,
                instant_response=None,
                matched_prefix_tokens=reused,
                total_tokens=total_tokens,
                reuse_ratio=radix_match.reuse_ratio,
                quantization_plan=quant_plan,
                context_opt=opt_res,
                estimated_ttft_ms=round(est_ttft, 2),
                telemetry={
                    "matched_blocks_count": len(radix_match.matched_blocks),
                    "radix_reuse_ratio": radix_match.reuse_ratio,
                },
            )

        # Tier 3: Check L3 NVMe Persistent Snapshot
        if snapshot_id and self.snapshot_mgr.is_valid_and_warm(snapshot_id, cleaned_text):
            snap_meta = self.snapshot_mgr.get_snapshot(snapshot_id)
            snap_tokens = snap_meta.token_count if snap_meta else total_tokens
            return HierarchicalResolutionPlan(
                resolution_tier=CacheResolutionTier.L3_DISK_SNAPSHOT_KV,
                instant_response=None,
                matched_prefix_tokens=snap_tokens,
                total_tokens=total_tokens,
                reuse_ratio=round(snap_tokens / total_tokens, 4) if total_tokens > 0 else 1.0,
                quantization_plan=quant_plan,
                context_opt=opt_res,
                estimated_ttft_ms=3.0,  # Ultra-fast NVMe mmap load time
                telemetry={"snapshot_id": snapshot_id, "size_bytes": snap_meta.size_bytes if snap_meta else 0},
            )

        # Cold Prefill fallback
        est_ttft = max(1.0, total_tokens * 0.12)
        return HierarchicalResolutionPlan(
            resolution_tier=CacheResolutionTier.COLD_PREFILL_REQUIRED,
            instant_response=None,
            matched_prefix_tokens=0,
            total_tokens=total_tokens,
            reuse_ratio=0.0,
            quantization_plan=quant_plan,
            context_opt=opt_res,
            estimated_ttft_ms=round(est_ttft, 2),
            telemetry={"cold_prefill": True},
        )

    def record_and_cache_turn(
        self,
        prompt_text: str,
        token_seq: list[int] | tuple[int, ...],
        response_text: str,
        model_id: str = "qwen-3.8-27b",
        seq_id: str | None = None,
    ) -> None:
        """
        Record a completed conversation turn into L1, L2 and Paged memory.
        """
        # 1. Store in L2 Radix Tree
        self.radix_cache.insert_sequence(token_seq)

        # 2. Store in Paged Memory Table if seq_id given
        if seq_id:
            if not self.paged_kv.has_sequence(seq_id):
                self.paged_kv.allocate_sequence(seq_id, len(token_seq), model_id=model_id)
            else:
                self.paged_kv.append_tokens(seq_id, len(token_seq))

        # 3. Store in L1 Semantic cache if repetitive
        self.semantic_registry.store(
            key=prompt_text,
            response_content=response_text,
            model_id=model_id,
        )
