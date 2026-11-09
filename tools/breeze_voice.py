"""配置 Breeze 密钥，或在本机合成一句试听；绝不发送 QQ。"""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
from organs import paths
from organs.mouth import voice


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "status", "test"))
    parser.add_argument("--text", default="嗯？你叫我呀，我刚才走了一下神。")
    args = parser.parse_args()
    if args.action == "setup":
        key = getpass.getpass("粘贴 Breeze API 密钥（输入不显示，回车保存）：").strip()
        if not key.startswith("brz_") or any(c.isspace() for c in key):
            parser.error("请输入 Breeze 的 brz_ 开头密钥；兑换码 DAFEIYU 不能用作密钥")
        path = paths.organ_dir("mouth") / "breeze_api_key"
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(key + "\n")
        print("密钥已保存在本机。没有调用付费服务，QQ 语音开关未改变。")
    elif args.action == "test":
        result = voice.generate(args.text)
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] != "generated":
            raise SystemExit(1)
    else:
        print(json.dumps({"config": voice.load_config(), "credential_ready": bool(voice.read_key()),
                          "attempts_today": voice.attempts_today()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
