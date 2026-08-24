#!/bin/bash
# omlxc 使用统计报表
# 用法: bash scripts/usage-stats.sh

DB="$HOME/.config/omlxc/state.db"

echo "=== omlxc 使用统计 ==="
echo ""

# --- 口径警示 (2026-08-24 B 线排查结论) ---
# engine 层 stats.json 的 total_requests 含大量保活/探测请求(实测 96% 是
# max_tokens=1 的温着探测: coding 24916 reqs avg_completion=1.1), 直接看
# 总数会严重失真。state.db 的 request_metrics 只记数据面部分阶段(phase=
# before_content/complete, ~400 条), 覆盖也不全。要看真实使用面貌, 以
# 下面"真实使用 vs 保活"分流段为准(判据: 模型级 avg_completion > 5)。
echo "--- 真实使用 vs 保活流量 (engine stats.json 口径) ---"
python3 -c "
import json
d = json.load(open('$HOME/.omlx/stats.json'))
per = d.get('per_model', {})
real = [(m, s) for m, s in per.items() if s.get('requests', 0) > 0 and s.get('completion_tokens', 0)/s['requests'] > 5]
keep = [(m, s) for m, s in per.items() if s.get('requests', 0) > 0 and s.get('completion_tokens', 0)/s['requests'] <= 5]
rr = sum(s['requests'] for _, s in real); kr = sum(s['requests'] for _, s in keep)
print(f'  真实使用: {rr:>6d} reqs ({rr/max(rr+kr,1)*100:.1f}%) | 保活/探测: {kr:>6d} reqs')
for m, s in sorted(real, key=lambda x: -x[1]['requests']):
    print(f\"    {m:30s} {s['requests']:>5d} reqs  avg_prompt={s['prompt_tokens']/s['requests']:.0f}  avg_compl={s['completion_tokens']/s['requests']:.1f}\")
"
echo ""

echo "--- 总体指标 (state.db 口径, 仅数据面部分阶段) ---"
sqlite3 "$DB" "SELECT
  '总推理请求: ' || COUNT(*) FROM request_metrics;
SELECT
  '成功请求: ' || SUM(success) || ' (' || ROUND(AVG(success)*100, 1) || '%)' FROM request_metrics;
SELECT
  '平均延迟: ' || ROUND(AVG(latency_ms), 0) || 'ms' FROM request_metrics WHERE success=1;
"
echo ""

echo "--- 最近 7 天请求趋势 ---"
sqlite3 "$DB" "SELECT
  DATE(observed_at) as day,
  COUNT(*) as requests,
  ROUND(AVG(latency_ms), 0) as avg_latency_ms
FROM request_metrics
WHERE observed_at > DATETIME('-7 days')
GROUP BY day
ORDER BY day;
"
echo ""

echo "--- 路由决策统计 (Top 10) ---"
sqlite3 "$DB" "SELECT
  selected_placement_id,
  COUNT(*) as times_selected
FROM route_audits
WHERE selected_placement_id IS NOT NULL
GROUP BY selected_placement_id
ORDER BY times_selected DESC
LIMIT 10;
"
echo ""

echo "--- Job 状态 ---"
sqlite3 "$DB" "SELECT
  kind,
  state,
  COUNT(*) as count
FROM jobs
GROUP BY kind, state
ORDER BY kind, state;
"
echo ""

echo "--- 库存高水位 ---"
sqlite3 "$DB" "SELECT * FROM inventory_high_water;"
