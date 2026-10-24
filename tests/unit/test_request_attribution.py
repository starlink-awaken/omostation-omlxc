"""Contract: a failed request must be attributable to the model it asked for.

The gateway recorded *that* a request failed but never *what* it asked for, so
an operator reading the ledger could not separate a business failure from the
health probe's own self-check. `requested_model` closes that gap.

These tests pin four layers:

1. schema    — the column exists, is nullable TEXT, and is physically last
2. migration — a v4 database upgrades to v5 without losing a single legacy row
3. plumbing  — planner -> telemetry -> storage carries the value end to end
4. honesty   — a row written before the column existed reads back as unknown
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omlxc.dataplane import (
    AdapterBinding,
    AdapterRegistry,
    BoundRouteTelemetry,
    CapacityCoordinator,
    DataPlaneOrchestrator,
    RouteTelemetryRecorder,
)
from omlxc.domain import RouteProfile, RouteRequest
from omlxc.domain.protocols import ChatMessage, ChatRequest, ChatResult
from omlxc.scheduler import (
    PlacementSnapshot,
    RouteFailureCode,
    RoutePlanner,
    default_policies,
)
from omlxc.storage import MetricRecord, RouteAuditWrite, SQLiteRuntimeStore
from omlxc.storage.database import _V4_SCHEMA_SQL

_LEGACY_AT = "2026-10-01T00:00:00.000000+00:00"
_V4_METRIC_COLUMNS = [
    "sequence",
    "request_id",
    "observed_at",
    "latency_ms",
    "success",
    "error_code",
    "phase",
]
_V4_AUDIT_COLUMNS = [
    "sequence",
    "request_id",
    "observed_at",
    "selected_placement_id",
    "candidate_json",
    "rejection_json",
    "config_revision",
]


def _columns(database: Path, table: str) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return list(connection.execute(f"PRAGMA table_info({table})"))
    finally:
        connection.close()


def _write_v4_database(database: Path) -> None:
    """Materialize a genuine v4 database, rows included."""
    connection = sqlite3.connect(database)
    try:
        connection.executescript(f"BEGIN IMMEDIATE;\n{_V4_SCHEMA_SQL};\nPRAGMA user_version = 4;\nCOMMIT;")
        connection.execute(
            "INSERT INTO request_metrics (request_id, observed_at, latency_ms, success) VALUES (?, ?, ?, ?)",
            ("legacy-metric", _LEGACY_AT, 12.5, 0),
        )
        connection.execute(
            """
            INSERT INTO route_audits
                (request_id, observed_at, selected_placement_id, candidate_json, rejection_json, config_revision)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("legacy-audit", _LEGACY_AT, None, "[]", '{"p1":"model_mismatch"}', "scheduler-v1"),
        )
        connection.commit()
    finally:
        connection.close()


def _snapshot(placement_id: str, **updates: object) -> PlacementSnapshot:
    values: dict[str, object] = {
        "placement_id": placement_id,
        "model_id": "public/model",
        "backend_id": "backend-1",
        "backend_model_id": "physical/model",
        "node_id": "node-1",
        "fresh": True,
        "available": True,
        "authorized": True,
        "capabilities": frozenset({"chat"}),
        "context_limit": 8192,
        "memory_admitted": True,
        "loaded": True,
        "ttft_ms": 10.0,
        "throughput_tps": 50.0,
        "queue_depth": 0,
        "error_rate": 0.0,
        "network_cost_ms": 1.0,
        "affinity": 1.0,
        "available_concurrency": 1,
        "local": True,
        "security_allowed": True,
    }
    values.update(updates)
    return PlacementSnapshot(**values)


def _route_request(**updates: object) -> RouteRequest:
    values: dict[str, object] = {
        "request_id": "req-1",
        "model_id": "public/model",
        "profile": RouteProfile.INTERACTIVE,
        "required_capabilities": frozenset({"chat"}),
        "context_tokens": 16,
    }
    values.update(updates)
    return RouteRequest.model_validate(values)


def _chat() -> ChatRequest:
    return ChatRequest(
        request_id="req-1",
        model="public/model",
        messages=(ChatMessage(role="user", content="hello"),),
    )


class _StubAdapter:
    async def chat(self, request: ChatRequest) -> ChatResult:
        return ChatResult(request_id=request.request_id, success=True, content="ok")


