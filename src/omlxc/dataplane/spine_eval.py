"""
Spine 文风对齐评测脚手架 (BET-Y1Q3-T10-105 / Q2).

占位实现, 不做真实训练/真实语料评测:
- bleu_score: unigram precision 占位
- rouge_scores: ROUGE-1 / ROUGE-L 风格的 token-overlap F1 占位
- STYLE_ALIGNMENT_GAIN_THRESHOLD = 0.25 (done_when: 对齐度提升 ≥25%)
"""
from __future__ import annotations

#: done_when 阈值: 拟办草稿文风对齐度提升 ≥25%
STYLE_ALIGNMENT_GAIN_THRESHOLD = 0.25


def _tokens(text: str) -> list[str]:
    return text.split()


def bleu_score(hypothesis: str, reference: str) -> float:
    """Unigram-precision 占位 BLEU. 空 hypothesis 返回 0.0."""
    hyp = _tokens(hypothesis)
    if not hyp:
        return 0.0
    ref_counts: dict[str, int] = {}
    for tok in _tokens(reference):
        ref_counts[tok] = ref_counts.get(tok, 0) + 1
    hits = 0
    for tok in hyp:
        if ref_counts.get(tok, 0) > 0:
            hits += 1
            ref_counts[tok] -= 1
    return round(hits / len(hyp), 4)


def rouge_scores(hypothesis: str, reference: str) -> dict[str, float]:
    """Token-overlap F1 占位 ROUGE-1 / ROUGE-L. 不做真实 LCS."""
    hyp = _tokens(hypothesis)
    ref = _tokens(reference)
    if not hyp or not ref:
        return {"rouge1": 0.0, "rougeL": 0.0}
    ref_counts: dict[str, int] = {}
    for tok in ref:
        ref_counts[tok] = ref_counts.get(tok, 0) + 1
    hits = 0
    for tok in hyp:
        if ref_counts.get(tok, 0) > 0:
            hits += 1
            ref_counts[tok] -= 1
    precision = hits / len(hyp)
    recall = hits / len(ref)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {"rouge1": round(f1, 4), "rougeL": round(f1, 4)}


def style_alignment_gain(baseline: float, adapted: float) -> float:
    """相对提升率 (adapted - baseline) / baseline. baseline<=0 时返回 0.0."""
    if baseline <= 0:
        return 0.0
    return round((adapted - baseline) / baseline, 4)


def passes_threshold(gain: float, threshold: float = STYLE_ALIGNMENT_GAIN_THRESHOLD) -> bool:
    return gain >= threshold
