"""Tests for hard_negative_miner (BET-Y1Q3-T10-115): diff parse, intent classify, rule mining."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omlxc.dataplane.hard_negative_miner import (
    classify_hunk,
    export_rules,
    mine_negatives,
    parse_sample,
    parse_signature_diff,
)


def _replay_buffer() -> Path | None:
    """Locate the LoRA replay buffer.

    It is operator workspace state, not a repository fixture, so it is absent on
    any checkout that is not the operator's own — CI included.
    """
    override = os.environ.get("OMLX_TEST_REPLAY_BUFFER")
    candidate = (
        Path(override)
        if override
        else Path(__file__).resolve().parents[4] / ".omo" / "state" / "lora-replay-buffer.jsonl"
    )
    return candidate if candidate.exists() else None


def test_parse_signature_diff_detects_ops():
    draft = "为进一步推进全民健康平台建设，各单位要高度重视并切实做好数据互通工作。"
    signed = "全民健康平台建设由各单位落实数据互通。"
    hunks = parse_signature_diff(draft, signed)
    non_equal = [h for h in hunks if h.op != "equal"]
    assert non_equal, "no hunks detected"
    assert any(h.op in ("delete", "replace") for h in non_equal)


def test_classify_hunk_banned_phrase():
    draft = "为进一步推进相关工作，现就有关事项通知如下："
    signed = "现就有关事项通知如下："
    hunks = parse_signature_diff(draft, signed)
    intents = {classify_hunk(h) for h in hunks if h.op != "equal"}
    assert "banned_phrase" in intents


@pytest.mark.skipif(
    _replay_buffer() is None,
    reason="LoRA replay buffer is operator workspace state; set OMLX_TEST_REPLAY_BUFFER to point at one",
)
def test_parse_sample_structured_report_on_real_buffer():
    """done_when[0]: 真实 replay buffer 样本 → 结构化 Diff 报告（含语义分类）。"""
    buf = _replay_buffer()
    assert buf is not None
    count = 0
    with buf.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            report = parse_sample(json.loads(line))
            assert report.sample_id
            assert report.intent_counts or report.hunks or report.draft_len == report.signed_len == 0
            count += 1
            if count >= 5:
                break
    assert count >= 5, "buffer has fewer than 5 samples"


def test_mine_negatives_aggregates_recurring_patterns(tmp_path: Path):
    samples = [
        {"sample_id": f"s{i}", "domain": "document-review",
         "instruction": f"文件{i}：为进一步推进该项工作，请高度重视。",
         "output": f"文件{i}：请抓好落实。"}
        for i in range(4)
    ]
    buf = tmp_path / "buffer.jsonl"
    buf.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples), encoding="utf-8")
    rules, reports = mine_negatives(buf, min_count=2)
    assert len(reports) == 4
    assert rules, "no rules mined from recurring boilerplate"
    assert all(r.count >= 2 for r in rules)


def test_export_rules_jsonl(tmp_path: Path):
    samples = [
        {"sample_id": f"s{i}", "domain": "d",
         "instruction": "为进一步推进工作，应当认真落实。",
         "output": "应当落实。"}
        for i in range(3)
    ]
    buf = tmp_path / "b.jsonl"
    buf.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples), encoding="utf-8")
    rules, _ = mine_negatives(buf)
    out = tmp_path / "rules.jsonl"
    n = export_rules(rules, out)
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert n == len(lines) and lines and lines[0]["rule_id"]
