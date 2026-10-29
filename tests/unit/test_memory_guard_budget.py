"""Memory admission must use local physical capacity and include KV cache."""

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
from omlxc.domain import BackendKind
from omlxc.domain.protocols import (
    AdapterCapability,
    CapabilitySnapshot,
    ModelRuntime,
    ModelRuntimeState,
)


def _config(root: Path) -> AppConfig:
    return AppConfig(
        daemon=DaemonConfig(socket_path=root / "omlxcd.sock"),
        storage=StorageConfig(database_path=root / "state.db"),
        nodes=(
            NodeConfig(id="local-node", display_name="Local", platform="macos", memory_gb=128),
            NodeConfig(id="remote-node", display_name="Remote", platform="macos", memory_gb=32),
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
        models=(
            ModelConfig(id="large-model", category="llm", role="chat", engine="omlx"),
            ModelConfig(id="resident-model", category="llm", role="chat", engine="omlx"),
        ),
        placements=(
            PlacementConfig(
                id="local-large",
                model_id="large-model",
                backend_id="local-backend",
                backend_model_id="large/physical",
                context_limit=262144,
                memory_gb=20.4,
            ),
            PlacementConfig(
                id="local-resident",
                model_id="resident-model",
                backend_id="local-backend",
                backend_model_id="resident/physical",
                context_limit=8192,
                memory_gb=16.2,
            ),
            PlacementConfig(
                id="remote-large",
                model_id="large-model",
                backend_id="remote-backend",
                backend_model_id="large/physical",
                context_limit=8192,
                memory_gb=25,
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
        capabilities=frozenset({AdapterCapability.CHAT}),
    )


def _probe(config: AppConfig) -> tuple[CatalogProbe, SnapshotCatalog]:
    catalog = SnapshotCatalog(_configured_snapshots(config), now=lambda: datetime.now(UTC))
    probe = CatalogProbe(
        config=config,
        adapters={},
        catalog=catalog,
        tailscale=None,
        now=lambda: datetime.now(UTC),
        local_physical_memory_gb=24.0,
    )
    return probe, catalog


def test_loopback_guard_uses_physical_memory_instead_of_declared_node_memory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    probe, catalog = _probe(config)

    probe._apply(config.backends[0], _capability("local-backend"), (), authorized=True, local=True)

    assert catalog.get_one("local-large").memory_admitted is False


def test_memory_guard_includes_declared_context_kv_cache(tmp_path: Path) -> None:
    config = _config(tmp_path)
    probe, catalog = _probe(config)

    probe._apply(config.backends[0], _capability("local-backend"), (), authorized=True, local=True)

    snapshot = catalog.get_one("local-large")
    assert snapshot.memory_admitted is False


def test_memory_guard_accounts_for_loaded_placements_on_the_same_node(tmp_path: Path) -> None:
    config = _config(tmp_path)
    probe, catalog = _probe(config)
    loaded_model = ModelRuntime(
        id="resident/physical",
        state=ModelRuntimeState.LOADED,
        loaded=True,
        capabilities=frozenset({AdapterCapability.CHAT}),
        context_limit=8192,
    )

    probe._apply(
        config.backends[0],
        _capability("local-backend"),
        (loaded_model,),
        authorized=True,
        local=True,
    )

    assert catalog.get_one("local-resident").memory_admitted is True
    assert catalog.get_one("local-large").memory_admitted is False


def test_remote_guard_still_uses_configured_node_budget(tmp_path: Path) -> None:
    config = _config(tmp_path)
    probe, catalog = _probe(config)

    probe._apply(config.backends[1], _capability("remote-backend"), (), authorized=True, local=False)

    assert catalog.get_one("remote-large").memory_admitted is True
