"""Tests for LoraAdapterManager (BET-Y1Q3-T10-118): domain partition, registry, hot swap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omlxc.dataplane.lora_manager import (
    ADAPTER_NAMES,
    DOMAINS,
    LoraAdapterManager,
    partition_buffer_by_domain,
)


def _buffer(tmp_path: Path, samples: dict[str, list[dict]]) -> Path:
    buf = tmp_path / "buffer.jsonl"
    lines = []
    for domain, items in samples.items():
        for i, s in enumerate(items):
            lines.append(json.dumps({"sample_id": f"{domain}-{i}", "domain": "mixed", **s}, ensure_ascii=False))
    buf.write_text("\n".join(lines), encoding="utf-8")
    return buf


SAMPLES = {
    "gov": [
        {"instruction": "关于县域医共体建设方案的请示，请批复。", "output": "批复：同意按方案推进。"},
        {"instruction": "关于政务督办事项的通知，请各单位落实审批流程。", "output": "通知已阅，请抓好落实。"},
    ],
    "tech": [
        {"instruction": "系统架构评审意见：ADR-030 接口微服务拆分。", "output": "评审通过，注意数据模型一致性。"},
        {"instruction": "技术方案：部署拓扑与代码规范修订。", "output": "同意修订，控制重构范围。"},
    ],
    "email": [
        {"instruction": "收到来信，关于合作事宜请函复。", "output": "复函：感谢来信，保持沟通。"},
        {"instruction": "抄送邮件：答复收件人关于数据的疑问。", "output": "已答复收件人。"},
    ],
}


def test_partition_buffer_by_domain(tmp_path: Path):
    buf = _buffer(tmp_path, SAMPLES)
    shards = partition_buffer_by_domain(buf)
    assert set(shards) == set(DOMAINS)
    assert all(len(v) >= 2 for v in shards.values()), shards


def test_partition_empty_buffer(tmp_path: Path):
    buf = tmp_path / "none.jsonl"
    buf.write_text("", encoding="utf-8")
    shards = partition_buffer_by_domain(buf)
    assert set(shards) == set(DOMAINS) and all(len(v) == 0 for v in shards.values())


def test_registry_hot_swap_roundtrip(tmp_path: Path):
    mgr = LoraAdapterManager(workspace_root=tmp_path, buffer_path=tmp_path / "b.jsonl")
    # 未训练适配器: activate 诚实失败
    res = mgr.activate("gov")
    assert res["ok"] is False and res["status"]["exists"] is False

    # 伪造已训练适配器目录（机制验证，非伪造权重——目录由 dispatch_distill 产出）
    import shutil

    from omlxc.dataplane.experience_replay import adapter_dir

    d = adapter_dir(tmp_path, ADAPTER_NAMES["gov"])
    d.mkdir(parents=True)
    (d / "adapters.safetensors").write_bytes(b"stub")
    res2 = mgr.activate("gov")
    assert res2["ok"] is True and mgr.adapter_for_domain("gov") == ADAPTER_NAMES["gov"]

    # 重启（新实例）后注册表持久
    mgr2 = LoraAdapterManager(workspace_root=tmp_path, buffer_path=tmp_path / "b.jsonl")
    assert mgr2.adapter_for_domain("gov") == ADAPTER_NAMES["gov"]
    assert mgr2.deactivate("gov")["ok"] is True
    assert mgr2.adapter_for_domain("gov") is None


def test_distill_all_pending_samples_honest(tmp_path: Path):
    buf = _buffer(tmp_path, SAMPLES)  # 每域 2 条 < 8
    mgr = LoraAdapterManager(workspace_root=tmp_path, buffer_path=buf)
    records = mgr.distill_all()
    assert len(records) == 3
    assert all(r.status == "pending_samples" for r in records)
    assert all(r.sample_count == 2 for r in records)


def test_distill_all_real_dispatch_when_shard_sufficient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import omlxc.dataplane.lora_manager as lm

    big = {d: SAMPLES[d] * 6 for d in DOMAINS}  # 每域 12 条 ≥ 8
    buf = _buffer(tmp_path, big)
    mgr = LoraAdapterManager(workspace_root=tmp_path, buffer_path=buf)

    calls = []

    class _Job:
        def __init__(self, domain):
            self.sample_count = 12
            self.status = "routed"
            self.detail = "roamed"
            self.adapter_path = ""
            self.target_node = "node-m4"

    def fake_dispatch(manager, domain="", epochs=3, adapter_name="", router=None):
        calls.append(domain)
        return _Job(domain)

    monkeypatch.setattr(lm, "dispatch_distill", fake_dispatch)
    records = mgr.distill_all()
    assert calls == list(DOMAINS)
    assert all(r.status == "routed" and r.target_node == "node-m4" for r in records)


def test_shard_manager_reconstructs_real_replay_samples(tmp_path: Path):
    """dispatch_distill's local-MLX path reads .instruction/.output off whatever
    build_training_batch hands back. _ShardManager used to hand back the raw
    JSONL dicts unmodified — this crashed with AttributeError the moment
    mlx_lm was actually installed, but every test in this file monkeypatches
    dispatch_distill entirely, so nothing here exercised that path.
    """
    from omlxc.dataplane.experience_replay import ReplaySample
    from omlxc.dataplane.lora_manager import _ShardManager

    shard_path = tmp_path / "shard.jsonl"
    shard_path.write_text(
        json.dumps({"sample_id": "gov-0", "domain": "gov", "instruction": "请示", "output": "批复"}) + "\n",
        encoding="utf-8",
    )

    mgr = _ShardManager(tmp_path, shard_path)
    buf = mgr.get_or_create_buffer("gov")

    assert len(buf) == 1
    assert all(isinstance(s, ReplaySample) for s in buf.samples)

    batch = mgr.build_training_batch(fresh_samples=list(buf.samples), domain="gov")
    sample = batch.all_samples[0]
    assert sample.instruction == "请示"
    assert sample.output == "批复"


def test_list_adapters_shape(tmp_path: Path):
    mgr = LoraAdapterManager(workspace_root=tmp_path, buffer_path=tmp_path / "b.jsonl")
    rows = mgr.list_adapters(include_eval=True)
    assert len(rows) == 3
    assert {r["domain"] for r in rows} == set(DOMAINS)
    assert all(r["active"] is False and r["exists"] is False for r in rows)
