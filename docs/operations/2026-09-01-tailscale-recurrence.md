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

- Mac mini (100.99.210.78) / y7000p (100.64.43.36) 全节点失联
- `tailscale status` → `failed to connect to local tailscaled`（socket `/var/run/tailscaled.socket` 不存在）
- T3-02 P3 mesh 服务化被阻塞

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

## 处置（2026-09-01）

1. ✅ 心跳脚本 `bin/health/tailscale-heartbeat.sh`：status --json 产出断言 + **僵尸接口检测**（daemon 失败 + utun 挂 100.x → `zombie_interface: true` + 修复指令）；首跑即逮住本次僵尸
2. ✅ 用户级 LaunchAgent `com.omostation.tailscale-heartbeat`（600s 间隔，RunAtLoad）——防复发机制落地
3. ✅ config.toml executable → `/opt/homebrew/bin/tailscale`（brew 稳定 symlink）
4. ⏳ daemon 恢复（需 sudo，用户手动）：`sudo launchctl load -w /Library/LaunchDaemons/com.tailscale.brew.plist && tailscale up`
5. ⏳ 恢复后：验证 peers + SSH Mac mini + T3-02 P3 mesh 注册

## 治理条款沉淀（候选）

- **网络层产出断言**：心跳必须断言数据面（status --json / ping），接口与路由表存在性不可作为存活证据——与 8/25 "心跳不证明产出为真" 条款合并为通用规则
- **KeepAlive 陷阱**：launchd 服务"配置了 KeepAlive"≠"会自动拉起"——前提是已 load；健康检查应断言 `launchctl list` 里有 Label
