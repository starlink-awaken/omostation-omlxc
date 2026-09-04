"""Unit tests for distill job-receipt 落盘 (BET-Y1Q3-T10-105 / Q2)."""
from __future__ import annotations

from pathlib import Path

from omlxc.dataplane.distill_receipts import (
    RECEIPT_PATH_REL,
    append_job_receipt,
    load_job_receipts,
    receipt_path,
)
from omlxc.dataplane.lora_adapter_manager import SignatureDiffDistiller


def test_receipt_lands_in_gitignored_state_dir(tmp_path: Path) -> None:
    distiller = SignatureDiffDistiller()
    distiller.record_signature_diff("ctx", "draft", "signed")
    receipt = distiller.trigger_idle_distillation()
    path = append_job_receipt(receipt, tmp_path)
    assert path == tmp_path / RECEIPT_PATH_REL
    assert path.exists()
    assert path.relative_to(tmp_path).parts[0] == "run"  # run/ is gitignored


def test_receipt_roundtrip_and_bad_line_tolerance(tmp_path: Path) -> None:
    distiller = SignatureDiffDistiller()
    distiller.record_signature_diff("ctx", "draft", "signed")
    append_job_receipt(distiller.trigger_idle_distillation(), tmp_path)
    append_job_receipt({"job_id": "manual-1", "domain_tag": "signature-style"}, tmp_path)
    path = receipt_path(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write("{bad line}\n")
    receipts = load_job_receipts(tmp_path)
    assert len(receipts) == 2
    assert receipts[0]["status"] == "COMPLETED"


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_job_receipts(tmp_path) == []
