#!/bin/bash
# 双击这个启动「她」（核 + 通道 + NapCat）。真正的逻辑在 channels/onebot/start.command，这里只做入口。
# 双击 → 会问一句"要不要她真的说话"；也可以命令行传参：./start-her.command send
DIR="$(cd "$(dirname "$0")" && pwd)"
MODE=""

if [ $# -gt 0 ]; then
  MODE="$1"
elif [ -t 0 ]; then
  # 双击时问一句，比让人记参数靠谱；默认不回话（silent），按错了也不会乱发。
  echo "要她真的把话说出去吗？"
  read -r -p "  输 y = 真发（send），直接回车 = 只听（silent）： " ans
  case "$ans" in
    y|Y|yes|YES) MODE="send" ;;
    *) MODE="silent" ;;
  esac
fi

if [ -n "$MODE" ]; then
  exec "$DIR/channels/onebot/start.command" "$MODE"
else
  exec "$DIR/channels/onebot/start.command"
fi
