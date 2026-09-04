"""
Pre-Emptive KV Cache and Model VRAM Budget Estimator for omlxc.

Calculates dynamic key-value cache memory expansion for long-context requests
(32k~128k) to prevent out-of-memory (OOM) kernel crashes and Metal/CUDA swap storms.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class HeadroomAdmissionResult:
    """Result of KV Cache headroom evaluation with compaction advisory."""

    admitted: bool
    estimated_kv_mb: float
    reason: str
    compaction_advised: bool = False
    max_safe_tokens: int = 0
    recommended_compaction_ratio: float = 0.0

    def __iter__(self):
        yield self.admitted
        yield self.estimated_kv_mb
        yield self.reason


@dataclass(frozen=True, slots=True)
class ModelArchitectureMeta:
    """Transformer structural dimensions determining KV Cache memory growth."""

    model_id: str
    num_layers: int
    num_kv_heads: int
    head_dim: int
    bytes_per_elem: int = 2  # FP16 / BF16 = 2 bytes
    weights_vram_mb: float = 0.0

    @property
    def bytes_per_token(self) -> int:
        """KV Cache bytes per token = 2 (K+V) * layers * kv_heads * head_dim * dtype_bytes."""
        return 2 * self.num_layers * self.num_kv_heads * self.head_dim * self.bytes_per_elem


# Known profiles for local models in omostation cluster
DEFAULT_ARCH_PROFILES: Final[dict[str, ModelArchitectureMeta]] = {
    # 70B/72B class models
    "qwen-72b": ModelArchitectureMeta("qwen-72b", 80, 8, 128, 2, 42000.0),
    "deepseek-v3": ModelArchitectureMeta("deepseek-v3", 61, 8, 128, 2, 38000.0),
    # 27B~35B class models
    "qwen-3.8-27b": ModelArchitectureMeta("qwen-3.8-27b", 64, 8, 128, 2, 13500.0),
    "qwen-3.8-27b-dflash": ModelArchitectureMeta("qwen-3.8-27b-dflash", 64, 8, 128, 2, 18500.0),
    "coding": ModelArchitectureMeta("coding", 64, 8, 128, 2, 17500.0),
    # 9B~14B class models
    "qwen-3.5-9b": ModelArchitectureMeta("qwen-3.5-9b", 32, 4, 128, 2, 6200.0),
    "gemma-9b": ModelArchitectureMeta("gemma-9b", 42, 8, 256, 2, 6800.0),
    # 2B~4B class lightweight models
    "gemma-4b": ModelArchitectureMeta("gemma-4b", 26, 4, 256, 2, 2800.0),
    "gemma-2b": ModelArchitectureMeta("gemma-2b", 18, 1, 256, 2, 1600.0),
}


def reclaim_metal_memory_pool() -> dict[str, Any]:
    """
    Forcefully reclaims Metal cache and invokes garbage collection post-inference.
    Prevents long-running VRAM memory leaks and fragmentation.
    """
    import gc
    reclaimed_stats = {"gc_collected": 0, "metal_cleared": False}
    try:
        # If mlx is available in python environment
        import mlx.core as mx
        if hasattr(mx, "metal") and hasattr(mx.metal, "clear_cache"):
            mx.metal.clear_cache()
            reclaimed_stats["metal_cleared"] = True
    except Exception:
        pass
    reclaimed_stats["gc_collected"] = gc.collect()
    return reclaimed_stats


class VRAMPressureTier(StrEnum):
    GREEN = "green"    # < 70%: Fully safe, background indexing allowed
    YELLOW = "yellow"  # 70% ~ 75%: Soft threshold, defer P2 background tasks
    ORANGE = "orange"  # 75% ~ 82%: Compaction recommended, P1 throttled
    RED = "red"        # >= 82%: Hard ceiling, admission rejected to prevent swap storm


@dataclass(frozen=True, slots=True)
class TieredHeadroomResult:
    admitted: bool
    pressure_tier: VRAMPressureTier
    estimated_kv_mb: float
    total_projected_mb: float
    safe_ceiling_mb: float
    system_reserved_mb: float
    reason: str
    compaction_advised: bool
    max_safe_tokens: int
    recommended_compaction_ratio: float


def enforce_tiered_headroom_admission(
    model_id: str,
    requested_tokens: int,
    current_used_vram_mb: float,
    total_node_vram_mb: float = 131072.0,  # 128GB default for MBP M5 Max
    normal_safe_ratio: float = 0.75,       # 75% (~96GB) balanced allocation
    soft_warning_ratio: float = 0.70,      # 70% (~89.6GB) soft compaction warning
    emergency_hard_ratio: float = 0.82,    # 82% (~107.5GB) emergency limit
) -> TieredHeadroomResult:
    """
    Tiered dynamic VRAM admission governor.
    Balances maximum hardware utilization while reserving 25GB~32GB dedicated headroom
    for macOS, Xcode, browsers, and desktop responsiveness.
    """
    meta = DEFAULT_ARCH_PROFILES.get(model_id)
    if not meta:
        meta = ModelArchitectureMeta(model_id=model_id, num_layers=32, num_kv_heads=4, head_dim=128, bytes_per_elem=1)

    projected_kv_mb = (meta.bytes_per_token * requested_tokens) / (1024.0 * 1024.0)
    total_projected_mb = current_used_vram_mb + projected_kv_mb

    soft_mb = total_node_vram_mb * soft_warning_ratio
    safe_mb = total_node_vram_mb * normal_safe_ratio
    hard_mb = total_node_vram_mb * emergency_hard_ratio
    reserved_mb = total_node_vram_mb - total_projected_mb

    # Determine Pressure Tier
    if total_projected_mb < soft_mb:
        tier = VRAMPressureTier.GREEN
    elif total_projected_mb < safe_mb:
        tier = VRAMPressureTier.YELLOW
    elif total_projected_mb < hard_mb:
        tier = VRAMPressureTier.ORANGE
    else:
        tier = VRAMPressureTier.RED

    if tier == VRAMPressureTier.RED:
        safe_bytes = max(0.0, (safe_mb - current_used_vram_mb) * 1024.0 * 1024.0)
        safe_tokens = int(safe_bytes / meta.bytes_per_token) if meta.bytes_per_token > 0 else 0
        return TieredHeadroomResult(
            admitted=False,
            pressure_tier=tier,
            estimated_kv_mb=projected_kv_mb,
            total_projected_mb=total_projected_mb,
            safe_ceiling_mb=safe_mb,
            system_reserved_mb=reserved_mb,
            reason=(
                f"Emergency limit reached: {total_projected_mb:.1f} MB exceeds {emergency_hard_ratio*100:.0f}% "
                f"ceiling ({hard_mb:.1f} MB). OS Swap protection active."
            ),
            compaction_advised=True,
            max_safe_tokens=max(0, safe_tokens),
            recommended_compaction_ratio=round(max(0.0, 1.0 - (safe_tokens / max(requested_tokens, 1))), 4),
        )

    compaction_advised = tier in (VRAMPressureTier.YELLOW, VRAMPressureTier.ORANGE)
    return TieredHeadroomResult(
        admitted=True,
        pressure_tier=tier,
        estimated_kv_mb=projected_kv_mb,
        total_projected_mb=total_projected_mb,
        safe_ceiling_mb=safe_mb,
        system_reserved_mb=reserved_mb,
        reason=f"Admitted ({tier.value.upper()}): {total_projected_mb:.1f} MB <= {normal_safe_ratio*100:.0f}% budget ({safe_mb:.1f} MB)",
        compaction_advised=compaction_advised,
        max_safe_tokens=requested_tokens,
        recommended_compaction_ratio=0.15 if tier == VRAMPressureTier.ORANGE else 0.0,
    )


def enforce_strict_headroom_admission(
    model_id: str,
    requested_tokens: int,
    current_used_vram_mb: float,
    max_hard_quota_mb: float = 98304.0,  # Balanced 75% default on 128GB (96GB)
) -> HeadroomAdmissionResult:
    """
    Backwards-compatible wrapper delegating to balanced tiered admission.
    """
    res = enforce_tiered_headroom_admission(
        model_id=model_id,
        requested_tokens=requested_tokens,
        current_used_vram_mb=current_used_vram_mb,
        total_node_vram_mb=max_hard_quota_mb / 0.75 if max_hard_quota_mb > 0 else 131072.0,
    )
    return HeadroomAdmissionResult(
        admitted=res.admitted,
        estimated_kv_mb=res.estimated_kv_mb,
        reason=res.reason,
        compaction_advised=res.compaction_advised,
        max_safe_tokens=res.max_safe_tokens,
        recommended_compaction_ratio=res.recommended_compaction_ratio,
    )


class VRAMBudgetEstimator:
    """Estimates dynamic KV Cache growth and evaluates placement admission."""

    def __init__(self, custom_profiles: dict[str, ModelArchitectureMeta] | None = None) -> None:
        self._profiles = dict(DEFAULT_ARCH_PROFILES)
        if custom_profiles:
            self._profiles.update(custom_profiles)

    @property
    def registered_models(self) -> tuple[str, ...]:
        """List all model identifiers with registered architecture profiles."""
        return tuple(self._profiles.keys())

    def get_profile(self, model_id: str) -> ModelArchitectureMeta:
        """Resolve architecture profile or fallback to a standard 14B profile."""
        if model_id in self._profiles:
            return self._profiles[model_id]
        # Generic fallback: 32 layers, 4 kv heads, 128 head dim
        return ModelArchitectureMeta(model_id=model_id, num_layers=32, num_kv_heads=4, head_dim=128)

    def estimate_kv_cache_mb(
        self,
        model_id: str,
        context_tokens: int,
        max_output_tokens: int = 1024,
    ) -> float:
        """Calculate estimated KV Cache footprint in megabytes (MB)."""
        profile = self.get_profile(model_id)
        total_tokens = max(context_tokens + max_output_tokens, 1)
        total_bytes = profile.bytes_per_token * total_tokens
        return round(total_bytes / (1024.0 * 1024.0), 2)

    def estimate_total_vram_mb(
        self,
        model_id: str,
        context_tokens: int,
        max_output_tokens: int = 1024,
    ) -> float:
        """Calculate total VRAM needed (weights + KV Cache)."""
        profile = self.get_profile(model_id)
        kv_mb = self.estimate_kv_cache_mb(model_id, context_tokens, max_output_tokens)
        return round(profile.weights_vram_mb + kv_mb, 2)

    def check_headroom_admission(
        self,
        model_id: str,
        context_tokens: int,
        available_node_vram_mb: float,
        safe_headroom_ratio: float = 0.85,
        max_output_tokens: int = 1024,
    ) -> HeadroomAdmissionResult:
        """
        Check if request KV Cache fits within available node memory budget.

        Returns HeadroomAdmissionResult with admission decision and compaction advisory.
        """
        profile = self.get_profile(model_id)
        kv_mb = self.estimate_kv_cache_mb(model_id, context_tokens, max_output_tokens)
        safe_budget_mb = available_node_vram_mb * safe_headroom_ratio

        if kv_mb > safe_budget_mb:
            safe_bytes = safe_budget_mb * 1024.0 * 1024.0
            max_safe_total_tokens = int(safe_bytes / profile.bytes_per_token) if profile.bytes_per_token > 0 else 0
            max_safe_tokens = max(0, max_safe_total_tokens - max_output_tokens)
            compaction_ratio = round(max(0.0, 1.0 - (max_safe_tokens / max(context_tokens, 1))), 4)
            return HeadroomAdmissionResult(
                admitted=False,
                estimated_kv_mb=kv_mb,
                reason=(
                    f"estimated KV Cache ({kv_mb} MB) exceeds safe node headroom "
                    f"({safe_budget_mb:.1f} MB out of {available_node_vram_mb:.1f} MB)"
                ),
                compaction_advised=True,
                max_safe_tokens=max_safe_tokens,
                recommended_compaction_ratio=compaction_ratio,
            )
        return HeadroomAdmissionResult(
            admitted=True,
            estimated_kv_mb=kv_mb,
            reason=f"admitted: {kv_mb} MB within safe headroom ({safe_budget_mb:.1f} MB)",
            compaction_advised=False,
            max_safe_tokens=context_tokens,
            recommended_compaction_ratio=0.0,
        )


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Outcome of sliding context window distillation."""

    original_tokens: int
    compacted_tokens: int
    pruned_tokens: int
    compression_ratio: float
    retained_messages_count: int
    compacted_messages: list[dict[str, str]]
    distilled_summary: str | None


