"""Unit tests for spine eval scaffold (BET-Y1Q3-T10-105 / Q2)."""
from __future__ import annotations

from omlxc.dataplane.spine_eval import (
    STYLE_ALIGNMENT_GAIN_THRESHOLD,
    bleu_score,
    passes_threshold,
    rouge_scores,
    style_alignment_gain,
)


def test_threshold_is_25_percent() -> None:
    assert STYLE_ALIGNMENT_GAIN_THRESHOLD == 0.25


def test_bleu_placeholder_identical_and_empty() -> None:
    assert bleu_score("hello world", "hello world") == 1.0
    assert bleu_score("", "hello world") == 0.0
    partial = bleu_score("hello there", "hello world")
    assert 0.0 < partial < 1.0


def test_rouge_placeholder_shape_and_edge() -> None:
    scores = rouge_scores("hello world", "hello world")
    assert scores == {"rouge1": 1.0, "rougeL": 1.0}
    assert rouge_scores("", "hello") == {"rouge1": 0.0, "rougeL": 0.0}


def test_gain_and_threshold_gate() -> None:
    assert style_alignment_gain(0.4, 0.5) == 0.25
    assert passes_threshold(0.25) is True
    assert passes_threshold(0.24) is False
    assert style_alignment_gain(0.0, 0.5) == 0.0
