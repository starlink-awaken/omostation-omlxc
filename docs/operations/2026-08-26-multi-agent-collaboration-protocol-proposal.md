# 多 Agent 共享工作区协作协议 · 提案 v0.1（2026-08-26）

> 依据: 2026-08-25 全天三类互踩事故实证(成本 ~15-20% 工时), 以及既定
> 偏好"多 agent 协作机制需文档化固化并持续迭代"。本文为**提案**,
> 待用户裁决后由治理流程收编为正式标准。

## 一、事故实证(2026-08-25 当日样本)

| 类型 | 实例 | 频次 | 直接损失 |
|---|---|---|---|
| **checkout 拖回** | 共享主 worktree HEAD 被切到旧提交, 当日提交被绕过 | 3 次(omlxc) | 3 次复原 + 告警机制建设 |
| **文件回写拉锯** | `_llm_helper.py` 双方互覆写(sys.path 方案之争) | 3 次 | 双方各丢 3 轮修改, 一方被迫撤出 |
| **分支漂移断档** | 主 worktree 切到无运行时文件的分支, mail-daemon 空转 8h | 1 次(8h 断档) | 认知产出丢失 8h + 固化治理 |

**间接成本**(最大): 排障时"磁盘文件又变了"引入假设污染 —
"0.8s 空转之谜"追了 3 轮才确认是并发回写, **排障最大摩擦不是技术
是并发写**。

## 二、已落地的防线(本提案的现行部分)

| 防线 | 载体 | 状态 |
|---|---|---|
| 正朔裁决 | 远端 GitHub PR 流为正朔, 本地归档分支保全 | ✅ 已落地(PR #2184) |
| checkout 告警 | watchdog detached HEAD 5min 巡检 | ✅ 已落地(实战抓到 1 次) |
| 运行时固化 | runtime/ssot-stable 副本, plist 改指 | ✅ 已落地 |
| 产出断言 | mail-daemon 连续 0 产出告警 | ✅ 已落地 |
| 探测健康断言 | watchdog incompatible 连续 3 轮告警 | ✅ 已落地(630caa4) |

## 三、提案条款(待裁决)

### 条款 A: 活跃区登记(写入协调, 短期)

多 agent 共享的可变文件区(`bin/ssot/` 等)建立**轻量活跃区登记**:
- 编辑前查 mtime 是否新于自己上次读(本地 diff 检测)
- 或在 `.omo/state/` 登记当前 owner + 预计时长
- **违规成本为零, 依赖自觉 + 事后追溯**(与现行 git 纪律同强度)

### 条款 B: 常驻运行时与工作区强制解耦(中期, 推荐先做)

今日"运行时固化"的**通用化**:
> 一切 launchd/cron 常驻任务不得直接指向共享主 worktree 内的文件 —
> 必须经稳定副本(`runtime/*-stable/`)或安装产物(uv tools)。

配套: 稳定副本同步纪律(变更里程碑手动 cp, 或 Makefile target)。

### 条款 C: agent 身份标识(低成本, 立即可做) — ✅ 已落地 (2026-08-25)

**事故实录**: 全局 `~/.gitconfig` 的 `[user]` 段在 08-21 19:13 被改成
`test/test@test`(疑似某 agent 会话或 gitbutler 误写)。主仓靠 local 配置
顶住(漏网 95 commit), omlxc 无 local 配置全军覆没(108 commit 全 test)。

**已落地修复**:
1. 全局身份恢复为 `xiamingxing <234556587+starlink-awaken@users.noreply.github.com>`(主仓 local 为铁证)
2. omlxc 仓补 local user 配置(与主仓同款, 双保险 — 全局再被改也顶得住)
3. agent 提交姿势: 并行 agent 若需区分身份, 用 per-commit 标识
   `git -c user.name="xxx-agent" -c user.email="agent@local" commit ...`,
   不许改全局/仓库级 config

**历史污染**: 已推远端的 test 身份 commit 不 rewrite(重武器, 违反
submodule 纪律), 接受历史脏、保未来净。

## 四、裁决请求

- 条款 A/B/C 各自: 采纳 / 修改 / 驳回
- 收编路径: omostation 治理标准(.omo/standards/) or 保持 omlxc 运维提案

---
*本文由 2026-08-25 会话(老王)产出, 事故数据全部当日实证, 可复核。*
