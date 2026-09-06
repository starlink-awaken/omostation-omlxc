# Qwen3.8-Flash-Next 本地运行手册 (Runbook)

> 最后更新: 2026-09-06 | 维护: omostation governance
> 状态: **已实测通过** — 2026-09-06 冒烟全链路跑通 (加载 115s / 生成正常 / swap 未失控)

## 1. 背景: 2026-09-05 崩溃复盘

第一次冒烟测试无任何内存约束裸跑 103.77GB 模型加载, 加上 omlx-server 常驻模型
(实测 RSS 45.4GB) 和 8 个 CLI 工具 (~8GB), 总需求超物理 128GB → swap 爆炸 → 系统崩溃。

已落地的教训 (对应 `.omo/_knowledge/patterns/` 风格):

| 教训 | 修复 |
|---|---|
| 无预检裸跑大模型 | C1 启动前可用内存硬校验, 不够即 fail-fast |
| mlx 默认 wired limit ≈ 96GB < 模型体积, 生成中逐步 fault-in 触顶 | C2 `mx.metal.set_wired_limit(104GB)` 进程内硬顶 |
| max_tokens 无界 + 常驻服务化 = 内存随会话增长 | C3 max_tokens ≤ 128 (冒烟), KV cache 保持 MB 级 |
| 崩溃后无现场可查 | 本 runbook + 两脚本注释记录因果 |

## 2. 硬件数学: 为什么它只能是专用模式资产

物理内存 128GB, macOS 默认把 GPU 可 wire 的内存卡在 **~96GB (75%)**。

| | |
|---|---|
| 模型体积 | 103.8GB |
| 默认 wired 天花板 | 96GB |
| 结论 | **默认设置下数学上不可能全驻留**, 至少 8GB 必须走换页 |

而这是稀疏 MoE (512 专家/每 token 路由 10 个), 换页访问接近全权重随机读 ——
不是 llama.cpp mmap n-gram 表那种"冷页永不加载"的理想情况。

**实测代价**: wired 84GB (20GB 走外置 SSD 分页) → **0.6 tok/s**。
同硬件社区基准 33 tok/s, 差 54 倍。

### 2.1 内存分层规划 (128GB 的正确用法)

| 层 | 预算 | 内容 | 常驻 |
|---|---|---|---|
| 系统硬保留 | 25GB | macOS + CLI agents + 浏览器/IDE | 不可侵占 |
| 常驻核心 | 25GB | embedding 2GB + 编码主力 18.5GB (27B dflash) + reranker 1GB | ✅ |
| 按需插槽 | 50GB | 一次一个大模型, 用完即卸 | ❌ |
| 机动余量 | 28GB | 突发 / KV cache 增长 | — |

**Flash-Next 4bit 不属于任何一层 —— 它要整台机器。** 这就是专用模式存在的意义:
它不是可调度的 placement, 是独占设备的作业。

### 2.2 定位

- **日常主力**: 27B 档 (qwen-3.8-27b-dflash, 18.5GB) —— 完美契合常驻核心层
- **需要更强**: 走云 API (ARK / Agent Plan)
- **Flash-Next 4bit**: 专用模式资产, 周末深度任务 / 离线批处理
- **2bit 版本**: **不建议下载** —— 73GB 仍需清空常驻核心才能跑,
  却换来未经验证的质量 (2-bit 专家权重, 无公开评测), 性价比最差

## 3. 清场步骤

**注意: `omlxc models unload` 在 PR#61 合并前不可靠** (见 §7)。实际清场靠退出 App。

只 `kill omlx-server` 没用 —— watchdog 5 分钟内会把 App 拉起来重新吃 52GB。
必须从菜单栏退出 **oMLX App 本身**, 而且要先停 watchdog (`enter` 会自动做)。

## 4. 专用模式 (跑 4bit 全量版的唯一正确姿势)

```bash
omlx-dedicated-mode status   # 状态: wired 上限 / 可用内存 / 守护进程 / 内存大户
omlx-dedicated-mode enter    # 停守护 → 检查清场 → wired 提到 112GB (需 sudo)
omlx-qwen38-flash-next-smoke # 跑模型
omlx-dedicated-mode exit     # 恢复 wired 默认 + 恢复守护 (必须跑!)
```

`enter` 先停守护再查内存, 所以清场不彻底时它拒绝进入但守护已停 —— 清完场直接重跑
`enter` 即可, 不用先 `exit`。

### 4.1 为什么必须停守护 (最阴的坑)

**两个**自动拉起机制, 都是 300 秒周期:

| 机制 | 行为 | 代码位置 |
|---|---|---|
| `com.omlxc.watchdog` (launchd) | 探测 :8000 不通 → `pkill -x oMLX` + `open -a oMLX`, App 起来自动加载模型吃 ~52GB | `projects/omlxc/scripts/pipeline-watchdog.sh` |
| omlxcd resident reconcile | 把 `resident=true` 的 placement 重新加载回来 | `src/omlxc/daemon/composition.py:1302` |

时序上这是最难防的: 清场 → 设 wired → 加载模型**全部成功**, 5 分钟后守护把 oMLX
拉起来, 此时物理内存已被 wire 掉大半**且不可换出** → 硬卡死。

