#!/bin/bash
# 她的面板：起核（没在跑才起）+ 打开她自己的 DSH。
# 她住在自己的 DSH 数据目录里（~/qqagent-dsh），和你日常用的 ~/.dsh 完全分开。
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
export DSH_HOME="$HOME/qqagent-dsh"
PORT="${QQAGENT_CORE_PORT:-8787}"

echo "== 核 =="
HEALTH=$(curl -s --max-time 2 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null)
if [ -n "$HEALTH" ]; then
  # 核活着也可能跑的是旧代码：/healthz 里的 stale 就是为这件事准备的
  if printf "%s" "$HEALTH" | grep -q '"stale": true'; then
    echo "核在跑，但源码在进程脚下改过（stale）——重启它"
    kill "$(lsof -ti:"$PORT" | head -1)" 2>/dev/null; sleep 1
    ( cd "$REPO/plugin" && nohup python3 -m api.devserver --port "$PORT" > /tmp/qqagent-core.log 2>&1 & )
    sleep 2
  else
    echo "已经在跑，且跑的是当前代码（stale=false）"
  fi
else
  echo "没在跑，起一个…"
  ( cd "$REPO/plugin" && nohup python3 -m api.devserver --port "$PORT" > /tmp/qqagent-core.log 2>&1 & )
  sleep 2
fi
curl -s --max-time 3 "http://127.0.0.1:${PORT}/healthz" | head -c 300; echo

echo "== 她自己的 DSH =="
echo "数据目录：$DSH_HOME（和你日常的 ~/.dsh 分开）"
exec dsh --profile web
