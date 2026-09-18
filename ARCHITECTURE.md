---
type: ssot
owner: governance-team
last_updated: 2026-09-18
last-reviewed: 2026-09-18
---

# omlxc — Architecture

> **Layer**: 本地算力织网 (local compute fabric)
> **Role**: 私有本地计算中心 — omlxcd 控制/数据平面 / Unix-socket 客户端 / Typer CLI / Textual cockpit
> **Stack**: Python 3.13, uv, Hatchling, Ruff, Pyright strict
> **Version**: 3.4.0 (ADR-0433) — see [`pyproject.toml`](pyproject.toml)
> **Health**: See local CI and runtime probes
> **SSOT**: 版本、VRAM 策略、模型健康登记以本项目 CI、运行时探针和 workspace governance SSOT 为准
>
> 系统全景参见：[`../../docs/PANORAMA.md`](../../docs/PANORAMA.md)

---

## 1. 内部架构

- 控制/数据平面: [`src/omlxc/daemon/`](src/omlxc/daemon/) (`omlxcd`, 私有 Unix socket)
- CLI: [`src/omlxc/cli.py`](src/omlxc/cli.py) (`omlxc fabric inspect/triage/vram/warm/compact`)
- 交互式 cockpit: keyboard-first Textual cockpit (TTY 下直接运行 `omlxc` 打开)
- 客户端: [`src/omlxc/client/`](src/omlxc/client/) (typed Unix-socket client; JSON/NDJSON 含 `schema_version` + `request_id`)
- 推理数据平面: [`src/omlxc/dataplane/`](src/omlxc/dataplane/) — DFlash 2 块扩散投机解码 / Radix 前缀缓存 / Paged KV
- 调度与网格: [`src/omlxc/scheduler/`](src/omlxc/scheduler/) / [`src/omlxc/mesh/`](src/omlxc/mesh/) (Priority QoS P0/P1/P2, 75% 阶梯 VRAM 准入)
- 旧基线: [`bin/omlx`](bin/omlx) + 32 legacy characterization tests (保持不动)

详见 [`docs/ARCHITECTURE-FABRIC.md`](docs/ARCHITECTURE-FABRIC.md)。

## 2. v3 边界 (status quo)

- 交互式 `omlxc` 打开 compute cockpit; 非 TTY 调用必须指定子命令。
- 全部 CLI/TUI 状态变更只走 `omlxcd` Unix socket; daemon 无对应端点的命令返回 typed `unsupported` 错误。
- 不改动既有本地服务、不接触真实硬件 (常规测试)、不替换 `/opt/homebrew/bin/omlxc`。

## 3. 项目入口

- 项目说明: [`README.md`](README.md) · 开发约定: [`AGENTS.md`](AGENTS.md) · 会话启动: [`CLAUDE.md`](CLAUDE.md)
- 治理: [`GOVERNANCE.md`](GOVERNANCE.md) · 运维记录: [`docs/operations/`](docs/operations/)
