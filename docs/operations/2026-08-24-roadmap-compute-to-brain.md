# 算力底座 → 数字大脑执行层 · 全面规划（2026-08-24 晚）

> 背景: 本日完成算力体系四线修复+三机一池全员在线+制度层(DoD/心跳/预算/落盘)。
> 本规划回答"接下来全面落地什么"——核心论点: **今天的算力分层不是终点,
> 是数字大脑(用户既定愿景, P0=工作域)的执行器布局**。

## 算力分层 ↔ 数字大脑场景映射

| 数字大脑环节 | 执行器(已就绪) | 状态 |
|---|---|---|
| 邮件/文档文本感知(P0 主线) | MBP oMLX(coding/qwen-3.8 理解) | ✅ 常驻就绪 |
| RAG 嵌入/检索 | mac-mini ollama bge-m3(1.2G) | ✅ 模型就位, 48h 后迁移 |
| 重推理(文档起草/长生成) | mac-mini qwythos 4.78G 常驻 + 云池 | ✅ 常驻运行中 |
| 语音入口(会议/语音邮件) | y7000p faster-whisper(GPU) | 🔶 80%, 差一次本地执行 |
| OCR/视觉 | y7000p glm-ocr/qwen3-vl-4b | ⚪ 按需 JIT |
| 兜底/白嫖 | AetherForge 19 免费模型 | ✅ weekly 对账 |

## 阶段一 · 本周(收尾+激活)

1. **#6 48h 评审(周三)**: mac-mini 稳定性通过 → embedding 迁移(MBP 常驻
   56→48GB) + bf16 退役定案 → 算力底座宣告完工
2. **whisper 收官**: 用户在 y7000p 本地跑一次 `python asr_test.py`
   (C:\Users\xia, 用完整路径 Python313, Store stub 是坑) → 老王封装
   HTTP 服务 → 语音入口激活
3. **WindowServer 一条命令**: 用户在 mac-mini 执行
   `sudo pmset -a displaysleep 5`(显示器 5min 熄屏, 省 30% CPU 空转)
4. swap 终态确认(预期 <10GB)

## 阶段二 · 下周(数字大脑 P0 启动)

1. **email 感知引擎**启动(既定主线: Apple Mail + 网易邮箱大师 → LLM
   任务检测 → 文档起草 → 汇报闭环)——跑在阶段一的算力分层上
2. full-status 周报: status-history.log 落盘数据 → 周度趋势小结
   (节点在线率/常驻稳定性/真实使用量)
3. y7000p OCR 场景验证(第一份真实文档任务时)

## 阶段三 · 月度(自适应+进化)

1. **保活名单流量自适应**: usage-stats 的真实流量数据 → 自动调
   WARM_TARGETS(周期评审, 人批准)
2. Wan2.2 视频专线(4T 盘上权重就位, 有真实需求时激活)
3. 统一观测面: omlxc+aetherforge 的心跳/指标合并为一屏

## 治理原则(本周血泪固化, 长期有效)

- 一次一变量 + 48h 观察窗(二阶效应保险)
- 探测零副作用(探针钉轻量模型/显式限 ctx/卸载后不碰 API)
- 显示层是视图不是事实(验证看真接口或间接曲线)
- 自治机制必须有心跳(静默死=fabric 事故)
- 触发式决策(4T 搬迁/swap 自动收割等, 条件到才动)

## 当前台账快照

- 进行中: #6(48h)、#10(whisper 差一步)、#11(WindowServer 差 sudo)
- 今日提交: omlxc ~12 / aetherforge 3 / ecos 1, 测试 +12 全绿

## ✅ #6 结案 (2026-08-25 提前执行)

- **评审 PASS**: 150 次失败全是 qwen3.5:9b 下载期 502(08-24 00-17点, 预期噪音);
  稳态期 成功20/失败2 (91%), 全部自愈; uptime 覆盖评审窗, 内存 49% free
- **embedding 迁移完成**: MBP resident 除名(56→48GB) + mac-mini ollama
  bge-m3 常驻(remote_resident, /api/embed 维持), embed 1024 维直连实测通过
- **bf16 退役转正**: 08-24 的条件回滚条款解除
- **附带排障**: mac-mini 双 ollama 实例(直连落 GUI 版)、httpx no_proxy
  不认 CIDR 坑、tailscale 双后端(GUI 僵尸版清除)
- **算力底座: 宣告完工** 🎉 剩余: #10(whisper 差用户本地跑一次)、#11(差 sudo)
- 治理新债: 路由评分不感知 loaded(本地 JIT 兜底保留); daemon 3.4.0 升级
  后启用 config 侧 role 字段; mac-mini ollama 单实例统一(用户裁决)

## ✅ 阶段一全清 + 阶段二启动 (2026-08-25 午)

- **#10 whisper 完成本地验证**(老王 SSH 远程代跑): 脚本落 y7000p
  `C:\Users\xia\asr_test.py`, SAPI 合成语音 → faster-whisper tiny int8
  **CPU 2.4s 转写完美**("Hello world, this is a whisper test on the Y7000P.")。
  管线闭环 ✅; GPU 模式差一债: cudnn 9.24 `cudnnGetLibConfig` 符号加载
  失败(ctranslate2 4.6.0 + nvidia-cudnn-cu12 9.24 组合问题, 待版本对齐)。
  下一步: HTTP 服务封装(roadmap 既定)
- **#11 销账**: mac-mini `pmset -g` 显示 displaysleep 已 = 5(目标态已达成,
  无需 sudo)
