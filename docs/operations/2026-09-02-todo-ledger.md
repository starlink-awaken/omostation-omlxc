# omlxc 待办台账 (SSOT)

> 建立: 2026-09-02 · 维护者: 老王 · 勾选状态以本文件为准
> 背景: 主仓 `.omo/debt/items/` 下的 5 条 OMLXC-* 条目因主仓共享 worktree 被并行
> 机制清理而丢失, 台账 SSOT 移至本文件(omlxc 仓内, 随 git 推送保全)。
> 主仓有干净窗口时可再回写 debt 体系。

## Open

| ID | 事项 | 严重度 | 备注 |
|---|---|---|---|
| OMLXC-PMSET-DISPLAYSLEEP | mac-mini `sudo pmset -a displaysleep 5` | low | 需 mini 的 sudo 密码, SSH 免密进得去但提权进不去; 差用户一次手工执行 |
| ~~OMLXC-DAEMON-STATS-CALIBER~~ | ✅ 已解决(8-24 0dc3937 已实现分流) | - | usage-stats.sh 已含"真实 vs 保活"分流(判据 avg_completion>5), 实测 真实2965(3.8%) vs 探测75934; 台账条目系排查滞后 |
| OMLXC-KEEPALIVE-CONSOLIDATION | 保活三轨归一 | medium | **第一步完成(9-03)**: coding/qwen-3.8-27b resident=true, daemon 原生 reconcile 已自动拉起(实测 loaded=True×2, 内存窗口 25.2GB); 48h 观察后退役 scenario-warm-keep 本机目标 |
| OMLXC-SHARED-WORKTREE-RISK | 主仓共享 worktree 并行风险 | high | 2026-09-02 三撞: 提交被 checkout 甩掉(reflog 恢复)/台账被 clean; 短期靠"提交即 push"; 治本待 M2 git 收口 |

## Resolved (2026-09-02)

| ID | 事项 | 证据 |
|---|---|---|
| OMLXC-OLLAMA-LOOPBACK | Ollama 收紧 loopback | UI 自动化关闭 Expose 开关, 实测 `127.0.0.1:11434` + API 正常; 0.33.2 app 不理会 launchctl setenv |
| OMLXC-WHISPER-LAST-MILE | y7000p whisper 收官 | 根因=HF 无镜像下载卡死 → `setx HF_ENDPOINT=https://hf-mirror.com`; 修复 session 0 print 无 stdout 异常; 计划任务 `OllamaWhisperHTTP` ONLOGON 常驻; MBP→tailnet /asr 0.4s |
