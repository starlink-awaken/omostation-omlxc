# iris→cockpit 迁移债 · 调查结论与决策 (2026-08-25)

> 背景: iris 独立 CLI 已挂弃用警告("请使用 cockpit 替代"), journey-runner.py
> 5 处依赖 `iris --json list <connector> --limit N`。本轮全面排查后**决策:
> 记债不迁移**。依据如下, 防将来翻旧账。

## 一、调查结果

| 项 | 事实 |
|---|---|
| iris 现状 | `~/.local/bin/iris` (uv tool 安装, **不依赖共享 worktree**) 仍正常返回 JSON, 仅 stdout 混入弃用警告 |
| 警告处理 | journey-runner 已做前缀剥离(text.find 定位首个 `[`/`{`, 2026-08-25 修复, 有测试) |
| cockpit 命令面 | `bos-inbox` = status/search/pending/watch/archive, 操作对象是 `Documents/_inbox/` 落盘文件与致远 OA — **与 iris 的 connector 实时 list 语义不对齐** |
| 等价接口 | cockpit **无** `list <connector> --limit` JSON 接口; 强行迁移 = 在 cockpit 造新接口 = 重复造轮子 |

## 二、为什么现在不迁 (KISS/YAGNI)

1. iris 可用, 断档风险已被 watchdog RUNTIME_FILES 巡检覆盖(journey-runner 在清单)
2. mail-daemon 产出断言是第二道网: iris 真断 → journey 降级 → 产出归零 → 告警
3. cockpit 若未来提供 connector list 接口, 迁移点集中在 `_iris_list()` 单函数(306 行), 改动面小

## 三、迁移触发条件(满足其一开始)

- iris CLI 被移除或输出格式破坏性变更
- cockpit 提供 `list <connector> --json` 等价接口
- iris 源项目(提供方)宣布 EOL 日期

---
*老王 2026-08-25, 数据可复核: registry 探测 + cockpit cli.py/bos_inbox.py 源读。*
