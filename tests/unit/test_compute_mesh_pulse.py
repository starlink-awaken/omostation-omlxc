"""Socket-resolution contracts for the standalone compute mesh pulse."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from omlxc.config import safe_defaults


def _pulse_module() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts/compute-mesh-pulse.py"
    spec = importlib.util.spec_from_file_location("compute_mesh_pulse", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_explicit_socket_override_wins_without_loading_user_config(tmp_path: Path) -> None:
    pulse = _pulse_module()
    selected = tmp_path / "explicit.sock"

    def forbidden_loader():  # type: ignore[no-untyped-def]
        raise AssertionError("explicit override loaded user config")

    assert pulse.resolve_omlxc_socket(selected, config_loader=forbidden_loader) == selected


def test_user_config_socket_is_used_when_override_is_absent(tmp_path: Path) -> None:
    pulse = _pulse_module()
    configured = safe_defaults(base_directory=tmp_path).model_copy(
        update={
            "daemon": safe_defaults(base_directory=tmp_path).daemon.model_copy(
                update={"socket_path": tmp_path / "configured.sock"}
            )
        }
    )

    assert pulse.resolve_omlxc_socket(None, config_loader=lambda: configured) == tmp_path / "configured.sock"


def test_package_default_socket_and_probe_use_same_canonical_path(tmp_path: Path) -> None:
    pulse = _pulse_module()
    socket_path = tmp_path / "omlxcd.sock"
    socket_path.touch()
    defaults = safe_defaults(base_directory=tmp_path)

    resolved = pulse.resolve_omlxc_socket(None, config_loader=lambda: defaults)
    report = pulse.probe_node(
        {
            "id": "node-local",
            "name": "Local",
            "role": "test",
            "ip": "127.0.0.1",
            "is_local": True,
            "engines": {"omlxc": {"path": "/health"}},
        },
        omlxc_socket=resolved,
    )

    assert resolved == socket_path
    assert report.engines["omlxc"].alive is True
    assert "/tmp/omlxc.sock" not in (Path(__file__).parents[2] / "scripts/compute-mesh-pulse.py").read_text()
