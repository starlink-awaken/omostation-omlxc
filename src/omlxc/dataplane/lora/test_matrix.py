"""Self-contained test runner for the LoRA matrix (BET-Y2Q2-T3-02).

Run: ``uv run python -m omlxc.dataplane.lora.test_matrix`` (inside projects/omlxc).
Exit 0 = all checks pass. No pytest / MLX / network required.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from omlxc.dataplane.lora.eval_tone import guard_protected_terms, revision_rate, rouge_l
from omlxc.dataplane.lora.matrix import (
    ADAPTER_NAMES,
    DOMAINS,
    LoraMatrix,
    partition_buffer_by_domain,
    route_domain,
)

_CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _CHECKS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


def _buffer(tmp: Path) -> Path:
    samples = [
        ("关于县域医共体建设方案的请示，请批复。", "批复：同意按方案推进。"),
        ("收到卫生健康局督办通知，拟办意见如下。", "呈领导阅示。"),
        ("ADR-0203 需求迭代工作流的技术方案评审。", "评审结论：通过，附三条修订。"),
        ("微服务接口契约与数据模型设计说明。", "接口版本 v2，模型见附表。"),
        ("与合作方洽谈联名备忘录及签约安排。", "备忘录草案已备，待签约。"),
        ("对外协作会议：协同推进伙伴渠道事项。", "会议纪要已同步各方。"),
        ("随笔：深夜值班后的一点感悟与手记。", "心境安然，漫谈数语。"),
        ("札记：秋日杂感两则。", "随想如风，记录如实。"),
    ]
    buf = tmp / "buffer.jsonl"
    buf.write_text(
        "\n".join(json.dumps({"instruction": i, "output": o}, ensure_ascii=False) for i, o in samples),
        encoding="utf-8",
    )
    return buf


def main() -> int:
    # 1. Four-domain routing.
    check("route gov", route_domain("关于医共体请示，请批复。") == "gov")
    check("route tech", route_domain("ADR 技术方案评审与接口设计。") == "tech")
    check("route collab", route_domain("与合作方洽谈签约备忘录。") == "collab")
    check("route essay", route_domain("深夜随笔手记，一点感悟。") == "essay")
    check("route default gov", route_domain("今天天气不错。") == "gov")

    # 2. Specificity ordering: narrow domains beat broad gov words.
    check("specific collab over 通知", route_domain("对外协作会议通知：协同事项。") == "collab")
    check("specific essay kept", route_domain("通知：随笔漫谈活动安排。") == "essay")
    check("specific tech over 审批", route_domain("架构评审后的部署审批。") == "tech")

    # 3. Buffer partition 2/2/2/2.
    with tempfile.TemporaryDirectory() as td:
        buf = _buffer(Path(td))
        shards = partition_buffer_by_domain(buf)
        check("partition keys", set(shards) == set(DOMAINS), f"got {sorted(shards)}")
        check("partition 2 each", all(len(shards[d]) == 2 for d in DOMAINS), str({d: len(shards[d]) for d in DOMAINS}))
        check(
            "partition missing file", all(v == [] for v in partition_buffer_by_domain(Path(td) / "nope.jsonl").values())
        )

    # 4. Registry round-trip + seamless switch (isolated ws).
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        m = LoraMatrix(workspace_root=ws, buffer_path=ws / "empty.jsonl")
        check(
            "activate ok", m.activate("collab") == {"ok": True, "domain": "collab", "adapter": ADAPTER_NAMES["collab"]}
        )
        check("activate unknown", m.activate("nope")["ok"] is False)
        check("adapter_for_domain", m.adapter_for_domain("collab") == "lora-collab-v1")
        sw = m.switch("essay")
        check(
            "switch seamless",
            sw == {"ok": True, "prev": "collab", "current": "essay", "adapter": "lora-essay-v1", "reloaded": False},
            str(sw),
        )
        check("current tracks", m.current() == "essay")
        m2 = LoraMatrix(workspace_root=ws, buffer_path=ws / "empty.jsonl")
        check("registry persists (own file)", m2.active.get("essay") == "lora-essay-v1")
        check("registry file name", m.registry_path().name == "lora-matrix.json")
        check("deactivate ok", m.deactivate("essay")["ok"] is True)
        check("deactivate empty", m.deactivate("essay")["ok"] is False)
        check("switch unknown", m.switch("nope")["ok"] is False)
        routed = m.adapter_for_task("与合作方洽谈签约。")
        check("adapter_for_task", routed["domain"] == "collab" and routed["expected"] == "lora-collab-v1", str(routed))
        rows = m.list_matrix()
        check("list_matrix 4 rows", len(rows) == 4 and all("adapter" in r for r in rows))

    # 5. Honest distill statuses (this node has no MLX; shards are small).
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        buf = _buffer(ws)
        m = LoraMatrix(workspace_root=ws, buffer_path=buf)
        st = m.distill_status("gov")
        check("honest pending (2<8)", st.status == "pending_samples", st.status)
        check("honest unknown domain", m.distill_status("nope").status == "pending_samples")
        big = ws / "big.jsonl"
        big.write_text(
            "\n".join(
                json.dumps(
                    {"instruction": f"请示批复事项{i}，呈卫生健康局。", "output": "批复同意。"}, ensure_ascii=False
                )
                for i in range(9)
            ),
            encoding="utf-8",
        )
        m3 = LoraMatrix(workspace_root=ws, buffer_path=big)
        st3 = m3.distill_status("gov")
        try:
            import mlx_lm  # type: ignore

            want = "ready"
        except ImportError:
            want = "needs_mlx"
        check(f"honest {want} (9 samples)", st3.status == want, st3.status)

    # 6. ROUGE-L known values + revision rate + guard.
    check("rouge identical", rouge_l("批复同意。", "批复同意。") == 1.0)
    check("rouge empty pair", rouge_l("", "") == 1.0)
    check("rouge empty one side", rouge_l("有内容", "") == 0.0)
    partial = rouge_l("批复同意按方案推进", "批复同意")
    check("rouge partial in (0,1)", 0.0 < partial < 1.0, str(partial))
    check("revision identical 0", revision_rate("同文。", "同文。") == 0.0)
    check(
        "guard catches forgotten",
        guard_protected_terms("普通批复。", "卫生健康局批复，ADR 见附。") == ["卫生健康局", "ADR"],
    )
    check("guard clean", guard_protected_terms("卫生健康局批复。", "卫生健康局批复。") == [])

    # 7. Tone gate: pass + distorted fallback.
    with tempfile.TemporaryDirectory() as td:
        m = LoraMatrix(workspace_root=Path(td), buffer_path=Path(td) / "e.jsonl")
        ok = m.regulate("批复：同意按方案推进。", "批复：同意按方案推进。")
        check("gate pass", ok["pass"] is True and ok["fallback_neutral"] is False, str(ok))
        bad = m.regulate("天气不错，出去走走。", "批复：同意按医共体方案推进。")
        check("gate distorted fallback", bad["pass"] is False and bad["fallback_neutral"] is True, str(bad))
        forgot = m.regulate("普通批复同意。", "卫生健康局批复同意。")
        check(
            "gate forgotten fallback", forgot["fallback_neutral"] is True and "卫生健康局" in forgot["forgotten_terms"]
        )

    failed = [n for n, ok_, _ in _CHECKS if not ok_]
    print(f"\n{len(_CHECKS) - len(failed)}/{len(_CHECKS)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
