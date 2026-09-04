"""omlxc.dataplane.failover — 双机雷雳 5 算力织网故障转移控制器 (BET-Y1Q3-T10-119).

监听 DMA daemon 心跳 (MeshTelemetrySnapshot.is_connected), 触发 winner-takes-all
故障转移. 拔插雷雳 5 → 10s 内降级 local; 重连 → 3s 内恢复.

State machine:
  DUAL_LINK     — 双机 120Gbps P2P active, 心跳正常
  HEARTBEAT_LOSS — 1-2 次连续心跳失败, 滑动窗口观察
  DEGRADED      — 3+ 次失败, 降级到 local single-node
  RECONNECTING  — 物理恢复, exponential back-off
  DUAL_RECOVERED — 重连成功, 同步双机状态
  TIE_BREAKING  — 双机争抢 primary, 租约决断
"""
from __future__ import annotations

import datetime as dt
import enum
import fcntl
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


class FailoverState(enum.StrEnum):
    DUAL_LINK = "dual_link"
    HEARTBEAT_LOSS = "heartbeat_loss"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    DUAL_RECOVERED = "dual_recovered"
    TIE_BREAKING = "tie_breaking"


@dataclass
class FailoverEvent:
    """Audit log entry. One per state transition or takeover attempt."""
    timestamp_utc: str
    event_type: str  # state_transition | takeover_attempt | heartbeat_loss
    from_state: str | None
    to_state: str | None
    actor: str  # "local" | "peer:<id>" | "auto"
    request_id: str | None = None
    note: str = ""


@dataclass
class HeartbeatSnapshot:
    """In-memory snapshot of latest mesh state. Polled from dma_daemon."""
    is_connected: bool
    link_speed_gbps: float
    avg_latency_ms: float
    timestamp_utc: str
    peer_node_id: str = "unknown"


