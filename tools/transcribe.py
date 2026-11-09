"""本地音频转写；模型必须已经下载，不在收到聊天时临时联网下载。"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
from organs.ear.asr import transcribe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, nargs="?")
    parser.add_argument("--stdin", action="store_true", help="从标准输入读取 NapCat 返回的 base64 音频")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--sound-model", type=Path, help="可选的本地 YAMNet ONNX 声源模型")
    args = parser.parse_args()
    if not args.stdin and (not args.file or not args.file.is_file()):
        parser.error("音频文件不存在")
    if not (args.model / "model.bin").is_file():
        parser.error("模型目录缺少 model.bin，请先安装本地模型")

    if args.stdin:
        raw = base64.b64decode(sys.stdin.buffer.read(6_666_669), validate=True)
    else:
        with args.file.open("rb") as handle:
            raw = handle.read(5_000_001)
    if len(raw) > 5_000_000:
        parser.error("音频超过 5 MB")
    print(json.dumps(transcribe(raw, args.model, args.language, sound_model=args.sound_model), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