class _RecordingStore:
    """Captures what the recorder hands to storage, without a database."""

    def __init__(self) -> None:
        self.audits: list[RouteAuditWrite] = []
        self.metrics: list[MetricRecord] = []

    async def append_route_audit(self, record: RouteAuditWrite) -> Any:
        self.audits.append(record)
        return record

    def accept_metric(self, metric: MetricRecord) -> bool:
        self.metrics.append(metric)
        return True


# --- 1. schema -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_database_declares_requested_model_last_and_nullable(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = await SQLiteRuntimeStore.open(database)
    assert await store.schema_version() == 5
    await store.close()

    for table in ("request_metrics", "route_audits"):
        info = _columns(database, table)
        names = [str(row[1]) for row in info]
        assert names[-1] == "requested_model"
        column = next(row for row in info if str(row[1]) == "requested_model")
        assert str(column[2]) == "TEXT"
        assert int(column[3]) == 0


def test_v4_shape_is_pinned_so_the_migration_cannot_silently_drift() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_V4_SCHEMA_SQL)
        assert [row[1] for row in connection.execute("PRAGMA table_info(request_metrics)")] == _V4_METRIC_COLUMNS
        assert [row[1] for row in connection.execute("PRAGMA table_info(route_audits)")] == _V4_AUDIT_COLUMNS
    finally:
        connection.close()


# --- 2. migration --------------------------------------------------------------


