"""omlxc V5.0 — Hard-Negative Miner: signature diff reverse extraction (BET-Y1Q3-T10-115).

Mines the Experience Replay buffer (``.omo/state/lora-replay-buffer.jsonl``,
>=30 real signature samples delivered by T10-105) into hard-negative rules:
recurring edit patterns (banned boilerplate, verbose padding, terminology
fixes) that the drafting side must never reproduce.

Pipeline:
    buffer sample (instruction=draft, output=signed)
      -> parse_signature_diff   (difflib opcodes -> structured hunks)
      -> classify_hunk          (rule-based semantic intent)
      -> mine_negatives         (aggregate patterns seen >= min_count)
      -> export_rules           (JSONL rule library consumed by ecos diff_broker)

Pure stdlib. No model inference: rules are structural + lexical.
"""

from __future__ import annotations

import difflib
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

RULES_EXPORT_REL = ".omo/state/hard-negative-rules.jsonl"

# 中文公文高频套话（被署名删除 >=2 次即成禁用规则候选）
_BOILERPLATE_PATTERNS = [
    r"为进一步(?:推进|加强|深化)",
    r"(?:高度|十分|极其)重视",
    r"(?:切实|认真|扎实)做好",
    r"以.{2,12}为(?:契机|抓手|引领)",
    r"(?:不断|持续|进一步)提升.{0,8}水平",
    r"(?:在新形势下|站在新的起点上)",
    r"(?:众所周知|总的来说|综上所述)",
]

_PUNCT_SPACE = re.compile(r"[，。、；：\s]+")


@dataclass(slots=True)
class DiffHunk:
    """One structured edit between the draft and the signed version."""

    op: str  # delete | insert | replace | equal
    draft_text: str
    signed_text: str
    draft_pos: int
    signed_pos: int
    intent: str = ""  # filled by classify_hunk


@dataclass(slots=True)
class DiffReport:
    """Structured per-signature diff report (done_when[0])."""

    sample_id: str
    domain: str
    hunks: list[DiffHunk] = field(default_factory=list)
    intent_counts: dict[str, int] = field(default_factory=dict)
    draft_len: int = 0
    signed_len: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "domain": self.domain,
            "draft_len": self.draft_len,
            "signed_len": self.signed_len,
            "intent_counts": self.intent_counts,
            "hunks": [
                {k: v for k, v in asdict(h).items() if v not in ("", None) or k == "intent"}
                for h in self.hunks
                if h.op != "equal"
            ],
        }


@dataclass(slots=True)
class HardNegativeRule:
    """An aggregated anti-pattern rule extracted from recurring edits."""

    rule_id: str
    rule_type: str  # banned_phrase | verbose_trim | terminology_replace
    pattern: str  # regex or literal fragment to reject in drafts
    description: str
    count: int
    evidence_sample_ids: list[str]


def parse_signature_diff(instruction: str, output: str) -> list[DiffHunk]:
    """Word-level opcode diff between the draft and the signed version."""
    draft_words = re.split(r"(\s+)", instruction)
    signed_words = re.split(r"(\s+)", output)
    matcher = difflib.SequenceMatcher(a=draft_words, b=signed_words, autojunk=False)
    hunks: list[DiffHunk] = []
    dpos = spos = 0
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        dseg = "".join(draft_words[i1:i2])
        sseg = "".join(signed_words[j1:j2])
        if op == "equal":
            dpos += len(dseg)
            spos += len(sseg)
            continue
        if op == "delete":
            hunk = DiffHunk("delete", dseg, "", dpos, spos)
            dpos += len(dseg)
        elif op == "insert":
            hunk = DiffHunk("insert", "", sseg, dpos, spos)
            spos += len(sseg)
        else:
            hunk = DiffHunk("replace", dseg, sseg, dpos, spos)
            dpos += len(dseg)
            spos += len(sseg)
        hunks.append(hunk)
    return hunks


def classify_hunk(hunk: DiffHunk) -> str:
    """Rule-based semantic intent for one hunk (delete/insert/replace)."""
    removed = _PUNCT_SPACE.sub("", hunk.draft_text)
    added = _PUNCT_SPACE.sub("", hunk.signed_text)

    # 套话检查优先于分支（delete 与 replace 的被删侧都可能含套话）
    if hunk.op in ("delete", "replace"):
        for pat in _BOILERPLATE_PATTERNS:
            if re.search(pat, hunk.draft_text):
                return "banned_phrase"

    if hunk.op == "delete":
        if len(removed) >= 12:
            return "verbose_trim"
        return "punctuation_or_minor"
    if hunk.op == "insert":
        if not added:
            return "punctuation_or_minor"
        if len(added) <= 6:
            return "precision_add"
        return "content_add"
    # replace: terminology swap if same length ±2 and both non-empty
    if removed and added and abs(len(removed) - len(added)) <= 2:
        return "terminology_replace"
    if len(removed) > len(added) * 1.5:
        return "verbose_trim"
    return "fact_fix"


