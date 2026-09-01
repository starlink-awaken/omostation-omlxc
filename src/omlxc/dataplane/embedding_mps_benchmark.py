"""Verify-contract entry: python -m omlxc.dataplane.embedding_mps_benchmark (BET-Y1Q4-T3-02)."""

from __future__ import annotations

import json
import sys


def main() -> int:
    from omlxc.dataplane.embedding_mps import run_benchmark
    from omlxc.dataplane.reranker import run_rerank_benchmark

    embed = run_benchmark()
    rerank = run_rerank_benchmark()
    checks = {**embed["checks"], **rerank["checks"]}
    report = {
        "schema": "omlxc.dataplane.embedding-mps-benchmark.v1",
        "embedding": embed,
        "rerank": rerank,
        "checks": checks,
        "all_pass": all(checks.values()),
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
