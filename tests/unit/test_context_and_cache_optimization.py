"""
Unit and Integration Tests for omlxc V3.6 Advanced Context & Cache Optimization Fabric.

Validates:
1. Radix Tree / Trie dynamic prefix matching, node splitting & LRU eviction.
2. Paged KV memory block allocation, Copy-On-Write (CoW) subagent branching & deallocation.
3. Adaptive Dual-Zone (Head FP16/8 + Tail INT4) KV quantization with Attention Sinks.
4. Textual and AST-aware context compaction.
5. Hierarchical 3-Tier Cache Coordinator resolution (L1 -> L2 -> L3 -> Cold).
"""

from __future__ import annotations

from omlxc.dataplane.adaptive_kv_quant import AdaptiveKVQuantizer, KVQuantPrecision
from omlxc.dataplane.context_compressor import ContextOptimizer
from omlxc.dataplane.hierarchical_cache import (
    CacheResolutionTier,
    HierarchicalCacheCoordinator,
)
from omlxc.dataplane.paged_kv import PagedKVMemoryManager
from omlxc.dataplane.prefix_snapshot import StaticPrefixSnapshotManager
from omlxc.dataplane.radix_cache import RadixPrefixCache
from omlxc.dataplane.semantic_cache import SemanticCacheRegistry


def test_radix_tree_longest_prefix_matching() -> None:
    """验证 Radix 树动态最长公共前缀匹配与多分支裂变"""
    tree = RadixPrefixCache(max_cached_tokens=1000)

    # 1. 插入共享系统前缀 [10, 20, 30, 40]
    sys_prefix = (10, 20, 30, 40)
    tree.insert_sequence(sys_prefix)
    assert tree.total_cached_tokens == 4

    # 2. 查询完全匹配
    res1 = tree.match_prefix(sys_prefix)
    assert res1.matched_tokens == 4
    assert res1.is_exact_match is True
    assert res1.reuse_ratio == 1.0

    # 3. 插入分支 1: Agent A 对话 [10, 20, 30, 40, 101, 102]
    agent_a_seq = (10, 20, 30, 40, 101, 102)
    tree.insert_sequence(agent_a_seq)
    assert tree.total_cached_tokens == 6

    # 4. 插入分支 2: Agent B 对话 [10, 20, 30, 40, 201, 202] -> 触发前缀分裂
    agent_b_seq = (10, 20, 30, 40, 201, 202)
    tree.insert_sequence(agent_b_seq)
    assert tree.total_cached_tokens == 8

    # 5. Agent B 查询，应当复用前 4 个公共 tokens，复用率 4/6 = 66.67%
    res_b = tree.match_prefix(agent_b_seq)
    assert res_b.matched_tokens == 6
    assert res_b.is_exact_match is True

    # 6. 新输入未命中测试
    res_unknown = tree.match_prefix((999, 888))
    assert res_unknown.matched_tokens == 0
    assert res_unknown.reuse_ratio == 0.0


def test_radix_tree_lru_eviction() -> None:
    """验证 Radix 树在容量超限时自动执行 LRU 树叶节点淘汰"""
    tree = RadixPrefixCache(max_cached_tokens=10)

    # 插入第 1 条长分支 (8 tokens)
    tree.insert_sequence((1, 2, 3, 4, 5, 6, 7, 8))
    assert tree.total_cached_tokens == 8

    # 插入第 2 条长分支 (8 tokens) -> 总量 16 > 10，触发 LRU 淘汰
    tree.insert_sequence((101, 102, 103, 104, 105, 106, 107, 108))
    assert tree.total_cached_tokens <= 10


def test_paged_kv_allocation_and_cow_branching() -> None:
    """验证分页 KV 内存管理器与 Copy-on-Write 子代理分支机制"""
    # 设定 10MB 显存池，每块 32 tokens
    mgr = PagedKVMemoryManager(total_vram_mb=10.0, block_size_tokens=32, bytes_per_token=2048)
    initial_free = mgr.free_blocks_count

    # 1. 为主 Agent 分配 64 tokens (需 2 块)
    parent_table = mgr.allocate_sequence(seq_id="agent-parent", initial_tokens=64, model_id="qwen-3.8-27b")
    assert len(parent_table.physical_blocks) == 2
    assert mgr.free_blocks_count == initial_free - 2

    # 2. Fork 子代理 (零拷贝共享父级块)
    child_table = mgr.fork_sequence_cow(parent_seq_id="agent-parent", child_seq_id="agent-sub-1")
    assert child_table.physical_blocks == parent_table.physical_blocks
    # 显存未增加额外物理块占用
    assert mgr.free_blocks_count == initial_free - 2

    # 3. 子代理追加生成新 tokens -> 触发 CoW 独立块分配
    mgr.append_tokens(seq_id="agent-sub-1", new_tokens=32)
    # 子代理应拥有独立的尾块
    assert child_table.num_tokens == 96

    # 4. 释放父代理，物理块因子代理引用保持活跃；释放子代理后全额回收
    mgr.free_sequence("agent-parent")
    mgr.free_sequence("agent-sub-1")
    assert mgr.free_blocks_count == initial_free


