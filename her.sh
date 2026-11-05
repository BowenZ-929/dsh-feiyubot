#!/bin/bash
# 她的面板：起核（没在跑才起）+ 打开她自己的 DSH。
# 她住在自己的 DSH 数据目录里（~/qqagent-dsh），和你日常用的 ~/.dsh 完全分开。
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
export DSH_HOME="$HOME/qqagent-dsh"
PORT="${QQAGENT_CORE_PORT:-8787}"

echo "== 核 =="
if curl -s --max-time 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
  echo "已经在跑（人起的优先，不重复起）"
else
  echo "没在跑，起一个…"
  ( cd "$REPO/plugin" && nohup python3 -m api.devserver --port "$PORT" > /tmp/qqagent-core.log 2>&1 & )
  sleep 2
fi
curl -s --max-time 3 "http://127.0.0.1:${PORT}/healthz" | head -c 300; echo

echo "== 她自己的 DSH =="
echo "数据目录：$DSH_HOME（和你日常的 ~/.dsh 分开）"
exec dsh --profile web
