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
  # 两件事分开读：stale = 进程跑的是旧提交（必须重启）；dirty = 有未提交改动（重启更保险）
  STALE=0; DIRTY=0
  printf "%s" "$HEALTH" | grep -q '"stale": true' && STALE=1
  printf "%s" "$HEALTH" | grep -q '"dirty": true' && DIRTY=1
  if [ "$STALE" = "1" ]; then
    echo "核在跑，但进程跑的是旧提交（stale=true）——必须重启"
    kill "$(lsof -ti:"$PORT" | head -1)" 2>/dev/null; sleep 1
    ( cd "$REPO/plugin" && nohup python3 -m api.devserver --port "$PORT" > /tmp/qqagent-core.log 2>&1 & )
    sleep 2
  elif [ "$DIRTY" = "1" ]; then
    echo "核在跑，提交没变但有未提交改动（dirty=true）——重启更保险，不重启也不算陈旧"
  else
    echo "已经在跑，且跑的就是当前提交、工作树干净（stale=false, dirty=false）"
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
