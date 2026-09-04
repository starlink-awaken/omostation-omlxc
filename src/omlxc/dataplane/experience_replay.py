"""
omlxc V5.0 -- Experience Replay Buffer for LoRA Signature Alignment (ADR-0437).

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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


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
            k = random.randint(0, self._seen_count - 1)  # noqa: S311
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
        buf = self._get_or_create_buffer(domain)
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
        buf = self._get_or_create_buffer(domain)
        n_replay = max(1, int(target_batch_size * self.replay_ratio))
        n_replay = min(n_replay, len(buf))

        replay_samples = buf.sample(n_replay) if n_replay > 0 else []
        actual_fresh = fresh_samples[:target_batch_size - len(replay_samples)]

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
                for s in buf._samples:
                    f.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")
                    count += 1
        return count

    def stats(self) -> dict:
        return {
            domain: {"size": len(buf), "capacity": buf.max_size}
            for domain, buf in self._buffers.items()
        }

    def _get_or_create_buffer(self, domain: str) -> DomainReplayBuffer:
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
                    sample = ReplaySample(**data)
                    self._get_or_create_buffer(sample.domain).add(sample)
                    count += 1
        except Exception:
            pass
        return count


def _detect_ws() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "project-registry.yaml").is_file():
            return parent
    return Path.home() / "Workspace"
