#!/usr/bin/env python3
"""mbp 内存哨兵: 持续监控真实可用内存 + 识别高内存风险模型 + 分级留痕。

背景 (2026-08-23): qwythos-9b (LM Studio, context 852736, ~18.8GB) 在
generating 时把 swap 打到 24.2GB/26GB, 挤崩了 oMLX App 进程(不是卡住,
是进程真的退出了, ps 里找不到, /v1/models 完全无响应)。

设计边界(刻意划清, 不越界):
  - omlxc daemon 自己管理的 placement 已经有 idle_ttl_seconds 机制
    (2026-08-22 P0 修复) 负责按闲置时长自动释放, 这里不重复造轮子。
  - LM Studio 里用户直接手动加载的模型不受 omlxc 管辖, 这次真实故障
    恰恰发生在这个盲区。这个脚本只做监控 + 告警 + 留痕, 不会替用户
    卸载 LM Studio 里的模型 —— 那是用户直接控制的领域, 脚本没有被
    要求过要越权干预, 贸然 unload 可能打断用户还想继续的对话。
  - 铁律, 无条件: 任何 status=generating 的模型, 不管在哪个 backend,
    绝不触碰、绝不建议卸载。

分级阈值:
  SAFE(>=15GB)    : 静默, 只写一行趋势日志。
  WARN(8-15GB)    : 记录当前全部已加载模型快照, 提示关注。
  CRITICAL(<8GB)  : 额外记录进程 RSS 排序快照(诊断用), 对非-generating
                    的高风险模型给出"建议人工 review"的日志, 不自动执行。

高风险模型判定: 体积 > HIGH_RISK_SIZE_GB 或 context_length > HIGH_RISK_CTX
  (852736 这种量级明显偏离正常使用, 826 token 场景不会用到这么长)。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

WARN_GB = 15.0
CRITICAL_GB = 8.0
HIGH_RISK_SIZE_GB = 15.0
HIGH_RISK_CTX = 200_000

LOG = "/Users/xiamingxing/.config/omlxc/memory-sentinel.log"


def _ts() -> str:
    return datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _log(line: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{_ts()} {line}\n")
    print(line)


def real_available_gb() -> float:
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
    vals: dict[str, int] = {}
    for line in out.splitlines():
        for key in ("Pages free", "Pages purgeable", "Pages inactive"):
            if line.startswith(key):
                vals[key] = int(line.split(":")[1].strip().rstrip(".").replace(",", ""))
    free = vals.get("Pages free", 0)
    purg = vals.get("Pages purgeable", 0)
    inact = vals.get("Pages inactive", 0)
    return (free + purg + inact * 0.7) * 16384 / 1024**3


def swap_used_gb() -> float | None:
    try:
        out = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True, timeout=10).stdout
        # vm.swapusage: total = 26624.00M  used = 24761.94M  free = 1862.06M
        used_part = [p for p in out.split("used =")[1].split() if p][0]
        return float(used_part.rstrip("M")) / 1024
    except Exception:
        return None


def lms_models() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            ["/Users/xiamingxing/.local/bin/lms", "ps", "--json"], capture_output=True, text=True, timeout=15
        )
        return json.loads(result.stdout)
    except Exception:
        return []


def omlx_app_models() -> list[dict[str, Any]]:
    try:
        import httpx

        r = httpx.get("http://127.0.0.1:8000/v1/models", timeout=5.0)
        if r.status_code != 200:
            return []
        return r.json().get("data", [])
    except Exception:
        return []


def high_risk_models(rows: list[dict[str, Any]]) -> list[str]:
    risky: list[str] = []
    for r in rows:
        size_gb = r.get("sizeBytes", 0) / 1024**3
        ctx = r.get("contextLength", 0) or 0
        if size_gb > HIGH_RISK_SIZE_GB or ctx > HIGH_RISK_CTX:
            risky.append(f"{r.get('modelKey')}(size={size_gb:.1f}GB ctx={ctx} status={r.get('status')})")
    return risky


def proc_rss_top(n: int = 12) -> list[str]:
    try:
        out = subprocess.run(
            ["/bin/ps", "-axo", "pid,rss,comm"], capture_output=True, text=True, timeout=10
        ).stdout
        lines = out.splitlines()[1:]
        parsed: list[tuple[int, str]] = []
        for line in lines:
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            try:
                rss_kb = int(parts[1])
            except ValueError:
                continue
            parsed.append((rss_kb, parts[2]))
        parsed.sort(reverse=True)
        return [f"{rss_kb / 1024**2:.2f}GB {comm}" for rss_kb, comm in parsed[:n]]
    except Exception:
        return []


def main() -> int:
    available = real_available_gb()
    swap = swap_used_gb()
    lms_rows = lms_models()
    omlx_rows = omlx_app_models()

    risky = high_risk_models(lms_rows)
    for entry in risky:
        # 高风险模型只要在加载, 不管当前内存是否紧张都留痕 —— 这样未来
        # 复盘时能第一时间看到"是不是又是这个模型", 不用像这次一样现场
        # 手动排查大半天。
        _log(f"[RISK-MODEL] LM Studio 高风险模型加载中: {entry}")

    swap_note = f" swap_used={swap:.1f}GB" if swap is not None else ""

    if available < CRITICAL_GB:
        _log(f"[CRITICAL] 可用内存 {available:.1f}GB < {CRITICAL_GB}GB{swap_note}")
        for line in proc_rss_top():
            _log(f"[CRITICAL-PROC] {line}")
        for r in lms_rows:
            if r.get("status") != "generating":
                size_gb = r.get("sizeBytes", 0) / 1024**3
                if size_gb > HIGH_RISK_SIZE_GB:
                    _log(
                        f"[SUGGEST-REVIEW] {r.get('modelKey')} 空闲且体积 {size_gb:.1f}GB, "
                        f"内存危急时建议人工确认是否需要手动释放 (脚本不自动 unload)"
                    )
        return 1

    if available < WARN_GB:
        loaded_local = [m.get("id") for m in omlx_rows]
        loaded_lms = [r.get("modelKey") for r in lms_rows]
        _log(
            f"[WARN] 可用内存 {available:.1f}GB < {WARN_GB}GB{swap_note} "
            f"omlx-app已加载={loaded_local} lm_studio已加载={loaded_lms}"
        )
        return 0

    _log(f"[OK] 可用内存 {available:.1f}GB{swap_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
