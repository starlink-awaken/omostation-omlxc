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

refresh 后 registry: **366 模型 / 17 引擎分组, 唯独没有 ENG-OMLX-LOCAL**
(SSOT model defs 里明明有 23 个 OMLX 定义, adapter discover 产出 0,
连 "discover failed" 日志都没有 — 纯静默)。对照: LMSTUDIO-MACBOOKPRO 34 个
正常产出。所以本地模型全走 "不可解析" → LM Link 兜底(功能通, 绕远路)。

**下一棒查**: `SSOTProviderAdapter` 对 OMLX 类型引擎的模型发现路径 —
它 discover 时打的端点(oMLX App 8000? omlxc daemon?)与 engine yaml 里
endpoint 配置的匹配; 对照 LMSTUDIO-MACBOOKPRO 的 discover 为何成功。

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
