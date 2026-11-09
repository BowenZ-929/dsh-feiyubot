#!/usr/bin/env python3
"""提炼公开评论的手机读屏 JSON；候选不会自动进入聊天。

python3 tools/learn_comments.py .scratch/phone-acceptance/comments-scrolled.json --context '已观察的视频/帖子语境'
python3 tools/learn_comments.py --enable RECORD_ID
python3 tools/learn_comments.py --disable RECORD_ID
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
from organs.home import expressions
from organs.mind import expression_learning


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", nargs="?")
    parser.add_argument("--context")
    parser.add_argument("--root", type=Path, help="可选的独立语料库根目录，用于验收")
    switch = parser.add_mutually_exclusive_group()
    switch.add_argument("--enable")
    switch.add_argument("--disable")
    args = parser.parse_args()
    if args.enable or args.disable:
        result = expressions.enable(args.enable or args.disable, bool(args.enable), args.root)
    elif args.snapshot and args.context:
        result = expression_learning.learn(json.loads(Path(args.snapshot).read_text()), context=args.context, root=args.root)
    else:
        parser.error("请提供读屏文件和 --context，或用 --enable / --disable 管理已验证的卡片")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
