#!/bin/bash
# 织星算力池链路守护：定期探测各后端，自动拉起挂掉的 oMLX App。
# 由 launchd (com.omlxc.watchdog) 周期调度，也可手动运行做一次性检查。
set -uo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
OMLXC="$HOME/.local/bin/omlxc"

LOG_DIR="$HOME/.config/omlxc"
LOG="$LOG_DIR/watchdog.log"
mkdir -p "$LOG_DIR"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "$(ts) $1" >> "$LOG"; }

check_http() {
  curl -sf -m 3 "$1" >/dev/null 2>&1
}

# --- oMLX App (2026-09-13 诊断实锤后改版) ---
# 旧版在这里 pkill -x oMLX + open -a oMLX 自愈, 但已确认: GUI 双击 / launchd
# (不论直接还是间接) 拉起的 oMLX 进程链条, 会在 model_discovery.py 对
# /Volumes/Model 上的文件 open() 时永久卡死内核态 —— 根因是 launchd 派生的
# 进程没有 macOS 的 SECURITYSESSIONID (图形界面安全会话), fork/setsid 都救
# 不回来(实测: launchd 脚本自己再 setsid 双重 fork 依然卡死)。而这个
# watchdog 本身就是 launchd LaunchAgent, 所以旧版的"自愈"实际上是每 5 分钟
# 用 pkill+open -a 把一个可能正常启动中的实例腰斩、再触发一次必然卡死的
# 重启, 是过去反复复现"卡死"的元凶之一, 不是缓解手段。
# 治本方案是 omlx-server-ensure.sh (从终端登录会话里调用, 天然带合法安全
# 会话) + ~/.zshrc 里每次开新终端顺手检查。这里改成只检测+告警, 不再由
# launchd 自己动手重启。
if ! check_http "http://127.0.0.1:8000/v1/models"; then
  log "[WARN] oMLX App 端口 8000 无响应 — watchdog 不再自动重启(launchd 拉起必卡死, 见脚本注释), 已发桌面通知提醒手动处理"
  osascript -e 'display notification "oMLX 本地服务无响应, 请打开一个终端窗口(会自动检测拉起)或手动运行 omlx-server-ensure.sh" with title "oMLX 服务告警"' >/dev/null 2>&1
fi

# --- 共享 worktree detached HEAD 告警 (2026-08-25 二发事故催生) ---
# 08-24 与 08-25 两起: 并行 agent/机制把共享主 worktree checkout 到旧
# 提交(非 reset --hard, M1/M2/M3 防不住这条通道), 当日新增提交被绕过。
# 检测成本一条命令; 复原手段: git checkout main (main 从未被移动, 零丢失)。
for _repo in "$HOME/Workspace/projects/omlxc" "$HOME/Workspace"; do
  _branch=$(git -C "$_repo" rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ -n "$_branch" ] && [ "$_branch" = "HEAD" ]; then
    log "[WARN] 共享worktree detached HEAD: $_repo — 并行机制可能已拖回旧提交, git checkout main 可复原"
  fi
done

# --- 常驻任务运行时文件巡检 (2026-08-26, 协作协议条款 B 轻量版) ---
# 多条 cron 直指主 worktree 文件(分支漂移即断档, mail-daemon 8h 实锤)。
# 全部固化副本 = N 份同步负担(过度工程); 本段用 test -f 链在 5min 内
# 抓文件消失 — 断档发现从"人工" 提到 "分钟级"。
RUNTIME_FILES=(
  "$HOME/Workspace/bin/gac/remediation-engine.py"
  "$HOME/Workspace/bin/gac/anti-corrosion-check.py"
  "$HOME/Workspace/bin/gac/unified-health-score.py"
  "$HOME/Workspace/bin/ssot/north-star-weekly.py"
  "$HOME/Workspace/bin/ssot/system-health-check.py"
  "$HOME/Workspace/bin/ssot/mail_daemon.py"
  "$HOME/Workspace/bin/ssot/journey-runner.py"
  "$HOME/Workspace/projects/omlxc/scripts/weekly-report.py"
)
for _f in "${RUNTIME_FILES[@]}"; do
  if [ ! -e "$_f" ]; then
    log "[WARN] 常驻任务运行时文件消失: $_f — 分支漂移断档风险(参考 runtime/ssot-stable 固化模式)"
  fi
done