- **阶段二·mail-daemon 路由病修复**: 邮件感知引擎(读邮件→LLM分类→任务
  提取→日报→草稿, 30min 周期)此前 auto-route 落 mythos-fast(本地 oMLX 只有
  mythos, 无 -fast)→ 每轮 LM Link 兜底。修复: `_llm_helper.llm_ask` 加
  显式 model 参数 + mail_agent 三处调用指定 qwen-3.8-27b(本地稳定主力,
  真实流量 91%)。gateway complexity_chains 低档链头配置病挂账 aetherforge 侧

## ⚠️ 事故记录(20:3x): 共享 worktree 被并行机制拖回旧提交

- 现象: full-status/usage-stats/scenario-warm-keep 三个当日脚本在磁盘上
  变回下午旧版(心跳段/预算红线/口径分流全部"消失")
- 根因: reflog 显示 `checkout: moving from main to b2c1af8` —— 某并行
  agent/机制将共享主 worktree 的 HEAD 切到旧提交(detached), 工作区随之
  回退。**已提交内容零丢失**(全部在 main), 一条 git checkout main 复原
- 教训: git 保护系统(M1/M2/M3)防 reset --hard, 但 "checkout 拖回旧提交"
  是另一条通道 —— 建议主 worktree 出现非 main detached HEAD 时告警;
  多 agent 共享主树期间, 每轮运维脚本执行前先 `git rev-parse --abbrev-ref HEAD`
  自检(成本一条命令)
- 恢复: git checkout main(f4f6e12), 三文件 grep 验证心跳/预算/分流齐全

## 📡 数字大脑 P0 全链路勘测 (2026-08-25 深夜)

**组件完备度: 100%** — 全链路六个环节全部存在且有实现:

| 环节 | 实现 | 状态 |
|---|---|---|
| 感知 | mail_daemon.py (30min, 稳定副本运行) | ✅ 今日修复认知层, 141 累计轮次 |
| 认知 | mail_agent.py (分类/任务提取, qwen-3.8-27b) | ✅ 今日修复, 2/5/7 任务连续产出 |
| 起草 | doc_generator.py | ✅ 今日实测 8任务4草稿 |
| 流程 | journey-runner.py + admin_scenes.py (9步状态机) | ✅ 存在且注册 |
| 汇报 | mail_sender.py (.eml 草稿) | ✅ 存在, 被状态机调用 |
| 确认 | 人工发送 (by design 安全边界) | ✅ "所有行动只生成草稿" |

**唯一断点: 任务→journey 的自动触发** — mail-daemon 提取的任务停在
briefing/草稿层, 进入 journey 流程目前靠人工启动(或有其他触发器未勘明)。
这是产品级决策: 自动串接 vs 人工确认后再入流程。
**建议**: 保持人工断点(安全), 但给 briefing 加"一键启动 journey"命令提示。

**安全边界(文件头明文)**: "所有行动只生成草稿, 不自动发送" — 正确,
勿在自动串接时破坏此边界。

## 🕐 周报数据管道贯通 (2026-08-25 夜)

- 勘测发现: full-status **从未挂调度**, status-history.log 断供一天
  (8/24 18:51 后零快照) — 脚本注释设计"数百快照覆盖数周"纯属纸面
  (纸面设计 vs 执行落地的又一例, 已入复盘病谱系)
- 修复: cron 每小时 :50 自动快照(crontab 备份 .backup-20260825-fullstatus),
  当场补跑一轮续上今日数据
- 管道全景: 每小时快照 → status-history.log → 周一 08:05 weekly-report.py
  自动出周报(placement/内存/节点/常驻四维)

## ✅ 数字大脑端到端 Demo (2026-08-26 凌晨, dry-run 级)

真实数据全链验证:
1. **感知** ✓ mail-daemon 稳定副本轮转(19:44 轮, 20封)
2. **认知** ✓ briefing 3 任务带 🚀 桥接行(真实邮件: JetBrains PR 讨论)
3. **桥接命令 shell 合法性** ✓ 毒数据(单引号 subject + \r sender)经
   shlex.quote 转义, bash 真跑成功(转义 bug 当场发现当场修: {!r} repr
   → json.dumps+shlex.quote)
4. **journey 状态机** ✓ dry-run 1 步达 terminal(exit 0)
5. **--live 真草稿**: 留给用户确认后执行(安全边界 by design)

**数字大脑 P0 五环节实测贯通**(感知→认知→起草→流程→汇报)。

## ✅ 断链大扫除 + 毕业礼正确阴性 (2026-08-26)

**用户点名的断链排查, 一并修**:
- kairon 挪窝(projects/ → projects/knowledge/)致 **10 个 editable pth 断链**
  — 批量 sed 修复, iris 等全系统复活
- omo-debt 死环境: 溯源 ADR-0412(已归并入 omo, 非失踪) → uv tool 卸载
- 全系统 editable 断链终检: **0 个 ✓**

**毕业礼链路的三个隐藏 bug 连修**:
1. iris 弃用警告污染 stdout → json.loads 炸(两处解析点剥离非 JSON 前缀)
2. _has_real_data 启发式缺 admin-inbox 的 mails/has_task 字段 → 恒 degraded
3. 修复后 LIVE 跑通: **degraded 消失 + has_task=false 如实阴性**
   (真实邮箱无任务邮件 → 正确不产草稿 — 数字大脑如实运转的证明)
- .eml 产出: 等真实任务邮件到达自然触发(生产邮箱, 不伪造数据)

**遗留**: journey 系 B 分支 422(SSOTProviderAdapter 请求体字段 — 走兜底
不影响功能, 第十层挂账); iris 已弃用应迁 cockpit(迁移债)。
