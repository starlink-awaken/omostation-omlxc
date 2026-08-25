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

## 📌 终局情报 (2026-08-25 深夜, 老王撤出该战场)

- `_llm_helper.py` 是**并行 agent 的活跃施工区**(本轮第三次回写老王的修改,
  sys.path 的 final-ae3570f 优先被改回主仓正序) — 依据多 agent 纪律撤出,
  避免互踩(同日已发生三次 checkout 拖回事故)。
- **新悬案实证**: 主仓版(Phase6 合并后)llm_gateway 被 import 时,
  `_get_gateway` 的 refresh 仅 0.8s 瞬间完成且 registry=0(成功那次 6.6s/366
  模型) — 疑主仓版 refresh/create 行为与 final-ae3570f 版**版本漂移**(两份
  代码不同行为的直接证据)。openai import 1.5s 预热后依旧 — 非 import 锁。
- **下一棒起点**: 先统一"进程内到底该 import 哪份 gateway 代码"(final 运行
  副本 vs 主仓 Phase6 版), 再看主仓版 refresh 为何 0.8s 空转。老王侧的
  unix:// 白名单(aetherforge 03eca4c)与 qwen-3.8-27b 入 SSOT(ecos 9b0cc62)
  两修复在两份代码里均已生效, 不受此影响。

## ✅ UDS transport 交付闭环 (2026-08-25 深夜追加)

**已交付**(aetherforge 23640d6, 主仓+final 运行副本均已推送):
`OpenAIProvider._get_client/_get_async_client` 对 `unix://` base_url 分支:
httpx UDS transport(uds=default_omlxc_socket()) + 虚拟 `http://omlxc/api/v1`。
**验证双证**: UDS list_models 18 模型全通; chat 穿透到 daemon 路由裁决层
(返回业务错误 E400 no eligible candidate — 传输层完整, 非传输问题)。

**端到端出话的唯一剩余阻塞 — 内存, 非代码**:
- daemon 判 coding/qwen-3.8-27b 全 placement unavailable, 根因是
  warm-keep 的 SKIP-MEM 守卫: `coding 需要 ~24GB, 可用 21GB, 跳过`
- 当前 MBP: 44% free + swap 14.6GB(阶段一目标 <10GB 未达终态)
- 内存腾挪(辨认可卸模型+不破明早保活)是独立工作, 留内存治理窗口
- **内存窗口打开后的验证一条命令**: `llm_ask(model='coding')` 经 UDS
  直连应 <5s 出话(对照: 当前 LM Link 兜底绕行 11s+)

## ✅✅ UDS 端点修正 — 传输层完全打通 (深夜终版)

**修正**(aetherforge 7245ecc): SDK base_url `http://omlxc/api/v1` →
`http://omlxc/openai/v1` — 对齐 daemon 真实 chat 端点
`/openai/v1/chat/completions`(omlxc_client.py:297 既有契约)。

**验证链终态**: 404(路径错) → ReadTimeout(路径对, 请求穿透到推理管线,
等待 24GB coding 冷加载, 内存 21GB<24GB) — 传输层+路径层 100% 正确。
mail-daemon.err 同步实证: resolve 成功 + UDS 客户端构造 + 进入 generate。

**端到端出话验证的一条命令**(内存窗口打开后, 如重启或卸载驻留后):
```bash
python3 -c "sys.path.insert(0,'/Users/xiamingxing/Workspace/bin/ssot'); \
from _llm_helper import llm_ask; print(llm_ask('1+1=?', model='coding'))"
```
预期 <5s 出话(UDS 本地直连), 对照 LM Link 兜底绕行 11s+。

**容量窗口开法**(明早或内存富余时): warm-keep 会在可用内存 ≥24+8GB 时
自动温 coding(WATCH 目标在册), 无需人工干预 — 届时 mail-daemon 下一轮
自动走本地直连。

## 🎯 UDS 三层剥穿终报 (深夜续)

SDK 完整请求体 → 422 E100(daemon 校验层挑剔 SDK 附加字段);
**最小请求体(model+messages) → 409 insufficient_capacity** —
传输/路径/协议三层全通, 最终门 = 内存容量(与 warm-keep SKIP-MEM 同一
物理约束: 可用 21GB < coding 24GB)。

**端到端出话的完整判定链到此收敛为单一变量: 内存**。
warm-keep 在可用 ≥32GB 时自动温 coding → UDS 直连即刻可用。
另: 422(SDK 字段兼容)是次要层, 若 daemon 校验层愿放宽(或 provider
侧过滤附加字段)可顺带修 — 挂 omlxc daemon 侧小债。

## ✅✅✅ 422 根治实证 (深夜终章, 7f2f149)

**修复**: OpenAIChatBody 补 OpenAI 标准字段集(stop/top_p/n/双penalty/
seed/user), str→tuple validator, 未知字段仍拒(防注入保留)。
**三路 app 层实证**(TestClient 直打, injectable app 零后端副作用):
- SDK 完整体(temperature+stop+top_p+penalties) → 503 E200(穿透校验, 达服务层) ✓
- 裸 str stop → 503 E200 ✓
- 未知字段 → 422 E100 仍拒 ✓
**生效**: daemon 3.4.0 → repo HEAD 升级后(与 role 字段同账双修复)。

**UDS 全链路修复总账(今日)**: unix://白名单 → UDS transport → base_url
端点 → 422 字段集 — 四层全修, 每层有提交/测试/实证。剩余唯一: 409 容量
(物理约束, warm-keep 自动窗口)。
