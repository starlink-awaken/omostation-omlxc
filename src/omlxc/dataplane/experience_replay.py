"""
omlxc V5.0 -- Experience Replay Buffer for LoRA Signature Alignment (ADR-0439).

Prevents catastrophic forgetting during online LoRA distillation by maintaining
a bounded reservoir of historical (instruction, reference) pairs and replaying
them alongside new signature diff samples at a configurable mixing ratio.

Design:
- Ring buffer with reservoir sampling (size=2048 by default).
- Replay mix ratio: 30% replay / 70% fresh (configurable).
- Domain-keyed sharding: each domain tag maintains its own sub-buffer.
- Serialization: JSON Lines to <workspace>/.omo/state/lora-replay-buffer.jsonl
"""

from __future__ import annotations

import json
import random
import subprocess
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ReplaySample:
    """A single (instruction, output) training pair in the replay buffer."""

    sample_id: str
    domain: str
    instruction: str
    output: str
    captured_at: float = field(default_factory=time.time)
    replay_count: int = 0
    importance_weight: float = 1.0
    task_id: str = ""


@dataclass
class ReplayBatch:
    """A mixed batch of fresh + replay samples ready for fine-tuning."""

    batch_id: str
    domain: str
    fresh_samples: list[ReplaySample]
    replay_samples: list[ReplaySample]
    fresh_ratio: float
    replay_ratio: float
    total_samples: int

    @property
    def all_samples(self) -> list[ReplaySample]:
        return self.fresh_samples + self.replay_samples


class DomainReplayBuffer:
    """Bounded reservoir buffer for a single domain tag."""

    def __init__(self, domain: str, max_size: int = 512) -> None:
        self.domain = domain
        self.max_size = max_size
        self._samples: list[ReplaySample] = []
        self._seen_count: int = 0

    def add(self, sample: ReplaySample) -> None:
        """Reservoir sampling insertion (Algorithm R)."""
        self._seen_count += 1
        if len(self._samples) < self.max_size:
            self._samples.append(sample)
        else:
            # Reservoir: replace random slot with decreasing probability
            k = random.randint(0, self._seen_count - 1)  # noqa: S311 — 水塘抽样, 统计用途非加密
            if k < self.max_size:
                self._samples[k] = sample

    def sample(self, n: int) -> list[ReplaySample]:
        """Sample n items uniformly from the buffer."""
        if not self._samples:
            return []
        n = min(n, len(self._samples))
        chosen = random.sample(self._samples, n)
        for s in chosen:
            s.replay_count += 1
        return chosen

    @property
    def samples(self) -> tuple[ReplaySample, ...]:
        """Read-only view for callers that need to iterate the reservoir."""
        return tuple(self._samples)

    def __len__(self) -> int:
        return len(self._samples)


class ExperienceReplayManager:
    """
    Manages multi-domain experience replay buffers to prevent catastrophic
    forgetting during online LoRA distillation on Mac mini M4.
    """

    def __init__(
        self,
        workspace_root: Path | None = None,
        buffer_size_per_domain: int = 512,
        replay_ratio: float = 0.30,
        persist_path_rel: str = ".omo/state/lora-replay-buffer.jsonl",
    ) -> None:
        self.ws = workspace_root or _detect_ws()
        self.buffer_size_per_domain = buffer_size_per_domain
        self.replay_ratio = replay_ratio
        self.persist_path = self.ws / persist_path_rel
        self._buffers: dict[str, DomainReplayBuffer] = {}

        # Try to restore from disk
        self._restore()

    def add_sample(
        self,
        instruction: str,
        output: str,
        domain: str = "signature-style",
    ) -> ReplaySample:
        """Add a new (instruction, output) pair from a user signature diff."""
        buf = self.get_or_create_buffer(domain)
        sample = ReplaySample(
            sample_id=f"{domain}-{int(time.time() * 1000)}",
            domain=domain,
            instruction=instruction,
            output=output,
        )
        buf.add(sample)
        return sample

    def build_training_batch(
        self,
        fresh_samples: list[ReplaySample],
        domain: str = "signature-style",
        target_batch_size: int = 64,
    ) -> ReplayBatch:
        """
        Mix fresh samples with replay samples at the configured ratio.
        Ensures model does not overfit to recent signal and retains past alignment.
        """
        buf = self.get_or_create_buffer(domain)
        n_replay = max(1, int(target_batch_size * self.replay_ratio))
        n_replay = min(n_replay, len(buf))

        replay_samples = buf.sample(n_replay) if n_replay > 0 else []
        actual_fresh = fresh_samples[: target_batch_size - len(replay_samples)]

        actual_fresh_ratio = len(actual_fresh) / max(1, len(actual_fresh) + len(replay_samples))
        actual_replay_ratio = 1.0 - actual_fresh_ratio

        return ReplayBatch(
            batch_id=f"batch-{int(time.time())}",
            domain=domain,
            fresh_samples=actual_fresh,
            replay_samples=replay_samples,
            fresh_ratio=round(actual_fresh_ratio, 3),
            replay_ratio=round(actual_replay_ratio, 3),
            total_samples=len(actual_fresh) + len(replay_samples),
        )

    def persist(self) -> int:
        """Serialize all buffers to JSONL for durability across daemon restarts."""
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with self.persist_path.open("w", encoding="utf-8") as f:
            for buf in self._buffers.values():
                for s in buf.samples:
                    f.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")
                    count += 1
        return count

    def stats(self) -> dict[str, dict[str, int]]:
        return {domain: {"size": len(buf), "capacity": buf.max_size} for domain, buf in self._buffers.items()}

    def get_or_create_buffer(self, domain: str) -> DomainReplayBuffer:
        if domain not in self._buffers:
            self._buffers[domain] = DomainReplayBuffer(domain, self.buffer_size_per_domain)
        return self._buffers[domain]

    def _restore(self) -> int:
        if not self.persist_path.exists():
            return 0
        count = 0
        try:
            with self.persist_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    valid_keys = getattr(ReplaySample, "__dataclass_fields__", {})
                    filtered = {k: v for k, v in data.items() if k in valid_keys} if valid_keys else data
                    sample = ReplaySample(**filtered)
                    self.get_or_create_buffer(sample.domain).add(sample)
                    count += 1
        except Exception:
            pass
        return count


