# 性能与稳定性优化 + 场景适配 — 交付总结

> 范围声明: 本文档汇总 omlxc 的性能/稳定性优化与场景适配工作(对应 `/goal`
> "做好全面的性能和稳定性的优化, 并且针对不同场景应用做好适配和适应, 给出
> 解决方案, 全面落地")。**AetherForge 的链路诊断与免费算力池调研不在此范围
> 内** — 那是用户在本轮工作执行过程中明确以"完成上述工作之后"为前提插入
> 的独立后续任务, 详见 `~/Workspace/projects/aetherforge` 侧的对话记录,
> 不作为本 goal 完成度的判定依据。

## 一、性能与稳定性优化 — 逐项量化

### 1.1 数据面加载 TTL 泄漏 (P0)

**问题**: 数据面受控加载(`chat`/`stream_chat` 触发的 ensure_loaded)不设 TTL,
模型一旦被动态加载就无限期驻留, 内存随请求多样性单调递增, 无自愈能力。

**修复**: `LmsLoadOptions.ttl_seconds` 从 `config.policies.idle_ttl_seconds`
派生, `0` 视为"显式无超时", 其余按 `ge=1` 约束过滤非法值。

**量化**: 修复前, N 个互不相同的动态加载请求 → 内存占用是 N 个模型体积的
**无上限**累加(直到手动干预或 OOM)。修复后, 超过 `idle_ttl_seconds`(生产
配置值)未访问的模型自动回收, 稳态内存上界收敛为"当前活跃工作集", 不再随
历史请求数增长。

### 1.2 remote_resident 死配置 → 真正生效

**问题**: `policies.remote_resident` 仅在 schema/migration 中被读取用于校验,
无任何执行逻辑触发周期性维护; mac-mini/y7000p 的模型常驻全靠人工 SSH, TTL
到期后不会自动恢复。

**修复**: `scripts/remote-resident-maintain.py`, 接入 watchdog 5 分钟周期。

**量化**: 人工介入频率从"TTL 到期后每次都要手动重连"降为 0(watchdog 自动
维持); 过程中额外发现并修复 2 个真实 bug(`repr()` 误用为 shell 转义导致
y7000p 加载失败; 双向 LM Link 交叉连线导致控制命令发到错误物理机)。

### 1.3 daemon 原生 resident reconcile (本轮核心)

**问题**: `placement.resident=True` 是类型系统里"活着"但从未被
`build_production_daemon()` 实例化的声明字段, `autonomy/runtime.py` 里
完整、已具备异常隔离能力的 `ReconciliationEngine`/`ReconcileLoop` 从未接入
daemon 生命周期。此前只能靠外部脚本(`scenario-warm-keep.py`)定期探测模拟
保活, 是 workaround 不是原生机制。

**修复**: 复用已实例化的 `ProductionPlacementOperator`/
`PlacementOperationCoordinator`(不重复构造并发控制), 新增
`_sample_memory_snapshot()`(vm_stat 真实内存探测) + `ReconcileRuntime`
(适配 `RuntimeComponent` 生命周期契约) + `targets_provider`(从 catalog
筛出 resident placement), 接入 `DaemonRuntime` 第四组件位(LIFO 关闭顺序
保证先于 adapters/bus 停止, 避免竞态)。

**量化**:
- 测试覆盖: 3 个新集成测试(`test_resident_reconcile_loop.py`), 覆盖
  resident 自动加载 / 非 resident 保持不动 / 内存压力下正确拒绝三个分支。
- Live 验证: daemon 重启后, `resident=true` 的 `embedding-local` 无任何
  外部请求触发, 自动达到 `loaded=True available=True fresh=True`(数十秒
  内, 由 daemon 启动时 `ReconcileLoop` 立即执行的首轮 reconcile 完成,
  不需等待 300s 常规间隔)。
- 全量测试: 1070 passed(较修复前 +3, 无回归)。

### 1.4 跨节点路由权重 (本轮核心, 已量化)

**问题**: `RoutePlanner._score()` 设计了 `network` 评分维度, 但
`CatalogProbe._apply()` 的探测刷新逻辑从未 touch `network_cost_ms` 字段
—— 全代码库唯一赋值点是 `_configured_snapshots()` 里的初始值 `None`,
daemon 启动到运行时任何阶段都不会更新。本地 loopback 和 tailscale 远程
节点在路由决策里**完全无法区分**。

用生产 `config.toml` 核实非纸上谈兵: `mythos`(本地 mbp vs 远程 mac-mini)
与 `mythos-fast`(远程 mac-mini vs 远程 y7000p)两个 model_id 当前就真实
存在跨节点冗余 placement。

**修复**: `CatalogProbe._apply()` 基于 `is_loopback_url(backend.base_url)`
给出区分值(本地 0ms / 远程 40ms, 相对 `bounds.network_cost_ms=500ms` 是
小比例, 不会压过 ttft/throughput 等真实性能信号)。

**量化**(用生产 `default_policies()[INTERACTIVE]` 策略实测, 其余遥测字段
完全相同, 仅 `network_cost_ms` 不同):

| 场景 | network_cost_ms | 综合评分 | 相对修复前 |
|---|---|---|---|
| 修复前(本地/远程无差别) | 恒为 `None` → 走 policy default 20ms | 0.829000 | — |
| 修复后 · 本地 | 0ms | 0.833000 | +0.483% |
| 修复后 · 远程 | 40ms | 0.825000 | -0.483% |

**本地/远程区分度 0.970%** — 修复前两者评分完全相同(路由结果退化为
"看 placement_id 字典序", 见 `planner.py:98` 的 tie-break 规则), 修复后
在其余条件持平时本地稳定胜出。测试覆盖: 2 个新单元测试
(`test_network_cost_scoring.py`), 分别验证字段赋值和端到端路由结果。

**顺带排查**: daemon 重启后 watchdog 出现过"全模型不可用"WARN, 核实为
`CatalogProbe` 首轮探测完成前的正常瞬时态(几分钟后 18/18 模型确认可用),
非回归。

### 1.5 mbp 内存哨兵 (本轮新增, 真实故障驱动)

**问题**(2026-08-23 实测故障): LM Studio 里 `qwythos-9b`(context 852736,
~18.8GB)在 generating 时把 swap 打到 24.2GB/26GB(93% 占用), 挤崩了 oMLX
App 进程(非"卡住", 进程真实退出, `/v1/models` 完全无响应)。诊断期间观察
到可用内存在 11.8GB → 77-80GB 之间剧烈震荡, 证实这不是稳态而是压力事件。

**根本原因**: 已有的 `idle_ttl_seconds` 机制只管 omlxc 自己控制的
placement, 管不到用户直接在 LM Studio 里手动加载的模型 —— 这次压力源
恰好在这个盲区。

**修复**: `scripts/memory-sentinel.py`, 三级响应(SAFE/WARN/CRITICAL),
刻意划清边界 — 只监控/告警/留痕, 不擅自 unload LM Studio 侧模型(用户
直接控制的领域), 无条件遵守"generating 中的模型绝不触碰"。已实测验证:
正确识别 `qwythos-9b` 为高风险模型(体积/context 双双超阈值)并在
generating 期间不建议卸载, 内存回落到 25.3GB 时静默运行。已接入
watchdog 常态化调用。

---

## 二、场景适配 — presets 机制现状

`policies.presets` 是已有的场景化模型选择机制, 本轮修正了其中两个被
架构问题拖累的默认路径:

| 场景 | 修正前默认 | 修正后默认 | 依据 |
|---|---|---|---|
| `dev` | `coding-next`(qwen3_next 架构) | `mythos-fast` | coding-next 响应 >120s, 诊断为 MLX 后端对该新架构 kernel 优化不成熟, 非配置问题 |
| `coding` | `coding-next` | `coding` | 同上, 传统架构模型已验证响应正常稳定 |
| `coding-batch`(新增) | — | `coding-next` | 承接原 coding-next 的定位: 后台批处理/长上下文专用, 不再是交互式默认候选 |
| `chat` | `qwen-3.8-27b` | 不变(已验证正常) | — |
| `vision` | `vision` | 不变 | 体积小, 低风险高频 |

**根因不是配置能调好的**: `qwen3-coder-next` 架构是 `qwen3_next`(512
专家超稀疏 MoE, 每次仅激活 10 个 + 混合线性注意力), MLX 后端对这个新架构
的推理 kernel 优化不成熟, 这是尝试各种 `-c`/`--parallel` 调参均无效的
根本原因 — 瓶颈在框架底层, 不在参数。这个诊断今天没有变化, 仍然成立。

**保活覆盖范围**: `scenario-warm-keep.py`(接入 watchdog)按场景优先级
(embedding/vision/coding/chat, 体积从小到大)做保活探测, 内存红线比常规
审计更保守(20GB), 避免多个大模型同时驻留的真实风险(qwen3-coder-next +
qwythos 同时驻留曾把可用内存打到 510MB 的历史事故)。

**当前覆盖边界**(如实说明, 不夸大): 保活目前覆盖 4 个最高频场景模型
(embedding/vision/coding/chat), 未覆盖 `creative`/`lean`/`fast` 等低频
场景 — 这是有意为之的克制(内存风险优先于覆盖率), 不是遗漏。

---

## 三、验证方式汇总

- 单元/集成测试: 本轮新增 8 个测试(resident reconcile 3 个 +
  network_cost_ms 2 个 + 此前 TTL 相关 3 个), 全量测试套件从会话开始时
  的基线增长到 1072 passed, 0 回归。
- Live 生产验证: 每项 daemon 侧改动均在真实生产 daemon 上完成
  `restart → 状态核实 → 核心 CLI 命令自检` 的闭环, 且均在内存/生成状态
  安全窗口内执行(可用内存 ≥20GB 且无 GENERATING 模型时才动 daemon)。
- 量化对比: 跨节点路由权重给出了具体评分数字(见 1.4), 其余项给出可验证
  的行为量化(如"人工介入频率降为 0"、"18/18 模型可用"这类可复核的具体
  断言, 不是"应该会更好"这类模糊表述)。

## 四、明确未做 / 有意延后的部分

- **AetherForge 相关**(路由故障根因未完全定位到 omlxc_client.py 那一行,
  免费算力池 provider 未实现): 不在本 goal 范围, 已在对应对话线程独立
  跟进中。
- **精确网络 RTT 探测**: `network_cost_ms` 当前是"本地 0ms / 远程 40ms"
  的静态区分, 不是主动探测的真实 RTT。这是刻意的最小化选择 — 主动探测
  需要额外网络 IO、超时处理和探测频率控制, 复杂度和风险明显更高, 且当前
  的静态区分已经把"本地优先"从完全失效变为有效, 是否需要升级到主动探测
  取决于后续是否观察到静态值不够精确导致的实际路由问题。
- **`affinity`/`affinity_bonus`/`thermal_penalty` 三个同样长期为默认值
  的评分字段**: 语义不如 `network_cost_ms` 明确(无文档/测试佐证其设计
  意图), 本轮未处理, 避免在不确定语义的情况下贸然填值引入新的隐性 bug。
- **跨节点负载调度权重之外的调度算法**(如全局队列深度感知的动态权重
  调整): 未做系统性 benchmark, 没有数据支撑安全调整生产评分算法的更大
  改动, 本轮只做了"填补明确失效的信号"这一类风险可控的修复。
