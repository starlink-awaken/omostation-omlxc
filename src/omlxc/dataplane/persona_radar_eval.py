"""omlxc.dataplane.persona_radar_eval — CLI evaluation entry point.

BET-Y2Q2-T3-01: 个人文风一致性多维雷达评估与语气自适应调节引擎.

Usage:
    uv run python -m omlxc.dataplane.persona_radar_eval [OPTIONS]

    # Default evaluation with built-in sample text
    uv run python -m omlxc.dataplane.persona_radar_eval

    # Evaluate a specific file
    uv run python -m omlxc.dataplane.persona_radar_eval --file path/to/text.txt

    # Shift tone direction
    uv run python -m omlxc.dataplane.persona_radar_eval --tone sharp --strength 0.5

    # Set alignment threshold
    uv run python -m omlxc.dataplane.persona_radar_eval --threshold 90

    # Custom author name
    uv run python -m omlxc.dataplane.persona_radar_eval --author "xiamingxing"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .persona_radar import (
    RadarEvalResult,
    ToneDirection,
    ToneProfile,
    compute_radar,
    compute_radar_dimensions,
    raw_metrics,
    tone_shift,
    auto_rewrite_suggestion,
)


# Default sample text representing a typical professional writing style
_DEFAULT_SAMPLE = """
We have observed a remarkable trend in the development of sovereign compute infrastructure. The convergence of distributed systems and local-first architectures has created unprecedented opportunities for data sovereignty. As we move forward, it is imperative that we maintain our commitment to these fundamental principles.

There are several key considerations that must be addressed in this context. First, we must ensure that our infrastructure scales gracefully under load. Second, we need to implement robust monitoring and alerting mechanisms. Third, we should prioritize the development of interoperable protocols that enable seamless integration across different system boundaries.

In conclusion, the path forward requires both strategic vision and tactical execution. We are committed to building a system that is not only powerful and efficient but also aligned with the core values of openness and autonomy that have always defined our approach.
"""


def _load_text(args: argparse.Namespace) -> str:
    """Load text from file or use default sample."""
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            print(f"ERROR: File not found: {path}", file=sys.stderr)
            sys.exit(1)
        return path.read_text(encoding="utf-8")
    return _DEFAULT_SAMPLE


def _build_profile(args: argparse.Namespace) -> ToneProfile:
    """Build ToneProfile from CLI args (or defaults)."""
    profile = ToneProfile(
        formality=args.formality,
        warmth=args.warmth,
        authority=args.authority,
        brevity=args.brevity,
        concreteness=args.concreteness,
        rhythm=args.rhythm,
        originality=args.originality,
    )
    if args.tone:
        direction_map = {
            "solemn": ToneDirection.SOLEMN,
            "sharp": ToneDirection.SHARP,
            "gentle": ToneDirection.GENTLE,
        }
        direction = direction_map.get(args.tone.lower())
        if direction:
            profile = tone_shift(profile, direction, strength=args.strength)
    return profile


def main() -> int:
    """Main entry point for persona_radar_eval."""
    parser = argparse.ArgumentParser(
        description="BET-Y2Q2-T3-01: Personal writing-style radar evaluation engine",
    )
    parser.add_argument("--file", "-f", help="Text file to evaluate (default: built-in sample)")
    parser.add_argument("--author", "-a", default="anonymous", help="Author name for report")
    parser.add_argument("--threshold", "-t", type=float, default=85.0,
                        help="Alignment threshold (default: 85.0)")
    parser.add_argument("--tone", help="Tone shift direction: solemn | sharp | gentle")
    parser.add_argument("--strength", type=float, default=1.0,
                        help="Tone shift strength 0-1 (default: 1.0)")
    # Individual dimension overrides (0.0-1.0)
    parser.add_argument("--formality", type=float, default=0.5)
    parser.add_argument("--warmth", type=float, default=0.4)
    parser.add_argument("--authority", type=float, default=0.6)
    parser.add_argument("--brevity", type=float, default=0.5)
    parser.add_argument("--concreteness", type=float, default=0.6)
    parser.add_argument("--rhythm", type=float, default=0.5)
    parser.add_argument("--originality", type=float, default=0.5)
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show raw metrics")

    args = parser.parse_args()

    # Clamp all numeric args to [0.0, 1.0]
    for dim in ("formality", "warmth", "authority", "brevity",
                "concreteness", "rhythm", "originality"):
        setattr(args, dim, max(0.0, min(1.0, getattr(args, dim))))

    text = _load_text(args)
    target = _build_profile(args)

    # Compute raw metrics
    metrics = raw_metrics(text)

    # Compute radar profile
    profile = compute_radar(text, args.author, target, threshold=args.threshold)

    # Build eval result
    result = RadarEvalResult(profile=profile, raw_metrics=metrics)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_report(result)

    # Check auto-rewrite trigger
    rewrite_msg = auto_rewrite_suggestion(text, profile)
    if rewrite_msg and not args.json:
        print(f"\n{rewrite_msg}")

    return 0


def _print_report(result: RadarEvalResult) -> None:
    """Print a human-readable radar evaluation report."""
    p = result.profile
    metrics = result.raw_metrics

    print(f"{'=' * 60}")
    print(f"  个人文风一致性多维雷达评估报告 (BET-Y2Q2-T3-01)")
    print(f"{'=' * 60}")
    print(f"  Author:        {p.author}")
    print(f"  Alignment:     {p.alignment_score:.1f} / 100")
    print(f"  Threshold:     {p.threshold:.1f}")
    print(f"  Status:        {'⚠️ BELOW THRESHOLD' if p.below_threshold else '✓ PASSED'}")
    print(f"  Word count:    {metrics['word_count']}")
    print(f"  Sentences:     {metrics['sentence_count']}")
    print()
    print(f"  {'Dimension':<14} {'Current':>8} {'Target':>8} {'Gap':>8}")
    print(f"  {'-' * 40}")
    for d in p.dimensions:
        flag = "⚠" if d.gap > 10 else ""
        print(f"  {d.label:<12} {d.current:>8.1f} {d.target:>8.1f} {d.gap:>+8.1f} {flag}")
    print()

    if p.suggestions:
        print("  Suggestions:")
        for s in p.suggestions:
            print(f"    • {s}")
        print()

    print(f"{'=' * 60}")


if __name__ == "__main__":
    sys.exit(main())