DEFAULT_ADAPTER_NAME = "adapter-xiamingxing-v1"
ADAPTER_DIR_REL = ".omo/state/lora-adapters"
DISTILL_MIN_SAMPLES = 8


@dataclass
class DistillJob:
    """A LoRA distillation dispatch record (BET-Y1Q3-T10-105).

    status semantics (honest states, never simulated):
    - dispatched: local MLX training ran to completion, adapter produced.
    - routed: no local MLX; a mesh target node was decided for the job.
    - needs_mlx: neither local MLX nor a mesh peer is available.
    - insufficient_samples: domain buffer below DISTILL_MIN_SAMPLES.
    """

    job_id: str
    domain: str
    epochs: int
    sample_count: int
    status: str
    target_node: str = ""
    target_endpoint: str = ""
    adapter_path: str = ""
    detail: str = ""


def adapter_dir(workspace_root: Path | None = None, name: str = DEFAULT_ADAPTER_NAME) -> Path:
    ws = workspace_root or _detect_ws()
    return ws / ADAPTER_DIR_REL / name


def adapter_status(name: str = DEFAULT_ADAPTER_NAME, workspace_root: Path | None = None) -> dict[str, Any]:
    """Inspect a trained adapter on disk without loading model weights."""
    path = adapter_dir(workspace_root, name)
    if not path.is_dir():
        return {"name": name, "exists": False, "path": str(path)}
    files = sorted(p.name for p in path.iterdir() if p.is_file())
    return {
        "name": name,
        "exists": True,
        "path": str(path),
        "files": files,
        "size_bytes": sum(p.stat().st_size for p in path.rglob("*") if p.is_file()),
    }


def _mlx_lm_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("mlx_lm") is not None
    except Exception:
        return False


def _tokenize_text(text: str) -> list[str]:
    import re

    tokens: list[str] = []
    for piece in re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9_\.\-]+|[^\s\w]", text):
        if piece.strip():
            tokens.append(piece)
    return tokens


def _rouge_l_f1(hypothesis: str, reference: str) -> float:
    """Lightweight ROUGE-L (LCS-based F1) supporting multilingual and Chinese text."""
    hyp_tokens = _tokenize_text(hypothesis)
    ref_tokens = _tokenize_text(reference)
    if not hyp_tokens or not ref_tokens:
        return 0.0
    lcs = [[0] * (len(ref_tokens) + 1) for _ in range(len(hyp_tokens) + 1)]
    for i, h in enumerate(hyp_tokens, 1):
        for j, r in enumerate(ref_tokens, 1):
            lcs[i][j] = lcs[i - 1][j - 1] + 1 if h == r else max(lcs[i - 1][j], lcs[i][j - 1])
    lcs_len = lcs[-1][-1]
    precision = lcs_len / len(hyp_tokens)
    recall = lcs_len / len(ref_tokens)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def evaluate_alignment(
    reference: str,
    base_output: str,
    adapter_output: str,
) -> dict[str, Any]:
    """Score adapter vs base outputs against a user-signed reference (ROUGE-L)."""
    base_score = _rouge_l_f1(base_output, reference)
    adapter_score = _rouge_l_f1(adapter_output, reference)
    improvement = 0.0 if base_score == 0 else (adapter_score - base_score) / base_score
    return {
        "base_score": round(base_score, 4),
        "adapter_score": round(adapter_score, 4),
        "relative_improvement": round(improvement, 4),
        "target_improvement": 0.25,
        "meets_target": improvement >= 0.25,
    }


