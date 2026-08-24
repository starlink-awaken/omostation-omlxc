#!/bin/bash
# mac-mini 7x24 主力常驻节点 · 开机后一次性到位脚本 (2026-08-24)
#
# 背景: mac-mini 升格为主力算力节点(用户决策)。此前它处于"想起来才开"
# 状态, 2026-08-23 15:14 后深度离线(关机/断电, tailscale Endpoints 全空)。
# 本脚本在 mac-mini 开机联网后, 从 MBP 通过 SSH 执行, 一次配齐:
#   1. pmset: 永不睡眠 + 网络唤醒 + 断电自启 + 每日 09:00 兜底开机
#   2. tailscale 常驻确认
#   3. LM Studio server 自启 + q8 qwythos 模型部署(~9.5GB 下载, 可跳过用拷贝)
#   4. SSH 公钥送钥匙(2026-08-23 SSH 密钥加固后新节点的标准动作)
#
# 用法: 在 MBP 上执行  bash scripts/mac-mini-7x24-setup.sh
# 前提: mac-mini 已开机、与你同网或 tailscale 已连上。
set -uo pipefail

TARGET="xiamingxing@mac-mini.tail0483a1.ts.net"
SSH="ssh -o ConnectTimeout=10 -o BatchMode=yes"

echo "=== 0. 连通性 ==="
if ! $SSH "$TARGET" "echo reachable"; then
  echo "❌ SSH 不通。可能原因: (a) mac-mini 尚未开机/联网 (b) tailscale 未连"
  echo "   (c) 2026-08-23 SSH 加固后该节点还没有你的公钥 —— 需要先用密码送钥匙:"
  echo "       ssh-copy-id $TARGET"
  exit 1
fi

echo "=== 1. pmset 7x24 配置(需 sudo, 会提示输密码) ==="
$SSH "$TARGET" "sudo pmset -a sleep 0 disksleep 0 womp 1 autorestart 1 && sudo pmset repeat wakeorpoweron MTWRFSU 09:00:00 && pmset -g | grep -E 'sleep|womp|autorestart'"

echo "=== 2. tailscale 状态 ==="
$SSH "$TARGET" "tailscale status --self 2>/dev/null || /Applications/Tailscale.app/Contents/MacOS/Tailscale status --self 2>/dev/null || echo 'tailscale CLI 未找到, 检查登录项'"

echo "=== 3. LM Studio server 自启 + 模型盘点 ==="
$SSH "$TARGET" 'export PATH="$HOME/.lmstudio/bin:$PATH"; lms server start 2>/dev/null; lms ls 2>/dev/null | grep -i "qwythos" || echo "NO_QWYTHOS_Q8: 需要下载(见步骤4)"'

echo "=== 4. q8 模型部署(仅当缺失时手动执行) ==="
cat <<'EOF'
  在 mac-mini 上执行:
    lms get xunkutech-ai/Qwythos-9B-Claude-Mythos-5-1M-MLX-oq8-mtp
  (若搜索不到精确名, 用: lms search qwythos 找 oq8 变体)
  或从 MBP 拷贝模型目录(约 9.5GB):
    rsync -avP ~/.lmstudio/models/xunkutech-ai/ TARGET:~/.lmstudio/models/
EOF

echo "=== 5. 验证: 回 MBP 后跑 ==="
echo "  bash scripts/full-status.sh   # mac-mini 应转为 online"
echo "  python3 scripts/remote-resident-maintain.py  # 手动触发一轮常驻补齐"
echo ""
echo "完成。watchdog 每 5 分钟会自动维持常驻(config.toml policies.remote_resident)。"
