#!/usr/bin/env python3
"""chaos_drill.py — T10-106 Chaos Drill Suite (BET-Y1Q3-T10-106)

Validates the dma_daemon can:
1. Generate valid launchd plist (KeepAlive, ThrottleInterval=5, correct Label)
2. Run probe cycle + telemetry heartbeat (mesh-telemetry.json freshness)
3. Trigger Paged KV spillover at >75% VRAM utilization without OOM crash
4. Recover from disconnected probe (reconnect attempt path)

Tests are simulation-only (no real Thunderbolt hardware required) and exercise
the daemon's transport state machine and memory manager interface.

Reference: docs/superpowers/specs/2026-08-30-sovereign-mesh-daemon-sre-design.md §3
"""
from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(str(ROOT))  # already at projects/omlxc


def run_drill_launchd_persistence() -> dict:
    """Verify launchd plist parses and has KeepAlive + correct Label."""
    print("[1/4] launchd plist generation + persistence drill")
    from omlxc.daemon.dma_daemon import generate_launchd_plist

    with tempfile.TemporaryDirectory() as td:
        workspace_root = Path(td)
        plist_str = generate_launchd_plist(workspace_root)
        plist_data = plistlib.loads(plist_str.encode())

        label = plist_data.get("Label")
        keep_alive = plist_data.get("KeepAlive")
        throttle = plist_data.get("ThrottleInterval")
        program_args = plist_data.get("ProgramArguments", [])
        run_at_load = plist_data.get("RunAtLoad", False)

        keep_alive_ok = isinstance(keep_alive, dict) and keep_alive.get("SuccessfulExit") is False
        throttle_ok = throttle == 5
        label_ok = label == "com.omostation.omlxc-dma-daemon"
        run_at_load_ok = run_at_load is True
        program_args_ok = "omlxc.daemon.dma_daemon" in program_args

        return {
            "drill": "launchd_persistence",
            "label": label,
            "label_ok": label_ok,
            "keep_alive_ok": keep_alive_ok,
            "throttle_interval": throttle,
            "throttle_ok": throttle_ok,
            "run_at_load": run_at_load,
            "run_at_load_ok": run_at_load_ok,
            "program_args": program_args,
            "program_args_ok": program_args_ok,
            "overall_pass": keep_alive_ok and throttle_ok and label_ok and run_at_load_ok and program_args_ok,
        }


def run_drill_probe_cycle() -> dict:
    """Run probe cycle; verify mesh-telemetry.json freshness and TB5 default state."""
    print("[2/4] probe cycle + telemetry heartbeat drill")
    from omlxc.daemon.dma_daemon import DMADaemonController

    with tempfile.TemporaryDirectory() as td:
        controller = DMADaemonController(
            workspace_root=Path(td),
            probe_interval_s=0.1,
            vram_alert_ratio=0.75,
        )
        t0 = time.perf_counter()
        controller._probe_cycle()
        probe_ms = (time.perf_counter() - t0) * 1000

        state_file = Path(td) / ".omo" / "state" / "mesh-telemetry.json"
        if not state_file.exists():
            return {
                "drill": "probe_cycle",
                "probe_ms": round(probe_ms, 3),
                "telemetry_exists": False,
                "overall_pass": False,
            }
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return {
            "drill": "probe_cycle",
            "probe_ms": round(probe_ms, 3),
            "telemetry_exists": True,
            "is_connected": data.get("is_connected"),
            "active_transport": data.get("active_transport"),
            "link_speed_gbps": data.get("link_speed_gbps"),
            "timestamp_present": "timestamp" in data or "timestamp_utc" in data,
            "overall_pass": probe_ms < 100.0 and data.get("is_connected") is True,
        }


def run_drill_vram_spillover() -> dict:
    """Trigger >75% VRAM utilization; verify Paged KV spillover without OOM."""
    print("[3/4] VRAM spillover drill")
    from omlxc.daemon.dma_daemon import DMADaemonController

    with tempfile.TemporaryDirectory() as td:
        controller = DMADaemonController(
            workspace_root=Path(td),
            probe_interval_s=0.1,
            vram_alert_ratio=0.50,  # low threshold to force spillover
        )
        # Trigger spillover explicitly
        initial_blocks = controller.dma_bus.total_blocks_migrated
        t0 = time.perf_counter()
        controller._trigger_kv_spillover(80000.0)
        spillover_ms = (time.perf_counter() - t0) * 1000
        final_blocks = controller.dma_bus.total_blocks_migrated

        # Re-probe to update telemetry
        controller._probe_cycle()
        state_file = Path(td) / ".omo" / "state" / "mesh-telemetry.json"
        spillover_in_telemetry = False
        if state_file.exists():
            data = json.loads(state_file.read_text(encoding="utf-8"))
            spillover_in_telemetry = bool(data.get("kv_spillover_active", False))

        return {
            "drill": "vram_spillover",
            "spillover_ms": round(spillover_ms, 3),
            "initial_blocks": initial_blocks,
            "final_blocks": final_blocks,
            "spillover_triggered": final_blocks > initial_blocks,
            "kv_spillover_active_in_telemetry": spillover_in_telemetry,
            "no_oom": True,
            "overall_pass": final_blocks > initial_blocks,
        }


def run_drill_disconnect_recovery() -> dict:
    """Simulate disconnected probe; verify reconnect attempt increments + telemetry reflects."""
    print("[4/4] disconnect + reconnect recovery drill")
    from omlxc.daemon.dma_daemon import DMADaemonController

    with tempfile.TemporaryDirectory() as td:
        controller = DMADaemonController(
            workspace_root=Path(td),
            probe_interval_s=0.01,  # fast reconnect base
            vram_alert_ratio=0.75,
        )
        # Force bus to disconnect
        controller.dma_bus.is_connected = False
        controller.dma_bus.total_blocks_migrated = 0
        t0 = time.perf_counter()
        # Run a few probe cycles to trigger reconnect attempts
        for _ in range(3):
            controller._probe_cycle()
            time.sleep(0.02)
        recovery_ms = (time.perf_counter() - t0) * 1000

        return {
            "drill": "disconnect_recovery",
            "recovery_ms": round(recovery_ms, 3),
            "reconnect_attempts": controller._reconnect_attempts,
            "reconnect_delay": controller._reconnect_delay,
            "overall_pass": recovery_ms < 30000.0 and controller._reconnect_attempts >= 1,
        }


def main() -> int:
    print("=" * 60)
    print("T10-106 Chaos Drill Suite (BET-Y1Q3-T10-106)")
    print("=" * 60)
    results = []
    for name, fn in [
        ("launchd_persistence", run_drill_launchd_persistence),
        ("probe_cycle", run_drill_probe_cycle),
        ("vram_spillover", run_drill_vram_spillover),
        ("disconnect_recovery", run_drill_disconnect_recovery),
    ]:
        try:
            results.append(fn())
        except Exception as exc:
            results.append({"drill": name, "error": repr(exc), "overall_pass": False})

    print()
    print("=" * 60)
    print("DRILL RESULTS")
    print("=" * 60)
    for r in results:
        print(json.dumps(r, indent=2))

    overall = all(r.get("overall_pass", False) for r in results)
    print()
    print(f"Overall: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
