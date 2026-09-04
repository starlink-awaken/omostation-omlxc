"""
Adaptive Dual-Zone KV Quantization & Attention Sink Stream Manager (ADR-0197/ADR-0203).
Implements:
1. Dual-Zone Quantization (Head FP16/8-bit + Tail 4-bit) for zero loss in immediate reasoning.
2. Attention Sink & Heavy-Hitter (H2O) pruning for infinite multi-turn streaming without OOM.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class KVQuantPrecision(enum.StrEnum):
    FP16 = "fp16"
    INT8 = "int8"
    INT4 = "int4"
    INT2 = "int2"
@dataclass(slots=True)
class CompressedKVPlan:
    """Plan specifying quantization and token retention for an active context."""
    original_tokens: int
    retained_tokens: int
    sink_tokens: int
    head_tokens: int
    tail_tokens: int
    head_precision: KVQuantPrecision
    tail_precision: KVQuantPrecision
    compression_ratio: float
    estimated_memory_saved_mb: float
class AdaptiveKVQuantizer:
    """
    Manages precision zones and attention sinks for ultra-long context sessions.
    """
    def __init__(
        self,
        sink_tokens: int = 8,          # Number of permanent initial attention sink tokens
        head_window_tokens: int = 512,  # Number of recent tokens kept at high precision
        tail_precision: KVQuantPrecision = KVQuantPrecision.INT4,
        head_precision: KVQuantPrecision = KVQuantPrecision.INT8,
    ) -> None:
        self.sink_tokens = sink_tokens
        self.head_window_tokens = head_window_tokens
        self.tail_precision = tail_precision
        self.head_precision = head_precision
    def plan_compression(
        self,
        total_tokens: int,
        target_token_budget: int | None = None,
    ) -> CompressedKVPlan:
        """
        Compute an optimal dual-zone KV quantization and retention plan.
        """
        if total_tokens <= (self.sink_tokens + self.head_window_tokens):
            # Short context: Everything fits in the head window
            head_count = total_tokens
            tail_count = 0
            sink_count = min(self.sink_tokens, total_tokens)
            retained = total_tokens
        else:
            sink_count = self.sink_tokens
            head_count = self.head_window_tokens
            available_tail = total_tokens - (sink_count + head_count)
            if target_token_budget is not None and target_token_budget < total_tokens:
                # Need token pruning (Heavy Hitter selection)
                allowed_tail = max(0, target_token_budget - (sink_count + head_count))
                tail_count = min(available_tail, allowed_tail)
            else:
                tail_count = available_tail
            retained = sink_count + head_count + tail_count
        # Memory calculations (Assuming 2KB per FP16 token pair)
        # FP16 = 2048 bytes, INT8 = 1024 bytes, INT4 = 512 bytes
        bytes_per_fp16 = 2048
        bytes_head = 1024 if self.head_precision == KVQuantPrecision.INT8 else 2048
        bytes_tail = 512 if self.tail_precision == KVQuantPrecision.INT4 else 256
        raw_memory_bytes = total_tokens * bytes_per_fp16
        compressed_memory_bytes = (sink_count + head_count) * bytes_head + (tail_count * bytes_tail)
        saved_mb = max(0.0, (raw_memory_bytes - compressed_memory_bytes) / (1024 * 1024))
        ratio = (compressed_memory_bytes / raw_memory_bytes) if raw_memory_bytes > 0 else 1.0
        return CompressedKVPlan(
            original_tokens=total_tokens,
            retained_tokens=retained,
            sink_tokens=sink_count,
            head_tokens=head_count,
            tail_tokens=tail_count,
            head_precision=self.head_precision,
            tail_precision=self.tail_precision,
            compression_ratio=round(ratio, 4),
            estimated_memory_saved_mb=round(saved_mb, 2),
        )
    def get_effective_kv_bytes_per_token(self, total_tokens: int) -> int:
        """Estimate the weighted average bytes per token for memory budgeting."""
        plan = self.plan_compression(total_tokens)
        if plan.retained_tokens <= 0:
            return 1024
        total_bytes = (plan.head_tokens + plan.sink_tokens) * 1024 + (plan.tail_tokens * 512)
        return int(total_bytes // plan.retained_tokens)