`exit` 必须跑, 否则重启后 watchdog 仍是停用状态, 算力池失去自愈能力。

### 4.2 约束机制

- `omlx-qwen38-flash-next-smoke` — 一次性: 预检 → wired → 加载 → 生成 64 tok → 退出
- `omlx-serve-qwen38-flash-next` — 服务模式: 同预检+wired, OpenAI 兼容 API 于 :8197
- wired 值动态算 `min(模型+2GB, 可用-8GB)`: 清场充分给到 ~106GB (全驻留);
  清场不足自动收缩走 SSD 分页 (慢但不崩)

### 4.3 文件位置

单一数据源是本目录 (`projects/omlxc/scripts/dedicated-mode/`)——三个脚本 + 本文档都在
这里, 随仓库走 ruff/pyright/CI。`~/.local/bin/omlx-*` 是指回这里的符号链接, 改一处
两边生效。

`~/omlx` 是指向 `~/.local/share/omlxc/releases/v3.0.14/` 的符号链接 —— omlxc 一升级
就换目录, 之前踩过坑把资产放那儿丢过一次, 现在一律不放那里。

| 资产 | 位置 |
|---|---|
| 模型权重 | `~/Models/Qwen3.8-Flash-Next-MLX-4bit` (内置盘, 已排除 Time Machine) |
| 三个脚本 (真身) | `projects/omlxc/scripts/dedicated-mode/omlx-{dedicated-mode,qwen38-flash-next-smoke,serve-qwen38-flash-next}` |
| 三个脚本 (本机可执行入口) | `~/.local/bin/omlx-*` → 上面那三个的符号链接 |
| 本文档 | `projects/omlxc/scripts/dedicated-mode/README.md` |

## 5. 要不要接入 omlxc 调度

**默认不接。** 理由: omlxc 的 placement 模型假设"模型可被调度器按需装卸并与其他模型
共存", 而这个模型的实际约束是"独占整台机器 + 需要改系统 sysctl + 需要停守护进程"。
把它登记成一条 placement, 等于让调度器以为自己能调度一个它根本调度不了的东西 ——
这正是声明/执行鸿沟的制造方式。

正确形态: **专用模式作业**, 通过 `omlx-dedicated-mode` + `omlx-serve-*` 手动进出。

若将来真要接入 (比如换成能常驻的小尺寸版本), 配置片段:

```toml
[[models]]
id = "qwen-3.8-flash-next"
category = "reasoning"
role = "reasoning"
engine = "mlx_lm"
size_gb = 103.8
context_limit = 65536

[[placements]]
id = "qwen-3.8-flash-next-local"
model_id = "qwen-3.8-flash-next"
backend_id = "mbp-m5-max-128g-omlx-app"
backend_model_id = "qwen-3.8-flash-next"
model_path = "/Users/xiamingxing/Models/Qwen3.8-Flash-Next-MLX-4bit"
context_limit = 65536
memory_gb = 103.8
resident = false           # 绝不常驻
legacy_port = 8197
```

## 6. 启动检查清单

1. `omlx-dedicated-mode status` —— 看清场差多少
2. 菜单栏退出 oMLX App + 退出占内存的 CLI agent
3. `omlx-dedicated-mode enter` —— 停守护 + 提 wired (预检不过会拦, 继续清场重跑)
4. `omlx-qwen38-flash-next-smoke` —— 冒烟, 确认加载+生成正常
5. (可选) `omlx-serve-qwen38-flash-next` —— 起服务在 :8197
6. **`omlx-dedicated-mode exit`** —— 必须跑, 恢复 wired 默认 + 恢复守护自愈

## 7. 已知 bug 与修复状态

### 7.1 `omlxc models load/unload` 派发到错误后端 (PR#61, 待合并)

`_placement_for_model` 用 `return candidates[0]` 按 catalog 顺序取第一个 placement,
不看请求类型。模型在多后端都有 placement 时操作会打到错误的后端。

实测: `mythos-fast` 常驻在 oMLX App, 但 `unload` 被派发到 LM Studio 的 placement,
`error_code=operation_failed`, **45GB 纹丝不动**。

**诊断陷阱**: `progress=0.2` 既是 RUNNING 检查点 (`composition.py:1069`) 又是 FAILED
哨兵值 (`composition.py:1123`), 且 CLI 不显示 error_code —— 失败的 job 看起来像"卡住"。
真相在库里:

```bash
sqlite3 -header -column ~/.config/omlxc/state.db \
  "SELECT substr(job_id,1,8), kind, state, error_code, rollback_reference
   FROM jobs ORDER BY updated_at DESC LIMIT 10;"
```

修复: https://github.com/starlink-awaken/omostation-omlxc/pull/61
合并前, 清场请用"退出 App"而不是 `omlxc models unload`。

### 7.2 `mlx.core.metal` 无 `get_wired_limit`

mlx 0.31.3 只有 setter 没有 getter。想读当前值用 `sysctl -n iogpu.wired_limit_mb`
(返回 0 表示走系统默认 ~75%)。

### 7.3 oMLX App 重启后自动重新加载模型

kill `omlx-server` 子进程后 App 会重新拉起它并自动加载模型 (实测回到 52.7GB)。
清场必须退出 App 本身。
