"""
Distill job-receipt 落盘 (BET-Y1Q3-T10-105 / Q2).

FineTuningJobReceipt 以 JSONL 追加写入 gitignored 运行时目录
`<workspace>/run/spine/job-receipts.jsonl` (run/ 已在 .gitignore),
供后续审计与回放, 不进版本库. 不做真实训练.
"""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

RECEIPT_PATH_REL = "run/spine/job-receipts.jsonl"


def receipt_path(workspace_root: Path) -> Path:
    return workspace_root / RECEIPT_PATH_REL


def append_job_receipt(
    receipt: Any | Mapping[str, Any],
    workspace_root: Path,
) -> Path:
    """追加一条 job-receipt 并返回落盘路径."""
    path = receipt_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_dataclass(receipt):
        payload: dict[str, Any] = asdict(receipt)
    elif isinstance(receipt, Mapping):
        payload = dict(receipt)
    else:
        raise TypeError(f"unsupported receipt type: {type(receipt)!r}")
    payload.setdefault("recorded_at", time.time())
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def load_job_receipts(workspace_root: Path) -> list[dict[str, Any]]:
    """读取全部落盘 receipts; 坏行跳过, 文件缺失返回空列表."""
    path = receipt_path(workspace_root)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                out.append(data)
    return out
