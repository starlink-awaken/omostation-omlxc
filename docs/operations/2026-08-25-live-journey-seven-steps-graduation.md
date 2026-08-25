# --live 七步全链毕业 · admin-notification-workflow (2026-08-25)

> 承接: 此前 --live 验证停留在 dry-run 毕业 + 阴性证明。本轮受控验证
> 当场抓获状态机 schema bug, 修复后**七步全链 LIVE 打通**, 数字大脑
> 首次对真实任务邮件完成端到端运转。

## 一、Bug 捕获与修复

### 症状
`--live` 首 run: `has_task=true` 却停在 received, 报
"Terminal state reached: received"。

### 根因 (journey-runner.py:567)
```python
states = {s["name"]: s for s in spec.get("states", [])}
transitions = spec.get("transitions", [])   # 只读顶层
```
admin-notification-workflow.yaml 是 9 份 journey-spec 里唯一的
**嵌套 schema 异类**(transitions 内嵌在 states[].transitions)。
顶层 transitions 为空 → `state.get("next")` 恒空 → **任何状态都被
误判 Terminal**。dry-run 走不到 transition 真评估, 唯有 --live 才暴露
——受控验证的价值实证。

### 修复 (对齐规范, 不给 runner 养方言)
1. YAML 重写为规范形(顶层 transitions + states[].next), version 2→3
2. 删除 received 自环: 嵌套原语义"无任务自环等待"在 run-once 模型里
   表达不了, 且自环使 received 成为 transition target → validate 报
   "no entry point"。改为无匹配 transition 优雅结束(runner:748 既有行为)
3. 补 7 张 scene card(admin-inbox/classify/forward/collect/compile/
   review/submit): 运行时 7 个 dispatch 函数早已实现于 admin_scenes.py,
   缺的只是声明面; 有卡才有 capability-token。契约如实对齐实现。
4. `_has_real_data` 补草稿路径型输出识别(notice/form/report/eml 草稿
   落盘 = 真实产出), 消除后段状态误报 degraded

### 验证
- `validate` 全绿: [PASS] 7 states, 7 transitions
- `_has_real_data` 单测 5/5 (4 类草稿路径 True / 空输出 False)

## 二、七步全链 LIVE 实录 (run_id 20260825124455-784d0c27)

```
received → classified → forwarding → collecting → compiling → reviewing → submitted
   读邮件+分类   LLM任务分解   两份草稿生成   截止注册    汇总报告草稿   领导审阅草稿   提交草稿+trust闭环
```

真实任务: **房山区区属医院自有挂号渠道情况统计表**
产出(全部草稿, 零发送, requires_human_confirm 恒 true):
- `_drafts/2026-08-25-forward_notice-draft.md` (转发通知)
- `_drafts/2026-08-25-data_collection-draft.md` (数据收集表)
- `_drafts/2026-08-25-summary_report-draft.md` (汇总报告)
- `_drafts/2026-08-25-关于...统计表.eml` (领导审阅, 占位收件人)
- `_drafts/2026-08-25-关于...统计表-1.eml` (提交上级)
- deadline_registered=true, task_completed=true

安全面: risk_engine 三道检查(forward/send_email/submit) + 全程草稿模式 +
HITL 确认门 —— 设计红线未被突破。

## 三、顺带破案: LM Link 兜底之谜

首 run 观测到 `oMLX qwen-3.8-27b → 409 insufficient_capacity →
LM Link gemma 兜底`。结合同日 14.5s 本地直连成功: **路由本身正确
(registry 登记 ENG-OMLX-LOCAL), LM Link 是本地容量不足时的设计内降级**
(当日诱因: qwopus3.6-27b 26.6GB 高风险加载挤占内存)。非 bug, 分级兜底
机制按设计工作。

## 四、待人工

- 5 份草稿在 `Documents/@工作文档/卫健委/_drafts/` 待审阅 — 发送与否
  由人决定(HITL), 审阅通过才构成完整业务闭环
- 主仓 staged 混合变更(15 files)待合适时机提交

---
*老王 2026-08-25。run 记录: .omo 状态存储 + 本文档, 可复核。*
