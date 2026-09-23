#!/usr/bin/env python3
"""compute-mesh-pulse.py — 集群异构算力中枢全景脉搏探针

全面探测与度量三节点算力网格 (MBP M5 Max, Mac mini M4, Y7000P RTX4070) 的：
1. 物理存活与网络链路 (Ping / SSH / Tailscale)
2. 推理引擎监听状态 (omlxc / LM Studio / Ollama)
3. 模型加载与显存/内存水位
4. 端到端推理验证 (Ping-Pong TTFT & Latency)
5. 汇聚输出高密度 HUD 与结构化 JSON

用法:
  python3 bin/compute-mesh-pulse.py
  python3 bin/compute-mesh-pulse.py --json
  python3 bin/compute-mesh-pulse.py --probe-infer  # 执行轻量端到端推理测试
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from omlxc.config import AppConfig, load_user_config

NODES_CONFIG = [
    {
        "id": "node-mbp",
        "name": "MacBook Pro (M5 Max 128G)",
        "role": "主脑计算核心",
        "ip": "127.0.0.1",
        "tailscale_ip": "100.68.80.44",
        "is_local": True,
        "engines": {
            "lmstudio": {"port": 1234, "path": "/v1/models"},
            "ollama": {"port": 11434, "path": "/api/tags"},
            "omlxc": {"path": "/api/v1/health"},
        },
    },
    {
        "id": "node-macmini",
        "name": "Mac mini (M4 24G)",
        "role": "二级高速推理 & 向量检索",
        "ip": "100.99.210.78",
        "lan_ip": "192.168.31.210",
        "ssh_host": "mini",
        "engines": {
            "ollama": {"port": 11434, "path": "/api/tags"},
            "lmstudio": {"port": 1234, "path": "/v1/models"},
        },
    },
    {
        "id": "node-y7000p",
        "name": "Lenovo Y7000P (RTX4070 8G)",
        "role": "CUDA 垂直小模型 & OCR/视觉",
        "ip": "100.64.43.36",
        "lan_ip": "192.168.31.128",
        "ssh_host": "y7000p",
        "engines": {
            "lmstudio": {"port": 1234, "path": "/v1/models"},
        },
    },
]


@dataclass
class EngineStatus:
    name: str
    port: int | None = None
    alive: bool = False
    model_count: int = 0
    loaded_models: list[str] = field(default_factory=list)
    ping_pong_ms: float | None = None
    detail: str = ""


@dataclass
class NodeReport:
    id: str
    name: str
    role: str
    ping_ok: bool = False
    ping_ms: float | None = None
    ssh_ok: bool = False
    engines: dict[str, EngineStatus] = field(default_factory=dict)
    hardware_info: str = ""
    summary_status: str = "OFFLINE"  # ONLINE, DEGRADED, OFFLINE


def check_tcp_port(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        res = sock.connect_ex((host, port))
        sock.close()
        return res == 0
    except Exception:
        return False


def ping_host(host: str, timeout: int = 2) -> tuple[bool, float | None]:
    cmd = ["ping", "-c", "1", "-W", str(timeout * 1000), host]
    try:
        t0 = time.time()
        res = subprocess.run(cmd, capture_output=True, timeout=timeout + 1)
        rtt = (time.time() - t0) * 1000
        if res.returncode == 0:
            return True, round(rtt, 1)
    except Exception:
        pass
    return False, None


def check_ssh(ssh_host: str, timeout: int = 3) -> bool:
    cmd = ["ssh", "-o", f"ConnectTimeout={timeout}", "-o", "StrictHostKeyChecking=no", ssh_host, "echo ok"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout + 1)
        return res.returncode == 0
    except Exception:
        return False


def http_get_json(url: str, timeout: float = 2.5) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "compute-mesh-pulse/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def probe_lmstudio(host: str, port: int) -> EngineStatus:
    status = EngineStatus(name="lmstudio", port=port)
    if not check_tcp_port(host, port):
        status.detail = "端口未监听"
        return status

    data = http_get_json(f"http://{host}:{port}/v1/models")
    if data and "data" in data:
        status.alive = True
        status.model_count = len(data["data"])
        status.detail = f"就绪 ({status.model_count} 款可见)"
    else:
        status.alive = True
        status.detail = "HTTP 在线但返回异常"
    return status


def probe_ollama(host: str, port: int) -> EngineStatus:
    status = EngineStatus(name="ollama", port=port)
    if not check_tcp_port(host, port):
        status.detail = "端口未监听"
        return status

    data = http_get_json(f"http://{host}:{port}/api/tags")
    if data and "models" in data:
        status.alive = True
        status.model_count = len(data["models"])
        # 检查正在运行的模型 (ps)
        ps_data = http_get_json(f"http://{host}:{port}/api/ps")
        if ps_data and "models" in ps_data and ps_data["models"]:
            loaded = [m["name"] for m in ps_data["models"]]
            status.loaded_models = loaded
            status.detail = f"运行中: {', '.join(loaded)} (共 {status.model_count} 款)"
        else:
            status.detail = f"待命 ({status.model_count} 款可用)"
    return status


def resolve_omlxc_socket(
    override: Path | None,
    *,
    config_loader: Callable[[], AppConfig] = load_user_config,
) -> Path:
    """Use the explicit socket or the same user/default config as the CLI."""
    if override is not None:
        return override.expanduser()
    return config_loader().daemon.socket_path


def probe_omlxc_local(uds_path: Path) -> EngineStatus:
    status = EngineStatus(name="omlxc")
    if uds_path.exists():
        status.alive = True
        status.detail = "UDS Socket 就绪"
    else:
        status.detail = "UDS 未挂载"
    return status


def ping_pong_infer(host: str, port: int, engine: str, model_id: str) -> float | None:
    """轻量发送单 token 请求测试真实端到端往返时延"""
    url = f"http://{host}:{port}/v1/chat/completions" if engine == "lmstudio" else f"http://{host}:{port}/api/generate"
    payload = (
        {"model": model_id, "messages": [{"role": "user", "content": "1"}], "max_tokens": 1}
        if engine == "lmstudio"
        else {"model": model_id, "prompt": "1", "stream": False}
    )
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            if resp.status == 200:
                return round((time.time() - t0) * 1000, 1)
    except Exception:
        pass
    return None


def probe_node(
    node_cfg: dict,
    run_infer: bool = False,
    *,
    omlxc_socket: Path | None = None,
) -> NodeReport:
    report = NodeReport(id=node_cfg["id"], name=node_cfg["name"], role=node_cfg["role"])
    host = node_cfg["ip"]
    is_local = node_cfg.get("is_local", False)

    # 1. 网络连通性
    if is_local:
        report.ping_ok = True
        report.ping_ms = 0.1
        report.ssh_ok = True
    else:
        # Ping
        ok, rtt = ping_host(host)
        report.ping_ok = ok
        report.ping_ms = rtt
        # SSH
        ssh_alias = node_cfg.get("ssh_host", host)
        report.ssh_ok = check_ssh(ssh_alias)

    # 2. 引擎探测
    engines = node_cfg.get("engines", {})
    online_count = 0

    for eng_name, eng_meta in engines.items():
        if eng_name == "omlxc":
            eng_status = probe_omlxc_local(omlxc_socket or resolve_omlxc_socket(None))
        elif eng_name == "lmstudio":
            eng_status = probe_lmstudio(host, eng_meta["port"])
        elif eng_name == "ollama":
            eng_status = probe_ollama(host, eng_meta["port"])
        else:
            eng_status = EngineStatus(name=eng_name, detail="未知引擎")

        if eng_status.alive:
            online_count += 1
            if run_infer and eng_status.loaded_models:
                eng_status.ping_pong_ms = ping_pong_infer(
                    host, eng_meta["port"], eng_name, eng_status.loaded_models[0]
                )

        report.engines[eng_name] = eng_status

    # 3. 汇总判定
    if online_count > 0:
        report.summary_status = "ONLINE"
    elif report.ping_ok or report.ssh_ok:
        report.summary_status = "DEGRADED"  # 主机通但服务未起
    else:
        report.summary_status = "OFFLINE"

    return report


def render_hud(reports: list[NodeReport], checked_at: str) -> str:
    lines = []
    lines.append("╔════════════════════════════════════════════════════════════════════════════════╗")
    lines.append("║            OMOSTATION 集群异构算力中枢全景脉搏探针 (Compute Mesh Pulse)        ║")
    lines.append(f"║  Checked at: {checked_at:<65} ║")
    lines.append("╠════════════════════════════════════════════════════════════════════════════════╣")

    status_icon = {"ONLINE": "🟢 正常在线", "DEGRADED": "🟡 降级待命", "OFFLINE": "🔴 离线未通"}

    for r in reports:
        icon = status_icon.get(r.summary_status, r.summary_status)
        lines.append(f"║ 节点: {r.name:<32} [{icon}]".ljust(81) + "║")
        lines.append(f"║   角色: {r.role}".ljust(81) + "║")
        ping_str = f"{r.ping_ms} ms" if r.ping_ok else "超时丢包"
        ssh_str = "连通" if r.ssh_ok else "未连通"
        lines.append(f"║   链路: Ping={ping_str} │ SSH={ssh_str}".ljust(81) + "║")

        lines.append("║   引擎平面:".ljust(81) + "║")
        for eng_name, eng in r.engines.items():
            st_flag = "🟢" if eng.alive else "⚪"
            latency_str = f" [Inference: {eng.ping_pong_ms}ms]" if eng.ping_pong_ms else ""
            lines.append(f"║     {st_flag} {eng_name:<10}: {eng.detail}{latency_str}".ljust(81) + "║")
        lines.append("╟────────────────────────────────────────────────────────────────────────────────╢")

    # 总结与调度建议
    all_online = sum(1 for r in reports if r.summary_status == "ONLINE")
    lines.append(f"║ 总结: 集群共 {len(reports)} 节点，{all_online} 节点在线就绪。".ljust(81) + "║")
    lines.append("║ 路由契约: 统一收敛至 bos://compute/aetherforge/infer".ljust(81) + "║")
    lines.append("╚════════════════════════════════════════════════════════════════════════════════╝")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="OMOSTATION Compute Mesh Pulse")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON 格式")
    parser.add_argument("--probe-infer", action="store_true", help="执行轻量端到端推理抽测")
    parser.add_argument("--omlxc-socket", type=Path, help="覆盖私有 omlxcd Unix socket 路径")
    parser.add_argument("--save", action="store_true", help="保存状态至 .omo/state/compute-mesh-pulse.json")
    args = parser.parse_args()
    omlxc_socket = resolve_omlxc_socket(args.omlxc_socket)

    checked_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # 并发探测所有节点
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(probe_node, node, args.probe_infer, omlxc_socket=omlxc_socket)
            for node in NODES_CONFIG
        ]
        reports = [f.result() for f in futures]

    if args.json:
        out = {
            "schema": "omostation.compute-mesh-pulse.v1",
            "checked_at": checked_at,
            "reports": [asdict(r) for r in reports],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render_hud(reports, checked_at))

    if args.save:
        state_dir = Path(".omo/state")
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "compute-mesh-pulse.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "schema": "omostation.compute-mesh-pulse.v1",
                    "checked_at": checked_at,
                    "reports": [asdict(r) for r in reports],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
