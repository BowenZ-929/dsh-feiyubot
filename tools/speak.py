"""macOS 本地语音合成，输出可供 QQ 发送的 WAV，不调用付费服务。"""
import argparse
import json
from pathlib import Path
import subprocess
import wave


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--voice", default="Tingting")
    args = parser.parse_args()
    if not args.text.strip():
        parser.error("朗读文本不能为空")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/bin/say", "-v", args.voice, "-o", str(args.out),
                    "--file-format=WAVE", "--data-format=LEI16@24000"],
                   input=args.text, text=True, check=True, timeout=60)
    with wave.open(str(args.out), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
        if duration <= 0:
            raise ValueError("语音合成结果为空")
    print(json.dumps({"ok": True, "file": str(args.out.resolve()),
                      "duration": duration, "voice": args.voice}, ensure_ascii=False))


if __name__ == "__main__":
    main()
