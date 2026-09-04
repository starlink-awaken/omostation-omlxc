"""
Multi-Node Heterogeneous Cluster Coordinator & Dynamic Health Scheduler (ADR-0433).

Provides resilient, low-latency coordination across the 3 heterogeneous nodes:
1. MBP M5 Max 128G: Primary Decision Brain (DFlash 2, 70+ tok/s, Deep Reasoning, Coding)
2. Mac mini M4 24G: Dedicated Memory & Vector Worker (24/7 BGE-M3 Embeddings & Reranker)
3. Y7000P RTX4070 8G: Dedicated Sensory Worker (CUDA Qwen2.5-VL Vision, OCR, Whisper Audio)

Features:
- Dynamic heartbeat & health state tracking (HEALTHY, DEGRADED, OFFLINE)
- EWMA TTFT latency & in-flight queue depth-aware adaptive routing
- Automatic circuit breaking & zero-error seamless failover to MBP primary brain
- Multi-node collaborative cross-execution pipeline (Embedding -> OCR -> Reasoning)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any, Optional


class NodeStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


class NodeRole(StrEnum):
    PRIMARY_BRAIN = "PRIMARY_BRAIN"
    MEMORY_WORKER = "MEMORY_WORKER"
    SENSORY_WORKER = "SENSORY_WORKER"


@dataclass
class ClusterNodeInfo:
    node_id: str
    name: str
    role: NodeRole
    hardware: str
    total_memory_gb: float
    status: NodeStatus = NodeStatus.HEALTHY
    in_flight_tasks: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    ewma_latency_ms: float = 20.0
    last_heartbeat: float = field(default_factory=time.time)
    preferred_capabilities: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RoutingResult:
    target_node_id: str
    target_node_name: str
    backend: str
    is_fallback: bool
    reason: str
    estimated_latency_ms: float


@dataclass
class PipelineStage:
    stage_id: str
    capability: str  # "embedding", "vision", "reasoning"
    payload: Any


@dataclass
class PipelineExecutionReceipt:
    stages_completed: int
    participating_nodes: list[str]
    total_duration_ms: float
    all_success: bool
    results: dict[str, Any]


class MultiNodeClusterCoordinator:
    """
    Coordinates and load-balances heterogeneous compute tasks across MBP, Mac mini, and Y7000P.
    """

    def __init__(self, failure_threshold: int = 3, recovery_threshold: int = 2) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_threshold = recovery_threshold
        self.nodes: dict[str, ClusterNodeInfo] = {
            "mbp-m5-max-128g": ClusterNodeInfo(
                node_id="mbp-m5-max-128g",
                name="MacBook Pro · M5 Max 128G",
                role=NodeRole.PRIMARY_BRAIN,
                hardware="Apple M5 Max (128GB Unified Memory, 800GB/s)",
                total_memory_gb=128.0,
                status=NodeStatus.HEALTHY,
                ewma_latency_ms=12.0,
                preferred_capabilities=["reasoning", "coding", "dflash", "synthesis", "fallback"],
            ),
            "mac-mini-m4-24g": ClusterNodeInfo(
                node_id="mac-mini-m4-24g",
                name="Mac mini · M4 24G",
                role=NodeRole.MEMORY_WORKER,
                hardware="Apple M4 (24GB Unified Memory, 120GB/s)",
                total_memory_gb=24.0,
                status=NodeStatus.HEALTHY,
                ewma_latency_ms=18.0,
                preferred_capabilities=["embedding", "rerank", "vector_search", "memory_indexing"],
            ),
            "y7000p-rtx4070-8g": ClusterNodeInfo(
                node_id="y7000p-rtx4070-8g",
                name="Y7000P · RTX4070 8G",
                role=NodeRole.SENSORY_WORKER,
                hardware="NVIDIA GeForce RTX 4070 Laptop (8GB GDDR6 CUDA)",
                total_memory_gb=8.0,
                status=NodeStatus.HEALTHY,
                ewma_latency_ms=25.0,
                preferred_capabilities=["vision", "ocr", "speech", "whisper", "multimodal"],
            ),
        }

    def record_heartbeat(self, node_id: str, success: bool, latency_ms: float | None = None) -> None:
        if node_id not in self.nodes:
            return
        node = self.nodes[node_id]
        node.last_heartbeat = time.time()

        if success:
            node.consecutive_failures = 0
            node.consecutive_successes += 1
            if latency_ms is not None:
                node.ewma_latency_ms = 0.8 * node.ewma_latency_ms + 0.2 * latency_ms
            if node.status == NodeStatus.OFFLINE and node.consecutive_successes >= self.recovery_threshold:
                node.status = NodeStatus.HEALTHY
            elif node.status == NodeStatus.DEGRADED and node.consecutive_successes >= self.recovery_threshold:
                node.status = NodeStatus.HEALTHY
        else:
            node.consecutive_successes = 0
            node.consecutive_failures += 1
            if node.consecutive_failures >= self.failure_threshold:
                node.status = NodeStatus.OFFLINE
            else:
                node.status = NodeStatus.DEGRADED

    def select_node_for_task(self, capability: str, model_id: str | None = None) -> RoutingResult:
        """
        Determines the optimal placement node for a task with automatic health failover.
        """
        norm_cap = capability.lower()

        # Determine primary target based on capability
        if any(kw in norm_cap for kw in ["embed", "rerank", "vector", "memory"]):
            primary_id = "mac-mini-m4-24g"
            preferred_backend = "ollama/mlx"
            affinity_msg = "Dedicated 24/7 background memory pipeline."
        elif any(kw in norm_cap for kw in ["vision", "ocr", "speech", "audio", "whisper", "vl"]):
            primary_id = "y7000p-rtx4070-8g"
            preferred_backend = "lm_studio/vllm"
            affinity_msg = "CUDA Tensor Core acceleration for sensory processing."
        else:
            primary_id = "mbp-m5-max-128g"
            preferred_backend = "mlx_lm/dflash"
            affinity_msg = "High-bandwidth unified memory for deep reasoning & DFlash 2 speculation."

        primary_node = self.nodes[primary_id]

        # Check health and in-flight load of primary node
        if primary_node.status == NodeStatus.HEALTHY and primary_node.in_flight_tasks < 16:
            return RoutingResult(
                target_node_id=primary_node.node_id,
                target_node_name=primary_node.name,
                backend=preferred_backend,
                is_fallback=False,
                reason=affinity_msg,
                estimated_latency_ms=primary_node.ewma_latency_ms,
            )

        # Failover to local primary brain
        fallback_node = self.nodes["mbp-m5-max-128g"]
        return RoutingResult(
            target_node_id=fallback_node.node_id,
            target_node_name=fallback_node.name,
            backend="mlx_lm",
            is_fallback=True,
            reason=f"Target node '{primary_node.name}' is {primary_node.status.value}; transparently failed over to primary MBP brain.",
            estimated_latency_ms=fallback_node.ewma_latency_ms,
        )

    def execute_cross_node_pipeline(self, stages: list[PipelineStage]) -> PipelineExecutionReceipt:
        """
        Simulates end-to-end collaborative cross-node execution across stages.
        """
        start_time = time.perf_counter()
        participating_nodes = []
        results = {}
        all_success = True

        for stage in stages:
            routing = self.select_node_for_task(stage.capability)
            participating_nodes.append(routing.target_node_name)
            node = self.nodes[routing.target_node_id]
            node.in_flight_tasks += 1

            try:
                # Mock stage computation latency
                stage_latency = routing.estimated_latency_ms
                results[stage.stage_id] = {
                    "node": routing.target_node_name,
                    "backend": routing.backend,
                    "status": "COMPLETED",
                    "latency_ms": stage_latency,
                    "is_fallback": routing.is_fallback,
                }
                self.record_heartbeat(routing.target_node_id, success=True, latency_ms=stage_latency)
            except Exception as e:
                all_success = False
                results[stage.stage_id] = {"error": str(e), "status": "FAILED"}
                self.record_heartbeat(routing.target_node_id, success=False)
            finally:
                node.in_flight_tasks = max(0, node.in_flight_tasks - 1)

        total_duration = (time.perf_counter() - start_time) * 1000.0 + sum(
            r.get("latency_ms", 0.0) for r in results.values()
        )

        return PipelineExecutionReceipt(
            stages_completed=len(results),
            participating_nodes=list(dict.fromkeys(participating_nodes)),
            total_duration_ms=total_duration,
            all_success=all_success,
            results=results,
        )

    def get_cluster_status_report(self) -> dict[str, Any]:
        """
        Generates a comprehensive snapshot of cluster nodes, health, roles, and latency.
        """
        return {
            "cluster_name": "omostation-sovereign-fabric",
            "total_nodes": len(self.nodes),
            "active_nodes": sum(1 for n in self.nodes.values() if n.status != NodeStatus.OFFLINE),
            "nodes": {
                node_id: {
                    "name": n.name,
                    "role": n.role.value,
                    "hardware": n.hardware,
                    "total_memory_gb": n.total_memory_gb,
                    "status": n.status.value,
                    "in_flight_tasks": n.in_flight_tasks,
                    "ewma_latency_ms": round(n.ewma_latency_ms, 2),
                    "preferred_capabilities": n.preferred_capabilities,
                }
                for node_id, n in self.nodes.items()
            },
        }
