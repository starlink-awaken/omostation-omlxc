---
schema_version: operations-note/v1
status: active
lifecycle: history
owner: xiamingxing
created: 2026-09-01
last-reviewed: 2026-09-01
title: Tailscale 僵尸态复发 (2026-08-25 同款病理)
---

# Tailscale 僵尸态复发复盘（2026-09-01）

## 症状

- 排查初期误判 Mac mini "全节点失联"（bonjour 名解析失败误导——tailscale 节点
  本就不用 bonjour 名，应直接 ping 100.x 地址）
- `tailscale status`（不带 --socket）→ `failed to connect to local tailscaled`
- T3-02 P3 mesh 服务化被阻塞

## 真相（修正诊断）

**daemon 一直活着**——brew daemon 监听 `/var/run/tailscale.brew.sock`，CLI
默认连 `/var/run/tailscaled.socket` → "CLI 连错后端" 假死。与 2026-08-25
复盘原话一字不差：**"CLI 不带 --socket 连错后端"**。

- `tailscale --socket=/var/run/tailscale.brew.sock status` → **Mac mini active**（relay hkg）
- ping 100.99.210.78 通；SSH 通（uptime 9d）
- utun0 RUNNING + 100.x 路由 = **真实工作态**，非残留假象（初判"僵尸接口"为误诊）
- `sudo launchctl load` 报错 `5: Input/output error` 的真因：服务已在运行，重复加载当然失败

**教训（本 agent 自身复现了认知陷阱）**：不查 socket 变体就断言 "daemon 死"——
8/25 已付过一次学费的病理，本次排查第一轮又踩。socket 钉死必须成为肌肉记忆。

## 病理（与 2026-08-25 复盘完全同构）

| 层 | 表象 | 真相 |
|----|------|------|
| 接口 | utun0 RUNNING + 100.68.80.44 | daemon 死后残留，无数据流 |
| 路由 | `100.64/10 → utun0` 在路由表 | 同上 |
| 服务 | plist 存在且 KeepAlive=true | launchd 从未加载（Loaded: false）——KeepAlive 只对已加载服务生效 |
| 进程 | CLI 报 pid 590 | 进程早已不存在（过时信息） |

**"表象活着、数据流死了、无一处主动报错"** —— 8/25 复盘病理原话，本次一字不差复现。

## 根因链

1. 直接根因：`com.tailscale.brew.plist` 未被 launchd 加载（何时卸载不可考——8/25 清僵尸操作或 brew 升级）
2. 放大器 A：utun/路由残留制造网络假象 → 无告警
3. 放大器 B：无任何定期产出断言（omlxc `run_direct_doctor` 有 tailscale 检查但从未被调度）
4. 放大器 C：`~/.config/omlxc/config.toml` 的 executable 指向版本化 Cellar symlink（brew upgrade 即断链）

## 处置（2026-09-01，全部完成）

1. ✅ 心跳脚本 `bin/health/tailscale-heartbeat.sh`：**socket 钉死**（--socket=/var/run/tailscale.brew.sock）
   + status --json 产出断言 + 僵尸/错socket 检测；修正后首跑全绿
   （ok=true, peers=4/2 online, macmini_online=true）
2. ✅ 用户级 LaunchAgent `com.omostation.tailscale-heartbeat`（600s 间隔，RunAtLoad）
3. ✅ config.toml executable → `/opt/homebrew/bin/tailscale`（brew 稳定 symlink）
4. ✅ 连通验证：ping + SSH Mac mini (uptime 9d) 全通——**无需 sudo 恢复，daemon 本来就在跑**
5. ⏭ T3-02 P3 mesh 注册：解除阻塞，Mac mini 在线即可执行

## 治理条款沉淀（候选）

- **网络层产出断言**：心跳必须断言数据面（status --json / ping），接口与路由表存在性不可作为存活证据——与 8/25 "心跳不证明产出为真" 条款合并为通用规则
- **KeepAlive 陷阱**：launchd 服务"配置了 KeepAlive"≠"会自动拉起"——前提是已 load；健康检查应断言 `launchctl list` 里有 Label
