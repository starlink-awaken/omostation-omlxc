"""network_cost_ms 此前对所有 placement 恒为 None(_configured_snapshots 的初始值
从未被 CatalogProbe._apply 的探测刷新逻辑触碰), 评分算法虽然设计了 network 这个
维度, 但实际运行时永远退化到同一个 policy 默认值 —— loopback 和 tailscale 远程
节点在路由决策里完全无法区分, "本地优先"这个常识性偏好从未真正生效。

这里验证两件事: (1) CatalogProbe._apply 现在会基于 backend.base_url 是否 loopback
给出区分的 network_cost_ms; (2) 在其他条件完全相同时, 这个区分确实让 RoutePlanner
优先选中本地 placement, 而不只是字段被赋值但从不影响实际路由结果。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from omlxc.config import (
    AppConfig,
    BackendConfig,
    DaemonConfig,
    ModelConfig,
    NodeConfig,
    PlacementConfig,
    StorageConfig,
)
from omlxc.daemon.composition import CatalogProbe, SnapshotCatalog, _configured_snapshots
from omlxc.domain import BackendKind, RouteDecision, RouteProfile, RouteRequest
from omlxc.domain.protocols import AdapterCapability, CapabilitySnapshot, ModelRuntime, ModelRuntimeState
from omlxc.scheduler import RoutePlanner, default_policies


def _config(root: Path) -> AppConfig:
    return AppConfig(
        daemon=DaemonConfig(socket_path=root / "omlxcd.sock"),
        storage=StorageConfig(database_path=root / "state.db"),
        nodes=(
            NodeConfig(id="local-node", display_name="Local", platform="macos", memory_gb=16),
            NodeConfig(id="remote-node", display_name="Remote", platform="macos", memory_gb=16),
        ),
        backends=(
            BackendConfig(
                id="local-backend",
                node_id="local-node",
                kind=BackendKind.OMLX_APP,
                base_url="http://127.0.0.1:8000",
            ),
            BackendConfig(
                id="remote-backend",
                node_id="remote-node",
                kind=BackendKind.OMLX_APP,
                base_url="http://100.64.0.10:8000",
            ),
        ),
        models=(ModelConfig(id="shared/model", category="llm", role="chat", engine="omlx"),),
        placements=(
            PlacementConfig(
                id="local-placement",
                model_id="shared/model",
                backend_id="local-backend",
                backend_model_id="physical/model",
                context_limit=8192,
                memory_gb=2,
            ),
            PlacementConfig(
                id="remote-placement",
                model_id="shared/model",
                backend_id="remote-backend",
                backend_model_id="physical/model",
                context_limit=8192,
                memory_gb=2,
            ),
        ),
    )


def _capability(backend_id: str) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        backend_id=backend_id,
        reachable=True,
        compatible=True,
        model_available=True,
        generation_ready=True,
        observed_at=datetime.now(UTC),
        capabilities=frozenset({AdapterCapability.CHAT, AdapterCapability.STREAMING}),
    )


def _models() -> tuple[ModelRuntime, ...]:
    return (
        ModelRuntime(
            id="physical/model",
            state=ModelRuntimeState.LOADED,
            loaded=True,
            capabilities=frozenset({AdapterCapability.CHAT}),
            context_limit=8192,
        ),
    )


def test_catalog_probe_assigns_zero_network_cost_to_loopback_and_nonzero_to_remote(tmp_path: Path) -> None:
    config = _config(tmp_path)
    catalog = SnapshotCatalog(_configured_snapshots(config), now=lambda: datetime.now(UTC))
    probe = CatalogProbe(config=config, adapters={}, catalog=catalog, tailscale=None, now=lambda: datetime.now(UTC))

    probe._apply(config.backends[0], _capability("local-backend"), _models(), authorized=True, local=True)
    probe._apply(config.backends[1], _capability("remote-backend"), _models(), authorized=True, local=True)

    assert catalog.get_one("local-placement").network_cost_ms == 0.0
    assert catalog.get_one("remote-placement").network_cost_ms == 40.0


def test_route_planner_prefers_local_placement_when_only_network_cost_differs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    catalog = SnapshotCatalog(_configured_snapshots(config), now=lambda: datetime.now(UTC))
    probe = CatalogProbe(config=config, adapters={}, catalog=catalog, tailscale=None, now=lambda: datetime.now(UTC))
    probe._apply(config.backends[0], _capability("local-backend"), _models(), authorized=True, local=True)
    probe._apply(config.backends[1], _capability("remote-backend"), _models(), authorized=True, local=True)

    # 除 network_cost_ms 外, 两个 placement 的一切遥测都设成完全一致, 排除掉
    # ttft/throughput/queue/error_rate 这些维度可能掩盖 network 差异的可能性。
    identical_telemetry = {
        "ttft_ms": 200.0,
        "throughput_tps": 50.0,
        "queue_depth": 0,
        "error_rate": 0.0,
        "affinity": 0.0,
    }
    catalog.update("local-placement", **identical_telemetry)
    catalog.update("remote-placement", **identical_telemetry)

    placements = catalog.get()
    assert {p.placement_id: p.network_cost_ms for p in placements} == {
        "local-placement": 0.0,
        "remote-placement": 40.0,
    }

    planner = RoutePlanner(default_policies())
    request = RouteRequest(
        request_id="req-1",
        model_id="shared/model",
        profile=RouteProfile.INTERACTIVE,
        required_capabilities=frozenset({"chat"}),
        context_tokens=100,
    )
    decision = planner.plan(request, placements)

    assert isinstance(decision, RouteDecision)
    assert decision.selected_placement_id == "local-placement"
    assert decision.candidate_scores["local-placement"] > decision.candidate_scores["remote-placement"]
