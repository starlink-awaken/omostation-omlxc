"""omlxc.dataplane.persona_radar — Multi-dimensional writing-style radar engine.

BET-Y2Q2-T3-01: 个人文风一致性多维雷达评估与语气自适应调节引擎.

Provides:
  - RadarDimension: a single axis of the writing-style radar (0-100).
  - RadarProfile: aggregate radar for an author across N dimensions.
  - ToneProfile: tunable target profile (formality / warmth / authority / ...).
  - ToneDirection: shift vectors for "solemn" / "sharp" / "gentle".
  - compute_radar(): measure a text sample against a target ToneProfile.
  - compute_alignment_score(): overall 0-100 alignment.
  - tone_shift(): adjust a ToneProfile in a given direction.
  - auto_rewrite_suggestion(): flag text below threshold with suggestions.

All heuristics are lightweight text analytics (no ML model required).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RadarDimension:
    """One axis of the writing-style radar."""

    name: str  # machine key
    label: str  # human label (Chinese)
    current: float  # 0-100 measured from text
    target: float  # 0-100 desired (from ToneProfile)
    gap: float  # target - current (positive = need more)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "current": round(self.current, 1),
            "target": round(self.target, 1),
            "gap": round(self.gap, 1),
        }


@dataclass
class RadarProfile:
    """Full radar profile for an author against a target tone."""

    author: str
    dimensions: list[RadarDimension]
    alignment_score: float  # 0-100
    threshold: float = 85.0
    below_threshold: bool = False
    suggestions: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, Any]:
        return {
            "author": self.author,
            "alignment_score": round(self.alignment_score, 1),
            "threshold": self.threshold,
            "below_threshold": self.below_threshold,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "suggestions": self.suggestions,
        }


@dataclass
class ToneProfile:
    """Target writing style profile.  All fields 0.0-1.0."""

    formality: float = 0.5
    warmth: float = 0.4
    authority: float = 0.6
    brevity: float = 0.5
    concreteness: float = 0.6
    rhythm: float = 0.5
    originality: float = 0.5

    def to_targets(self) -> dict[str, float]:
        """Convert 0-1 profile values to 0-100 radar targets."""
        return {
            "formality": self.formality * 100,
            "warmth": self.warmth * 100,
            "authority": self.authority * 100,
            "brevity": self.brevity * 100,
            "concreteness": self.concreteness * 100,
            "rhythm": self.rhythm * 100,
            "originality": self.originality * 100,
        }

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass
class RadarEvalResult:
    """Result of running persona_radar_eval."""

    profile: RadarProfile
    raw_metrics: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "raw_metrics": {k: round(v, 2) for k, v in self.raw_metrics.items()},
        }


# ---------------------------------------------------------------------------
# Tone directions
# ---------------------------------------------------------------------------


class ToneDirection(Enum):
    """Three preset tone shift directions."""

    SOLEMN = "solemn"  # 更庄严稳重
    SHARP = "sharp"  # 更犀利专业
    GENTLE = "gentle"  # 更柔和温和


# Shift vectors: (formality, warmth, authority, brevity, concreteness, rhythm, originality)
_TONE_SHIFTS: dict[ToneDirection, tuple[float, float, float, float, float, float, float]] = {
    ToneDirection.SOLEMN: (0.15, 0.0, 0.15, 0.05, 0.05, 0.05, -0.05),
    ToneDirection.SHARP: (0.10, -0.10, 0.20, 0.10, 0.10, 0.0, 0.05),
    ToneDirection.GENTLE: (-0.05, 0.20, -0.15, 0.0, -0.05, 0.10, 0.0),
}

_LABELS: dict[str, str] = {
    "formality": "正式度",
    "warmth": "亲和力",
    "authority": "权威感",
    "brevity": "简洁度",
    "concreteness": "具体性",
    "rhythm": "节奏感",
    "originality": "原创性",
}


def tone_shift(profile: ToneProfile, direction: ToneDirection, strength: float = 1.0) -> ToneProfile:
    """Return a new ToneProfile shifted in *direction* by *strength* (0-1)."""
    delta = _TONE_SHIFTS[direction]

    def clamp(v: float) -> float:
        return max(0.0, min(1.0, v))

    return ToneProfile(
        formality=clamp(profile.formality + delta[0] * strength),
        warmth=clamp(profile.warmth + delta[1] * strength),
        authority=clamp(profile.authority + delta[2] * strength),
        brevity=clamp(profile.brevity + delta[3] * strength),
        concreteness=clamp(profile.concreteness + delta[4] * strength),
        rhythm=clamp(profile.rhythm + delta[5] * strength),
        originality=clamp(profile.originality + delta[6] * strength),
    )


# ---------------------------------------------------------------------------
# Text analytics heuristics
# ---------------------------------------------------------------------------

_FORMAL_TOKENS = {
    "the",
    "shall",
    "therefore",
    "consequently",
    "furthermore",
    "henceforth",
    "hereby",
    "forthwith",
    "whereas",
    "herein",
    "aforementioned",
    "notwithstanding",
    "regarding",
    "pursuant",
    "thereupon",
    "hence",
    "whence",
    "hitherto",
}
_INFORMAL_TOKENS = {
    "gonna",
    "wanna",
    "gotta",
    "kinda",
    "sorta",
    "y'all",
    "ain't",
    "cuz",
    "u",
    "thx",
    "btw",
    "imo",
    "idk",
    "lol",
    "omg",
    "pls",
    "ngl",
    "tbh",
    "fr",
}

_WARM_WORDS = {
    "love",
    "heart",
    "beautiful",
    "wonderful",
    "amazing",
    "fantastic",
    "precious",
    "warm",
    "kind",
    "gentle",
    "tender",
    "beloved",
    "dear",
    "cherish",
    "treasure",
    "hug",
    "smile",
    "laugh",
    "joy",
    "happiness",
    "comfort",
    "embrace",
}
_COLD_WORDS = {
    "cold",
    "clinical",
    "sterile",
    "impersonal",
    "mechanical",
    "procedural",
    "automated",
    "standardized",
    "protocol",
    "regulation",
    "statute",
    "code",
}

_ASSERTIVE_STARTERS = re.compile(
    r"^(I|We|You|Let's|Our|This|The)\s",
    re.IGNORECASE,
)
_HEDGES = {
    "maybe",
    "perhaps",
    "possibly",
    "might",
    "could",
    "may",
    "somewhat",
    "slightly",
    "arguably",
    "tentatively",
    "in some way",
    "sort of",
    "kind of",
}

_CONCRETE_NOUNS = {
    "table",
    "chair",
    "book",
    "phone",
    "car",
    "tree",
    "river",
    "mountain",
    "house",
    "room",
    "street",
    "city",
    "country",
    "money",
    "time",
    "day",
    "year",
    "month",
    "week",
    "person",
    "child",
    "man",
    "woman",
    "friend",
    "food",
    "water",
    "fire",
    "light",
    "sound",
    "color",
    "name",
    "word",
    "sentence",
    "paragraph",
    "chapter",
    "page",
    "idea",
    "thought",
    "feeling",
    "action",
    "result",
    "effect",
    "cause",
    "reason",
    "question",
    "answer",
    "solution",
    "problem",
    "mistake",
    "error",
    "success",
    "failure",
    "goal",
}
_ABSTRACT_NOUNS = {
    "truth",
    "justice",
    "freedom",
    "love",
    "hope",
    "faith",
    "soul",
    "spirit",
    "wisdom",
    "knowledge",
    "intelligence",
    "consciousness",
    "reality",
    "existence",
    "meaning",
    "purpose",
    "destiny",
    "eternity",
    "infinity",
    "essence",
    "nature",
}


def _extract_sentences(text: str) -> list[str]:
    """Split text into sentences, keeping only non-trivial ones."""
    parts = re.split(r"[.!?。！？\n]+", text)
    return [s.strip() for s in parts if len(s.strip()) > 10]


def _extract_words(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z]+", text.lower())


def _measure_formality(words: list[str]) -> float:
    """0-1 formality score based on formal vs informal vocabulary ratio."""
    if not words:
        return 0.5
    formal_hits = sum(1 for w in words if w in _FORMAL_TOKENS)
    informal_hits = sum(1 for w in words if w in _INFORMAL_TOKENS)
    ratio = (formal_hits + 1) / (informal_hits + 1)
    return min(1.0, ratio / 3.0)


def _measure_warmth(words: list[str]) -> float:
    """0-1 warmth based on warm vs cold word frequency."""
    if not words:
        return 0.3
    warm_hits = sum(1 for w in words if w in _WARM_WORDS)
    cold_hits = sum(1 for w in words if w in _COLD_WORDS)
    score = (warm_hits + 1) / (cold_hits + 1)
    return min(1.0, score / 5.0)


def _measure_authority(sentences: list[str], words: list[str]) -> float:
    """0-1 authority from assertive sentence starters vs hedges."""
    if not sentences:
        return 0.5
    assertive = sum(1 for s in sentences if _ASSERTIVE_STARTERS.match(s))
    hedge_count = sum(1 for w in words if w in _HEDGES)
    ratio = (assertive + 1) / (hedge_count + 1)
    return min(1.0, ratio / 5.0)


def _measure_brevity(sentences: list[str], words: list[str]) -> float:
    """0-1 brevity: shorter sentences = higher score."""
    if not sentences:
        return 0.5
    avg_len = sum(len(s.split()) for s in sentences) / len(sentences)
    if avg_len <= 12:
        return 1.0
    if avg_len <= 20:
        return 0.7
    if avg_len <= 30:
        return 0.4
    return 0.2


def _measure_concreteness(words: list[str]) -> float:
    """0-1 concreteness: concrete nouns vs abstract nouns."""
    if not words:
        return 0.5
    concrete = sum(1 for w in words if w in _CONCRETE_NOUNS)
    abstract = sum(1 for w in words if w in _ABSTRACT_NOUNS)
    ratio = (concrete + 2) / (abstract + 1)
    return min(1.0, ratio / 4.0)


def _measure_rhythm(sentences: list[str]) -> float:
    """0-1 rhythm: sentence length variation (coefficient of variation)."""
    if len(sentences) < 2:
        return 0.5
    lengths = [len(s.split()) for s in sentences]
    mean_len = sum(lengths) / len(lengths)
    if mean_len == 0:
        return 0.5
    variance = sum((length - mean_len) ** 2 for length in lengths) / len(lengths)
    std = variance**0.5
    cv = std / mean_len
    if 0.2 <= cv <= 0.5:
        return 1.0
    if cv < 0.2:
        return 0.4  # too monotonous
    return 0.6  # too erratic


def _measure_originality(words: list[str]) -> float:
    """0-1 originality: lexical diversity (unique / total ratio)."""
    if len(words) < 10:
        return 0.5
    unique = len(set(words))
    ratio = unique / len(words)
    if ratio >= 0.5:
        return min(1.0, (ratio - 0.4) / 0.3 + 0.7)
    return max(0.0, (ratio - 0.2) / 0.3)


# ---------------------------------------------------------------------------
# Radar computation
# ---------------------------------------------------------------------------


def compute_radar_dimensions(text: str, target: ToneProfile) -> list[RadarDimension]:
    """Measure text against target profile across all radar dimensions."""
    words = _extract_words(text)
    sentences = _extract_sentences(text)

    raw = {
        "formality": _measure_formality(words),
        "warmth": _measure_warmth(words),
        "authority": _measure_authority(sentences, words),
        "brevity": _measure_brevity(sentences, words),
        "concreteness": _measure_concreteness(words),
        "rhythm": _measure_rhythm(sentences),
        "originality": _measure_originality(words),
    }

    targets = target.to_targets()
    dimensions: list[RadarDimension] = []
    for name, label in _LABELS.items():
        current = raw[name] * 100
        tgt = targets[name]
        gap = tgt - current
        dimensions.append(
            RadarDimension(
                name=name,
                label=label,
                current=current,
                target=tgt,
                gap=gap,
            )
        )
    return dimensions


def compute_alignment_score(dimensions: list[RadarDimension]) -> float:
    """Overall alignment score 0-100 (inverse gap average)."""
    if not dimensions:
        return 0.0
    total = 0.0
    for d in dimensions:
        total += max(0.0, 100.0 - abs(d.gap))
    return round(total / len(dimensions), 1)


def compute_radar(
    text: str,
    author: str,
    target: ToneProfile,
    threshold: float = 85.0,
) -> RadarProfile:
    """Full radar evaluation: compute dimensions + alignment + suggestions."""
    dimensions = compute_radar_dimensions(text, target)
    alignment = compute_alignment_score(dimensions)
    below = alignment < threshold

    suggestions: list[str] = []
    if below:
        worst = sorted(dimensions, key=lambda d: d.gap, reverse=True)[:3]
        for d in worst:
            if d.gap > 10:
                suggestions.append(
                    f"{d.label}: 当前 {d.current:.0f}, 目标 {d.target:.0f}, 差距 {d.gap:.0f} — 建议{d.label}提升"
                )
        if not suggestions:
            suggestions.append("整体对齐度偏低，建议调整语气方向")

    return RadarProfile(
        author=author,
        dimensions=dimensions,
        alignment_score=alignment,
        threshold=threshold,
        below_threshold=below,
        suggestions=suggestions,
    )


def auto_rewrite_suggestion(text: str, profile: RadarProfile) -> str | None:
    """If below threshold, produce a partial rewrite suggestion summary."""
    if not profile.below_threshold:
        return None
    worst = sorted(profile.dimensions, key=lambda d: d.gap, reverse=True)
    top = [d for d in worst if d.gap > 5][:3]
    if not top:
        return None
    return (
        f"⚠️ 文风雷达对齐度 {profile.alignment_score:.0f} < 阈值 {profile.threshold:.0f}\n"
        f"主要差距维度: "
        + ", ".join(f"{d.label}({d.gap:+.0f})" for d in top)
        + "\n建议: 根据差距维度调整措辞或参考历史署名文风进行局部重写。"
    )


def raw_metrics(text: str) -> dict[str, float]:
    """Return raw 0-1 metric values for each dimension (no target comparison)."""
    words = _extract_words(text)
    sentences = _extract_sentences(text)
    return {
        "formality": _measure_formality(words),
        "warmth": _measure_warmth(words),
        "authority": _measure_authority(sentences, words),
        "brevity": _measure_brevity(sentences, words),
        "concreteness": _measure_concreteness(words),
        "rhythm": _measure_rhythm(sentences),
        "originality": _measure_originality(words),
        "sentence_count": len(sentences),
        "word_count": len(words),
    }