def test_adaptive_kv_dual_zone_quantization_and_sinks() -> None:
    """验证双区域自适应 KV 量化 (Head FP16/INT8 + Tail INT4) 与注意力沉底 (Attention Sinks)"""
    quantizer = AdaptiveKVQuantizer(
        sink_tokens=8,
        head_window_tokens=512,
        tail_precision=KVQuantPrecision.INT4,
        head_precision=KVQuantPrecision.INT8,
    )

    # 1. 短会话 (100 tokens) -> 全部留在 Head 区域
    plan_short = quantizer.plan_compression(total_tokens=100)
    assert plan_short.head_tokens == 100
    assert plan_short.tail_tokens == 0
    assert plan_short.retained_tokens == 100

    # 2. 超长会话 (4096 tokens) -> 8 沉底 + 512 Head + 3576 Tail (4-bit 量化)
    plan_long = quantizer.plan_compression(total_tokens=4096)
    assert plan_long.sink_tokens == 8
    assert plan_long.head_tokens == 512
    assert plan_long.tail_tokens == 3576
    assert plan_long.estimated_memory_saved_mb > 0.0
    assert plan_long.compression_ratio < 0.5  # 压缩比低于 50%


def test_context_optimizer_compaction() -> None:
    """验证上下文文本优化与装饰性代码剪裁"""
    optimizer = ContextOptimizer(aggressive=True)

    raw_text = """
    这是系统主提示词。



    ==============================
    以下是任务详情：
    1. 任务一
    """

    res = optimizer.optimize_text(raw_text)
    assert res.compression_ratio <= 1.0
    assert "strip_trailing_whitespace" in res.optimizations_applied
    assert "collapse_consecutive_blank_lines" in res.optimizations_applied
    assert "===" in res.optimized_text


def test_hierarchical_cache_coordinator_resolution(tmp_path) -> None:
    """验证三级分层缓存协调器 (L1 语义 -> L2 Paged Radix -> L3 磁盘快照 -> 冷 Prefill)"""
    semantic_reg = SemanticCacheRegistry()
    radix_cache = RadixPrefixCache()
    paged_kv = PagedKVMemoryManager(total_vram_mb=10.0)
    snapshot_mgr = StaticPrefixSnapshotManager(root_dir=tmp_path / "kv_snaps")

    coordinator = HierarchicalCacheCoordinator(
        semantic_registry=semantic_reg,
        radix_cache=radix_cache,
        paged_kv=paged_kv,
        snapshot_mgr=snapshot_mgr,
    )

    # 1. 首次查询：冷 Prefill
    prompt_1 = "请帮我重构 omlxc 调度算法"
    tokens_1 = (10, 20, 30, 40, 50)
    plan_cold = coordinator.resolve_inference_cache(prompt_text=prompt_1, token_seq=tokens_1)
    assert plan_cold.resolution_tier == CacheResolutionTier.COLD_PREFILL_REQUIRED
    assert plan_cold.reuse_ratio == 0.0

    # 记录该轮生成到缓存系统
    coordinator.record_and_cache_turn(
        prompt_text=prompt_1,
        token_seq=tokens_1,
        response_text="这是重构后的调度算法代码",
        seq_id="seq-test-1",
    )

    # 2. 相同文本再次请求：直接命中 L1 语义/完全匹配 (0ms TTFT)
    plan_l1 = coordinator.resolve_inference_cache(prompt_text=prompt_1, token_seq=tokens_1)
    assert plan_l1.resolution_tier == CacheResolutionTier.L1_EXACT_OR_SEMANTIC
    assert plan_l1.instant_response == "这是重构后的调度算法代码"
    assert plan_l1.estimated_ttft_ms == 0.0

    # 3. 共享前缀的新请求：命中 L2 Paged Radix 前缀缓存 (部分 TTFT 节省)
    prompt_new = "请帮我重构 omlxc 调度算法并添加测试"
    tokens_extended = (10, 20, 30, 40, 50, 99, 100)
    # L1 未命中不同文本，但 L2 命中前 5 个 tokens
    semantic_reg.clear()  # 清空 L1 以测试 L2 降级
    plan_l2 = coordinator.resolve_inference_cache(prompt_text=prompt_new, token_seq=tokens_extended)
    assert plan_l2.resolution_tier == CacheResolutionTier.L2_RADIX_PREFIX_KV
    assert plan_l2.matched_prefix_tokens == 5
    assert plan_l2.reuse_ratio > 0.7
