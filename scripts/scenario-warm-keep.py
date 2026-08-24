#!/usr/bin/env python3
"""高频场景模型保活: 定期对指定的 omlx-app placement 发一个轻量请求,
触发 ensure_loaded(未加载则加载) 并因最近访问而不被 idle_ttl 淘汰。

背景 (2026-08-22): placement.resident=True 这个字段虽然在类型系统里
"活着"(PlacementTarget 引用它), 但真正执行"周期性检查+ensure_loaded"
的 reconcile 循环从未被 daemon 组装流程(composition.py)实例化启动 ——
和 remote_resident 是同一类"写好了但没接入"的模式。omlx-app 没有显式
卸载 CLI, 无法像 lm_studio 系那样用 --ttl 精确控制, 只能靠"定期戳一下"
的保活模式让高频模型不因 idle_ttl(1800s) 过期而冷启动。

刻意只覆盖 coding 这一个最高频的开发场景模型, 不做大范围预热 ——
多个大模型同时驻留是今天已实测过的真实风险(qwen3-coder-next+qwythos
同时驻留曾把内存打到 510MB), 保活的价值必须和内存压力仔细权衡。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

for _proxy_var in ("all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
    os.environ.pop(_proxy_var, None)

BASE_URL = "http://127.0.0.1:8000"
LM_URL = "http://127.0.0.1:1234"
MIN_FREE_GB = 20.0  # 比常规 12GB 红线更保守: 这是主动预热, 不是响应真实请求

# MBP 常驻内存总预算(2026-08-24 用户红线: "常驻模型做好内存控制, 别整崩溃"):
# oMLX 侧保活目标的 mem_gb 总和不得超此值, 超了启动即报警退出 —— 防止
# 未来往 WARM_TARGETS 里随手加目标把常驻集合撑爆(128G 物理内存扣除系统/
# Xcode/浏览器等 ~30-40G, 60G 常驻预算是安全上限; memory-sentinel 的 15G
# 可用红线兜底监控, idle_timeout 1800s 收割闲置)。
OMLX_RESIDENT_BUDGET_GB = 60.0

WARM_TARGETS = [
    # (backend_model_id, 逻辑用途, memory_gb, role, base_url) — role 决定
    # 探测/保活走 chat 端点还是 embeddings 端点(2026-08-22 实测: embedding
    # 角色模型打 /v1/chat/completions 会 400 "not an LLM/chat model")。
    # 2026-08-24 减配: vision(6GB) 移出保活 — 真实使用统计(9天391次真实
    # 请求)中 vision 全是探测流量(avg_compl=1.2), 常驻纯属浪费, 按需 JIT 即可。
    ("embedding", "embedding 场景默认模型(RAG 常用), resident 复核", 8.0, "embedding", BASE_URL),
    ("coding", "coding 场景默认模型, 已验证响应正常且稳定", 24.0, "chat", BASE_URL),
    ("qwen-3.8-27b", "chat 场景默认模型(真实流量91%走它), 已验证响应正常", 24.0, "chat", BASE_URL),
    # 2026-08-24 职责转移: mythos 的 LM 兜底已由 mac-mini 常驻(4.78GB 量化版,
    # remote-resident-maintain 维护)接管, oMLX 侧 mythos 为主路径 —— MBP 的
    # bf16(18.84GB)纯冗余且是 swap 压力大头, 移出保活并手动卸载。若 mac-mini
    # 48h 观察不稳, 加回此行即恢复: ("qwythos-9b-claude-mythos-5-1m-mlx",
    # "mythos 本机 LM 兜底", 19.0, "chat", LM_URL)
]


def real_free_gb() -> float:
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


def lms_generating_locally() -> bool:
    import json

    result = subprocess.run(
        ["/Users/xiamingxing/.local/bin/lms", "ps", "--json"], capture_output=True, text=True, timeout=15
    )
    try:
        rows = json.loads(result.stdout)
    except Exception:
        return False
    return any(r.get("status") == "generating" for r in rows)


def _probe(model_id: str, role: str, timeout: float, base_url: str = BASE_URL) -> int | None:
    """返回 HTTP 状态码, 网络层失败返回 None。role 决定走哪个端点
    (2026-08-22 实测: embedding 角色打 chat 端点会 400)。base_url 区分
    oMLX App(8000) 与本机 LM Studio(1234) 两个保活面。"""
    try:
        if role == "embedding":
            r = httpx.post(f"{base_url}/v1/embeddings", json={"model": model_id, "input": "hi"}, timeout=timeout)
        else:
            r = httpx.post(
                f"{base_url}/v1/chat/completions",
                json={"model": model_id, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
                timeout=timeout,
            )
        return r.status_code
    except httpx.TimeoutException:
        return None
    except Exception:
        return None


def is_warm(model_id: str, role: str, base_url: str = BASE_URL) -> bool:
    # oMLX App 没有 lms ps 那样的"已加载"状态查询, 用一次极短超时的探测
    # 请求判断是否已温着(冷启动会显著更慢, 这里只关心"能否快速响应",
    # 不消耗额外资源去验证生成内容本身)。LM Studio 侧同理: 模型已加载
    # 时短探测秒回, 未加载时 JIT 冷启动撑不进 3s 窗口。
    return _probe(model_id, role, timeout=3.0, base_url=base_url) == 200


def lm_loaded(model_id: str) -> bool:
    """LM Studio 侧已加载判断: lms ps 只列已加载模型, 命中 identifier/modelKey
    即 loaded。绝不走 HTTP 探测触发 JIT —— JIT 加载用 LM Studio 应用默认
    context length(qwythos 是满血 1M), 仅 KV cache 就把 swap 打爆过
    (2026-08-24 实测: 保活探测触发 JIT 后 swap 17→24.6GB)。"""
    result = subprocess.run(
        [str(Path.home() / ".lmstudio" / "bin" / "lms"), "ps", "--json"],
        capture_output=True, text=True, timeout=15,
    )
    try:
        rows = json.loads(result.stdout)
    except Exception:
        return False
    return any(
        r.get("identifier") == model_id or r.get("modelKey") == model_id
        for r in rows if isinstance(r, dict)
    )


def lm_load_capped(model_id: str, context_length: int) -> bool:
    """显式限 ctx 加载(lms load), 与 remote-resident-maintain 同一模式。
    c=64K: 权重之外的 KV cache 从 1M ctx 的几十 GB 收敛到 ~2GB 量级。"""
    result = subprocess.run(
        [str(Path.home() / ".lmstudio" / "bin" / "lms"), "load", model_id,
         "-c", str(context_length), "--ttl", "3600"],
        capture_output=True, text=True, timeout=180,
    )
    return result.returncode == 0


def main() -> int:
    # 常驻总预算校验(2026-08-24): oMLX 侧保活集合超预算直接拒绝启动 ——
    # 这是声明式红线, 让"往清单里加目标"这个动作在评审时就必须面对内存代价。
    omlx_total = sum(t[2] for t in WARM_TARGETS if t[4] == BASE_URL)
    if omlx_total > OMLX_RESIDENT_BUDGET_GB:
        print(
            f"ABORT-BUDGET: oMLX 保活集合 {omlx_total:.0f}GB 超总预算 "
            f"{OMLX_RESIDENT_BUDGET_GB:.0f}GB, 拒绝预热(减目标或提预算)"
        )
        return 1

    if lms_generating_locally():
        print("SKIP-BUSY: LM Studio 本地有模型正在 GENERATING, 让路")
        return 0

    # 按体积从小到大尝试, 每个目标独立用"此刻实时可用内存"判断 —— 避免
    # 一个大模型的内存需求把排在它前面、原本能轻松预热的小模型也一起
    # 卡死。每次真正触发加载后重新测量内存, 因为 omlx-app 的加载会实时
    # 占用内存, 后续目标的判断必须基于最新状态。
    for model_id, note, mem_gb, role, base_url in sorted(WARM_TARGETS, key=lambda t: t[2]):
        if base_url == LM_URL:
            # LM Studio 侧: lms ps 判温 + lms load 限 ctx 显式加载。
            # 不用 HTTP is_warm/_probe —— 那会触发 JIT 按 1M 满血 ctx
            # 分配 KV cache, 是 swap 爆炸的直接通道。
            if lm_loaded(model_id):
                print(f"OK: {model_id} 已温着 ({note})")
                continue
            free = real_free_gb()
            if free < MIN_FREE_GB or free < mem_gb + 8:
                print(f"SKIP-MEM: {model_id} 未温着, 需要 ~{mem_gb}GB 触发新加载, 可用 {free:.1f}GB 不足, 跳过")
                continue
            print(
                f"{'WARMED' if lm_load_capped(model_id, 65536) else 'FAIL'}: {model_id} "
                "(lms load, c=64K 防 1M KV cache 爆 swap)"
            )
            continue

        # oMLX App 侧: 先确认是否已温着 —— 这一步只是个短超时探测, 几乎
        # 不占内存, 必须排在内存预算检查之前。否则"已加载但此刻空闲内存
        # 偏紧"的模型会被误判为需要新触发加载而 SKIP, 白白浪费一次已有的
        # 热身。
        if is_warm(model_id, role, base_url):
            print(f"OK: {model_id} 已温着 ({note})")
            continue

        free = real_free_gb()
        if free < MIN_FREE_GB or free < mem_gb + 8:
            print(f"SKIP-MEM: {model_id} 未温着, 需要 ~{mem_gb}GB 触发新加载, 可用 {free:.1f}GB 不足, 跳过")
            continue

        status = _probe(model_id, role, timeout=90.0, base_url=base_url)
        print(f"{'WARMED' if status == 200 else 'FAIL'}: {model_id} status={status}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
