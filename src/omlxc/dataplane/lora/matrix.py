"""omlxc — Four-domain personal-tone LoRA adapter matrix (BET-Y2Q2-T3-02).

Extends the T10-118 three-domain manager (gov/tech/email) to a four-domain
matrix — 政务公文 (gov) / 技术架构 (tech) / 对外协作 (collab) / 随想随笔 (essay)
— with inference-time domain routing, seamless registry-only switching, and a
ROUGE-L tone gate with circuit-breaker fallback to the neutral template.

Honesty contract (inherited from T10-105/T10-118): domains without enough
samples report ``pending_samples``; a missing training backend reports
``needs_mlx``. Weight packages are never fabricated. This module owns
routing + switching + evaluation only; actual distillation stays with
``LoraAdapterManager.distill_all``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omlxc.dataplane.lora.eval_tone import guard_protected_terms, revision_rate, rouge_l

DOMAINS = ("gov", "tech", "collab", "essay")
ADAPTER_NAMES = {
    "gov": "lora-gov-v2",
    "tech": "lora-tech-v2",
    "collab": "lora-collab-v1",
    "essay": "lora-essay-v1",
}

# Domain keyword map. Specificity-first ordering is a correctness requirement
# (T10-118 retro: broad gov words eat narrow-domain signals).
_DOMAIN_KEYWORDS: dict[str, re.Pattern[str]] = {
    "collab": re.compile(r"对外协作|合作方|伙伴|联名|洽谈|签约|备忘录|合作协议|协同"),
    "essay": re.compile(r"随笔|随想|手记|感悟|杂感|札记|心境|漫谈"),
    "tech": re.compile(r"架构|ADR|技术方案|评审|接口|微服务|部署|代码|系统设计|数据模型"),
    "gov": re.compile(r"公文|政务|批复|请示|公函|函件|通知|政策|卫生(?:健康)?局|督办|审批"),
}
_DOMAIN_ORDER = ("collab", "essay", "tech", "gov")
_DEFAULT_DOMAIN = "gov"
DISTILL_MIN_SAMPLES = 8
TONE_GATE = 0.75  # done_when: ROUGE-L >= 0.75


def route_domain(task_text: str) -> str:
    """Route a task to one of the four matrix domains (gov is the default)."""
    text = task_text or ""
    for domain in _DOMAIN_ORDER:
        if _DOMAIN_KEYWORDS[domain].search(text):
            return domain
    return _DEFAULT_DOMAIN


def partition_buffer_by_domain(buffer_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Split replay samples into the four matrix domains."""
    path = Path(buffer_path)
    shards: dict[str, list[dict[str, Any]]] = {d: [] for d in DOMAINS}
    if not path.exists():
        return shards
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = f"{sample.get('instruction', '')}\n{sample.get('output', '')}"
            shards[route_domain(text)].append(sample)
    return shards


@dataclass(slots=True)
class MatrixDistillStatus:
    """Honest per-domain training readiness (no weights are fabricated here)."""

    domain: str
    adapter_name: str
    sample_count: int
    status: str  # ready | pending_samples | needs_mlx
    detail: str = ""


