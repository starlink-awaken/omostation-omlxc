"""omlxc V5.0 — Domain LoRA Adapter Manager (BET-Y1Q3-T10-118).

Partitions the signature replay buffer into three vertical domains
(Gov / Tech / Email), distills per-domain LoRA adapters via the T10-105
honest-dispatch pipeline, and maintains a hot-swap registry that Spine
Draft consults for second-level domain routing.

Honesty contract (inherited from T10-105): a domain with insufficient
samples reports ``pending_samples``; a missing training backend reports
``needs_mlx``. Weight packages are never fabricated.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from omlxc.dataplane.experience_replay import (
    DEFAULT_ADAPTER_NAME,
    adapter_dir,
    adapter_status,
    dispatch_distill,
)
from omlxc.dataplane.hard_negative_miner import parse_sample

DOMAINS = ("gov", "tech", "email")
ADAPTER_NAMES = {"gov": "lora-gov-v1", "tech": "lora-tech-v1", "email": "lora-email-v1"}

# 域关键词映射（结构化分片依据，非语义判断）
_DOMAIN_KEYWORDS: dict[str, re.Pattern[str]] = {
    # 特异性优先: email 的"函复/回信"先于 gov 的宽"函"匹配
    "email": re.compile(r"邮件|来信|函复|回信|收件|抄送|答复"),
    "tech": re.compile(r"架构|ADR|技术方案|评审|接口|微服务|部署|代码|系统设计|数据模型"),
    "gov": re.compile(r"公文|政务|批复|请示|公函|函件|通知|政策|卫生(?:健康)?局|督办|审批"),
}
_DEFAULT_DOMAIN = "gov"
DISTILL_MIN_SAMPLES = 8


@dataclass(slots=True)
class DomainDistillRecord:
    """Per-domain distillation outcome (honest states only)."""

    domain: str
    adapter_name: str
    sample_count: int
    status: str  # dispatched | routed | pending_samples | needs_mlx
    detail: str = ""
    adapter_path: str = ""
    target_node: str = ""
    evaluated_improvement: float | None = None


def partition_buffer_by_domain(
    buffer_path: str | Path,
) -> dict[str, list[dict[str, Any]]]:
    """Split replay samples into vertical domains by keyword mapping."""
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
            for domain, pattern in _DOMAIN_KEYWORDS.items():
                if pattern.search(text):
                    shards[domain].append(sample)
                    break
            else:
                shards[_DEFAULT_DOMAIN].append(sample)
    return shards


class LoraAdapterManager:
    """Registry + hot-swap controller for per-domain LoRA adapters."""

    def __init__(self, workspace_root: Path | None = None, buffer_path: str | Path | None = None) -> None:
        self.ws = workspace_root or _detect_ws()
        self.buffer_path = Path(buffer_path) if buffer_path else self.ws / ".omo/state/lora-replay-buffer.jsonl"
        self.active: dict[str, str] = {}  # domain -> adapter_name
        self._load_registry()

    # -- registry ---------------------------------------------------------

    def registry_path(self) -> Path:
        return self.ws / ".omo/state/lora-registry.json"

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

    # -- hot swap ----------------------------------------------------------

    def activate(self, domain: str) -> dict[str, Any]:
        """Second-level hot swap: mark a domain's adapter as active (no reload)."""
        if domain not in DOMAINS:
            return {"ok": False, "error": f"unknown domain: {domain}"}
        name = ADAPTER_NAMES[domain]
        st = adapter_status(name, self.ws)
        if not st.get("exists"):
            return {"ok": False, "error": f"adapter not trained: {name}", "status": st}
        self.active[domain] = name
        self._save_registry()
        return {"ok": True, "domain": domain, "adapter": name, "path": st["path"]}

    def deactivate(self, domain: str) -> dict[str, Any]:
        if self.active.pop(domain, None) is None:
            return {"ok": False, "error": f"{domain} has no active adapter"}
        self._save_registry()
        return {"ok": True, "domain": domain}

    def adapter_for_domain(self, domain: str) -> str | None:
        """Draft 域路由查询: 返回该域当前激活的适配器名（未激活返回 None）。"""
        return self.active.get(domain)

    # -- distill & evaluate --------------------------------------------------

    def distill_all(self, epochs: int = 3, router: object | None = None) -> list[DomainDistillRecord]:
        """Distill every domain shard via the T10-105 honest dispatch."""
        shards = partition_buffer_by_domain(self.buffer_path)
        records: list[DomainDistillRecord] = []
        for domain in DOMAINS:
            samples = shards.get(domain, [])
            if len(samples) < DISTILL_MIN_SAMPLES:
                records.append(
                    DomainDistillRecord(
                        domain=domain, adapter_name=ADAPTER_NAMES[domain],
                        sample_count=len(samples), status="pending_samples",
                        detail=f"{len(samples)}/{DISTILL_MIN_SAMPLES} samples in shard",
                    )
                )
                continue
            # 域分片训练: 写临时 buffer 供 dispatch 复用
            shard_path = self.ws / ".omo/state" / f"lora-buffer-{domain}.jsonl"
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            with shard_path.open("w", encoding="utf-8") as f:
                for s in samples:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")
            shard_mgr = _ShardManager(self.ws, shard_path)
            job = dispatch_distill(shard_mgr, domain=domain, epochs=epochs,
                                   adapter_name=ADAPTER_NAMES[domain], router=router)
            records.append(
                DomainDistillRecord(
                    domain=domain, adapter_name=ADAPTER_NAMES[domain],
                    sample_count=job.sample_count, status=job.status,
                    detail=job.detail, adapter_path=job.adapter_path,
                    target_node=job.target_node,
                )
            )
        return records

    def list_adapters(self, include_eval: bool = False) -> list[dict[str, Any]]:
        rows = []
        for domain in DOMAINS:
            name = ADAPTER_NAMES[domain]
            st = adapter_status(name, self.ws)
            row = {
                "domain": domain,
                "adapter": name,
                "exists": st.get("exists", False),
                "active": self.active.get(domain) == name,
                "size_bytes": st.get("size_bytes", 0),
            }
            if include_eval:
                row["evaluated_improvement"] = self.evaluated_improvement(domain)
            rows.append(row)
        return rows

    def evaluated_improvement(self, domain: str) -> float | None:
        eval_file = self.ws / ".omo/state" / f"lora-eval-{domain}.json"
        if not eval_file.exists():
            return None
        try:
            return float(json.loads(eval_file.read_text(encoding="utf-8")).get("relative_improvement"))
        except Exception:
            return None


class _ShardManager:
    """Minimal manager shape满足 dispatch_distill 的 duck-typing（域分片视图）。"""

    def __init__(self, ws: Path, shard_path: Path) -> None:
        self.ws = ws
        self.shard_path = shard_path
        self._samples: list[dict[str, Any]] = []
        with shard_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self._samples.append(json.loads(line))

    def _get_or_create_buffer(self, domain: str):
        return _ShardBuffer(self._samples)

    def build_training_batch(self, fresh_samples, domain: str = "", target_batch_size: int = 64):
        from omlxc.dataplane.experience_replay import ReplayBatch

        all_samples = fresh_samples or []
        return ReplayBatch(
            batch_id=f"shard-{int(time.time())}", domain=domain,
            fresh_samples=all_samples, replay_samples=[],
            fresh_ratio=1.0, replay_ratio=0.0, total_samples=len(all_samples),
        )


class _ShardBuffer:
    def __init__(self, samples: list[dict[str, Any]]) -> None:
        self._samples = samples

    def __len__(self) -> int:
        return len(self._samples)


def _detect_ws() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "project-registry.yaml").is_file():
            return parent
    return Path.home() / "Workspace"