# --- brew tailscaled (临时进程守护: 死了报警; 持久化方案待用户批准) ---
if ! pgrep -f "tailscale.brew.sock" > /dev/null; then
  log "[ERROR] brew tailscaled 不在运行 — 远程节点链路已断"
fi

# --- LM Studio：只监控记录，不自动重启 (2026-08-26 用户卸载本机 LM Studio;
#     改为存在才探测 — 消失静默跳过, 将来重装自适应, 不替用户做架构决策) ---
# 历史注记: LM Studio 有已知的 JIT 加载失控上下文问题(见
# docs/operations/2026-08-22-*), 自动重启解决不了根因; qwen 双加载事故
# 根因之一也是它(JIT 副本无卸载机制, 见 2026-08-26 ops 记录)。
if pgrep -x "LM Studio" > /dev/null 2>&1; then
  check_http "http://127.0.0.1:1234/v1/models" || log "[ERROR] LM Studio (MBP) 端口 1234 无响应"
fi

# --- Ollama (自愈：2026-08-22 单次会话内观察到两次崩溃，均干净重启即恢复，
#     无 LM Studio 那类失控加载风险) ---
if ! check_http "http://127.0.0.1:11434/api/tags"; then
  log "[WARN] Ollama 端口 11434 无响应，尝试重启"
  open -a Ollama 2>/dev/null
  for i in 1 2 3 4 5 6; do
    sleep 5
    check_http "http://127.0.0.1:11434/api/tags" && break
  done
  if check_http "http://127.0.0.1:11434/api/tags"; then
    log "[OK] Ollama 已恢复"
  else
    log "[ERROR] Ollama 重启后仍无响应，需要人工检查"
  fi
fi

# --- omlxc daemon ---
if ! "$OMLXC" daemon status --json 2>/dev/null | grep -q '"running"'; then
  log "[WARN] omlxc daemon 未运行，尝试重启"
  "$OMLXC" daemon restart --yes --confirm-impact >>"$LOG" 2>&1
fi

# --- remote 节点在线告警 (2026-08-24: mac-mini/y7000p 物理离线 23h/1d 无任何
#     通知, 只能靠手动 full-status 被动发现。tailscale status 实测 tx>0/rx=0
#     证明确认过是对端关机/断网而非 tailscale 故障, 属"只能告警不能自愈"类。
#     桌面通知 30 分钟去重, 状态落盘防止重启后刷屏) ---
TS_CLI=""
for _c in /opt/homebrew/bin/tailscale /usr/local/bin/tailscale "$HOME/.local/bin/tailscale" "/Applications/Tailscale.app/Contents/MacOS/Tailscale"; do
  if [ -x "$_c" ] || command -v "$_c" >/dev/null 2>&1; then TS_CLI="$_c"; break; fi
done
if [ -n "$TS_CLI" ]; then
  # 必须钉死 brew socket: 本机还有 GUI 版 Tailscale.app(macsys)在跑且 logged out,
  # 不带 --socket 时 CLI 默认连 GUI 版的 IPN bus → Peer=null → 节点告警失明
  # (2026-08-25 实锤: 双后端共存, brew 版才是承载 tailnet 的正主)
  "$TS_CLI" --socket=/var/run/tailscale.brew.sock status --json 2>/dev/null | python3 -c "
import json,subprocess,sys,time
try:
    # "Peer": null 时 get 默认值不生效(key 存在但值为 null), 必须 or {} 兜底,
    # 否则 peers.values() 空指针崩掉整段节点告警 (2026-08-25 watchdog.log 实锤)
    peers=json.load(sys.stdin).get('Peer') or {}
except Exception:
    sys.exit(0)
WATCH=('mac-mini','xia-y7000p')  # mac-mini=主力常驻节点(7x24), y7000p=弹性; 其余设备不盯
# Peer 的 key 是 nodekey:xxx, 人类可读名在 DNSName(如 mac-mini.xxx.ts.net.)
# 或 HostName(可能是中文如 '夏明星的Mac mini'), 用子串匹配两者最稳。
def _watched(p):
    names=f\"{p.get('DNSName','')} {p.get('HostName','')}\"
    return any(w in names for w in WATCH)
offline=sorted(p.get('DNSName','').split('.')[0] or p.get('HostName','?')
               for p in peers.values() if _watched(p) and not p.get('Online'))
state_file='$LOG_DIR/node-offline-notified.json'
try:
    last=json.load(open(state_file))
