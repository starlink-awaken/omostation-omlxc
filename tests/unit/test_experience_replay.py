"""Unit tests for Experience Replay Buffer (ADR-0437)."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from omlxc.dataplane.experience_replay import (
    DomainReplayBuffer,
    ExperienceReplayManager,
    ReplaySample,
)


def test_domain_replay_buffer_reservoir() -> None:
    buf = DomainReplayBuffer(domain="test-domain", max_size=5)
    for i in range(20):
        sample = ReplaySample(
            sample_id=f"s-{i}",
            domain="test-domain",
            instruction=f"inst-{i}",
            output=f"out-{i}",
        )
        buf.add(sample)

    assert len(buf) == 5
    sampled = buf.sample(3)
    assert len(sampled) == 3
    assert all(s.domain == "test-domain" for s in sampled)


def test_experience_replay_manager(tmp_path: Path) -> None:
    mgr = ExperienceReplayManager(
        workspace_root=tmp_path,
        buffer_size_per_domain=10,
        replay_ratio=0.30,
    )

    # Add historical samples
    for i in range(5):
        mgr.add_sample(
            instruction=f"def calc_{i}(): return {i}",
            output=f"def calc_{i}() -> int:\n    return {i}",
            domain="signature-style",
        )

    # Persist and restore
    count = mgr.persist()
    assert count == 5
    assert (tmp_path / ".omo" / "state" / "lora-replay-buffer.jsonl").exists()

    # Restore in a new manager
    mgr2 = ExperienceReplayManager(workspace_root=tmp_path)
    stats = mgr2.stats()
    assert stats["signature-style"]["size"] == 5

    # Build a mixed training batch
    fresh = [
        ReplaySample(sample_id="fresh-1", domain="signature-style", instruction="f1", output="o1"),
        ReplaySample(sample_id="fresh-2", domain="signature-style", instruction="f2", output="o2"),
    ]
    batch = mgr2.build_training_batch(fresh, domain="signature-style", target_batch_size=4)
    assert len(batch.all_samples) >= 2
    assert batch.domain == "signature-style"
    assert batch.total_samples == len(batch.fresh_samples) + len(batch.replay_samples)


# --- BET-Y1Q3-T10-105: real distill dispatch, adapter lifecycle, alignment ---

from omlxc.dataplane.experience_replay import (  # noqa: E402
    DEFAULT_ADAPTER_NAME,
    dispatch_distill,
    evaluate_alignment,
    adapter_dir,
    adapter_status,
)


class _FakeDecision:
    def __init__(self) -> None:
        self.target_node_id = "node-macmini-m4"
        self.target_endpoint = "192.168.1.20:8765"
        self.decision_reason = "roamed to mesh peer"


class _FakeRouter:
    def route_job(self, *, job_id: str, model_id: str, estimated_vram_gb: float = 6.0):
        return _FakeDecision()


def _manager_with_samples(tmp_path: Path, n: int) -> ExperienceReplayManager:
    mgr = ExperienceReplayManager(workspace_root=tmp_path)
    for i in range(n):
        mgr.add_sample(instruction=f"inst-{i}", output=f"out-{i}")
    return mgr


def test_dispatch_distill_insufficient_samples(tmp_path: Path) -> None:
    mgr = _manager_with_samples(tmp_path, 3)
    job = dispatch_distill(mgr, router=None)
    assert job.status == "insufficient_samples"
    assert job.sample_count == 3


def test_dispatch_distill_routes_to_mesh_when_no_local_mlx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omlxc.dataplane.experience_replay as er

    monkeypatch.setattr(er, "_mlx_lm_available", lambda: False)
    mgr = _manager_with_samples(tmp_path, 12)
    job = dispatch_distill(mgr, router=_FakeRouter())
    assert job.status == "routed"
    assert job.target_node == "node-macmini-m4"
    assert job.target_endpoint.endswith(":8765")


def test_dispatch_distill_honest_failure_without_router(tmp_path: Path) -> None:
    import omlxc.dataplane.experience_replay as er

    monkeypatch_target = er
    monkeypatch_target = pytest.MonkeyPatch()
    try:
        monkeypatch_target.setattr(er, "_mlx_lm_available", lambda: False)
        mgr = _manager_with_samples(tmp_path, 12)
        job = dispatch_distill(mgr, router=None)
        assert job.status == "needs_mlx"
        assert "mlx_lm not installed" in job.detail
    finally:
        monkeypatch_target.undo()


def test_dispatch_distill_local_training_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MLX present path: subprocess is invoked with mlx_lm.lora and must succeed."""
    import omlxc.dataplane.experience_replay as er

    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # Simulate adapter artifacts produced by training
        adapter_arg = cmd[cmd.index("--adapter-path") + 1]
        Path(adapter_arg).mkdir(parents=True, exist_ok=True)
        (Path(adapter_arg) / "adapters.safetensors").write_bytes(b"stub")
        return _Proc()

    monkeypatch.setattr(er, "_mlx_lm_available", lambda: True)
    monkeypatch.setattr(er.subprocess, "run", fake_run)
    mgr = _manager_with_samples(tmp_path, 12)
    job = dispatch_distill(mgr, router=None)
    assert job.status == "dispatched"
    assert job.target_node == "local"
    assert len(calls) == 1 and "mlx_lm" in calls[0][2]
    assert adapter_status(DEFAULT_ADAPTER_NAME, tmp_path)["exists"] is True


def test_adapter_status_missing_and_present(tmp_path: Path) -> None:
    st = adapter_status("nonexistent", tmp_path)
    assert st["exists"] is False
    d = adapter_dir(tmp_path, "a1")
    d.mkdir(parents=True)
    (d / "adapters.safetensors").write_bytes(b"x")
    st2 = adapter_status("a1", tmp_path)
    assert st2["exists"] is True and st2["files"] == ["adapters.safetensors"]


def test_evaluate_alignment_meets_target() -> None:
    ref = "the quick brown fox jumps over the lazy dog"
    base = "a fox saw the dog and something completely different entirely"
    adapter = "the quick brown fox leaps over the lazy dog today"
    result = evaluate_alignment(ref, base_output=base, adapter_output=adapter)
    assert result["adapter_score"] > result["base_score"]
    assert result["relative_improvement"] > 0
    assert result["meets_target"] is (result["relative_improvement"] >= 0.25)


def test_evaluate_alignment_zero_base_guard() -> None:
    result = evaluate_alignment("some reference text", base_output="", adapter_output="x")
    assert result["relative_improvement"] == 0.0
    assert result["meets_target"] is False
