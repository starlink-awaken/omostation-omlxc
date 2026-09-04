"""Speculative Execution & Hybrid Routing Engine (ADR-0197).

Routes agent queries to local-first lightweight speculative models (8B/14B Q4_K_M)
or cascades to frontier cloud models based on task complexity, AST safety, and policy depth.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

# BatchA (BET-Y1Q3-T10-114): DFlash 2 block-diffusion draft constants.
# Target: Qwen3.8-27B speculative decoding, throughput >=120 tokens/s, speedup 2.4x.
DFLASH2_DRAFT_MODEL = "qwen3.8-27b-dflash2"
DFLASH2_TARGET_MODEL = "qwen3.8-27b"
DFLASH2_EXPECTED_SPEEDUP = 2.4
# Circuit breaker (bet contract): draft acceptance < 40% -> degrade to
# standard autoregressive decoding.
SPECULATIVE_FALLBACK_ACCEPTANCE_THRESHOLD = 0.40


@dataclass(frozen=True, slots=True)
class SpeculativeRoutingDecision:
    target_tier: str  # "local" | "cloud" | "hybrid-speculative"
    recommended_model: str
    draft_model: str | None
    estimated_speedup_ratio: float
    reasoning: str
    spec_engine: str | None = None  # "dflash2" | "standard" | None (BatchA T10-114)
    fallback_applied: bool = False  # True when <40% circuit breaker fired

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_tier": self.target_tier,
            "recommended_model": self.recommended_model,
            "draft_model": self.draft_model,
            "estimated_speedup_ratio": self.estimated_speedup_ratio,
            "reasoning": self.reasoning,
            "spec_engine": self.spec_engine,
            "fallback_applied": self.fallback_applied,
        }


class SpeculativeRouter:
    """Evaluates task context and determines optimal local vs cloud speculative routing."""

    def __init__(self) -> None:
        pass

    def evaluate(self, prompt: str, domain: str = "general") -> SpeculativeRoutingDecision:
        text = prompt.strip()
        length = len(text)

        # 1. Check for quick AST / Syntax / Format tasks -> Local 8B/14B
        is_local_triage = length < 120 and not any(k in text for k in ["架构设计", "长远愿景", "博弈推演", "复杂重构", "红蓝对抗"])
        if is_local_triage:
            return SpeculativeRoutingDecision(
                target_tier="local",
                recommended_model="qwen2.5-coder:14b",
                draft_model="qwen2.5-coder:7b",
                estimated_speedup_ratio=2.8,
                reasoning="任务特征属于高频结构化/语法分诊类，由本地 14B Q4_K_M 模型独占处理，0 成本且 0 隐私泄露。",
            )

        # 2. Check for complex strategic / architectural / multi-perspective tasks -> Hybrid Speculative or Cloud
        is_deep_reasoning = any(k in text for k in ["架构", "愿景", "长远", "推演", "博弈", "审计", "合规", "红蓝对抗", "立项方案"])
        if is_deep_reasoning or length > 500:
            return SpeculativeRoutingDecision(
                target_tier="hybrid-speculative",
                recommended_model="claude-3-5-sonnet / deepseek-r1",
                draft_model="qwen2.5-coder:14b",
                estimated_speedup_ratio=1.9,
                reasoning="涉及深层架构规划与政策博弈推演，启用本地 14B 投机草稿生成 + 云端 Frontier 模型核验级联。",
            )

        # 3. Default general task
        return SpeculativeRoutingDecision(
            target_tier="local",
            recommended_model="qwen2.5-coder:14b",
            draft_model=None,
            estimated_speedup_ratio=1.5,
            reasoning="常规领域任务，优先分配本地算力底座处理。",
        )

    def select_draft_engine(self, prompt: str, domain: str = "general") -> str:
        """BatchA (T10-114): choose DFlash2 block-diffusion draft when eligible.

        DFlash2 diffusion draft pays off on medium+ prompts (>=120 chars) or
        code/general domains; tiny triage prompts stay on standard drafting.
        """
        text = prompt.strip()
        if len(text) >= 120 or domain in ("code", "general"):
            return "dflash2"
        return "standard"

    @staticmethod
    def should_fallback_to_autoregressive(acceptance_rate: float) -> bool:
        """Circuit breaker: acceptance < 40% -> degrade to autoregressive."""
        return acceptance_rate < SPECULATIVE_FALLBACK_ACCEPTANCE_THRESHOLD

    def route_with_dflash2(
        self,
        prompt: str,
        domain: str = "general",
        acceptance_rate: float | None = None,
    ) -> SpeculativeRoutingDecision:
        """BatchA entry: base routing + DFlash2 draft + <40% fallback.

        When a measured draft acceptance rate is provided and below threshold,
        degrades to standard autoregressive decoding (no draft, speedup 1.0).
        """
        if acceptance_rate is not None and self.should_fallback_to_autoregressive(acceptance_rate):
            base = self.evaluate(prompt, domain)
            return replace(
                base,
                draft_model=None,
                estimated_speedup_ratio=1.0,
                spec_engine="standard",
                fallback_applied=True,
                reasoning=f"投机草稿命中率 {acceptance_rate:.0%} 低于 40% 熔断阈值，已降级为自回归标准解码（原路由 {base.target_tier}）。",
            )
        base = self.evaluate(prompt, domain)
        engine = self.select_draft_engine(prompt, domain)
        if engine == "dflash2" and base.draft_model is not None:
            return replace(
                base,
                draft_model=DFLASH2_DRAFT_MODEL,
                estimated_speedup_ratio=max(base.estimated_speedup_ratio, DFLASH2_EXPECTED_SPEEDUP),
                spec_engine="dflash2",
                reasoning=base.reasoning + "BatchA：草稿侧切换为 DFlash2 块扩散草稿（qwen3.8-27b-dflash2），目标加速比 2.4x。",
            )
        return replace(base, spec_engine=engine)
