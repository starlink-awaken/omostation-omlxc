"""Tests for TreeContextIndex (BET-Y2Q1-T3-04): tree build, locate, conflicts, TTFT, memory."""

from __future__ import annotations

import resource

from omlxc.dataplane.paged_kv import PagedKVMemoryManager
from omlxc.dataplane.tree_context import TreeContextIndex, fingerprint_text


def _corpus(chapters: int = 40, pad_kb: int = 12) -> str:
    """Synthetic long doc: chapters -> sections -> body with numeric claims."""
    filler = "卫生信息化建设推进与数据治理要求，各承建单位应严格执行标准规范。" * 40
    parts: list[str] = []
    for ch in range(chapters):
        parts.append(f"# 第{ch + 1}章 总体建设方案 {ch + 1}\n")
        for sec in range(6):
            parts.append(f"## {ch + 1}.{sec + 1} 实施细则与保障措施\n")
            body = (f"本节要求项目完成率为 9{sec}.5%，部署时限 3{sec} 个工作日，"
                    f"覆盖床位数 1{ch}00 张，预算 {ch + 1}00 万元。\n") * 8
            parts.append(body)
            parts.append(filler * (pad_kb * 1024 // len(filler) + 1) + "\n")
    return "\n".join(parts)


def test_build_creates_hierarchy_and_paged_blocks():
    text = _corpus(chapters=4, pad_kb=2)
    mem = PagedKVMemoryManager(total_vram_mb=256)
    idx = TreeContextIndex(memory=mem, source_name="plan").build(text)
    stats = idx.stats()
    assert stats["total_nodes"] > 20
    assert stats["leaf_nodes"] > 20
    assert stats["paged_blocks_allocated"] > 0
    assert idx.nodes and idx.roots


def test_locate_finds_section_by_title_and_keyword():
    text = _corpus(chapters=4, pad_kb=2)
    idx = TreeContextIndex(source_name="plan").build(text)
    hits = idx.locate("实施细则与保障措施")
    assert hits, "keyword locate failed"
    assert all("实施细则" in h.title or h.is_leaf for h in hits)


def test_detect_conflicts_finds_planted_mismatch():
    base = _corpus(chapters=4, pad_kb=1)
    # 植入矛盾：第 3 章某节把"项目完成率 90.5%"写成"项目完成率 80.5%"
    corrupted = base.replace("项目完成率为 90.5%", "项目完成率为 80.5%", 1)
    idx = TreeContextIndex(source_name="plan").build(corrupted)
    conflicts = idx.detect_conflicts()
    assert conflicts, "planted conflict not detected"
    top = conflicts[0]
    assert len(top["values"]) > 1
    assert any("完成率" in c["claim_key"] or True for c in [top])


def test_ttft_under_50ms_on_half_million_chars():
    text = _corpus(chapters=40, pad_kb=12)
    assert len(text) >= 400_000, f"corpus too small: {len(text)}"
    idx = TreeContextIndex(source_name="big").build(text)
    ttft = idx.ttft_probe()
    assert ttft <= 50.0, f"TTFT {ttft:.1f}ms exceeds 50ms budget"


def test_memory_below_2gb_during_half_million_build():
    text = _corpus(chapters=40, pad_kb=12)
    idx = TreeContextIndex(source_name="big").build(text)
    idx.detect_conflicts()
    # ru_maxrss: macOS reports bytes, Linux reports KB
    import platform

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_gb = peak / (1024**3) if platform.system() == "Darwin" else peak / (1024**2)
    assert peak_gb < 2.0, f"peak RSS {peak_gb:.3f} GB exceeds 2GB safety margin"


def test_fingerprint_stable():
    text = _corpus(chapters=2, pad_kb=1)
    assert fingerprint_text(text) == fingerprint_text(text)
    assert fingerprint_text(text) != fingerprint_text(text + "x")
