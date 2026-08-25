# MBP 任务全景图谱 · 五层体系 + 全谱任务台账机制（2026-08-25 抢救落盘）

> **来源**: 本文档从 2026-08-25 晨 session（`d45519fe`）聊天记录中抢救落盘。
> 该轮完成了全机定时任务/后台任务/开机启动任务的全面探查（cron + launchd +
> 登录项 + agent 状态文件，含 Hermes / Claude 生态 / opencode），成果此前仅
> 存在于会话 jsonl 中未落盘——上下文 `/clear` 后即失明，违反"工作必须积累"
> 原则，特此固化为文档。
>
> **探查期关键发现**: Hermes 全线休眠 3 个月、opencode mcp-health 死 3 个月、
> foundry 半死于 08-07、omostation 四个新生任务脐带未接（`.omo/logs` 不存在）。

## 五层任务图谱

### 第一层 · 算力底座（6 任务）——🟢 2026-08-24 已治理
daemon / gateway / watchdog(300s) / lmstudio / ollama / whisper(远程)
——心跳✅ 预算✅ 探针✅（治理细节见 `2026-08-24-compute-fabric-four-fixes.md`）

### 第二层 · omostation 治理（~30 任务）——核心生产层

| 分组 | 任务 | **意义与作用** |
|---|---|---|
| **信号流** | signal-poller(常驻)、signals(5min)、watch-dispatch(每分钟) | 生活的原始信号采集→事件化；watch-dispatch 是文件变更的事件驱动替代（FDA 权限绕行） |
| **常驻自治** | resident ×5(2min)、agent-tick、mail-daemon、cron-service | 五角色事件自治（sediment/decision/execute/monitor/heartbeat）；邮件感知（数字大脑 P0 前哨，模型刚修） |
| **治理扫描** | governance-scanner(30min)、meta-doctor、anti-corrosion、remediation | GaC 防腐体系：漂移检测→修复建议→自动修复 |
| **健康度量** | system-health-check(日)、UHS、north-star(周) | 系统健康→统一健康分→北极星价值度量 |
| **审计闭环** | audit-rollout(周)、async-audit、check-convergence、coordination-backup | 多仓审计、异步核对、收敛检测、协同存储备份 |
| **知识沉淀** | knowledge-foundry、kos ingest(周日)、session-brief | 知识铸造、文档域 KOS 摄取、会话简报 |
| **编排节奏** | agent-workflow status、governance-evolution×2、debt/mof-drift/mof-state-bridge(周) | P74 工作流心跳、治理演化、债务刷新、MOF 漂移桥接 |
| 🔧 脐带未接 | remediation / anti-corrosion / UHS / north-star（探查当轮新增） | `.omo/logs` 目录不存在 → 从未落地（纸面任务） |

### 第三层 · 生活域（~12）——🟢 活跃
驾驶舱系列（domain-sync / session-brief / signals-rotate / bridge-refresh）+
卫健委系列（kems / controller / ocr / predictor）+ kos ingest

### 第四层 · Agent 工具层（该轮新发现）——🟡 半死不活

| 体系 | 任务 | 状态 | 意义 |
|---|---|---|---|
| **Claude Code** | 8 种事件 hook（SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / TaskCreated / SessionEnd / ConfigChange / PostToolUseFailure） | ✅ 活（每次会话都在跑：ContextReduction / TaskGovernance / WorkCompletionLearning / EventLogger...） | **事件驱动任务层**——会话治理的核心机制 |
| Claude Desktop | MCP：MCP_DOCKER、cockpit | ✅ | MCP 非定时，按需 |
| **opencode** | model-scheduler(08-03)、mcp-health(**05-15 死**)、upgrade-omo | 🟡 | 模型调度（model-scheduler.sh 的 launchd 在册）、MCP 健康检查已死 3 月 |
| **Hermes** | event-watcher(**05-29 死**)、outbound_relay(07-31)、codexbar quota(05-28) | 🔴 **全线休眠 3 个月** | 曾经的 agent 网关/quota 中继——但 settings.json 的 `hermes_model` 引用还在（引用 vs 运行矛盾，待裁决） |

### 第五层 · 第三方（~10）——⚪ 待用户认领
pg/neo4j（僵尸）、猎豹/ToDesk/CleanMyMac/docker/Google/ClashX helper + 登录项 5

## 抓手设计：「全谱任务台账」机制

**哲学：扩展现有 SSOT，不建平行体系**——挂进 omostation 的 `.omo/state/`（P74 同级）：

```
.omo/state/task-registry.yaml     ← 台账 SSOT（每任务: id/体系/频率/载体/意图/心跳证据路径/预期周期）
bin/gac/task-inventory.py         ← 审计器（只读采集: cron+launchd+登录项+agent状态文件）
                                      判活规则:
                                        ① 心跳证据 mtime > N×周期 → ⚠️ DEAD
                                        ② 有配置无日志产物 → ⚠️ PAPER(纸面/脐带未接)
                                        ③ 引用存在但状态文件休眠 → ⚠️ DORMANT(如 Hermes)
输出: omo task inventory 一屏 / 并入 full-status 心跳段
节奏: 挂现有 governance-scanner(30min) 或日批, 不新增调度槽
```

**这个抓手一次性治四类病**：纸面任务（探查当轮的坑）、静默死（free_pool 的坑）、
僵尸休眠（Hermes/pg）、以及"你不知道自己有什么"（mail-daemon 的坑）。

## 执行清单（探查轮结论原样保留）

| 级 | 动作 |
|---|---|
| **P0 当时计划** | ① `mkdir .omo/logs`（4 新生儿复活）② cron 备份 + 砍 signal-poller 死轨 ③ 重修 `_llm_helper` 的 glm 修复（被并行机制二次回滚）④ gen_index 搬正式目录 |
| **P1 台账落地** | `task-registry.yaml` 初版（审计数据灌入）+ `task-inventory.py` 审计器 + 挂 governance-scanner |
| **P2 用户裁决** | Hermes 退役 or 复活（settings 引用同步改）/ pg/neo4j / 猎豹/ToDesk / cockpit/agora/cron-service / mcp-health 复活 |

## 落盘时的现状核对（2026-08-25 09:3x）

- 上表 P0 各项**是否已在原 session 执行未得到确认**（会话在执行前被截断/重启），
  需按台账逐项核对后再勾销，不预设已完成。
- 同轮关联交付：`pipeline-watchdog.sh` tailscale 段 `"Peer": null` 空指针修复
  （该 bug 导致节点离线告警段反复崩溃，见当日 watchdog.log）。
- 心跳机制有效性再验证：今晨 gateway 被 SIGTERM 重启期间，`free_pool scan`
  心跳如实抓到 50min 停摆窗口——机制立功第二次。
