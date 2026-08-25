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
