# Gateway 进程内 discover 病 · 排障接力笔记（2026-08-25）

> 状态: **总闸已修一半**(2026-08-25 晚): `_llm_helper._get_gateway` 补
> `asyncio.run(registry.refresh())` 后, 进程内 registry 从 0 → 366 模型,
> **llm_ask 恢复出话**(11s, 走 LM Link gemma@mac-mini)。剩余优化层:
> ENG-OMLX-LOCAL 本地直连仍绕行, 见下"下一层病"。

## ✅ 已修复(功能层恢复)

**总闸根因**: `ModelGateway.create()` 只把 provider register 进 `_providers`,
`_models` 缓存必须 `refresh()` 填充 —— cli.py(launchd 版装配)显式跑了
`asyncio.run(reg.refresh())`, 进程内实例没人跑 → registry 恒空 → 一切模型
解析失败 → llm_ask 46s 慢死。修复: `_llm_helper._get_gateway` create() 后
补 refresh(一次性 ~7s, 单例进程终身复用) + sys.path 优先 final-ae3570f
(与运行中 gateway 同源)。

实测: llm_ask('1+1', model='qwen-3.8-27b') 11.0s -> "二"(LM Link gemma)。

## ⚠️ 下一层病(优化层, 未修): ENG-OMLX-LOCAL discover 0 模型

### 已修复(本轮追加)

- **unix:// 白名单**(aetherforge 03eca4c + final-ae3570f 运行副本): 
  `OpenAIProvider.is_available` 白名单加 `unix://` — OMLX 引擎
  base_url=unix://omlxc/api/v1 此前被判"无凭据不可用", discover 准入闸
  静默归零。修复后 provider is_available=True, **手动 discover 返回 24 个**。
- **qwen-3.8-27b 入 SSOT**(ecos 9b0cc62): MODEL-BREW-OMLX-LOCAL 补该模型
  (命名断层: oMLX 实际在册但 SSOT 缺席)。

### 仍悬而未决(下一棒靶心, 证据已钉死)

`registry.refresh()` 的 **gather 空收问题**: 手动 `asyncio.run(prov.discover())`
返回 24 个 ✓, 但 `asyncio.run(registry.refresh())` 后 `_models` 为 0 —
对照实验成立。嫌疑: refresh 的 `asyncio.gather(return_exceptions=True)` +
`wait_for(10s)` + `asyncio.to_thread` 线程池在 python3.14 下的行为/
系统负载下全超时。曾经成功过一次(366), 后续稳定 0 — flaky 变永久,
时间相关。**下一棒直接查 registry.refresh 实现的 gather/timeout**。

### 再下一层(UDS transport, 独立 feature)

resolve 成功后 chat 走 `OpenAIProvider(unix://...)` — OpenAI SDK 不认
unix:// base_url(Missing credentials/非法URL)。需要 httpx UDS transport
+ endpoint_ref socket path 解析 — 属 aetherforge 独立 feature 工程。
本地直连前, llm_ask 走 LM Link 兜底(mac-mini gemma, 功能正常)。

## 已验证排除的假设(勿重走)

| 假设 | 结果 |
|---|---|
| ~~AETHERFORGE_PROJECT_DIR / AETHERFORGE_M1_DIR env 缺失~ | 路径矩阵实测: 无 env 时默认解析到 ecos m1(存在且正确); 带 M1_DIR 反而指向不存在目录 | 
| ~~/etc/hosts 污染~~ | PTR 反查=官方真 IP(lb.fra.tailscale.com) |
| ~~gateway 时序~~ | 重启 launchd 版无效; 进程内实例独立装配 |
| ~~无鉴权 curl 0 模型=registry 空~~ | 401 视图陷阱(log 200 1821B 实证带鉴权有货) |
| ~~yaml 层缺失~~ | ENG-OMLX-LOCAL yaml: active + 23 model defs 全在 |

## 关联

- mail-daemon 恢复路径: 本修复 + mail_agent 三处显式 model 参数
  (f1d7d33) — 30min 周期下一轮 jsonl 应见非零分类
- 排障摩擦教训: mail-daemon.err **无时间戳**, 新旧症状混流; 建议下一棒
  先给 err 加时间戳