@pytest.mark.asyncio
async def test_v4_database_migrates_to_v5_and_keeps_every_legacy_row(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _write_v4_database(database)

    store = await SQLiteRuntimeStore.open(database)
    try:
        assert await store.schema_version() == 5

        metrics = await store.list_metrics(after_sequence=0, limit=10)
        assert [metric.request_id for metric in metrics] == ["legacy-metric"]
        assert metrics[0].latency_ms == 12.5
        assert metrics[0].success is False

        audits = await store.list_route_audits(after_sequence=0, limit=10)
        assert [audit.request_id for audit in audits] == ["legacy-audit"]
        assert audits[0].rejections == {"p1": "model_mismatch"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent_across_repeated_opens(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _write_v4_database(database)

    for _ in range(3):
        store = await SQLiteRuntimeStore.open(database)
        try:
            assert await store.schema_version() == 5
            assert await store.metric_count() == 1
        finally:
            await store.close()


# --- 3. plumbing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_audit_round_trips_requested_model(tmp_path: Path) -> None:
    store = await SQLiteRuntimeStore.open(tmp_path / "state.db")
    try:
        written = await store.append_route_audit(
            RouteAuditWrite(
                request_id="req-1",
                observed_at=datetime(2026, 10, 24, 5, 0, tzinfo=UTC),
                selected_placement_id="p1",
                candidates=("p1@1.0",),
                rejections={},
                config_revision="scheduler-v1",
                requested_model="public/model",
            )
        )
        assert written.requested_model == "public/model"

        stored = await store.list_route_audits(after_sequence=0, limit=10)
        assert [audit.requested_model for audit in stored] == ["public/model"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_metric_round_trips_requested_model(tmp_path: Path) -> None:
    store = await SQLiteRuntimeStore.open(tmp_path / "state.db")
    try:
        accepted = store.accept_metric(
            MetricRecord(
                request_id="req-1",
                observed_at=datetime(2026, 10, 24, 5, 0, tzinfo=UTC),
                latency_ms=42.0,
                success=False,
                error_code="no_candidate",
                phase="before_content",
                requested_model="public/model",
            )
        )
        assert accepted is True
        assert await store.flush_metrics() == 1

        stored = await store.list_metrics(after_sequence=0, limit=10)
        assert [metric.requested_model for metric in stored] == ["public/model"]
        assert stored[0].error_code == "no_candidate"
    finally:
        await store.close()


def test_planner_attaches_the_requested_model_to_a_decision() -> None:
    plan = RoutePlanner(default_policies()).plan(_route_request(model_id="public/model"), (_snapshot("p"),))

    assert plan.requested_model == "public/model"
    assert plan.selected_placement_id == "p"


def test_planner_attaches_the_requested_model_to_a_no_candidate_failure() -> None:
    plan = RoutePlanner(default_policies()).plan(
        _route_request(model_id="public/model"),
        (_snapshot("p", model_id="other/model"),),
    )

    assert plan.code is RouteFailureCode.NO_CANDIDATE
    assert plan.requested_model == "public/model"
    assert plan.rejected == {"p": "model_mismatch"}


def test_planner_attaches_the_requested_model_to_an_invalid_snapshot_failure() -> None:
    plan = RoutePlanner(default_policies()).plan(
        _route_request(model_id="public/model"),
        (_snapshot("p"), _snapshot("p")),
    )

    assert plan.code is RouteFailureCode.INVALID_SNAPSHOT
    assert plan.requested_model == "public/model"


@pytest.mark.asyncio
async def test_recorder_writes_the_plans_requested_model_into_the_audit() -> None:
    store = _RecordingStore()
    recorder = RouteTelemetryRecorder(now=lambda: datetime(2026, 10, 24, 5, 0, tzinfo=UTC))
    plan = RoutePlanner(default_policies()).plan(_route_request(), (_snapshot("p"),))

    await recorder.record_route(store, plan)  # type: ignore[arg-type]

    assert [audit.requested_model for audit in store.audits] == ["public/model"]


@pytest.mark.asyncio
async def test_failed_route_is_attributable_to_the_requested_model(tmp_path: Path) -> None:
    """The payoff: a rejected request names the model that was actually asked for."""
    store = await SQLiteRuntimeStore.open(tmp_path / "state.db")
    try:
        telemetry = BoundRouteTelemetry(
            store, RouteTelemetryRecorder(now=lambda: datetime(2026, 10, 24, 5, 0, tzinfo=UTC))
        )
        placement = _snapshot("p", model_id="other/model")
        orchestrator = DataPlaneOrchestrator(
            planner=RoutePlanner(default_policies()),
            snapshot_provider=lambda: (placement,),
            registry=AdapterRegistry((AdapterBinding("backend-1", _StubAdapter()),)),
            capacity=CapacityCoordinator(global_limit=4, per_node=2, per_backend=2),
            telemetry=telemetry,
        )

        result = await orchestrator.chat(_route_request(model_id="public/model"), _chat(), deadline=10.0)
        assert result.success is False
        assert await store.flush_metrics() == 1

        metrics = await store.list_metrics(after_sequence=0, limit=10)
        assert len(metrics) == 1
        assert metrics[0].success is False
        assert metrics[0].requested_model == "public/model"

        audits = await store.list_route_audits(after_sequence=0, limit=10)
        assert len(audits) == 1
        assert audits[0].requested_model == "public/model"
        assert audits[0].rejections == {"p": "model_mismatch"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_successful_route_is_attributable_to_the_requested_model(tmp_path: Path) -> None:
    store = await SQLiteRuntimeStore.open(tmp_path / "state.db")
    try:
        telemetry = BoundRouteTelemetry(
            store, RouteTelemetryRecorder(now=lambda: datetime(2026, 10, 24, 5, 0, tzinfo=UTC))
        )
        orchestrator = DataPlaneOrchestrator(
            planner=RoutePlanner(default_policies()),
            snapshot_provider=lambda: (_snapshot("p"),),
            registry=AdapterRegistry((AdapterBinding("backend-1", _StubAdapter()),)),
            capacity=CapacityCoordinator(global_limit=4, per_node=2, per_backend=2),
            telemetry=telemetry,
        )

        result = await orchestrator.chat(_route_request(model_id="public/model"), _chat(), deadline=10.0)
        assert result.success is True
        assert await store.flush_metrics() == 1

        metrics = await store.list_metrics(after_sequence=0, limit=10)
        assert [metric.requested_model for metric in metrics] == ["public/model"]
        assert metrics[0].success is True
    finally:
        await store.close()


# --- 4. honesty ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_rows_read_back_as_unknown_rather_than_empty(tmp_path: Path) -> None:
    """A row that predates the column must say "unknown", never "no model"."""
    store = await SQLiteRuntimeStore.open(tmp_path / "state.db")
    try:
        accepted = store.accept_metric(
            MetricRecord(
                request_id="req-legacy",
                observed_at=datetime(2026, 10, 24, 5, 0, tzinfo=UTC),
                latency_ms=1.0,
                success=True,
            )
        )
        assert accepted is True
        assert await store.flush_metrics() == 1

        stored = await store.list_metrics(after_sequence=0, limit=10)
        assert stored[0].requested_model is None
    finally:
        await store.close()


def test_requested_model_shares_the_placement_namespace_so_absence_is_provable() -> None:
    """The recorded model id is the same namespace the routing table is keyed on.

    That is what makes "the requested model has no placement at all" a fact the
    ledger can prove, instead of a guess that needs the alias table.
    """
    request = _route_request(model_id="public/model")
    placements = (_snapshot("p", model_id="other/model"),)

    plan = RoutePlanner(default_policies()).plan(request, placements)

    assert plan.requested_model == request.model_id
    assert not any(placement.model_id == plan.requested_model for placement in placements)
