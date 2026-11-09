#!/bin/bash
# 启动「她」：器官核 + 业务通道 + NoneBot + NapCat（QQ 内）。
# 免扫码登录：把账号密码写进 channels/onebot/.state/qq.env（已 gitignore），格式见文件里的注释。
# 用法：start.command [silent|send]
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CH="$REPO/channels/onebot"
QQ="/Applications/QQ.app/Contents/MacOS/QQ"
CORE_PORT="${QQAGENT_CORE_PORT:-8787}"
LOGS="$CH/.state/logs"
MODE="${1:-${QQAGENT_CHANNEL_MODE:-silent}}"
TRANSPORT="${QQAGENT_TRANSPORT:-nonebot}"
NB_PYTHON="$REPO/.scratch/nonebot-venv/bin/python"
if [ "$TRANSPORT" != "nonebot" ] && [ "$TRANSPORT" != "direct" ]; then
  echo "QQAGENT_TRANSPORT 只能是 nonebot 或 direct"; exit 1
fi
if [ "$TRANSPORT" = "nonebot" ] && { [ ! -x "$NB_PYTHON" ] || [ ! -f "$CH/.state/nonebot.json" ]; }; then
  echo "NoneBot 尚未配置，见 channels/nonebot/README.md"; exit 1
fi

mkdir -p "$LOGS"

# 免扫码的凭据：账号/密码只落在这个本地文件里，绝不进仓库（.state/ 已 gitignore）。
# 认的是 NapCat 自己的变量名，所以谁看都明白这是给 NapCat 用的。
CRED="$CH/.state/qq.env"
if [ -f "$CRED" ]; then
  set -a; . "$CRED"; set +a
fi
ACCOUNT="${NAPCAT_QUICK_ACCOUNT:-2284938721}"
export NAPCAT_QUICK_ACCOUNT="$ACCOUNT"
[ -n "${NAPCAT_QUICK_PASSWORD:-}" ] && export NAPCAT_QUICK_PASSWORD
[ -n "${NAPCAT_QUICK_PASSWORD_MD5:-}" ] && export NAPCAT_QUICK_PASSWORD_MD5

echo "════════ 启动「她」════════"
echo " 仓库：$REPO"
echo " 模式：${MODE}（改模式就换一个词再启动：silent / send）"
echo

# 模型环境独立运行；未启用时不占用本机 GPU。
if (cd "$REPO/plugin" && python3 -c 'from organs.hand import comfy; raise SystemExit(0 if comfy.config()["enabled"] else 1)'); then
  bash "$REPO/tools/start-image.command" || echo "生图服务未启动；QQ 和逐笔绘画继续启动。"
fi

# ── 1) 核 ────────────────────────────────────────────────────────────
echo "• 核："
HEALTH="$(curl -s --max-time 2 "http://127.0.0.1:$CORE_PORT/healthz" 2>/dev/null)"
if [ -n "$HEALTH" ]; then
  echo "  已在跑（端口 ${CORE_PORT}）"
  # stale = 进程跑的是旧提交：本轮改了 organs/ 或 api/ 就必须重启，否则她说的还是旧代码。
  printf "%s" "$HEALTH" | grep -q '"stale": true' && echo "  ⚠ 跑的是旧提交（stale=true），要吃到新代码请先停核再启动"
else
  ( cd "$REPO/plugin" && QQAGENT_TRACE=1 nohup python3 -m api.devserver --port "$CORE_PORT" > "$LOGS/core.log" 2>&1 & )
  for i in $(seq 1 25); do
    curl -s --max-time 1 "http://127.0.0.1:$CORE_PORT/healthz" >/dev/null 2>&1 && break
    sleep 1
  done
  if curl -s --max-time 2 "http://127.0.0.1:$CORE_PORT/healthz" >/dev/null 2>&1; then
    echo "  ✓ 已起（日志：$LOGS/core.log）"
  else
    echo "  ✗ 没起来，看：tail -40 \"$LOGS/core.log\""
  fi
fi

echo
# ── 2) 通道 ──────────────────────────────────────────────────────────
echo "• 通道："
if lsof -nP -iTCP:6200 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  已在跑（端口 6200，pid=$(lsof -nP -iTCP:6200 -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $2}')）——不是本脚本起的，日志去向不明"
  echo "  想让它归本脚本管：先 stop-her.command 再重新启动"
else
  ( cd "$CH" && QQAGENT_CHANNEL_MODE="$MODE" nohup node server.ts --core "http://127.0.0.1:$CORE_PORT" > "$LOGS/channel.log" 2>&1 & )
  # 轮询读日志而不是 lsof：lsof 在 macOS 上每次 1–2 秒，几十圈就把等待吃光了。
  for i in $(seq 1 20); do
    grep -q 'listen' "$LOGS/channel.log" 2>/dev/null && break
    sleep 0.5
  done
  if grep -q 'listen' "$LOGS/channel.log" 2>/dev/null; then
    GATE="$(grep -m1 ' gate ' "$LOGS/channel.log" 2>/dev/null | sed 's/.*gate *//')"
    echo "  ✓ 已起（日志：$LOGS/channel.log）"
    [ -n "$GATE" ] && echo "  门：$GATE"
  else
    echo "  ✗ 没起来（6200 被占或 node 报错），看：tail -40 \"$LOGS/channel.log\""
  fi
