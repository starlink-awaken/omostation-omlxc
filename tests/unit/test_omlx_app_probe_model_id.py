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

2026-09-15 incident: build_configured_adapters auto-derives probe_candidates
from every chat-role model placed on a backend (so the probe can still find
something to target when the pinned id isn't the one currently loaded), but
build_configured_adapter combined it as `probe_candidates or
backend.probe_model_id` -- auto-derived candidates first. Since a backend with
any real chat placements almost always has a non-empty probe_candidates set,
an operator's explicit backend.probe_model_id was silently never used. In
practice this meant the readiness probe (a real chat completion sent every
probe_interval_seconds) would pin itself to whatever chat model a backend
happened to have loaded -- including ones nobody intended to keep warm -- and
continually refresh its idle TTL, with no config knob able to override it.
Fixed by flipping the precedence to `backend.probe_model_id or
probe_candidates`: an explicit config value now wins, and auto-derivation
remains the fallback when nothing is configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import omlxc.daemon.composition as composition_module
from omlxc.config import AppConfig, BackendConfig, DaemonConfig, ModelConfig, NodeConfig, PlacementConfig, StorageConfig
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


class TestExplicitProbeModelIdBeatsAutoDerivedCandidates:
    """2026-09-15 regression: a backend with real chat placements always has a
    non-empty auto-derived probe_candidates set, so `probe_candidates or
    backend.probe_model_id` never reached the explicit config value -- it was
    dead code in any realistic deployment. These tests build a config with
    several real chat-role placements on one backend (so probe_candidates is
    provably non-empty) and confirm an explicit backend.probe_model_id still
    wins.
    """

    @staticmethod
    def _config_with_chat_placements(tmp_path: Path, backend: BackendConfig) -> AppConfig:
        models = (
            ModelConfig(id="coding", category="coding", role="chat", engine="mlx_lm"),
            ModelConfig(id="mythos", category="chat", role="chat", engine="mlx_lm"),
        )
        placements = (
            PlacementConfig(
                id=f"coding--{backend.id}",
                model_id="coding",
                backend_id=backend.id,
                backend_model_id="mistralai_devstral-small-2-24b-instruct-2512-mlx",
            ),
            PlacementConfig(
                id=f"mythos--{backend.id}",
                model_id="mythos",
                backend_id=backend.id,
                backend_model_id="qwythos-9b-claude-mythos-5-1m-mlx",
            ),
        )
        return AppConfig(
            daemon=DaemonConfig(socket_path=tmp_path / "daemon.sock"),
            storage=StorageConfig(database_path=tmp_path / "state.db"),
            nodes=(NodeConfig(id="local", display_name="Local", platform="macos"),),
            backends=(backend,),
            models=models,
            placements=placements,
        )

    def test_omlx_app_explicit_probe_model_id_wins_over_candidates(
        self, tmp_path: Path, captured: dict[str, object]
    ) -> None:
        backend = BackendConfig(
            id="omlx-app",
            node_id="local",
            kind=BackendKind.OMLX_APP,
            base_url="http://127.0.0.1:8000",
            probe_model_id="coding",
        )
        composition_module.build_configured_adapters(self._config_with_chat_placements(tmp_path, backend))

        # Without the fix this would be the auto-derived frozenset of both
        # backend_model_ids (coding's and mythos's), silently discarding the
        # operator's explicit choice.
        assert captured["probe_model_id"] == "coding"

    def test_lm_studio_explicit_probe_model_id_wins_over_candidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        class FakeLmStudioAdapter:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

        monkeypatch.setattr(composition_module, "LmStudioAdapter", FakeLmStudioAdapter)
        backend = BackendConfig(
            id="lm-studio",
            node_id="local",
            kind=BackendKind.LM_STUDIO,
            base_url="http://127.0.0.1:1234",
            probe_model_id="mistralai_devstral-small-2-24b-instruct-2512-mlx",
        )
        composition_module.build_configured_adapters(self._config_with_chat_placements(tmp_path, backend))

        assert captured["probe_model_id"] == "mistralai_devstral-small-2-24b-instruct-2512-mlx"

    def test_no_explicit_probe_model_id_still_falls_back_to_candidates(
        self, tmp_path: Path, captured: dict[str, object]
    ) -> None:
        """Backends that don't pin a probe target keep the pre-existing
        auto-derivation behavior (probe whichever configured chat model is
        loaded)."""
        backend = BackendConfig(
            id="omlx-app",
            node_id="local",
            kind=BackendKind.OMLX_APP,
            base_url="http://127.0.0.1:8000",
        )
        composition_module.build_configured_adapters(self._config_with_chat_placements(tmp_path, backend))

        assert captured["probe_model_id"] == frozenset(
            {"mistralai_devstral-small-2-24b-instruct-2512-mlx", "qwythos-9b-claude-mythos-5-1m-mlx"}
        )
