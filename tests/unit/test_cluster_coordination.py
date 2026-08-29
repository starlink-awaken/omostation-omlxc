"""
Unit tests for MultiNodeClusterCoordinator & Dynamic Failover (ADR-0433).
"""

from omlxc.dataplane.cluster_coordinator import (
    MultiNodeClusterCoordinator,
    NodeRole,
    NodeStatus,
    PipelineStage,
)


def test_cluster_coordinator_initial_topology():
    coord = MultiNodeClusterCoordinator()
    report = coord.get_cluster_status_report()
    assert report["total_nodes"] == 3
    assert report["active_nodes"] == 3
    assert "mbp-m5-max-128g" in report["nodes"]
    assert "mac-mini-m4-24g" in report["nodes"]
    assert "y7000p-rtx4070-8g" in report["nodes"]
    assert report["nodes"]["mbp-m5-max-128g"]["role"] == NodeRole.PRIMARY_BRAIN.value


def test_cluster_routing_affinity_and_fallback():
    coord = MultiNodeClusterCoordinator()

    # 1. Vector task -> routes to Mac mini M4
    r1 = coord.select_node_for_task("embedding")
    assert r1.target_node_id == "mac-mini-m4-24g"
    assert not r1.is_fallback

    # 2. Vision task -> routes to Y7000P
    r2 = coord.select_node_for_task("vision_ocr")
    assert r2.target_node_id == "y7000p-rtx4070-8g"
    assert not r2.is_fallback

    # 3. Simulate Y7000P offline (consecutive failures >= 3)
    coord.record_heartbeat("y7000p-rtx4070-8g", success=False)
    coord.record_heartbeat("y7000p-rtx4070-8g", success=False)
    coord.record_heartbeat("y7000p-rtx4070-8g", success=False)
    assert coord.nodes["y7000p-rtx4070-8g"].status == NodeStatus.OFFLINE

    # 4. Vision task now transparently fails over to MBP M5 Max
    r3 = coord.select_node_for_task("vision_ocr")
    assert r3.target_node_id == "mbp-m5-max-128g"
    assert r3.is_fallback
    assert "OFFLINE" in r3.reason

    # 5. Y7000P recovers with heartbeats
    coord.record_heartbeat("y7000p-rtx4070-8g", success=True, latency_ms=15.0)
    coord.record_heartbeat("y7000p-rtx4070-8g", success=True, latency_ms=14.0)
    assert coord.nodes["y7000p-rtx4070-8g"].status == NodeStatus.HEALTHY

    # 6. Vision task routes back to Y7000P
    r4 = coord.select_node_for_task("vision_ocr")
    assert r4.target_node_id == "y7000p-rtx4070-8g"
    assert not r4.is_fallback


def test_cross_node_collaborative_pipeline_execution():
    coord = MultiNodeClusterCoordinator()
    stages = [
        PipelineStage(stage_id="s1_embed", capability="embedding", payload={"text": "query"}),
        PipelineStage(stage_id="s2_vision", capability="vision", payload={"image_path": "/tmp/img.png"}),
        PipelineStage(stage_id="s3_reason", capability="reasoning", payload={"prompt": "Synthesize context"}),
    ]

    receipt = coord.execute_cross_node_pipeline(stages)
    assert receipt.all_success
    assert receipt.stages_completed == 3
    assert len(receipt.participating_nodes) == 3
    assert "s1_embed" in receipt.results
    assert "s2_vision" in receipt.results
    assert "s3_reason" in receipt.results
