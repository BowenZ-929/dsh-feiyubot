#!/bin/bash
TASK_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$TASK_DIR/tools/breeze_voice.py" setup
read -r -p "按回车关闭窗口。" _
