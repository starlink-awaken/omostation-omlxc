"""Tiered speculative router — light/mid/heavy 三层分级 (BET-Y1Q4-T3-03).

扩展 ADR-0197 SpeculativeRouter 的两层语义为三层 (DRY — 组合复用, 不重写):
  light  1.5B/3B  — 意图分类 + 槽位提取 (<5ms), 简单指令就地秒结
  mid    8B/14B   — 公文格式校验 + 草稿初筛
  heavy  27B/70B  — 深度拟稿 + 政策推演

路由决策 = 纯规则热路径 (<1ms, 零模型调用 — done_when 无抖动契约);
投机级联: draft 始于 light, 置信信号不足自动升阶重写。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "omlxc.dataplane.speculative-router.v1"

TIERS = {
    "light": {"models": ["qwen3.5-1.5b", "qwen3.5-3b"], "budget_ms": 5, "role": "意图分类+槽位提取"},
    "mid": {"models": ["qwen2.5-coder:8b", "qwen2.5-coder:14b"], "budget_ms": 800, "role": "格式校验+草稿初筛"},
    "heavy": {"models": ["qwen3.8-27b", "llama-4-70b"], "budget_ms": 5000, "role": "深度拟稿+政策推演"},
}

# 复杂度信号 (升阶触发词) — 语义继承 speculative.py, 三层化细分
_HEAVY_SIGNALS = ("架构设计", "长远愿景", "博弈推演", "复杂重构", "红蓝对抗", "立项方案", "政策推演", "战略规划", "深度分析")
_MID_SIGNALS = ("校验", "初筛", "审阅", "格式", "摘要", "翻译", "改写", "多段", "报告", "公文生成")
_LIGHT_ACTIONS = ("查", "看看", "几点", "天气", "提醒", "记录", "备注", "打开", "关闭", "搜索", "设置")

_SLOT_PATTERNS = {
    "date": r"今天|明天|后天|本周|下周|\d{1,2}月\d{1,2}日|\d{4}-\d{2}-\d{2}",
    "time": r"\d{1,2}[点:：]\d{2}|上午|下午|晚上",
    "doc_type": r"通知|报告|请示|函件|纪要|方案",
    "entity": r"卫健委|医保局|工信部|公司|医院",
}


@dataclass(frozen=True, slots=True)
class TieredDecision:
    """三层路由决策 (done_when: 路由 <1ms 无抖动)."""

    tier: str
    model: str
    draft_model: str | None  # 投机级联: light 先行
    escalate_reason: str
    slots: dict[str, str] = field(default_factory=dict)
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier, "model": self.model, "draft_model": self.draft_model,
            "escalate_reason": self.escalate_reason, "slots": self.slots,
            "latency_ms": round(self.latency_ms, 4),
        }


def extract_slots(prompt: str) -> dict[str, str]:
    """槽位提取 (light 层职责): 规则面, 轻模型接入位."""
    slots: dict[str, str] = {}
    for name, pattern in _SLOT_PATTERNS.items():
        m = re.search(pattern, prompt)
        if m:
            slots[name] = m.group()
    return slots


def route(prompt: str, domain: str = "general") -> TieredDecision:
    """三层路由决策 — 纯规则热路径, <1ms 契约.

    决策序: heavy 信号 > mid 信号 > light 动作/短文本兜底.
    non_goal 红线: 低延迟敏感任务 (light 命中) 不唤醒高开销大模型.
    """
    t0 = time.perf_counter()
    text = prompt.strip()
    length = len(text)

    if any(k in text for k in _HEAVY_SIGNALS) or length > 600 or domain in ("strategy", "policy-deep"):
        tier, model, reason = "heavy", TIERS["heavy"]["models"][0], "深度推演信号/长文/战略域 → 升阶主力大模型"
    elif any(k in text for k in _MID_SIGNALS) or length > 120:
        tier, model, reason = "mid", TIERS["mid"]["models"][0], "校验/初筛/结构化任务 → 中层模型"
    elif any(a in text for a in _LIGHT_ACTIONS) or length <= 60:
        tier, model, reason = "light", TIERS["light"]["models"][0], "简单指令 → 超轻端侧秒结"
    else:
        tier, model, reason = "light", TIERS["light"]["models"][0], "短常规文本 → 轻端兜底"

    # 投机级联: mid/heavy 的草稿由 light 先行 (置信不足时升级重写)
    draft = TIERS["light"]["models"][0] if tier != "light" else None
    elapsed = (time.perf_counter() - t0) * 1000
    return TieredDecision(tier, model, draft, reason, extract_slots(text), elapsed)


class TieredRouter:
    """有状态门面 (供 aetherforge gateway / pipeline 复用)."""

    def route(self, prompt: str, domain: str = "general") -> TieredDecision:
        return route(prompt, domain)

    def route_batch(self, prompts: list[str]) -> list[TieredDecision]:
        return [route(p) for p in prompts]


def speculative_cascade(prompt: str, light_confidence: float) -> TieredDecision:
    """投机执行语义: light 草稿置信度低 (<0.7) → 升阶 mid; 深度信号 → heavy."""
    decision = route(prompt)
    if decision.tier == "light" and light_confidence < 0.7:
        return TieredDecision("mid", TIERS["mid"]["models"][0], decision.model,
                              f"light 置信 {light_confidence:.2f} <0.7 投机升阶", decision.slots, decision.latency_ms)
    return decision
