"""Read-only direct diagnostics used when the daemon is unavailable."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any

from omlxc.config import AppConfig
from omlxc.daemon.composition import (
    build_configured_adapter,
    build_configured_tailscale,
    is_loopback_url,
)
from omlxc.domain import BackendKind
from omlxc.service import LaunchdPaths


async def run_direct_doctor(config: AppConfig) -> dict[str, Any]:
    """Inspect configured surfaces without creating state or mutating services."""
    checks: list[dict[str, object]] = [{"name": "config", "ok": True}]
    paths = LaunchdPaths.for_home(Path.home())
    checks.append(_private_path_check("launchd_plist", paths.plist_path, expected_mode=0o600))
    checks.append(_private_path_check("daemon_socket", config.daemon.socket_path, expected_mode=0o600))

    tailscale = build_configured_tailscale(config)
    if config.tailscale is not None:
        if tailscale is None:
            checks.append({"name": "tailscale", "ok": False, "detail": "configuration unavailable"})
        else:
            try:
                await tailscale.snapshot()
                checks.append({"name": "tailscale", "ok": True})
            except asyncio.CancelledError:
                raise
            except Exception:
                checks.append({"name": "tailscale", "ok": False, "detail": "identity check failed"})

    nodes = {node.id: node for node in config.nodes}
    for backend in config.backends:
        adapter = None
        try:
            try:
                adapter = build_configured_adapter(backend, tailscale=tailscale)
                if not is_loopback_url(backend.base_url):
                    node = nodes[backend.node_id]
                    if node.tailscale is None or tailscale is None:
                        raise PermissionError
                    tailscale.authorize_http(node.id, backend.base_url)
                    if (
                        backend.kind in {BackendKind.LM_STUDIO, BackendKind.LM_LINK}
                        and backend.control_endpoint is not None
                    ):
                        authorized_ssh = tailscale.authorize_ssh(node.id, backend.control_endpoint)
                        if authorized_ssh.target != backend.control_endpoint:
                            raise PermissionError
                await adapter.discover()
                checks.append({"name": f"backend:{backend.id}", "ok": True})
                checks.append(await _placement_inventory_check(config, backend, adapter))
            except asyncio.CancelledError:
                raise
            except Exception:
                checks.append(
                    {
                        "name": f"backend:{backend.id}",
                        "ok": False,
                        "detail": "read-only probe failed",
                    }
                )
        finally:
            close = getattr(adapter, "aclose", None) if adapter is not None else None
            if close is not None:
                await asyncio.gather(close(), return_exceptions=True)
    healthy = all(bool(check["ok"]) for check in checks)
    return {"status": "healthy" if healthy else "degraded", "checks": checks}


async def _placement_inventory_check(
    config: AppConfig,
    backend: Any,
    adapter: Any,
) -> dict[str, object]:
    """Reconcile configured backend_model_id values against what the backend serves.

    A placement whose backend_model_id is absent from the backend's inventory stays
    visible in config and in the routing catalog, but every operation on it fails —
    and the failure reads as a generic operation error rather than "the target does
    not exist". The daemon only reports this as an inventory count delta, which does
    not say which placements went missing.
    """
    name = f"inventory:{backend.id}"
    placements = [item for item in config.placements if item.backend_id == backend.id]
    if not placements:
        return {"name": name, "ok": True, "detail": "no placements configured"}

    try:
        served = {model.id for model in await adapter.list_models()}
    except asyncio.CancelledError:
        raise
    except Exception:
        return {"name": name, "ok": False, "detail": "inventory unavailable"}

    missing = sorted(item.id for item in placements if item.backend_model_id not in served)
    if missing:
        return {
            "name": name,
            "ok": False,
            "detail": f"{len(missing)}/{len(placements)} placements target a model the backend does not serve",
            "placements": missing,
        }
    return {"name": name, "ok": True, "detail": f"{len(placements)} placements reconciled"}


def _private_path_check(name: str, path: Path, *, expected_mode: int) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"name": name, "ok": False, "detail": "missing"}
    except OSError:
        return {"name": name, "ok": False, "detail": "unreadable"}
    mode = stat.S_IMODE(metadata.st_mode)
    ok = not stat.S_ISLNK(metadata.st_mode) and metadata.st_uid == os.geteuid()
    if name == "daemon_socket":
        ok = ok and stat.S_ISSOCK(metadata.st_mode)
    else:
        ok = ok and stat.S_ISREG(metadata.st_mode)
    ok = ok and mode == expected_mode
    return {"name": name, "ok": ok, "detail": "ok" if ok else "unsafe permissions or type"}
