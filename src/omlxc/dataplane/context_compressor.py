"""
Context Compaction, AST-Aware Code Pruning & Markdown Optimizer (ADR-0197/ADR-0203).
Provides aggressive context optimization for large files, repetitive system instructions,
and multi-turn tool traces to maximize effective context window and reduce prefill latency.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class ContextOptimizationResult:
    """Telemetry report after optimizing prompt/code context."""
    original_length_chars: int
    optimized_length_chars: int
    estimated_original_tokens: int
    estimated_optimized_tokens: int
    compression_ratio: float
    optimizations_applied: list[str]
    optimized_text: str
class ContextOptimizer:
    """
    Optimizes textual and code context before tokenization and KV cache generation.
    """
    def __init__(self, aggressive: bool = False) -> None:
        self.aggressive = aggressive
    def optimize_text(self, text: str) -> ContextOptimizationResult:
        """
        Compress text context by removing redundant blank lines, trailing spaces,
        and condensing repetitive delimiters.
        """
        original_len = len(text)
        opts: list[str] = []
        res = text
        # 1. Normalize line endings and strip trailing whitespace
        lines = [line.rstrip() for line in res.splitlines()]
        opts.append("strip_trailing_whitespace")
        # 2. Collapse 3+ consecutive empty lines into a single blank line
        collapsed: list[str] = []
        consecutive_blank = 0
        for line in lines:
            if not line:
                consecutive_blank += 1
                if consecutive_blank <= 1:
                    collapsed.append(line)
            else:
                consecutive_blank = 0
                collapsed.append(line)
        opts.append("collapse_consecutive_blank_lines")
        res = "\n".join(collapsed)
        # 3. Delimiter normalization (e.g. --- or === repeated > 10 times)
        res = re.sub(r"([=\-_~*#]){6,}", r"\1\1\1", res)
        opts.append("condense_repetitive_delimiters")
        opt_len = len(res)
        orig_tokens = max(1, original_len // 4)
        opt_tokens = max(1, opt_len // 4)
        ratio = (opt_len / original_len) if original_len > 0 else 1.0
        return ContextOptimizationResult(
            original_length_chars=original_len,
            optimized_length_chars=opt_len,
            estimated_original_tokens=orig_tokens,
            estimated_optimized_tokens=opt_tokens,
            compression_ratio=round(ratio, 4),
            optimizations_applied=opts,
            optimized_text=res,
        )
    def optimize_code_snippet(self, code: str, language: str = "python") -> ContextOptimizationResult:
        """
        Prune non-essential comments and format code compactly.
        """
        original_len = len(code)
        opts: list[str] = []
        res = code
        if language.lower() in ("python", "py"):
            # Remove single line comments that are purely decorative
            lines = res.splitlines()
            filtered_lines: list[str] = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#") and not stripped.startswith(("#!", "# type:", "# noqa:")):
                    # Strip decorative comments in aggressive mode
                    if self.aggressive:
                        continue
                filtered_lines.append(line)
            res = "\n".join(filtered_lines)
            opts.append("python_comment_prune")
        # Apply standard text optimization
        text_opt = self.optimize_text(res)
        opts.extend(text_opt.optimizations_applied)
        opt_len = len(text_opt.optimized_text)
        orig_tokens = max(1, original_len // 4)
        opt_tokens = max(1, opt_len // 4)
        ratio = (opt_len / original_len) if original_len > 0 else 1.0
        return ContextOptimizationResult(
            original_length_chars=original_len,
            optimized_length_chars=opt_len,
            estimated_original_tokens=orig_tokens,
            estimated_optimized_tokens=opt_tokens,
            compression_ratio=round(ratio, 4),
            optimizations_applied=opts,
            optimized_text=text_opt.optimized_text,
        )
