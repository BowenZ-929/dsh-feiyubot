#!/bin/bash
# 守夜：核（8787）与通道（6200）掉线或"每句都崩"时自动拉起。
# 三层：进程在 → 健康 200 → 真调用探针（走闸与审计，不花模型钱、不写她的记忆）
# 用法：nohup bash watch.sh > /dev/null 2>&1 &
# 暂停：touch channels/onebot/.state/watch.off
set -uo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
CH="$REPO/channels/onebot"
LOG="/tmp/watch.log"
say() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"; }
fails=0

restart_core() {
  ( cd "$REPO/plugin" && kill "$(lsof -ti:8787 2>/dev/null)" 2>/dev/null; sleep 1
    nohup python3 -m api.devserver --port 8787 >> /tmp/qqagent-core.log 2>&1 & )
  sleep 5
}

while true; do
  if [ -f "$CH/.state/watch.off" ]; then sleep 20; continue; fi


  # ⓪ 只许有一个核：多个 api.devserver 抢 8787 会让 /chat 一调就崩
  n=$(pgrep -f 'api.devserver' | wc -l | tr -d ' ')
  if [ "$n" -gt 1 ]; then
    keep=$(pgrep -f 'api.devserver' | head -1)
    for pid in $(pgrep -f 'api.devserver'); do
      [ "$pid" != "$keep" ] && kill "$pid" 2>/dev/null
    done
    say "发现 $n 个核，只留 pid=$keep，其余已杀"
  fi

  # ⓪2 核"活着但 /chat 崩"：近 3 分钟出现 2 次以上「核不可达」→ 重启核
  bad=$(grep '核不可达' "$CH/.state/logs/channel.log" 2>/dev/null | tail -50 | while read -r line; do
          ts=$(echo "$line" | cut -c1-20)
          echo "$ts"
        done | python3 -c "
import sys,datetime
now=datetime.datetime.now(datetime.timezone.utc)
n=0
for s in sys.stdin:
    try: t=datetime.datetime.fromisoformat(s.strip().replace('Z','+00:00'))
    except Exception: continue
    if (now-t).total_seconds()<180: n+=1
print(n)
" 2>/dev/null || echo 0)
  if [ "${bad:-0}" -ge 2 ]; then
    say "近 3 分钟「核不可达」${bad} 次——核活着但 /chat 崩，重启核"
    restart_core
  fi

  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8787/healthz 2>/dev/null || echo 000)
  if [ "$code" != "200" ]; then
    say "核挂了（healthz $code）→ 拉起"
    restart_core
  fi

  probe=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:8787/mouth/gate?text=%E5%9C%A8%E5%90%97" 2>/dev/null || echo 000)
  if [ "$probe" = "200" ]; then
    [ "$fails" -gt 0 ] && say "探针恢复（之前连续失败 $fails 次）"
    fails=0
  else
    fails=$((fails+1))
    say "探针失败第 $fails 次（/mouth/gate $probe）"
    if [ "$fails" -ge 3 ]; then
      say "连续 3 次失败，停止盲重启——代码可能是半成品，等人看"
      fails=0
    else
      say "→ 重启核"
      restart_core
    fi
  fi

  if ! lsof -ti:6200 >/dev/null 2>&1; then
    say "通道挂了 → 拉起（send）"
    ( cd "$REPO" && nohup bash "$CH/start.command" send >> /tmp/her-watch.log 2>&1 & )
    sleep 10
  fi

  sleep 45
done