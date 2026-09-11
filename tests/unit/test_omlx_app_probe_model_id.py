"""Regression test: OMLX_APP/OLLAMA backends must forward probe_model_id.

2026-09-11 incident: build_configured_adapter constructed OmlxAppAdapter and
OllamaAdapter without passing backend.probe_model_id (LmStudioAdapter already
received it correctly). Without an explicit probe target, the adapter's
readiness probe (a real chat completion, not a lightweight check) picks
"whatever happens to be first in the loaded-models list" -- which can be a
non-chat model (embedding/reranker, guaranteed to 400) or, when it lands on a
real chat model, competes for that model's single execution slot against
genuine user requests roughly every probe interval. Wiring probe_model_id
through lets deployments pin the probe at a known-cheap, always-resident chat
model instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import omlxc.daemon.composition as composition_module
from omlxc.config import AppConfig, BackendConfig, DaemonConfig, NodeConfig, StorageConfig
from omlxc.domain import BackendKind


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeAdapter:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(composition_module, "OmlxAppAdapter", FakeAdapter)
    monkeypatch.setattr(composition_module, "OllamaAdapter", FakeAdapter)
    return captured


def _config(tmp_path: Path, backend: BackendConfig) -> AppConfig:
    return AppConfig(
        daemon=DaemonConfig(socket_path=tmp_path / "daemon.sock"),
        storage=StorageConfig(database_path=tmp_path / "state.db"),
        nodes=(NodeConfig(id="local", display_name="Local", platform="macos"),),
        backends=(backend,),
    )


class TestProbeModelIdForwarding:
    def test_omlx_app_forwards_probe_model_id(self, tmp_path: Path, captured: dict[str, object]) -> None:
        backend = BackendConfig(
            id="omlx-app",
            node_id="local",
            kind=BackendKind.OMLX_APP,
            base_url="http://127.0.0.1:8000",
            probe_model_id="mythos-fast",
        )
        adapters = composition_module.build_configured_adapters(_config(tmp_path, backend))

        assert set(adapters) == {"omlx-app"}
        assert captured["probe_model_id"] == "mythos-fast"
        assert captured["base_url"] == "http://127.0.0.1:8000"

    def test_omlx_app_probe_model_id_defaults_to_none(self, tmp_path: Path, captured: dict[str, object]) -> None:
        """No configured probe target still passes explicitly (None), rather
        than silently omitting the kwarg -- callers must not have to guess
        whether the adapter's own fallback-to-first-loaded is in play."""
        backend = BackendConfig(
            id="omlx-app",
            node_id="local",
            kind=BackendKind.OMLX_APP,
            base_url="http://127.0.0.1:8000",
        )
        composition_module.build_configured_adapters(_config(tmp_path, backend))

        assert captured["probe_model_id"] is None

    def test_ollama_forwards_probe_model_id(self, tmp_path: Path, captured: dict[str, object]) -> None:
        backend = BackendConfig(
            id="ollama",
            node_id="local",
            kind=BackendKind.OLLAMA,
            base_url="http://127.0.0.1:11434",
            probe_model_id="qwen3.5:9b",
        )
        adapters = composition_module.build_configured_adapters(_config(tmp_path, backend))

        assert set(adapters) == {"ollama"}
        assert captured["probe_model_id"] == "qwen3.5:9b"
