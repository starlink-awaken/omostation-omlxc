"""Empty-OS peers must not poison the whole tailnet status document.

2026-10-09 production incident: one tailnet device reported OS='' and
``_parse_peer`` raised INVALID_SNAPSHOT for the entire document, which
vetoed authorization for every remote backend at once (all placements
went stale simultaneously). The peer-level ``os`` field is informational
only; an empty value must degrade to ``unknown`` instead of failing the
snapshot. A structurally missing OS key still invalidates the document.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omlxc.adapters.process import ProcessOutput
from omlxc.adapters.tailscale import (
    TailscaleAdapter,
    TailscaleErrorCode,
    TailscaleFailure,
    TailscaleNodePolicy,
)

_KEY = "nodekey:FAKEPEER000000000000000000000001"
_EMPTY_OS_KEY = "nodekey:FAKEPEER000000000000000000000002"
_DNS = "compute-a.example.test"


def _executable(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    executable = directory / "tailscale-fake"
    executable.write_text("empty-os fixture; never executed\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _policy() -> TailscaleNodePolicy:
    return TailscaleNodePolicy(
        node_id="node-a",
        expected_peer_id="peeridFAKE000000000000000000000001",
        expected_public_key=_KEY,
        magic_dns_name=_DNS,
        allowed_ips=frozenset({"100.64.0.10"}),
        allowed_http_ports=frozenset({1234}),
        allowed_ssh_users=frozenset({"operator"}),
    )


def _status() -> dict[str, Any]:
    return {
        "Self": {
            "ID": "peeridFAKESELF000000000000000000000",
            "PublicKey": "nodekey:FAKESELF000000000000000000000000",
            "HostName": "controller",
            "DNSName": "controller.example.test.",
            "TailscaleIPs": ["100.64.0.1"],
            "Online": True,
            "OS": "macOS",
        },
        "Peer": {
            _KEY: {
                "ID": "peeridFAKE000000000000000000000001",
                "PublicKey": _KEY,
                "HostName": "compute-a",
                "DNSName": "compute-a.example.test.",
                "TailscaleIPs": ["100.64.0.10"],
                "Online": True,
                "OS": "linux",
            }
        },
    }


def _with_empty_os_peer(status: dict[str, Any]) -> dict[str, Any]:
    peers = dict(status["Peer"])
    peers[_EMPTY_OS_KEY] = {
        "ID": "peeridFAKE000000000000000000000002",
        "PublicKey": _EMPTY_OS_KEY,
        "HostName": "muse",
        "DNSName": "muse.example.test.",
        "TailscaleIPs": ["100.64.0.99"],
        "Online": True,
        "OS": "",
    }
    status["Peer"] = peers
    return status


def _runner(response: object) -> Any:
    async def run(argv: tuple[str, ...], timeout: float) -> ProcessOutput:
        del argv, timeout
        return ProcessOutput(0, json.dumps(response), "")

    return run


@pytest.mark.asyncio
async def test_empty_os_peer_keeps_snapshot_and_authorization_alive(
    tmp_path: Path,
) -> None:
    status = _with_empty_os_peer(_status())
    adapter = TailscaleAdapter(
        policies=(_policy(),),
        tailscale_executable=_executable(tmp_path / "trusted-bin"),
        process_runner=_runner(status),
    )

    snapshot = await adapter.snapshot()
    assert [node.node_id for node in snapshot.nodes] == ["node-a"]
    endpoint = adapter.authorize_http("node-a", f"http://{_DNS}:1234")
    assert endpoint.host == _DNS
    assert endpoint.port == 1234


@pytest.mark.asyncio
async def test_missing_os_key_still_invalidates_snapshot(tmp_path: Path) -> None:
    status = _with_empty_os_peer(_status())
    del status["Peer"][_EMPTY_OS_KEY]["OS"]
    adapter = TailscaleAdapter(
        policies=(_policy(),),
        tailscale_executable=_executable(tmp_path / "trusted-bin"),
        process_runner=_runner(status),
    )

    with pytest.raises(TailscaleFailure) as captured:
        await adapter.snapshot()
    assert captured.value.code is TailscaleErrorCode.INVALID_SNAPSHOT


@pytest.mark.asyncio
async def test_empty_os_on_authorized_peer_degrades_to_unknown(
    tmp_path: Path,
) -> None:
    status = _status()
    status["Peer"][_KEY]["OS"] = ""
    adapter = TailscaleAdapter(
        policies=(_policy(),),
        tailscale_executable=_executable(tmp_path / "trusted-bin"),
        process_runner=_runner(status),
    )

    snapshot = await adapter.snapshot()
    assert all(node.os == "unknown" for node in snapshot.nodes)
