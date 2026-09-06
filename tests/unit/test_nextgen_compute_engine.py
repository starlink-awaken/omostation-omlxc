"""
Unit tests for Next-Gen omlxc V4.0 Sovereign Compute Engine (ADR-0434).
"""

import asyncio

import pytest

from omlxc.dataplane.distributed_kv_pool import (
    DistributedKVPoolManager,
    KVStorageTier,
)
from omlxc.dataplane.entropy_speculator import (
    EntropyAdaptiveSpeculator,
)
from omlxc.dataplane.metal_fused_attention import (
    MetalFusedAttentionEngine,
)
from omlxc.dataplane.predictive_warmup import (
    PredictiveWarmupEngine,
)
from omlxc.dataplane.semantic_quantizer import (
    SemanticKVQuantizer,
)
from omlxc.dataplane.streaming_mesh import (
    StreamingMeshPipeline,
)


def test_entropy_adaptive_speculation() -> None:
    speculator = EntropyAdaptiveSpeculator(min_n=2, max_n=10, base_n_max=7)

    # 1. Test low entropy (deterministic syntax) -> Boost to max_n (10)
    low_entropy = 0.20
    top1_prob = 0.95
    step, reason = speculator.adapt_speculative_step(low_entropy, top1_prob)
    assert step == 10
    assert "MAX_THROUGHPUT_BOOST" in reason

    # 2. Test high entropy (creative/divergent branch) -> Throttle to min_n (2)
    high_entropy = 1.85
    low_top1 = 0.35
    step, reason = speculator.adapt_speculative_step(high_entropy, low_top1)
    assert step == 2
    assert "CONSERVATIVE_THROTTLING" in reason

    # 3. Test speculative tree builder
    tree_res = speculator.build_speculative_tree("def solve():", depth=4, branch_factor=2)
    assert tree_res.total_candidate_tokens > 0
    assert tree_res.tree_depth == 4
    assert tree_res.estimated_speedup > 1.0
    assert len(tree_res.paths) > 0


def test_metal_in_tile_fused_dequant() -> None:
    engine = MetalFusedAttentionEngine(hardware_memory_bandwidth_gbps=800.0)
    profile = engine.profile_fused_execution(
        batch_size=1,
        context_length=8192,
        hidden_dim=4096,
        num_heads=32,
    )

    # Bandwidth reduction should be >= 60%
    assert profile.bandwidth_saved_ratio >= 0.60
    assert profile.fused_vram_bandwidth_gbps < profile.traditional_vram_bandwidth_gbps
    assert profile.temp_vram_saved_mb > 0.0
    assert profile.estimated_tps_gain_percent > 50.0


@pytest.mark.asyncio
async def test_streaming_mesh_pipeline() -> None:
    pipeline = StreamingMeshPipeline()
    receipt = await pipeline.execute_streaming_pipeline(
        pipeline_id="pipe_test_001",
        initial_input="OCR and embed doc",
        num_chunks=3,
        chunk_processing_delay_ms=1.0,
    )

    assert receipt.success is True
    assert receipt.total_chunks == 3
    assert len(receipt.stages) == 3
    assert len(receipt.nodes_involved) == 3
    assert receipt.first_chunk_ttft_ms <= receipt.total_duration_ms


def test_predictive_warmup_engine() -> None:
    engine = PredictiveWarmupEngine()

    # 1. Test code refactoring keystroke stream
    receipt = engine.process_typing_stream("帮我重构 projects/omlxc 的 cluster_coordinator.py")
    assert receipt.predicted_domain == "code_refactor"
    assert receipt.matched_prefix_tokens == 1250
    assert receipt.is_ready_for_zero_ttft is True
    assert "coding" in receipt.target_models or "qwen-3.8-27b-dflash" in receipt.target_models

    # 2. Test governance keystroke stream
    receipt_gov = engine.process_typing_stream("跑一下 gac local gate 验证")
    assert receipt_gov.predicted_domain == "governance_audit"
    assert receipt_gov.matched_prefix_tokens == 1800


def test_distributed_kv_pool() -> None:
    mgr = DistributedKVPoolManager(local_vram_limit_mb=1000.0, mac_mini_memory_limit_mb=500.0)

    # 1. Allocate active turn -> Local memory
    blk1 = mgr.allocate_or_migrate("seq_001", tokens_count=4096, is_active_turn=True)
    assert blk1.tier == KVStorageTier.LOCAL_UNIFIED_MEMORY
    assert blk1.pinned is True

    # 2. Inactive turn -> Offload to Mac mini L3
    blk2 = mgr.allocate_or_migrate("seq_002", tokens_count=4096, is_active_turn=False)
    assert blk2.tier == KVStorageTier.DISTRIBUTED_MAC_MINI_MEMORY
    assert blk2.pinned is False

    status = mgr.get_swarm_status()
    assert status.total_managed_blocks == 2
    assert status.local_memory_blocks == 1
    assert status.distributed_mac_mini_blocks == 1
    assert status.effective_context_multiplier >= 1.0


def test_semantic_quantizer_sinks() -> None:
    quantizer = SemanticKVQuantizer(sink_token_count=8)
    tokens = ["<|im_start|>", "system", "\n", "You", "are", "Antigravity", ".", "\n"] + [
        "def",
        " ",
        "calculate_sum",
        "(",
        "a",
        ":",
        "int",
        ",",
        "b",
        ":",
        "int",
        ")",
        "->",
        "int",
        ":",
        "return",
        " ",
        "a",
        " ",
        "+",
        " ",
        "b",
    ]

    plan = quantizer.generate_semantic_plan(tokens, hidden_dim=4096, num_layers=32)

    assert plan.total_tokens == len(tokens)
    assert plan.sink_tokens == 8
    assert plan.critical_syntax_tokens > 0
    assert plan.quantized_size_mb < plan.raw_fp16_size_mb
    assert plan.compression_ratio < 0.60
    assert plan.perplexity_degradation_percent < 0.05
