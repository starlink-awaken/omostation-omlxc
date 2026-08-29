"""
Multi-Agent Dynamic VRAM Priority Scheduler & Semaphore (ADR-0205).

Provides dynamic queuing and preemption policies for multi-agent workloads:
- P0: Interactive / Human User / WeChat instant reply (Zero-wait bypass)
- P1: Automated SOP Pipeline / Agent subtasks (Concurrency-managed FIFO)
- P2: Background Maintenance / Vector Indexing (Deferred on VRAM pressure)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from omlxc.dataplane.vram_budget import (
    VRAMPressureTier,
    enforce_tiered_headroom_admission,
    TieredHeadroomResult,
)


class TaskPriority(IntEnum):
    P0_INTERACTIVE = 0  # Preempts background, zero-wait
    P1_PIPELINE = 1     # Agent standard execution
    P2_BACKGROUND = 2   # Yields when memory pressure > 70%


@dataclass(slots=True)
class QueuedInferenceRequest:
    request_id: str
    model_id: str
    priority: TaskPriority
    requested_tokens: int
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class PriorityVRAMScheduler:
    """
    Manages multi-agent concurrent inference scheduling based on live VRAM headroom
    and request priority tiers.
    """

    def __init__(
        self,
        total_node_vram_mb: float = 131072.0,  # 128GB default
        max_active_p1_slots: int = 4,
        max_active_p2_slots: int = 2,
    ) -> None:
        self.total_node_vram_mb = total_node_vram_mb
        self.max_active_p1_slots = max_active_p1_slots
        self.max_active_p2_slots = max_active_p2_slots
        self._current_used_vram_mb: float = 0.0
        self._active_requests: dict[str, QueuedInferenceRequest] = {}
        self._pending_queue: list[QueuedInferenceRequest] = []

    @property
    def current_vram_mb(self) -> float:
        return self._current_used_vram_mb

    def set_vram_baseline(self, vram_mb: float) -> None:
        self._current_used_vram_mb = max(0.0, vram_mb)

    def evaluate_admission(
        self,
        request: QueuedInferenceRequest,
    ) -> tuple[bool, TieredHeadroomResult, str]:
        """
        Evaluates whether a request can execute immediately, must queue, or is throttled.
        """
        admission = enforce_tiered_headroom_admission(
            model_id=request.model_id,
            requested_tokens=request.requested_tokens,
            current_used_vram_mb=self._current_used_vram_mb,
            total_node_vram_mb=self.total_node_vram_mb,
        )

        # 1. Hard intercept (RED tier)
        if not admission.admitted:
            return False, admission, "VRAM hard ceiling reached. Request rejected to protect OS stability."

        # 2. P0 Interactive: Always execute immediately if admitted
        if request.priority == TaskPriority.P0_INTERACTIVE:
            return True, admission, "P0 Interactive pass-through (zero-wait execution)."

        # 3. P2 Background: Throttle if memory pressure is YELLOW or ORANGE
        if request.priority == TaskPriority.P2_BACKGROUND:
            if admission.pressure_tier in (VRAMPressureTier.YELLOW, VRAMPressureTier.ORANGE):
                return False, admission, "P2 Background deferred due to elevated memory pressure."
            active_p2 = sum(1 for r in self._active_requests.values() if r.priority == TaskPriority.P2_BACKGROUND)
            if active_p2 >= self.max_active_p2_slots:
                return False, admission, "P2 Background concurrency limit reached. Queued."

        # 4. P1 Pipeline: Check active concurrency slot limits
        active_p1 = sum(1 for r in self._active_requests.values() if r.priority == TaskPriority.P1_PIPELINE)
        if active_p1 >= self.max_active_p1_slots:
            return False, admission, "P1 Pipeline concurrency limit reached. Queued."

        return True, admission, "Admitted for immediate execution."

    def acquire_slot(self, request: QueuedInferenceRequest) -> bool:
        can_exec, _, _ = self.evaluate_admission(request)
        if can_exec:
            self._active_requests[request.request_id] = request
            return True
        self._pending_queue.append(request)
        self._pending_queue.sort(key=lambda r: (r.priority.value, r.created_at))
        return False

    def release_slot(self, request_id: str) -> None:
        if request_id in self._active_requests:
            del self._active_requests[request_id]

    def get_queue_status(self) -> dict[str, Any]:
        return {
            "active_count": len(self._active_requests),
            "pending_count": len(self._pending_queue),
            "active_by_priority": {
                "P0_interactive": sum(1 for r in self._active_requests.values() if r.priority == TaskPriority.P0_INTERACTIVE),
                "P1_pipeline": sum(1 for r in self._active_requests.values() if r.priority == TaskPriority.P1_PIPELINE),
                "P2_background": sum(1 for r in self._active_requests.values() if r.priority == TaskPriority.P2_BACKGROUND),
            },
            "current_vram_mb": self._current_used_vram_mb,
            "total_vram_mb": self.total_node_vram_mb,
        }