def parse_sample(sample: dict[str, Any]) -> DiffReport:
    instruction = str(sample.get("instruction", ""))
    output = str(sample.get("output", ""))
    hunks = parse_signature_diff(instruction, output)
    for h in hunks:
        h.intent = classify_hunk(h)
    counts: dict[str, int] = defaultdict(int)
    for h in hunks:
        counts[h.intent] += 1
    return DiffReport(
        sample_id=str(sample.get("sample_id", "")),
        domain=str(sample.get("domain", "")),
        hunks=hunks,
        intent_counts=dict(counts),
        draft_len=len(instruction),
        signed_len=len(output),
    )


def _normalize_fragment(text: str) -> str:
    return _PUNCT_SPACE.sub("", text)[:40]


def mine_negatives(
    buffer_path: str | Path,
    min_count: int = 2,
) -> tuple[list[HardNegativeRule], list[DiffReport]]:
    """Aggregate recurring edit patterns into hard-negative rules.

    done_when[0]: every sample yields a structured DiffReport with intent
    classification; recurring >= min_count patterns become rules.
    """
    buffer_path = Path(buffer_path)
    reports: list[DiffReport] = []
    if not buffer_path.exists():
        return [], reports
    with buffer_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            reports.append(parse_sample(sample))

    # aggregate deletion / replacement drafts by normalized fragment
    phrase_hits: dict[str, list[tuple[str, str]]] = defaultdict(list)  # pattern -> [(sid, fragment)]
    term_hits: dict[tuple[str, str], list[str]] = defaultdict(list)  # (old, new) -> [sid]
    for rep in reports:
        for h in rep.hunks:
            if h.intent == "banned_phrase":
                # 聚合键 = 命中的套话短语本身（hunk 整段含样本特定前缀，不可作键）
                matched = next(
                    (m.group(0) for pat in _BOILERPLATE_PATTERNS if (m := re.search(pat, h.draft_text))),
                    _normalize_fragment(h.draft_text)[:20],
                )
                if matched:
                    phrase_hits[matched].append((rep.sample_id, matched))
            elif h.intent == "terminology_replace":
                key = (_normalize_fragment(h.draft_text), _normalize_fragment(h.signed_text))
                if key[0] and key[1]:
                    term_hits[key].append(rep.sample_id)
            elif h.intent == "verbose_trim" and len(h.draft_text) >= 20:
                frag = _normalize_fragment(h.draft_text)
                if frag:
                    phrase_hits[frag].append((rep.sample_id, frag))

    rules: list[HardNegativeRule] = []
    rid = 0
    for frag, hits in sorted(phrase_hits.items(), key=lambda kv: -len(kv[1])):
        if len(hits) < min_count:
            continue
        rid += 1
        sids = sorted({sid for sid, _ in hits})
        rules.append(
            HardNegativeRule(
                rule_id=f"HN-{rid:03d}",
                rule_type="banned_phrase" if len(frag) <= 30 else "verbose_trim",
                pattern=re.escape(frag[:30]),
                description=f"署名中 {len(hits)} 次删除该表述",
                count=len(hits),
                evidence_sample_ids=sids,
            )
        )
    for (old, new), sids in sorted(term_hits.items(), key=lambda kv: -len(kv[1])):
        if len(sids) < min_count:
            continue
        rid += 1
        rules.append(
            HardNegativeRule(
                rule_id=f"HN-{rid:03d}",
                rule_type="terminology_replace",
                pattern=re.escape(old[:30]),
                description=f"署名偏好以“{new[:16]}”替换“{old[:16]}”（{len(sids)} 次）",
                count=len(sids),
                evidence_sample_ids=sorted(set(sids)),
            )
        )
    return rules, reports


def export_rules(rules: list[HardNegativeRule], out_path: str | Path) -> int:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in rules:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    return len(rules)


def mine_and_export(buffer_path: str | Path, out_path: str | Path | None = None) -> tuple[int, int]:
    """Convenience: mine buffer and persist rules. Returns (rule_count, sample_count)."""
    ws = Path.cwd()
    for parent in [ws, *ws.parents]:
        if (parent / "docs" / "project-registry.yaml").is_file():
            ws = parent
            break
    out = Path(out_path) if out_path else ws / RULES_EXPORT_REL
    rules, reports = mine_negatives(buffer_path)
    export_rules(rules, out)
    return len(rules), len(reports)
