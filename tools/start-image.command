#!/bin/bash
# 已安装的本机生图服务；与聊天 Python 环境分开，仅监听回环地址。
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$REPO/local/image-runtime"
if curl --fail --silent --max-time 2 http://127.0.0.1:8188/system_stats >/dev/null; then
  echo "本地生图服务已在运行。"
  exit 0
fi
if lsof -tiTCP:8188 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "8188 已被占用；没有启动第二个生图进程。"
  exit 1
fi
if [ ! -x "$RUNTIME/venv/bin/python" ] || [ ! -f "$RUNTIME/ComfyUI/main.py" ]; then
  echo "本地生图环境尚未安装，见 docs/local-image-2026-11-08.md。"
  exit 1
fi
"$RUNTIME/venv/bin/python" - "$RUNTIME" <<'PY'
from pathlib import Path
import subprocess
import sys
root = Path(sys.argv[1])
with (root / 'server.log').open('ab') as log:
    process = subprocess.Popen([sys.executable, 'main.py', '--listen', '127.0.0.1',
                                '--port', '8188', '--disable-auto-launch'],
        cwd=root / 'ComfyUI', stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True)
(root / 'server.pid').write_text(str(process.pid) + '\n')
PY
echo "本地生图服务正在启动；日志：$RUNTIME/server.log"
