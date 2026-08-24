# 2026-08-24 · 算力体系复盘四线交付

> 背景: 上 session 末尾三次请求"算力体系全面复盘"均因 API 400 中断, 本轮冷启动
> 重做复盘并按 A1/A2/B/C/D 四线全面落地。复盘起点数据(15:00 full-status):
> `Placement 0/32 全灭` + 双 remote 节点离线 23h/1d + swap 15.3GB。

## D · remote 节点离线根因(先破案)

`tailscale status`: mac-mini / xia-y7000p 均 `tx>0 rx=0` + relay hkg 无直连
→ **设备物理离线**(关机/断网), 非 tailscale 故障, 软件不可修 → 转为 A2 告警。

## A1 · "0/32 全灭"口径误报

- 症状: full-status 报全灭, 实测同一时刻 oMLX App `model=coding` 生成正常。
- 根因: `available=False` 混杂两种成因 — `fresh=True`(探测确认不可用, 真问题)
  vs `fresh=False`(探测超时/过期 `_fail_stale`, 口径未知)。消费方一律当实锤展示。
- 修复: `full-status.sh` / `pipeline-watchdog.sh` 区分
  `全灭(探测确认)` 与 `探测过期(不代表不可用)` 两档输出; watchdog WARN 只报前者。
- 效果: `16/32 可用 + ℹ️ 探测过期: coding-fast, mythos-fast` — 不再把瞬态当实锤。

## A2 · remote 节点离线告警

- 症状: 离线 23h/1d 只能手动 full-status 被动发现, 无任何通知。
- 修复: `pipeline-watchdog.sh` 新增 tailscale 节点在线检测段 —
  DNSName/HostName 子串匹配(实测 Peer key 是 nodekey:xxx 且 HostName 可能是
  中文'夏明星的Mac mini', 不能拿 key 当主机名), osascript 桌面通知, 30min
  去重状态落盘 `~/.config/omlxc/node-offline-notified.json`。
- 验证: 实测检测出 `mac-mini、xia-y7000p` 并成功弹通知(rc=0)。

## B · LM Studio 侧保活断链(两层根因)

**根因一(daemon, 真凶)**: backend 级探测预算 `min(max(interval,0.1),5.0)` 硬
clamp 5s, 而 LM Studio 一次完整探测结构性需要 SSH 控制通道×2(discover 与
list_models 各调一次 `_list_models_with_control`, 实测 0.5s/次) + 就绪探测
chat 独立超时 `_PROBE_CHAT_TIMEOUT=5.0s` = 数学上 6s+ 必撞 5s → 只要目标模型
loaded, 每轮探测被掐死 → `_fail_stale` → 全 placement 误判 stale, 路由永远
不敢选 LM 兜底。SSH 通道本身实测 0.497s 完好(上 session 的 SSH 密钥加固无辜)。
- 修复: `composition.py` 探测预算改 `max(5.0, interval)` — 生产 interval=10s
  下预算 10s; TDD: `tests/unit/test_probe_budget.py`(先红后绿), 852 unit 回归全过。
- 效果: daemon 重启后 **16/32 → 29/32**, 本机 LM Studio 全部 placement
  `fresh=True + available=True`(剩 3 个为离线 remote 节点的)。

**根因二(保活面缺口)**: `scenario-warm-keep.py` 只温 oMLX App(8000),
`remote-resident-maintain.py` 只管 remote 节点 — 本机 LM Studio 的
qwythos-9b(mythos 系本机兜底) TTL 1h 到期卸载后无人拉起。
- 修复: WARM_TARGETS 增加 base_url 维度, 纳入 qwythos(19GB, 同一套内存红线
  `free >= 19+8GB`, 紧张时 SKIP-MEM 如实降级 — 2026-08-23 swap 事故教训)。
- 验证: 实跑输出 `OK: qwythos-9b-claude-mythos-5-1m-mlx 已温着
  (mythos 本机 LM 兜底, TTL 到期自动拉回)`。

## C · free 池 openrouter 静态清单早退(三层问题)

1. **静态清单停摆**: `MODEL-BREW-OPENROUTER-FREE.yaml` synced_at=2026-08-09,
   实测当日 openrouter 真免费 chat 模型已 19 个(含 z-ai/glm-5.2:free,
   nvidia/nemotron-3.5-lightning:free 等), 半个月漂移无人知晓。
2. **发现闭环是死代码**: 今晨新建的 `free_pool.py` 顶部漏 `from pathlib
   import Path`, FreePoolScanner 实例化即 NameError, gateway 的 try/except
   吞成 debug 日志 — "闭环"从未转过一圈(fabric 问题再一例)。
3. **刷新能力缺失**: scanner 只做源发现(发事件), 不做清单刷新。
- 修复: 补 Path import; 新增 `refresh_openrouter_free()`(拉 API → 过滤
  真免费纯 text 输出, 排除 lyria 音乐/content-safety 审核, 保留
  openrouter/free 官方路由 → 与 yaml diff → dry-run/--write 落盘,
  steward=free-pool-refresh); CLI 子命令 `free-pool scan|refresh [--write]`;
  `tests/test_free_pool.py` 10 用例全绿。
- 效果: yaml 5→19 模型(+120/-17), gateway 重启后 `list --ssot` 全部
  `in=$0 out=$0 quota=100%` 上线。

## 交付清单

| 仓 | 文件 | 内容 |
|---|---|---|
| omlxc | `src/omlxc/daemon/composition.py` | 探测预算 max(5.0, interval) |
| omlxc | `tests/unit/test_probe_budget.py` | 预算回归测试(2 用例) |
| omlxc | `scripts/full-status.sh` | 口径区分 探测确认/探测过期 |
| omlxc | `scripts/pipeline-watchdog.sh` | WARN 口径收紧 + 节点离线告警段 |
| omlxc | `scripts/scenario-warm-keep.py` | WARM_TARGETS +base_url, 纳入 qwythos |
| aetherforge | `free_pool.py` / `cli.py` / `test_free_pool.py` | Path import 修复 + 刷新能力 + CLI + 10 测试 |
| ecos | `MODEL-BREW-OPENROUTER-FREE.yaml` | 5→19 免费模型, synced 2026-08-24 |

## 遗留与后续

- remote 节点恢复在线后, `remote-resident-maintain` + A2 告警自动闭环,
  mythos-fast 的 remote placement 随之恢复(当前全灭是节点离线的直接后果)。
- free-pool refresh 目前是手动 CLI; 若要常态化, 建议挂 gateway 现有健康
  循环(每周一次 dry-run 报漂移, 写盘仍人工) — 未做(YAGNI, 待运营节奏定)。
- daemon stats 口径存疑(32930 请求 vs 71.8万 prompt tokens, 均值 22 tok/req)
  — 疑似只统计部分模型, 留待下次排查。
