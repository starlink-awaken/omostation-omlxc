# Gateway 进程内 discover 病 · 排障接力笔记（2026-08-25）

> 状态: **未修**。病根未定位, 本文档是排障证据链, 供下一棒(并行 phase6 agent
> 或后续 session)接力。症状影响: `bin/ssot/_llm_helper.py` 的进程内
> `ModelGateway.create()` 实例 discover 后 registry 为空 → 任何 model
> (本地 qwen-3.8-27b / 云端 glm-4.7-flash)都解析失败 → llm_ask 慢速返 None
> → mail-daemon LLM 层失明。

## 症状与实证

- `mail-daemon.err` 持续刷: `[ModelGateway] oMLX App 模型不可解析或端点自指:
  {mythos-fast|qwen-3.8-27b|...}` → LM Link 兜底
- **launchd 版 gateway(4000端口)正常**: log 里对 omlxc 的模型拉取 200,
  带鉴权客户端拿到非空模型表(14:43 200 1821B 实证)
- **进程内版(llm_ask)不正常**: 两个实例两种命运
- llm_ask 单发 48s 返 None(无论 model 指定什么)

## 已验证排除的假设(下一棒别重走)

| 假设 | 验证方式 | 结果 |
|---|---|---|
| ~~/etc/hosts 污染 controlplane~ (无关此病但顺手排了) | PTR 反查=官方式兰克福真 IP | 排除 |
| ~~shell 代理劫持 httpx~ | NO_PROXY 有 100.64.0.0/10 但 httpx 不认 CIDR(已在 maintain 脚本 trust_env=False 修复) | 与此病无关 |
| ~~AETHERFORGE_PROJECT_DIR env 缺失导致 _paths 解析偏~ | 带 env 跑 llm_ask 仍 48s None | **排除** |
| ~~gateway 时序(discover 未跑完)~ | launchctl kickstart 重启 gateway 后 llm_ask 依旧失败 | 排除(进程内实例与 launchd 实例独立) |
| ~~无鉴权 curl 得 0 模型=registry 空~ | log 实为 401(未带 key), 带鉴权客户端 200 | **视图陷阱, 勿重蹈** |

## 下一棒建议查的点

1. `_llm_helper._get_gateway()` 的 `ModelGateway.create()` 与 launchd 版
   `cli.py` 装配路径的 **diff**: create() 默认参数是否漏了 m1_model_dir /
   credentials / discover 调用(cli.py:62-67 是 launchd 版的装配参考)
2. `ModelRegistry` 的模型注册入口(register_many)在进程内路径是否被触发
3. `~/Library/Logs/aetherforge-gateway.log` 有时间戳, `mail-daemon.err`
   **无时间戳**——排障时先给 err 行加时间戳再复现, 否则新旧症状无法区分
   (本轮最大排障摩擦来源)
4. credentials.db 路径解析在非 launchd cwd 下的行为

## 关联

- mail-daemon 已由并行 phase6 agent 改走 glm-4.7-flash 方案(云端免费),
  但同样被本总闸病挡住——**修好 discover 两个方案都能活**
- 2026-08-25 老王侧已提交: mail 链路显式 model 参数(f1d7d33, 后被并行
  agent 改写为其方案)——两个方案不冲突, discover 是共同前置