class LoraMatrix:
    """Registry + seamless switch controller for the four-domain matrix.

    Uses its own registry file (``lora-matrix.json``) and never touches the
    T10-118 ``lora-registry.json``. Switching is registry-only: no reload,
    ``reloaded`` is always False by design.
    """

    def __init__(self, workspace_root: Path | None = None, buffer_path: str | Path | None = None) -> None:
        self.ws = workspace_root or _detect_ws()
        self.buffer_path = Path(buffer_path) if buffer_path else self.ws / ".omo/state/lora-replay-buffer.jsonl"
        self.active: dict[str, str] = {}
        self._load_registry()

    # -- registry ---------------------------------------------------------

    def registry_path(self) -> Path:
        return self.ws / ".omo/state/lora-matrix.json"

    def _load_registry(self) -> None:
        reg = self.registry_path()
        if reg.exists():
            try:
                data = json.loads(reg.read_text(encoding="utf-8"))
                self.active = {k: v for k, v in data.get("active", {}).items() if k in DOMAINS}
            except Exception:
                self.active = {}

    def _save_registry(self) -> None:
        reg = self.registry_path()
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(
            json.dumps({"active": self.active, "updated_at": time.time()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # -- routing + seamless switch -----------------------------------------

    def adapter_for_task(self, task_text: str) -> dict[str, Any]:
        """Inference-time routing: domain + currently active adapter (if any)."""
        domain = route_domain(task_text)
        return {"domain": domain, "adapter": self.active.get(domain), "expected": ADAPTER_NAMES[domain]}

    def adapter_for_domain(self, domain: str) -> str | None:
        return self.active.get(domain)

    def activate(self, domain: str) -> dict[str, Any]:
        if domain not in DOMAINS:
            return {"ok": False, "error": f"unknown domain: {domain}"}
        self.active[domain] = ADAPTER_NAMES[domain]
        self._save_registry()
        return {"ok": True, "domain": domain, "adapter": ADAPTER_NAMES[domain]}

    def deactivate(self, domain: str) -> dict[str, Any]:
        if self.active.pop(domain, None) is None:
            return {"ok": False, "error": f"{domain} has no active adapter"}
        self._save_registry()
        return {"ok": True, "domain": domain}

    def switch(self, domain: str) -> dict[str, Any]:
        """Seamless switch: pointer swap only, zero reload.

        The inference side observes the new pointer on its next
        ``adapter_for_domain`` read; ``reloaded`` is explicitly False.
        """
        if domain not in DOMAINS:
            return {"ok": False, "error": f"unknown domain: {domain}"}
        prev = self.current()
        self.active[domain] = ADAPTER_NAMES[domain]
        self._save_registry()
        return {"ok": True, "prev": prev, "current": domain, "adapter": ADAPTER_NAMES[domain], "reloaded": False}

    def current(self) -> str | None:
        if not self.active:
            return None
        # Most recently activated domain wins; dict preserves insertion order.
        return next(reversed(self.active))

    def list_matrix(self) -> list[dict[str, Any]]:
        return [
            {
                "domain": d,
                "adapter": ADAPTER_NAMES[d],
                "active": self.active.get(d) == ADAPTER_NAMES[d],
                "samples": len(partition_buffer_by_domain(self.buffer_path).get(d, [])),
            }
            for d in DOMAINS
        ]

    # -- honest training readiness ------------------------------------------

    def distill_status(self, domain: str) -> MatrixDistillStatus:
        """Report readiness without fabricating weights."""
        name = ADAPTER_NAMES.get(domain, f"lora-{domain}-v1")
        shards = partition_buffer_by_domain(self.buffer_path)
        count = len(shards.get(domain, []))
        if domain not in DOMAINS:
            return MatrixDistillStatus(domain, name, count, "pending_samples", f"unknown domain: {domain}")
        if count < DISTILL_MIN_SAMPLES:
            return MatrixDistillStatus(
                domain, name, count, "pending_samples", f"{count}/{DISTILL_MIN_SAMPLES} samples in shard"
            )
        try:
            import mlx_lm  # type: ignore
        except ImportError:
            return MatrixDistillStatus(domain, name, count, "needs_mlx", "MLX backend not installed on this node")
        return MatrixDistillStatus(domain, name, count, "ready", "samples sufficient, backend present")

    # -- tone gate + circuit breaker ------------------------------------------

    def regulate(self, candidate: str, reference: str, threshold: float = TONE_GATE) -> dict[str, Any]:
        """ROUGE-L tone gate; severe distortion trips the neutral fallback."""
        score = rouge_l(candidate, reference)
        forgotten = guard_protected_terms(candidate, reference)
        distorted = score < threshold or bool(forgotten)
        return {
            "rouge_l": round(score, 4),
            "threshold": threshold,
            "pass": not distorted,
            "forgotten_terms": forgotten,
            "fallback_neutral": distorted,
            "revision_rate": round(revision_rate(reference, candidate), 4),
        }


def _detect_ws() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "project-registry.yaml").is_file():
            return parent
    return Path.home() / "Workspace"