class ContextCompactor:
    """Sliding-window context compactor with semantic recency preservation."""

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Heuristic token estimator (approx 4 chars/token for code/EN, 1.5 chars for CJK)."""
        if not text:
            return 0
        cjk_chars = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        non_cjk_chars = len(text) - cjk_chars
        return int(cjk_chars / 1.5 + non_cjk_chars / 4.0) + 1

    @classmethod
    def compact_messages(
        cls,
        messages: list[dict[str, str]],
        target_safe_tokens: int,
        keep_recent_turns: int = 2,
    ) -> CompactionResult:
        """
        Compact conversation messages into target token budget while preserving recency.
        """
        if not messages:
            return CompactionResult(0, 0, 0, 0.0, 0, [], None)

        total_tokens = sum(cls.estimate_tokens(m.get("content", "")) for m in messages)
        if total_tokens <= target_safe_tokens or len(messages) <= keep_recent_turns + 1:
            return CompactionResult(
                original_tokens=total_tokens,
                compacted_tokens=total_tokens,
                pruned_tokens=0,
                compression_ratio=0.0,
                retained_messages_count=len(messages),
                compacted_messages=messages,
                distilled_summary=None,
            )

        # 1. Separate system prompt (if first message), recent turns, and middle messages
        system_msg: dict[str, str] | None = messages[0] if messages[0].get("role") == "system" else None
        body = messages[1:] if system_msg else messages
        recent_cutoff = max(0, len(body) - keep_recent_turns)
        middle_msgs = body[:recent_cutoff]
        recent_msgs = body[recent_cutoff:]

        # 2. Distill middle messages into a structured bullet summary
        distilled_lines: list[str] = []
        for msg in middle_msgs:
            role = msg.get("role", "user")
            content = msg.get("content", "").strip()
            snippet = content[:120].replace("\n", " ") + ("..." if len(content) > 120 else "")
            distilled_lines.append(f"- [{role}]: {snippet}")

        distilled_summary = (
            "[Auto-Compacted Context Window Summary]\n" + "\n".join(distilled_lines) + "\n[End of Compacted Summary]"
        )

        compacted_body: list[dict[str, str]] = [
            {"role": "system", "content": distilled_summary},
            *recent_msgs,
        ]
        compacted_messages: list[dict[str, str]] = [system_msg, *compacted_body] if system_msg else compacted_body

        compacted_tokens = sum(cls.estimate_tokens(m.get("content", "")) for m in compacted_messages)
        pruned_tokens = max(0, total_tokens - compacted_tokens)
        ratio = round(pruned_tokens / max(total_tokens, 1), 4)

        return CompactionResult(
            original_tokens=total_tokens,
            compacted_tokens=compacted_tokens,
            pruned_tokens=pruned_tokens,
            compression_ratio=ratio,
            retained_messages_count=len(compacted_messages),
            compacted_messages=compacted_messages,
            distilled_summary=distilled_summary,
        )
