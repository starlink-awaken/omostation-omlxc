#!/bin/bash
# daemon-upgrade-precheck.sh — daemon 3.4.0(uv tools) → repo HEAD 升级预检
#
# 背景(2026-08-25): repo HEAD 背了两项已修但未生效的修复 —
#   1. RemoteResidentConfig.role 字段(embedding 迁移配套)
#   2. OpenAIChatBody OpenAI 兼容字段集(UDS 直连 422 根治)
# 运行中的 daemon 是 uv tools 3.4.0(旧代码), 升级后双修复同时生效。
#
# 本脚本只读预检 + 打印执行命令, 不做任何变更。升级动作人工执行。
set -uo pipefail

REPO="$HOME/Workspace/projects/omlxc"
TOOLS_PY="$HOME/.local/share/uv/tools/omlxc/bin/python"
OMLXC="$HOME/.local/bin/omlxc"

echo "═══ omlxc daemon 升级预检 ═══"
echo

echo "[1/4] 版本基线"
if [ -x "$TOOLS_PY" ]; then
  cur=$("$TOOLS_PY" -c "import importlib.metadata; print(importlib.metadata.version('omlxc'))" 2>/dev/null)
  echo "  uv tools 当前: v${cur:-?}"
else
  echo "  ⚠️ uv tools 安装未找到"; exit 1
fi
repo_ver=$(grep '^version' "$REPO/pyproject.toml" | head -1 | sed 's/version = "\(.*\)"/\1/')
repo_sha=$(git -C "$REPO" rev-parse --short HEAD)
echo "  repo HEAD:    v${repo_ver} @ ${repo_sha}"
echo "  (同版本号不同代码 — 升级=同版本重装)"
echo

echo "[2/4] 工作区干净度"
dirty=$(git -C "$REPO" status --short | grep -v "^?? .omc" | wc -l | tr -d ' ')
if [ "$dirty" -gt 0 ]; then
  echo "  ⚠️ 有 $dirty 个未提交变更 — 建议先提交(避免升级后无法回滚到当前态)"
else
  echo "  ✅ 干净"
fi
echo

echo "[3/4] 测试绿检(全量 ~2min)"
cd "$REPO" && if uv run pytest -q 2>&1 | tail -1; then :; fi
echo

echo "[4/4] 执行命令(人工复制运行)"
cat <<EOF
  ── 升级 ──
  cd "$REPO" && uv tool install --force "$REPO"
  "$OMLXC" daemon restart --yes --confirm-impact

  ── 验证 ──
  "$OMLXC" daemon status
  "$OMLXC" models list --json | head -5
  # 422 修复验证(应不再报 E100):
  python3 -c "
import httpx
sock='/Users/xiamingxing/.config/omlxc/omlxcd.sock'
r=httpx.post('http://omlxc/openai/v1/chat/completions',json={'model':'coding','messages':[{'role':'user','content':'hi'}],'stop':['\\\\n']},transport=httpx.HTTPTransport(uds=sock),timeout=30)
print('stop字段:',r.status_code,'(期望 非422)')"

  ── 回滚(若异常) ──
  git -C "$REPO" checkout <升级前commit: ${repo_sha}>
  cd "$REPO" && uv tool install --force "$REPO"
  "$OMLXC" daemon restart --yes --confirm-impact
EOF
echo
echo "═══ 预检完成 — 升级动作请人工执行 ═══"