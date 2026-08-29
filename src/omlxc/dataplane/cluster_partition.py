"""
Heterogeneous Cluster Workload Partitioning & Offloading Router (ADR-0205).

Coordinates model routing across the 3 physical hardware nodes:
1. MBP M5 Max 128G: High-throughput reasoning, complex coding, DFlash 2 (Primary brain)
2. Mac mini M4 24G: 24/7 background embeddings, rerankers, semantic triage (Memory worker)
3. Y7000P RTX4070 8G: CUDA-accelerated OCR, speech transcription, vision (Sensory worker)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ClusterNodeRole(str, Enum):
    PRIMARY_BRAIN = "mbp-m5-max-128g"
    MEMORY_WORKER = "mac-mini-m4-24g"
    SENSORY_WORKER = "y7000p-rtx4070-8g"


@dataclass(frozen=True, slots=True)
class NodePlacementDecision:
    target_node_id: str
    target_node_name: str
    preferred_backend: str
    affinity_reason: str
    fallback_node_id: str


class HeterogeneousClusterRouter:
    """
    Directs inference requests to the optimal hardware node based on task category.
    """

    NODE_MAPPINGS = {
        # 1. Embeddings and Rerankers -> Offload to Mac mini M4 24G (24H dedicated background pipeline)
        "embedding": NodePlacementDecision(
            target_node_id="mac-mini-m4-24g",
            target_node_name="Mac mini · M4 24G",
            preferred_backend="ollama/mlx",
            affinity_reason="Dedicated 24/7 background vector pipeline (zero MBP bandwidth contention).",
            fallback_node_id="mbp-m5-max-128g",
        ),
        "embed-bge-m3": NodePlacementDecision(
            target_node_id="mac-mini-m4-24g",
            target_node_name="Mac mini · M4 24G",
            preferred_backend="ollama/mlx",
            affinity_reason="Dedicated 24/7 background vector pipeline (zero MBP bandwidth contention).",
            fallback_node_id="mbp-m5-max-128g",
        ),
        "baai-bge-reranker-v2-m3-mlx-fp16": NodePlacementDecision(
            target_node_id="mac-mini-m4-24g",
            target_node_name="Mac mini · M4 24G",
            preferred_backend="mlx",
            affinity_reason="Dedicated Reranker pipeline on M4 Neural Engine.",
            fallback_node_id="mbp-m5-max-128g",
        ),

        # 2. Vision, OCR, and CUDA Speech -> Offload to Y7000P RTX4070 (CUDA specialized compute)
        "vision": NodePlacementDecision(
            target_node_id="y7000p-rtx4070-8g",
            target_node_name="Y7000P · RTX4070 8G",
            preferred_backend="lm_studio/vllm",
            affinity_reason="CUDA Tensor Core acceleration for high-resolution vision/OCR encoding.",
            fallback_node_id="mbp-m5-max-128g",
        ),
        "mythos": NodePlacementDecision(
            target_node_id="y7000p-rtx4070-8g",
            target_node_name="Y7000P · RTX4070 8G",
            preferred_backend="lm_studio/vllm",
            affinity_reason="CUDA Tensor Core acceleration for multimodal vision models.",
            fallback_node_id="mbp-m5-max-128g",
        ),
        "ornith-35b": NodePlacementDecision(
            target_node_id="y7000p-rtx4070-8g",
            target_node_name="Y7000P · RTX4070 8G",
            preferred_backend="lm_studio/vllm",
            affinity_reason="CUDA Tensor Core acceleration for visual processing.",
            fallback_node_id="mbp-m5-max-128g",
        ),

        # 3. Heavy Reasoning, Coding, and DFlash 2 Speculation -> MBP M5 Max 128G (Unified Bandwidth SOTA)
        "qwen-3.8-27b": NodePlacementDecision(
            target_node_id="mbp-m5-max-128g",
            target_node_name="MBP · M5 Max 128G",
            preferred_backend="mlx_lm",
            affinity_reason="High-bandwidth 128GB unified memory for 27B model (70 tok/s DFlash 2).",
            fallback_node_id="mac-mini-m4-24g",
        ),
        "qwen-3.8-27b-dflash": NodePlacementDecision(
            target_node_id="mbp-m5-max-128g",
            target_node_name="MBP · M5 Max 128G",
            preferred_backend="llama_server_dflash",
            affinity_reason="Full 70+ tok/s DFlash 2 speculative pipeline on M5 Max unified memory.",
            fallback_node_id="mbp-m5-max-128g",
        ),
        "coding": NodePlacementDecision(
            target_node_id="mbp-m5-max-128g",
            target_node_name="MBP · M5 Max 128G",
            preferred_backend="mlx_lm",
            affinity_reason="Primary brain for complex code generation and architectural synthesis.",
            fallback_node_id="mac-mini-m4-24g",
        ),
        "reasoning": NodePlacementDecision(
            target_node_id="mbp-m5-max-128g",
            target_node_name="MBP · M5 Max 128G",
            preferred_backend="mlx_lm",
            affinity_reason="Large context reasoning on 128GB Unified Memory.",
            fallback_node_id="mac-mini-m4-24g",
        ),
    }

    @classmethod
    def route_model(cls, model_id: str) -> NodePlacementDecision:
        if model_id in cls.NODE_MAPPINGS:
            return cls.NODE_MAPPINGS[model_id]

        # Default fallback rule based on model naming heuristics
        if "embed" in model_id or "rerank" in model_id:
            return cls.NODE_MAPPINGS["embedding"]
        if "vision" in model_id or "ocr" in model_id or "vl" in model_id:
            return cls.NODE_MAPPINGS["vision"]
        return cls.NODE_MAPPINGS["coding"]
