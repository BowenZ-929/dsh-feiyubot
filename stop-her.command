#!/bin/bash
# 双击这个关闭「她」（通道 + NapCat/QQ；核默认留着，要一起停：./stop-her.command --with-core）
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/channels/onebot/stop.command" "$@"
