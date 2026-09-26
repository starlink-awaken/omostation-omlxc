"""readiness_probe defaults to state-based readiness and is passed through to adapters."""

from __future__ import annotations

from omlxc.config.schema import BackendConfig


def test_readiness_probe_defaults_to_state() -> None:
    backend = BackendConfig(id="lm", node_id="n", kind="lm_studio", base_url="http://127.0.0.1:1234")
    assert backend.readiness_probe == "state"


def test_readiness_probe_generation_opt_in() -> None:
    backend = BackendConfig(
        id="lm", node_id="n", kind="lm_studio", base_url="http://127.0.0.1:1234", readiness_probe="generation"
    )
    assert backend.readiness_probe == "generation"
