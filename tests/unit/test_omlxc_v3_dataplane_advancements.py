"""
omlxc V3.5 数据面全栈增强单元测试：
- DFlash 2 块扩散投机解码
- 异构三节点智能分工
- 75% 柔性阶梯显存治理
- 多 Agent 动态优先级调度
- 电源与电池自适应画像
"""

import pytest
from omlxc.dataplane.dflash_backend import DFlashConfig, DFlashBackendManager
from omlxc.dataplane.cluster_partition import HeterogeneousClusterRouter
from omlxc.dataplane.power_profile import PowerProfileGovernor, PowerSource
from omlxc.dataplane.priority_queue import PriorityVRAMScheduler, QueuedInferenceRequest, TaskPriority
from omlxc.dataplane.vram_budget import (
    VRAMPressureTier,
    enforce_tiered_headroom_admission,
    enforce_strict_headroom_admission,
    reclaim_metal_memory_pool,
)
from omlxc.dataplane.prefix_snapshot import StaticPrefixSnapshotManager


def test_dflash_backend_config_and_scaling():
    cfg = DFlashConfig(
        target_model_path="/Users/xiamingxing/omlx/models/Qwen3.8-27B-UD-Q4_K_XL.gguf",
        draft_model_path="/Users/xiamingxing/omlx/models/Qwen3.8-27B-DFlash2-Q8_0.gguf",
        port=8196,
        spec_draft_n_max=7,
    )
    args = cfg.build_cli_args("/usr/local/bin/llama-server")
    assert "--spec-type" in args
    assert "draft-dflash" in args
    assert "--spec-draft-n-max" in args
    assert "7" in args

    mgr = DFlashBackendManager(config=cfg)
    assert mgr.adjust_for_thermal_state("NOMINAL") == 7
    assert mgr.adjust_for_thermal_state("FAIR") == 4
    assert mgr.adjust_for_thermal_state("SERIOUS") == 0


def test_heterogeneous_cluster_routing():
    # 1. 向量 -> Mac mini M4
    assert HeterogeneousClusterRouter.route_model("embed-bge-m3").target_node_id == "mac-mini-m4-24g"
    # 2. 视觉 -> Y7000P RTX4070
    assert HeterogeneousClusterRouter.route_model("vision").target_node_id == "y7000p-rtx4070-8g"
    # 3. 核心大模型 -> MBP M5 Max
    assert HeterogeneousClusterRouter.route_model("qwen-3.8-27b-dflash").target_node_id == "mbp-m5-max-128g"


def test_tiered_headroom_admission_safety():
    res = enforce_tiered_headroom_admission(
        model_id="qwen-3.8-27b-dflash",
        requested_tokens=32768,
        current_used_vram_mb=25000.0,
        total_node_vram_mb=131072.0,
    )
    assert res.admitted is True
    assert res.pressure_tier == VRAMPressureTier.GREEN
    assert res.system_reserved_mb > 95000.0


def test_priority_scheduler_and_power_profile():
    scheduler = PriorityVRAMScheduler(total_node_vram_mb=131072.0)
    scheduler.set_vram_baseline(30000.0)
    req = QueuedInferenceRequest("r-1", "qwen-3.8-27b-dflash", TaskPriority.P0_INTERACTIVE, 4096)
    assert scheduler.acquire_slot(req) is True

    ac = PowerProfileGovernor.get_profile(PowerSource.AC)
    batt = PowerProfileGovernor.get_profile(PowerSource.BATTERY)
    assert ac.spec_draft_n_max == 7
    assert batt.spec_draft_n_max == 4
    assert batt.power_reduction_pct == 60.0


def test_metal_reclamation():
    rec = reclaim_metal_memory_pool()
    assert "gc_collected" in rec
