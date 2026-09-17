"""Unit tests for omlxc V5.0 DMA Daemon Controller (ADR-0439)."""

from __future__ import annotations

import json
from pathlib import Path

from omlxc.daemon.dma_daemon import (
    DMADaemonController,
    generate_launchd_plist,
)


def test_dma_daemon_initialization(tmp_path: Path) -> None:
    controller = DMADaemonController(
        workspace_root=tmp_path,
        probe_interval_s=0.1,
        vram_alert_ratio=0.75,
    )
    assert controller.ws == tmp_path
    assert controller.dma_bus is not None
    assert controller.paged_kv is not None


def test_dma_daemon_single_probe_cycle(tmp_path: Path) -> None:
    controller = DMADaemonController(
        workspace_root=tmp_path,
        probe_interval_s=0.1,
        vram_alert_ratio=0.75,
    )
    controller.update_lora_adapter("lora-signature-style")

    # Run single probe cycle
    controller._probe_cycle()

    # Verify telemetry file written
    state_file = tmp_path / ".omo" / "state" / "mesh-telemetry.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert "is_connected" in data
    assert data["is_connected"] is True
    assert data["active_transport"] == "THUNDERBOLT_5_DMA"
    assert data["link_speed_gbps"] == 120.0
    assert data["numa_pool_size_gb"] == 152.0
    assert data["lora_active_adapter"] == "lora-signature-style"


def test_dma_daemon_spillover_trigger(tmp_path: Path) -> None:
    controller = DMADaemonController(
        workspace_root=tmp_path,
        probe_interval_s=0.1,
        vram_alert_ratio=0.50,  # low threshold to trigger spillover
    )
    # Simulate high VRAM usage above 50%
    controller._trigger_kv_spillover(80000.0)
    assert controller.dma_bus.total_blocks_migrated >= 1


def test_generate_launchd_plist(tmp_path: Path) -> None:
    plist = generate_launchd_plist(tmp_path, python_path="/opt/homebrew/bin/python3")
    assert "<key>Label</key>" in plist
    assert "com.omostation.omlxc-dma-daemon" in plist
    assert "/opt/homebrew/bin/python3" in plist
    assert str(tmp_path) in plist
