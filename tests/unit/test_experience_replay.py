"""Unit tests for Experience Replay Buffer (ADR-0437)."""

from __future__ import annotations

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

from omlxc.dataplane.experience_replay import (
    DEFAULT_ADAPTER_NAME,
    adapter_dir,
    adapter_status,
    dispatch_distill,
    evaluate_alignment,
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


def test_dispatch_distill_routes_to_mesh_when_no_local_mlx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_dispatch_distill_local_training_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_evaluate_alignment_chinese_persona_style() -> None:
    ref = "原则同意测评方案。请信息技术处牵头，会同医政处、基卫处组成专班，明确测评五级乙等指标拆解清单；务必于9月25日17:00前完成首轮42项自评差距分析；核心业务系统改造成本控制在预算内，严禁未经安全审计直连。夏明星 2026-09-05"
    base = "关于区域全民健康信息平台互联互通成熟度测评方案的拟办意见：按照市局文件要求，认真领会精神，建议组织各科室和相关单位进一步研讨，结合我委实际情况稳步推进测评准备工作，待时机成熟时组织申报。"
    adapter = "原则同意测评方案。请信息技术处牵头，会同医政处、基卫处组成专班，明确测评五级乙等指标拆解清单；于9月25日17:00前完成首轮自评差距分析；改造成本控制在预算内，严禁未经安全审计直连。夏明星 2026-09-05"
    result = evaluate_alignment(ref, base_output=base, adapter_output=adapter)
    assert result["adapter_score"] > result["base_score"]
    assert result["relative_improvement"] >= 0.25
    assert result["meets_target"] is True


def test_experience_replay_restore_with_task_id(tmp_path: Path) -> None:
    mgr = ExperienceReplayManager(workspace_root=tmp_path)
    s1 = mgr.add_sample("inst1", "out1", domain="document-review")
    s1.task_id = "BET-Y1Q3-T10-105"
    s2 = mgr.add_sample("inst2", "out2", domain="tech-architecture")
    s2.task_id = "BET-Y1Q3-T10-105"
    mgr.persist()

    mgr2 = ExperienceReplayManager(workspace_root=tmp_path)
    stats = mgr2.stats()
    assert "document-review" in stats
    assert "tech-architecture" in stats
    assert stats["document-review"]["size"] == 1
    assert stats["tech-architecture"]["size"] == 1