except Exception:
    last={}
if not offline:
    open(state_file,'w').write('{}')
    sys.exit(0)
now=time.time()
due=[n for n in offline if now-last.get(n,0)>1800]
if not due:
    sys.exit(0)
msg='、'.join(due)
print(f'[WARN] remote 节点离线: {msg} (remote_resident 维护无对象, 算力池仅剩本机)')
for n in due:
    last[n]=now
open(state_file,'w').write(json.dumps(last))
subprocess.run(['osascript','-e',
    f'display notification \"{msg} 已离线超过阈值, 算力池仅剩本机\" with title \"omlxc 节点离线告警\"'],
    capture_output=True)
" >> "$LOG" 2>&1
else
  log "[WARN] tailscale CLI 不可用, 节点在线检测跳过"
fi

# --- remote_resident 常驻策略维护 (2026-08-22: 此前是纯声明性死配置,
#     全代码库无任何执行逻辑读取, mac-mini/y7000p 的常驻全靠人工 SSH
#     维持, TTL 到期不会自动恢复。本脚本让它真正生效) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/remote-resident-maintain.py" >>"$LOG" 2>&1

# --- 高频场景模型保活 (2026-08-22: placement.resident 的调度循环从未
#     接入 daemon, 用同样的"外部脚本 + 现有接口"模式补齐) ---
python3 "$SCRIPT_DIR/scenario-warm-keep.py" >>"$LOG" 2>&1

# --- mbp 内存哨兵 (2026-08-23: qwythos-9b 在 LM Studio 里 generating 时
#     把 swap 打到 24GB/26GB, 挤崩了 oMLX App 进程。omlxc 的 idle_ttl 只
#     管自己控制的 placement, 管不到 LM Studio 里用户手动加载的模型 ——
#     这次真实故障恰恰发生在这个盲区。只做监控+告警+留痕, 不擅自 unload
#     LM Studio 侧的模型) ---
python3 "$SCRIPT_DIR/memory-sentinel.py" >>"$LOG" 2>&1

# --- 模型级可用性(读探测缓存，不发真实生成请求，代价很低) ---
# 2026-08-24 口径修正: available=False ∧ fresh=False 是探测超时(_fail_stale,
# 常见诱因: LM Studio JIT 慢、remote 节点离线), 属瞬态口径未知, 不算实锤
# —— 此前曾把这类报成"全灭"误报。只对 fresh=True(探测成功)但
# available=False 的模型报 WARN。输出乱码这类要真实生成才测得到的
# 问题不在这一层，见 deep-registration-audit.sh。
zero_avail=$("$OMLXC" models list --json 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    items=d['data']['items']
    bad=[m['id'] for m in items if m.get('placement_states') and not any(p.get('available') or not p.get('fresh') for p in m['placement_states'])]
    print(','.join(bad))
except Exception:
    pass
" 2>/dev/null)
if [ -n "$zero_avail" ]; then
  log "[WARN] 以下模型所有 placement 均不可用: $zero_avail"
fi

# --- 探测健康断言 (2026-08-26): 版本契约拒/probe 门关死这类病在旧守护下
#     静默数周(实测: oMLX 0.6.2 被上界拒 compatible=False, 无任何告警,
#     全 placement 假死)。nodes diagnose 的 incompatible 计数连续 3 轮
#     (15min) > 0 即 WARN — 防同类病复发。 ---
INCOMPAT_N=$("$OMLXC" nodes diagnose mbp-m5-max-128g --json 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)['data']
    print(sum(o.get('count',0) for o in d.get('outcomes',[]) if o.get('code')=='incompatible'))
except Exception:
    print(-1)" 2>/dev/null || echo -1)
STREAK_F="$LOG_DIR/probe-incompat-streak"
if [ "${INCOMPAT_N:--1}" -gt 0 ] 2>/dev/null; then
  streak=$(( $(cat "$STREAK_F" 2>/dev/null || echo 0) + 1 ))
  echo "$streak" > "$STREAK_F"
  if [ "$streak" -ge 3 ]; then
    log "[WARN] MBP backend 探测 incompatible 连续 ${streak} 轮 — 版本契约/probe 门疑似关死(参考 2026-08-25 接力笔记⑦⑧层病历)"
  fi
else
  rm -f "$STREAK_F" 2>/dev/null
fi

# 日志裁剪，避免无限增长
if [ -f "$LOG" ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exit 0