fi

echo
# ── 2b) 成熟协议适配器：业务通道仍在 6200，NapCat 改连 6290 ───────────
if [ "$TRANSPORT" = "nonebot" ]; then
  echo "• NoneBot："
  if ! curl -fsS --max-time 2 http://127.0.0.1:6290/healthz >/dev/null 2>&1; then
    ( cd "$REPO/channels/nonebot" && nohup "$NB_PYTHON" run.py > "$LOGS/nonebot.log" 2>&1 & )
    for i in $(seq 1 20); do
      curl -fsS --max-time 1 http://127.0.0.1:6290/healthz >/dev/null 2>&1 && break
      sleep 0.5
    done
  fi
  if ! curl -fsS --max-time 2 http://127.0.0.1:6290/healthz >/dev/null 2>&1; then
    echo "  ✗ 启动失败，查看 $LOGS/nonebot.log"; exit 1
  fi
  echo "  ✓ 已起（NapCat → 6290 → 业务通道 6200）"
fi

echo
# ── 3) NapCat（跑在 QQ 里）───────────────────────────────────────────
echo "• NapCat / QQ："
echo "  账号：$ACCOUNT"
if [ -n "${NAPCAT_QUICK_PASSWORD:-}" ]; then
  echo "  密码：已配置（免扫码快速登录）"
elif [ -n "${NAPCAT_QUICK_PASSWORD_MD5:-}" ]; then
  echo "  密码：已配置 MD5（免扫码快速登录）"
else
  echo "  密码：未配置 → 会要求扫码。要免扫码，写 ${CRED}："
  echo "        NAPCAT_QUICK_ACCOUNT=$ACCOUNT"
  echo "        NAPCAT_QUICK_PASSWORD=你的小号密码"
fi
if pgrep -f "$QQ" >/dev/null 2>&1; then
  echo "  QQ 已在运行，跳过启动"
  # 环境变量只在进程启动那一刻进得去：改了 qq.env 不同时重起 QQ，等于白改。
  [ -f "$CRED" ] && echo "  ⚠ 改过 $CRED 的话必须重起 QQ 才生效：先 stop-her.command，再 start-her.command"
else
  # 必须带 --no-sandbox：loadNapCat.js 只在 argv 里有它时才加载 NapCat（open --args 传不进这个参数）
  nohup "$QQ" --no-sandbox >> "$LOGS/napcat.log" 2>&1 &
  for i in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 http://localhost:6099/ && break
    sleep 1
  done
  if curl -s -o /dev/null --max-time 1 http://localhost:6099/; then
    echo "  ✓ WebUI 就绪（6099）"
  else
    echo "  … 6099 还没听，看：tail -40 \"$LOGS/napcat.log\""
  fi
fi

echo
# ── 4) 等 NapCat 反连到通道（它自己的重连间隔是 30 秒）───────────────
echo "• 等 NapCat 连上通道（最多 35 秒）…"
CONNECTED=0
for i in $(seq 1 35); do
  if [ "$TRANSPORT" = "nonebot" ]; then
    if curl -fsS --max-time 1 http://127.0.0.1:6290/healthz 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(b["business_connected"] and b["running"] for b in d["bots"]) else 1)' 2>/dev/null; then CONNECTED=1; break; fi
  elif grep -q '已连接' "$LOGS/channel.log" 2>/dev/null; then CONNECTED=1; break
  fi
  sleep 1
done
if [ "$CONNECTED" = "1" ]; then
  grep -m1 'conn ' "$LOGS/channel.log" 2>/dev/null | sed 's/^/  ✓ /'
else
  if tail -80 "$LOGS/napcat.log" 2>/dev/null | grep -q 'prelogin'; then
    echo "  ✗ 没连上——QQ 还停在登录界面。"
    [ -z "${NAPCAT_QUICK_PASSWORD:-}${NAPCAT_QUICK_PASSWORD_MD5:-}" ] && echo "     现在没有密码可用，所以只能扫码；写进 $CRED 后重启即可免扫码。"
  else
    echo "  ✗ 没连上：查看网络配置（NoneBot: 6290/onebot/v11/ws；direct: 6200/ws）"
  fi
  echo "  面板：http://localhost:6099/webui"
fi

echo
echo "─────────────────────────────"
echo " 她说话的开关 : 本次 ${MODE}（silent=只记账不发 / send=真发）"
echo " 通道         : ${TRANSPORT}（业务入口 6200；NoneBot 入口 6290）"
echo " 核           : http://127.0.0.1:$CORE_PORT/healthz"
echo " NapCat 面板  : http://localhost:6099/webui"
echo " 账           : $CH/.state/ledger.jsonl"
echo " 追日志       : tail -f $LOGS/channel.log"
echo " 免扫码账号   : $CRED"
echo "─────────────────────────────"
echo
[ -t 0 ] && read -n 1 -s -p "按任意键关闭此窗口…"
echo
