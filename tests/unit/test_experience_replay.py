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


def test_restore_skips_bad_lines(tmp_path: Path) -> None:
    """坏行跳过: 单条损坏 JSON 不得中断整缓冲恢复 (BET-Y1Q3-T10-105)."""
    persist = tmp_path / ".omo" / "state"
    persist.mkdir(parents=True, exist_ok=True)
    buf_file = persist / "lora-replay-buffer.jsonl"
    good = {
        "sample_id": "good-1",
        "domain": "signature-style",
        "instruction": "i1",
        "output": "o1",
        "captured_at": 1.0,
        "replay_count": 0,
        "importance_weight": 1.0,
    }
    buf_file.write_text(
        json.dumps(good, ensure_ascii=False) + "\n"
        + "{this is not json}\n"
        + "[1, 2, 3]\n",
        encoding="utf-8",
    )
    mgr = ExperienceReplayManager(workspace_root=tmp_path)
    assert mgr.stats()["signature-style"]["size"] == 1


def test_restore_skips_empty_domain(tmp_path: Path) -> None:
    """空 domain 行被跳过, 不得创建空 domain 缓冲 (BET-Y1Q3-T10-105)."""
    persist = tmp_path / ".omo" / "state"
    persist.mkdir(parents=True, exist_ok=True)
    buf_file = persist / "lora-replay-buffer.jsonl"
    buf_file.write_text(
        json.dumps(
            {"sample_id": "x", "domain": "", "instruction": "i", "output": "o"},
        )
        + "\n",
        encoding="utf-8",
    )
    mgr = ExperienceReplayManager(workspace_root=tmp_path)
    assert mgr.stats() == {}


def test_build_training_batch_mixed_ratios(tmp_path: Path) -> None:
    """混合 batch: fresh + replay 按 replay_ratio 混合, 总量一致."""
    mgr = ExperienceReplayManager(
        workspace_root=tmp_path,
        buffer_size_per_domain=10,
        replay_ratio=0.30,
    )
    for i in range(5):
        mgr.add_sample(instruction=f"i-{i}", output=f"o-{i}", domain="signature-style")
    fresh = [
        ReplaySample(sample_id=f"fresh-{i}", domain="signature-style", instruction=f"f{i}", output=f"o{i}")
        for i in range(10)
    ]
    batch = mgr.build_training_batch(fresh, domain="signature-style", target_batch_size=10)
    assert batch.total_samples == len(batch.fresh_samples) + len(batch.replay_samples)
    assert len(batch.replay_samples) >= 1
    assert len(batch.fresh_samples) >= 1
    assert batch.total_samples <= 10
