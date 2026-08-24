#!/bin/bash
# 一键全貌：daemon/模型可用性/后端进程/内存/Tailscale/看门狗日志/磁盘，全部只读。
# 设计目标：agent(含未来的我) 一条命令拿到完整状态，不用再像 2026-08-22 那样
# 分散跑十几条命令做排查。
set -uo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "=== omlxc 全链路状态 $(date '+%Y-%m-%d %H:%M:%S') ==="
echo ""

echo "--- daemon ---"
omlxc daemon status 2>&1 | grep -o "running\|stopped\|error" | head -1 | sed 's/^/  状态: /'

echo ""
echo "--- 后端进程存活 (不代表模型可用，只代表进程/端口通不通) ---"
for name_url in "LM Studio|http://127.0.0.1:1234/v1/models" "oMLX App|http://127.0.0.1:8000/v1/models" "Ollama|http://127.0.0.1:11434/api/tags"; do
  name="${name_url%%|*}"; url="${name_url##*|}"
  if curl -sf -m 3 "$url" >/dev/null 2>&1; then
    printf "  ✅ %-12s %s\n" "$name" "$url"
  else
    printf "  ❌ %-12s %s\n" "$name" "$url"
  fi
done

echo ""
echo "--- 模型可用性 (探测缓存，非实时生成验证) ---"
omlxc models list --json 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    items=d['data']['items']
    total=len(items)
    total_p=sum(len(m.get('placement_states',[])) for m in items)
    avail_p=sum(1 for m in items for p in m.get('placement_states',[]) if p.get('available'))
    print(f'  模型: {total} 个 | Placement: {avail_p}/{total_p} 可用')
    # 2026-08-24 口径修正: available=False 有两种成因, 必须区分展示 --
    #   fresh=True  → 探测刚成功, loadable=False 是『探测确认不可用』(真问题)
    #   fresh=False → 探测超时/过期(_fail_stale), 只说明『口径未知』。
    # 此前 0/32 全灭误报正是把 LM Studio JIT 慢 + remote 离线造成的
    # 瞬态 stale 当成了实锤 (实测同一时刻 oMLX App coding 生成正常)。
    zero=[m['id'] for m in items if m.get('placement_states') and not any(p.get('available') or not p.get('fresh') for p in m['placement_states'])]
    if zero:
        print(f'  ⚠️  全灭(探测确认): {\", \".join(zero)}')
    stale_only=[m['id'] for m in items if m.get('placement_states') and not any(p.get('available') for p in m['placement_states']) and m['id'] not in zero]
    if stale_only:
        print(f'  ℹ️  探测过期(不代表不可用): {\", \".join(stale_only)}')
except Exception as e:
    print(f'  (读取失败: {e})')
"

echo ""
echo "--- 内存 ---"
# 朴素 "Pages free" 会显著低估可用内存 (2026-08-22 实测: 读到 ~0GB 但真实
# 可用 22.4GB) —— purgeable/inactive 页面本可回收却没被算入。口径统一自
# safe_audit.py 已验证的公式。
vm_stat | awk '
/Pages free/ {gsub(/\./,"",$3); free=$3}
/Pages purgeable/ {gsub(/\./,"",$3); purg=$3}
/Pages inactive/ {gsub(/\./,"",$3); inact=$3}
END {
  gb = (free + purg + inact*0.7) * 16384 / 1024 / 1024 / 1024
  printf "  真实可用: ~%.1fGB\n", gb
}'
lms ps 2>/dev/null | tail -n +2 | grep -v "^$" | while read -r line; do
  [ -n "$line" ] && echo "  LM Studio 驻留: $line"
done

echo ""
echo "--- Tailscale / 远程节点 ---"
if pgrep -f "tailscale.brew.sock" > /dev/null 2>&1; then
  /usr/local/bin/tailscale status 2>/dev/null | grep -E "mac-mini|y7000p" | while read -r line; do
    echo "  $line"
  done
  # 远程常驻模型存活 (2026-08-22 起 gemma-4-e4b 常驻 mac-mini 分担轻负载)
  if curl -sf -m 8 http://100.99.210.78:1234/v1/models 2>/dev/null | grep -q gemma-4-e4b; then
    echo "  ✅ mac-mini LM Studio 可达 (含常驻 gemma-4-e4b)"
  else
    echo "  ⚠️ mac-mini LM Studio 不可达或常驻模型丢失"
  fi
else
  echo "  ❌ brew tailscaled 未运行 — 远程链路断 (watchdog 应已报警)"
fi

echo ""
echo "--- AetherForge 网关 ---"
# 网关需要鉴权，未带 key 的探测会拿到 401——这代表"活着"不是"挂了"，
# 只有连接层面的失败(exit!=0，端口都连不上)才算真的下线。
if curl -s -o /dev/null -m 3 http://127.0.0.1:4000/v1/models 2>&1; then
  echo "  ✅ 端口 4000 响应正常 (有响应即视为活着，未校验鉴权)"
else
  echo "  ❌ 端口 4000 连接失败，真的下线了"
fi

echo ""
echo "--- 看门狗最近事件 (最近5条，全绿=无输出) ---"
tail -5 ~/.config/omlxc/watchdog.log 2>/dev/null || echo "  (无日志)"

echo ""
echo "--- 自治机制心跳 (状态文件最近写入距今; ⚠️=超期=疑似静默死) ---"
# 2026-08-24 P1-3: free_pool 曾因漏 import 静默死半月无人知, watchdog 的
# except 也吞过异常 —— 自治机制必须有"活着"的一屏可见证据。判据是各机制
# 周期性触碰的状态文件 mtime: 超过周期阈值即标 ⚠️, 提示人工核查。
heartbeat() { # $1=名称 $2=状态文件 $3=阈值分钟
  local name="$1" file="$2" max_min="$3"
  if [ ! -e "$file" ]; then printf "  ⚠️  %-28s 状态文件不存在\n" "$name"; return; fi
  local age_min=$(( ($(date +%s) - $(stat -f %m "$file")) / 60 ))
  if [ "$age_min" -le "$max_min" ]; then
    printf "  ✅ %-28s %smin 前 (阈值 %smin)\n" "$name" "$age_min" "$max_min"
  else
    printf "  ⚠️  %-28s %smin 前 (超阈值 %smin!)\n" "$name" "$age_min" "$max_min"
  fi
}
heartbeat "omlxc daemon 探测"      "$HOME/.omlx/stats.json"                            10
heartbeat "pipeline-watchdog 5min"  "$HOME/.config/omlxc/watchdog.log"                  10
heartbeat "gateway free_pool scan"  "$HOME/.aetherforge/state/free_pool_last_seen.json" 15

echo ""
echo "--- 磁盘 (模型卷) ---"
df -h /Volumes/Model 2>/dev/null | tail -1 || echo "  (无法读取)"
