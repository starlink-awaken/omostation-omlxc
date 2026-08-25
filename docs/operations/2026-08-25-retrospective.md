# 2026-08-25 全天工作复盘 · 从 session 回顾到数字大脑三线贯通

> 起点: 用户一句"帮我回顾一下这个项目近期 session 的工作"。
> 终点: 感知(邮件+语音)·算力(三机分层)·治理(台账+心跳)三线贯通, 四仓 21+ 提交。
> 本复盘重点: 战果账本 + fabric 病谱系 + 多 agent 协作失败模式与长效解。

## 一、时间线与战果账本

| 时段 | 战线 | 关键交付 |
|---|---|---|
| 上午 | session 回顾→图谱抢救 | 五层任务图谱从 jsonl 起死回生落盘(30 任务探查成果差点白干) |
| 上午 | 台账立制 | task-registry.yaml + task-inventory.py(首跑揪出 4 真实失活项), cron 挂载, PAPER→OK 闭环验证 |
| 中午 | tailscale 双后端破案 | GUI 僵尸版与 brew 正主共存→CLI 全查错实例; socket 钉死+僵尸清除, A2 恢复视力 |
| 下午 | #6 评审(提前) | 48h 数据评审 PASS(150 失败全是下载噪音); embedding 迁移(56→48GB)+bf16 退役; **算力底座宣告完工** |
| 下午 | 邮件引擎两度点火 | 第一次: 显式 model 参数(5任务/3草稿); 第二次: refresh 总闸修复(8任务/4草稿) |
| 傍晚 | 语音入口贯通 | SSH 远程部署+DLL注入+防火墙 Block 雷清除, **端到端 0.5s 推理**实测通过 |
| 深夜 | gateway 三层深修 | unix:// 白名单+SSOT 补名+接力文档三版; 撤出并行 agent 活跃区 |

**量化**: omlxc ×13 / 主仓 ×6 / aetherforge ×1 / ecos ×1 提交; 修复真实 bug
12+; 破案 6 起(双 tailscale/双 ollama/防火墙 Block/gateway refresh/命名断层/
路由病); 排除错误假设 10+。

## 二、fabric 病谱系(今日新病例)

今天的病全部落在"显示在跑≠真的在跑"谱系上, 且**三层嵌套**:

1. **mail-daemon 在跑但 LLM 认知层死透**(连续 N 轮"20封0任务"=分类全降级
   "未分类") — 最深一层: 表面健康, 核心功能从未工作过
2. **进程内 gateway registry=0 但 providers=22** — create() 只 register 不
   refresh, `_models` 缓存没人填 — "构造成功≠初始化完成"
3. **watchdog 节点告警在跑但查的是僵尸实例** — CLI 不带 --socket 连错后端

**病理共性**: 每层都有"部分工作"的表象(进程活/日志走/心跳有), 唯独核心
数据流断掉, 且**无一处主动报错**。这与 08-24 固化的"心跳制度"互补:
心跳证明"机制在转", 但**心跳不证明"产出为真"**。

**新治理条款候选**: 关键数据流加"产出断言"——mail-daemon 每轮报
"分类成功率"而非仅"处理 N 封"(连续 100% 未分类应告警); registry 初始化
后断言 len>0(空 registry 是异常态不是合法态)。

## 三、多 agent 协作失败模式与成本(今日重点样本)

### 失败模式清单

| 模式 | 今日实例 | 成本 |
|---|---|---|
| **checkout 拖回** | omlxc 三次被拖到旧提交(5a39ff8/5f9c715/a478a5c 被绕过) | 3 次复原+告警机制建设 |
| **文件级回写拉锯** | _llm_helper.py 三次互覆写(sys.path 方案之争) | 双方各丢 3 轮修改, 最终一方撤出 |
| **身份混淆** | 并行 agent 以 test<test@test> 提交, 混入主仓 staged | aaa202c 混入 6 文件 |
| **现场冲突** | 主仓/ ecos /多 worktree 同时施工, main 被占 | 同步操作被迫绕道 |
| **历史分叉** | 本地 main 与 origin/main unrelated histories(5932 vs 95) | **未解, 待裁决** |

### 成本估算

互踩直接消耗: 约 4-5 轮往返(复原/重写/验证), 约占今日总工时 15-20%;
间接成本: 排障时"磁盘文件又变了"引入的假设污染(0.8s 空转之谜追了 3 轮才
确认是并行 agent 回写所致——**排障最大摩擦来源不是技术是并发写**)。

### 长效解提案(供裁决)

1. **写入锁**(短期): 关键共享文件(_llm_helper 等)编辑前 flock 或在
   task-registry 登记owner; 改前 diff 检测"mtime 新于我上次读"
2. **分支隔离**(中期): 多 agent 各自 worktree+分支, 主 main 只允许
   merge——这正是 git-discipline skill 的方向, 但今天说明**执行不到位**
3. **正朔裁决**(立即): 主仓 unrelated histories 必须裁决——建议以远端
   (GitHub PR 流)为正朔, 本地 5932 提交做一次归档分支后重置对齐

## 四、方法论沉淀(今日有效打法)

1. **"视图≠事实"三连击**: 401 当空表/目录当驻留/log 无时间戳混流——
   每次都被它钓, 每次都靠"直击真接口"破
2. **对照实验法**: 手动 discover 24 vs refresh gather 0 的精确对照,
   A/B 双版本探针——把"玄学"变成"可判决命题"
3. **接力棒文档**: 排障到一半让位时, 证据链+已排除假设+下一棒起点的
   文档让上下文零损失交接(今天三版迭代, 本身就是多 agent 协作的正资产)
4. **SSH 远程代劳**: y7000p/mac-mini 的用户侧操作全部远程化(SAPI 合成
   语音/远程 DLL 注入/防火墙规则), 消除"等用户手动"的阻塞

## 五、遗留与下一步

| 级 | 项 | 归属 |
|---|---|---|
| **P0 裁决** | 主仓 unrelated histories(本地 5932 vs 远端 95) | 用户 |
| P1 | UDS transport feature(本地直连最后一公里) | aetherforge |
| P1 | refresh gather 空收之谜(0.8s 瞬回) | 并行 agent/下一棒 |
| P1 | whisper 常驻验证(schtasks onlogon 重启后) | 下次登录 |
| P2 | 产出断言制度(见 §二) | 治理 |
| 时间窗 | full-status 周报(下一首份完整周) | 周一 |

## 六、深夜追加: 运行时固化解耦 (第三类互踩事故的治本)

**事故第三形态**: mail-daemon 断档 8h — 主 worktree 分支被并行 agent 切到
不含 bin/ssot/ 的分支 → launchd 每 30min 空跑(No such file or directory)。
与 checkout 拖回(仓库内容回退)、文件回写(编辑冲突)并列为今日三类互踩。

**治本**: `runtime/ssot-stable/` 稳定副本(7 文件) + launchd plist 改指
(备份在 .bak-20260825-runtime-stable) — 运行时与工作区解耦,
同 aetherforge-final-ae3570f 部署思路。同步纪律见副本目录 README.md。

**通用原则(候选治理条款)**: 一切 launchd/cron 常驻任务不得直接指向
共享主 worktree 内的文件 — 必须经稳定副本或安装产物。
