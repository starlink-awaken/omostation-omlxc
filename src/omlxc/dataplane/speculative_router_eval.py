"""speculative_router_eval — T3-03 verify 契约 (python -m 直接可跑).

评测集: 80% 简单 (light 秒结) / 20% 复杂 (升阶) — done_when 比例;
断言: 秒结率≥80% · 复杂 100% 升阶 · 路由延迟 median <1ms · 槽位提取.
"""

from __future__ import annotations

import json
import sys
import time

from omlxc.dataplane.speculative_router import TIERS, extract_slots, route, speculative_cascade

_SIMPLE = [
    "今天天气怎么样", "帮我查一下医保目录", "明天几点开会", "提醒我下午三点回电",
    "记录一条备注", "搜索相关文件", "打开设置", "看看最新通知",
    "今天日期", "下午提醒我喝水", "查一下余额", "记录会议纪要要点",
    "设置闹钟", "搜索政策文件", "看看邮箱", "备注一下这个想法",
] * 5  # 80 条

_COMPLEX = [
    "对这个架构设计进行深度推演并给出红蓝对抗分析",
    "基于长远愿景制定立项方案，包含政策博弈推演",
    "这是一份关于数据要素互联互通的复杂重构与战略规划报告，需要深度分析多方立场和长期演进路径，输出完整的政策推演与建议书",  # 长文+信号
] * 5 + ["为省级医疗大模型试点做架构设计与长远规划推演"] * 5  # 20 条

_MID = [
    "帮我校验这份公文的格式是否合规",
    "对初稿做一轮审阅并提出修改建议",
    "把这份报告翻译成英文", "生成会议纪要摘要",
] * 5  # 20 条 mid 层 (额外)


def main() -> int:
    simple_hits = sum(1 for p in _SIMPLE if route(p).tier == "light")
    complex_heavy = sum(1 for p in _COMPLEX if route(p).tier == "heavy")
    mid_hits = sum(1 for p in _MID if route(p).tier == "mid")

    samples: list[float] = []
    all_prompts = _SIMPLE + _COMPLEX + _MID
    for _ in range(5):  # median-of-5 over full set
        t0 = time.perf_counter()
        for p in all_prompts:
            route(p)
        samples.append((time.perf_counter() - t0) * 1000 / len(all_prompts))
    samples.sort()
    route_ms = samples[len(samples) // 2]

    slots = extract_slots("明天下午三点在卫健委开报告讨论会")
    cascade = speculative_cascade("查一下", light_confidence=0.3)

    checks = {
        "simple_instant_rate_ge_80": simple_hits / len(_SIMPLE) >= 0.80,
        "complex_escalate_100pct": complex_heavy == len(_COMPLEX),
        "mid_routed_correctly": mid_hits == len(_MID),
        "route_latency_under_1ms": route_ms < 1.0,
        "slots_extracted": slots.get("date") == "明天" and slots.get("doc_type") == "报告",
        "speculative_cascade_escalates": cascade.tier == "mid",
    }
    report = {
        "schema": "omlxc.dataplane.speculative-router-eval.v1",
        "tiers": TIERS,
        "simple_total": len(_SIMPLE), "simple_light_hits": simple_hits,
        "instant_rate": round(simple_hits / len(_SIMPLE), 3),
        "complex_heavy_rate": round(complex_heavy / len(_COMPLEX), 3),
        "route_latency_ms_median": round(route_ms, 4),
        "checks": checks,
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
