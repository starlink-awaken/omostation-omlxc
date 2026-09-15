"""Tone evaluation primitives for the LoRA matrix (BET-Y2Q2-T3-02).

Pure standard library: character-granularity ROUGE-L (suitable for Chinese),
revision-rate derived from it, and a bilingual protected-term guard against
catastrophic forgetting of proper nouns / technical terms.
"""

from __future__ import annotations

import re


def _lcs_len(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    # Keep the shorter sequence on the inner axis for memory.
    if len(a) < len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if x == y else (prev[j] if prev[j] >= cur[j - 1] else cur[j - 1])
        prev = cur
    return prev[len(b)]


def _tokens(text: str) -> list[str]:
    # Character granularity: correct for Chinese, acceptable approximation for EN.
    return [c for c in text.strip() if not c.isspace()]


def rouge_l(candidate: str, reference: str) -> float:
    """ROUGE-L F-measure in [0, 1]. Empty/empty scores 1.0, either-empty 0.0."""
    if not candidate.strip() and not reference.strip():
        return 1.0
    c, r = _tokens(candidate), _tokens(reference)
    if not c or not r:
        return 0.0
    lcs = _lcs_len(c, r)
    prec = lcs / len(c)
    rec = lcs / len(r)
    if prec + rec == 0:
        return 0.0
    return 2 * prec * rec / (prec + rec)


def revision_rate(before: str, after: str) -> float:
    """Owner revision rate: fraction of tone/content the owner had to change."""
    return 1.0 - rouge_l(after, before)


# Bilingual anti-forgetting guard: terms that must survive adaptation verbatim.
PROTECTED_TERMS: tuple[str, ...] = (
    "AetherForge",
    "Spine",
    "Cockpit",
    "LoRA",
    "ROUGE-L",
    "夏明星",
    "卫生健康局",
    "医共体",
    "ADR",
    "MLX",
)


def guard_protected_terms(candidate: str, reference: str) -> list[str]:
    """Return protected terms present in reference but altered/missing in candidate."""
    missing: list[str] = []
    for term in PROTECTED_TERMS:
        if term in reference and term not in candidate:
            # Case-insensitive second chance for ASCII terms.
            if term.isascii() and re.search(re.escape(term), candidate, re.IGNORECASE):
                continue
            missing.append(term)
    return missing
