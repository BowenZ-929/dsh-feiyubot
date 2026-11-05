#!/bin/bash
# 双击关闭「她」：通道 + NapCat（NapCat 跑在 QQ 进程里，所以会退出 QQ）。
# 核默认不杀（面板还要用）；要连核一起停：./stop.command --with-core
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CH="$REPO/channels/onebot"
QQ="/Applications/QQ.app/Contents/MacOS/QQ"
CORE_PORT="${QQAGENT_CORE_PORT:-8787}"
WITH_CORE=0
[ "${1:-}" = "--with-core" ] && WITH_CORE=1

echo "════════ 关闭「她」════════"
echo
echo "⚠️  通道会停；NapCat 跑在 QQ 进程里，停它会一起退出 QQ。"
read -p "确认关闭？[y/N] " ans || ans=""
case "$ans" in
  y|Y) ;;
  *) echo "已取消"; sleep 1; exit 0 ;;
esac

echo
echo "• 停止通道…"
if lsof -ti:6200 -sTCP:LISTEN >/dev/null 2>&1; then
  lsof -ti:6200 -sTCP:LISTEN | xargs kill 2>/dev/null
  sleep 1
  if lsof -ti:6200 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  未退出，强制结束"
    lsof -ti:6200 -sTCP:LISTEN | xargs kill -9 2>/dev/null
  else
    echo "  ✓ 已停（6200 空出来了）"
  fi
else
  echo "  通道本来就没在跑"
fi

echo "• 停止 NapCat / QQ…"
if pkill -f "$QQ" 2>/dev/null; then
  sleep 2
  if pgrep -f "$QQ" >/dev/null 2>&1; then
    echo "  未退出，强制结束"; pkill -9 -f "$QQ" 2>/dev/null
  else
    echo "  ✓ 已退出"
  fi
else
  echo "  QQ 本来就没在运行"
fi

echo
if [ "$WITH_CORE" = "1" ]; then
  echo "• 停止核…"
  if lsof -ti:"$CORE_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    lsof -ti:"$CORE_PORT" -sTCP:LISTEN | xargs kill 2>/dev/null
    sleep 1
    lsof -ti:"$CORE_PORT" -sTCP:LISTEN >/dev/null 2>&1 && echo "  ⚠ $CORE_PORT 仍在监听" || echo "  ✓ 已停"
  else
    echo "  核本来就没在跑"
  fi
else
  echo "• 核：保留运行（面板还要用）。要一起停：./stop.command --with-core"
fi

echo
echo "─────────────────────────────"
lsof -nP -iTCP:6200 -sTCP:LISTEN >/dev/null 2>&1 && echo " ⚠ 6200 仍在监听" || echo " ✓ 通道已停（下次启动默认 silent）"
lsof -nP -iTCP:6099 -sTCP:LISTEN >/dev/null 2>&1 && echo " ⚠ 6099 仍在监听" || echo " ✓ NapCat 已停"
echo "─────────────────────────────"
echo
[ -t 0 ] && read -n 1 -s -p "按任意键关闭此窗口…"
echo
