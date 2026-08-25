#!/usr/bin/env python3
"""weekly-report.py — full-status 周度趋势小结 (roadmap 阶段二既定项).

数据源: ~/.config/omlxc/status-history.log (full-status.sh 落盘的全量快照)
       + ~/.config/omlxc/watchdog.log (remote_resident 稳定性补充)
输出: 一屏人读周报 (节点在线/placement 可用率/内存与 swap 曲线/常驻稳定性)。
fabric 红线: 数据不足如实标注, 不编造趋势; 快照数 < 7 时明确"数据积累中"。

挂载: cron 周一 08:05 (数据自 2026-08-24 起积累, 首份完整周报 2026-09-01)。
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOG = Path.home() / ".config/omlxc/status-history.log"
WATCHDOG = Path.home() / ".config/omlxc/watchdog.log"
DAYS = 7

SNAP_RE = re.compile(r"^=== omlxc 全链路状态 (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ===$")
PLACEMENT_RE = re.compile(r"Placement:\s*(\d+)/(\d+)\s*可用")
MEM_RE = re.compile(r"可用内存\s*([\d.]+)GB\s*swap_used=([\d.]+)GB")
NODE_RE = re.compile(r"^\s+(100\.\d+\.\d+\.\d+)\s+(\S+)\s+.*?(offline|active)")


def parse_snapshots() -> list[dict]:
    if not LOG.exists():
        return []
    snaps: list[dict] = []
    cur: dict | None = None
    for line in LOG.read_text(errors="replace").splitlines():
        m = SNAP_RE.match(line)
        if m:
            if cur:
                snaps.append(cur)
            cur = {"date": m.group(1), "time": m.group(2), "nodes": {}}
            continue
        if cur is None:
            continue
        m = PLACEMENT_RE.search(line)
        if m:
            cur["placement_ok"], cur["placement_total"] = int(m.group(1)), int(m.group(2))
        m = MEM_RE.search(line)
        if m:
            cur["mem_free"], cur["swap"] = float(m.group(1)), float(m.group(2))
        m = NODE_RE.match(line)
        if m:
            cur["nodes"][m.group(2)] = "online" if "active" in m.group(3) else "offline"
    if cur:
        snaps.append(cur)
    return snaps


def parse_resident_events(days: int) -> dict[str, dict[str, int]]:
    """watchdog.log 近 N 天 remote_resident 补齐/失败计数(按节点)."""
    if not WATCHDOG.exists():
        return {}
    cutoff = datetime.now() - timedelta(days=days)
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "fail": 0})
    for line in WATCHDOG.read_text(errors="replace").splitlines():
        m = re.match(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}", line)
        if not m or datetime.strptime(m.group(1), "%Y-%m-%d") < cutoff:
            continue
        rm = re.search(r"remote_resident(?:\(ollama\))? (?:补齐|加载失败): (\S+?)/", line)
        if rm:
            node = rm.group(1)
            stats[node]["ok" if "补齐" in line else "fail"] += 1
    return dict(stats)


def main() -> int:
    snaps = parse_snapshots()
    since = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d")
    recent = [s for s in snaps if s["date"] >= since]

    print(f"📊 omlxc 周报 · {datetime.now():%Y-%m-%d} (近 {DAYS} 天, 起 {since})")
    print(f"   快照总数 {len(snaps)} (近{DAYS}天 {len(recent)}) — ", end="")
    if len(recent) < 7:
        print(f"⚠️ 数据积累中(自 2026-08-24 起), 趋势仅供参考")
    else:
        print("数据充分")

    if not recent:
        print("   (无快照 — full-status 未落盘?)")
        return 1

    by_day: dict[str, list[dict]] = defaultdict(list)
    for s in recent:
        by_day[s["date"]].append(s)

    # placement 可用率
    ok_list = [s["placement_ok"] for s in recent if "placement_ok" in s]
    tot = recent[0].get("placement_total") if recent else None
    if ok_list:
        print(f"\n📦 Placement 可用性: 日均 {sum(ok_list)/len(ok_list):.1f}/{tot}"
              f" ({100*sum(ok_list)/len(ok_list)/(tot or 1):.0f}%) | 最低 {min(ok_list)} | 最高 {max(ok_list)}")

    # 内存/swap
    mems = [s["mem_free"] for s in recent if "mem_free" in s]
    swaps = [s["swap"] for s in recent if "swap" in s]
    if mems:
        print(f"💾 内存: 可用均值 {sum(mems)/len(mems):.0f}GB (区间 {min(mems):.0f}-{max(mems):.0f}) | "
              f"swap 均值 {sum(swaps)/len(swaps):.1f}GB (峰值 {max(swaps):.1f})")

    # 节点在线率
    node_seen: dict[str, list[bool]] = defaultdict(list)
    for s in recent:
        for name, st in s.get("nodes", {}).items():
            node_seen[name].append(st == "online")
    if node_seen:
        print("🌐 节点在线率:")
        for name, vals in sorted(node_seen.items()):
            print(f"   {name}: {100*sum(vals)/len(vals):.0f}% ({sum(vals)}/{len(vals)} 快照)")

    # 常驻稳定性(watchdog)
    res = parse_resident_events(DAYS)
    if res:
        print("🔁 remote_resident 稳定性(近7天):")
        for node, st in sorted(res.items()):
            total = st["ok"] + st["fail"]
            print(f"   {node}: 补齐 {st['ok']} | 失败 {st['fail']} ({100*st['fail']/total:.0f}% 失败率)")

    print(f"\n   生成: weekly-report.py @ {datetime.now():%H:%M} | 数据源 status-history.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