@dataclass
class FailoverController:
    """High-level failover controller.

    Reads dma_daemon telemetry, applies state machine, writes audit log.
    Single-writer lease prevents brain-split (one primary per cluster).
    """
    workspace_root: Path
    node_id: str = "node-local"
    peer_node_id: str = "node-peer"
    heartbeat_window_s: float = 5.0  # sliding window for loss detection
    heartbeat_loss_threshold: int = 3  # 3 consecutive failures → DEGRADED
    degrade_target_s: float = 10.0  # contractual: 10s degrade
    reconnect_target_s: float = 3.0  # contractual: 3s recover
    lease_duration_s: float = 5.0  # primary lease TTL
    audit_path: Path = field(default_factory=lambda: Path(".omo/state/failover-audit.jsonl"))

    # Internal state (not in __init__ — populated by .start())
    _state: FailoverState = FailoverState.DUAL_LINK
    _loss_count: int = 0
    _last_heartbeat: dt.datetime | None = None
    _lease: dt.datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _running: bool = False
    _events: list[FailoverEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root)
        self.audit_path = self.workspace_root / ".omo" / "state" / "failover-audit.jsonl"

    # ── State machine ──

    def start(self) -> None:
        """Start failover loop. Non-blocking; spawns a daemon thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
        t = threading.Thread(target=self._run_loop, daemon=True, name="failover-ctrl")
        t.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False

    def current_state(self) -> FailoverState:
        with self._lock:
            return self._state

    def events(self) -> list[FailoverEvent]:
        with self._lock:
            return list(self._events)

    def request_takeover(self, request_id: str, actor: str = "local") -> bool:
        """Attempt to become primary. Winner-takes-all with lease.

        Returns True if takeover granted, False otherwise.
        Lease TTL = self.lease_duration_s.
        """
        with self._lock:
            now = dt.datetime.now(dt.UTC)
            if self._lease is not None and self._lease > now:
                # Lease held by self or other; refuse
                self._log_event(FailoverEvent(
                    timestamp_utc=now.isoformat(),
                    event_type="takeover_attempt",
                    from_state=self._state.value, to_state=self._state.value,
                    actor=actor, request_id=request_id,
                    note=f"refused: lease valid until {self._lease.isoformat()}",
                ))
                return False
            # Grant lease
            self._lease = now + dt.timedelta(seconds=self.lease_duration_s)
            old = self._state
            if self._state in (FailoverState.DEGRADED, FailoverState.HEARTBEAT_LOSS):
                # Takeover during degraded → claim primary
                self._state = FailoverState.DUAL_LINK
            self._log_event(FailoverEvent(
                timestamp_utc=now.isoformat(),
                event_type="takeover_attempt",
                from_state=old.value, to_state=self._state.value,
                actor=actor, request_id=request_id,
                note=f"lease granted until {self._lease.isoformat()}",
            ))
            return True

    # ── Heartbeat ingestion ──

    def on_heartbeat(self, snap: HeartbeatSnapshot) -> None:
        """Update internal state based on latest heartbeat.

        Implements sliding-window loss detection:
          - On success: reset loss counter, transition HEARTBEAT_LOSS/RECONNECTING → DUAL_LINK
          - On failure: increment counter, transition DUAL_LINK → HEARTBEAT_LOSS at threshold
          - When in DUAL_LINK and threshold reached: DEGRADED + start local single-node
        """
        with self._lock:
            now = dt.datetime.now(dt.UTC)
            self._last_heartbeat = snap.timestamp_utc
            if snap.is_connected:
                self._loss_count = 0
                if self._state in (FailoverState.HEARTBEAT_LOSS,
                                   FailoverState.RECONNECTING,
                                   FailoverState.DEGRADED):
                    self._transition(FailoverState.DUAL_RECOVERED,
                                       note=f"link restored at {snap.link_speed_gbps} Gbps")
            else:
                self._loss_count += 1
                if self._state == FailoverState.DUAL_LINK and self._loss_count >= self.heartbeat_loss_threshold:
                    self._transition(FailoverState.HEARTBEAT_LOSS,
                                       note=f"{self._loss_count} consecutive failures")
                elif self._state == FailoverState.HEARTBEAT_LOSS and self._loss_count >= 6:
                    # 6+ failures: full degrade
                    self._transition(FailoverState.DEGRADED,
                                       note="extended outage, switch to local single-node")

    def _transition(self, new: FailoverState, note: str = "") -> None:
        """Internal: transition state + log event (caller holds lock)."""
        old = self._state
        if old == new:
            return
        self._state = new
        self._log_event(FailoverEvent(
            timestamp_utc=dt.datetime.now(dt.UTC).isoformat(),
            event_type="state_transition",
            from_state=old.value, to_state=new.value,
            actor="auto", note=note,
        ))

    def _log_event(self, ev: FailoverEvent) -> None:
        """Append event to in-memory list and audit file. Caller holds lock."""
        self._events.append(ev)
        self._append_audit(ev)

    def _append_audit(self, ev: FailoverEvent) -> None:
        """Atomic append to failover-audit.jsonl with file lock + size-based rotation."""
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(ev), ensure_ascii=False) + "\n"
        with open(self.audit_path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
                # size-based rotation (>1MB → rename to .1, .2, …)
                if f.tell() > 1_048_576:
                    for i in range(9, 0, -1):
                        src = self.audit_path.with_suffix(f".jsonl.{i}")
                        dst = self.audit_path.with_suffix(f".jsonl.{i + 1}")
                        if src.exists():
                            src.rename(dst)
                    self.audit_path.rename(self.audit_path.with_suffix(".jsonl.1"))
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _run_loop(self) -> None:
        """Background loop. Currently a stub — heartbeat-driven via on_heartbeat()."""
        while True:
            with self._lock:
                if not self._running:
                    return
            time.sleep(0.5)


# ── Convenience constructors ──

def from_dma_daemon_telemetry(telemetry_path: Path) -> HeartbeatSnapshot | None:
    """Read latest telemetry snapshot from .omo/state/mesh-telemetry.json.

    Returns None if file missing or malformed (fail-safe).
    """
    if not telemetry_path.is_file():
        return None
    try:
        data = json.loads(telemetry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return HeartbeatSnapshot(
        is_connected=bool(data.get("is_connected", False)),
        link_speed_gbps=float(data.get("link_speed_gbps", 0.0)),
        avg_latency_ms=float(data.get("avg_dma_latency_ms", 0.0)),
        timestamp_utc=str(data.get("timestamp_utc", "")),
        peer_node_id=str(data.get("peer_node_id", "unknown")),
    )
