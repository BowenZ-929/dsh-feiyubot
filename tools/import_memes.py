#!/usr/bin/env python3
"""把旧bot的semantic_metadata.json与原图复制到新家的表情库；不改旧库、不调用模型。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugin'))
from organs.home import memes

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('metadata',type=Path)
    parser.add_argument('--root',type=Path,help='独立验收用的家目录；默认使用当前项目的家')
    args=parser.parse_args()
    print(json.dumps(memes.import_legacy(args.metadata,root=args.root),ensure_ascii=False,indent=2))