class _DistillBufferLike(Protocol):
    """What dispatch_distill needs from a domain buffer.

    Structural, not nominal: LoraAdapterManager's per-shard _ShardBuffer
    satisfies this without inheriting from DomainReplayBuffer.
    """

    def __len__(self) -> int: ...
    @property
    def samples(self) -> Sequence[ReplaySample]: ...


class _DistillManagerLike(Protocol):
    """What dispatch_distill needs from a manager. See _DistillBufferLike."""

    ws: Path

    def get_or_create_buffer(self, domain: str) -> _DistillBufferLike: ...
    def build_training_batch(
        self,
        fresh_samples: list[ReplaySample],
        domain: str = ...,
        target_batch_size: int = ...,
    ) -> ReplayBatch: ...


class RouteDecisionLike(Protocol):
    target_node_id: str
    target_endpoint: str
    decision_reason: str


class RouterLike(Protocol):
    def route_job(self, *, job_id: str, model_id: str, estimated_vram_gb: float) -> RouteDecisionLike: ...


def dispatch_distill(
    manager: _DistillManagerLike,
    domain: str = "signature-style",
    epochs: int = 3,
    model: str = "qwen3.8-27b",
    adapter_name: str = DEFAULT_ADAPTER_NAME,
    router: RouterLike | None = None,
) -> DistillJob:
    """Dispatch a real LoRA distillation job for a domain buffer.

    Order of preference: local MLX training > mesh roaming target > honest
    failure. Never fabricates a completed job.
    """
    buf = manager.get_or_create_buffer(domain)
    job_id = f"ft-job-{int(time.time())}"
    if len(buf) < DISTILL_MIN_SAMPLES:
        return DistillJob(
            job_id=job_id,
            domain=domain,
            epochs=epochs,
            sample_count=len(buf),
            status="insufficient_samples",
            detail=f"domain '{domain}' has {len(buf)} samples, need >= {DISTILL_MIN_SAMPLES}",
        )

    out_dir = adapter_dir(manager.ws, adapter_name)
    if _mlx_lm_available():
        batch = manager.build_training_batch(
            fresh_samples=list(buf.samples)[-8:],
            domain=domain,
        )
        data_path = out_dir.parent / f"{domain}-train-{int(time.time())}.jsonl"
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        with data_path.open("w", encoding="utf-8") as f:
            for s in batch.all_samples:
                f.write(
                    json.dumps(
                        {
                            "messages": [
                                {"role": "user", "content": s.instruction},
                                {"role": "assistant", "content": s.output},
                            ]
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        cmd = [
            "python3",
            "-m",
            "mlx_lm",
            "lora",
            "--train",
            "--model",
            model,
            "--data",
            str(data_path),
            "--adapter-path",
            str(out_dir),
            "--iters",
            str(max(epochs * 100, 300)),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        data_path.unlink(missing_ok=True)
        if proc.returncode == 0:
            return DistillJob(
                job_id=job_id,
                domain=domain,
                epochs=epochs,
                sample_count=len(batch.all_samples),
                status="dispatched",
                target_node="local",
                adapter_path=str(out_dir),
                detail="mlx_lm.lora training completed",
            )
        return DistillJob(
            job_id=job_id,
            domain=domain,
            epochs=epochs,
            sample_count=len(buf),
            status="needs_mlx",
            detail=f"mlx_lm.lora exited {proc.returncode}: {proc.stderr[-200:]}",
        )

    # No local MLX: decide a mesh roaming target (e.g. Mac mini M4) for the job.
    if router is not None:
        try:
            decision = router.route_job(
                job_id=job_id,
                model_id=model,
                estimated_vram_gb=8.0,
            )
            return DistillJob(
                job_id=job_id,
                domain=domain,
                epochs=epochs,
                sample_count=len(buf),
                status="routed",
                target_node=decision.target_node_id,
                target_endpoint=decision.target_endpoint,
                adapter_path=str(out_dir),
                detail=decision.decision_reason,
            )
        except Exception as exc:
            return DistillJob(
                job_id=job_id,
                domain=domain,
                epochs=epochs,
                sample_count=len(buf),
                status="needs_mlx",
                detail=f"mesh routing failed: {exc}",
            )
    return DistillJob(
        job_id=job_id,
        domain=domain,
        epochs=epochs,
        sample_count=len(buf),
        status="needs_mlx",
        detail="mlx_lm not installed locally and no mesh discovery engine provided",
    )


def _detect_ws() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "project-registry.yaml").is_file():
            return parent
    return Path.home() / "Workspace"
