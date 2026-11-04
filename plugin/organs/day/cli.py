"""日子线的命令行入口：人在终端里推一次节律、跑一次夜、看账。

用法（在 plugin/ 下）：
    python3 -m organs.day.cli tick                    # 手工 tick 一次（--now 可注入时刻）
    python3 -m organs.day.cli plan                    # 计划账三栏
    python3 -m organs.day.cli night                   # 跑一次夜（--brain-root / --heart-root）
    python3 -m organs.day.cli log                     # 夜日志尾部

四条子命令都不起线程：节律线程只有 devserver --day 开；这里跑一次就退出。
时间一律可注入（--now），验收不依赖真实时钟。
日子的账一律落在 --root（默认 DAY_ROOT > 包内 data/）。--brain-root 不给就是
「脑未载入」，夜的第 1 步会如实记 failed，不假装结过。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from . import night, plan, rhythm


def _dump(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python3 -m organs.day.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, text in (("tick", "手工 tick 一次：数到点、算下一次、换日记账"),
                       ("plan", "计划账：pending / consumed / blocked 三栏"),
                       ("log", "夜日志尾部")):
        child = sub.add_parser(name, help=text)
        child.add_argument("--root", default=None, help="日子线数据根（默认 DAY_ROOT > 包内 data/）")
        if name != "plan":
            child.add_argument("--limit", type=int, default=20, help="日志条数上限")
        if name == "tick":
            child.add_argument("--now", default=None, help="注入时刻（ISO8601，带时区）")

    night_parser = sub.add_parser("night", help="跑一次夜：四步固定顺序，每步单独兜错")
    night_parser.add_argument("--root", default=None, help="日子线数据根（默认 DAY_ROOT > 包内 data/）")
    night_parser.add_argument("--now", default=None, help="注入时刻（ISO8601，带时区）")
    night_parser.add_argument("--brain-root", default=None, help="脑数据目录（不给就是脑未载入）")
    night_parser.add_argument("--heart-root", default=None, help="心数据目录（不给就用心的默认根）")
    night_parser.add_argument("--break-step", type=int, default=None,
                              help="验收用：故意让第 N 步抛错，看后面几步还跑不跑")
    return parser.parse_args(argv)


def _brain(path: Optional[str]) -> Any:
    """只在给了 --brain-root 时才载入脑；不给就是 None，夜的第 1 步会如实记 failed。"""
    if not path:
        return None
    from organs.brain import load as load_brain
    return load_brain(Path(path))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    if args.cmd == "tick":
        payload = rhythm.tick(args.now, root=args.root)
        _dump(payload)
        return 0
    if args.cmd == "plan":
        _dump(plan.listing(args.root))
        return 0
    if args.cmd == "log":
        _dump(night.log_tail(args.limit, root=args.root))
        return 0
    payload = night.run(args.now, root=args.root, brain=_brain(args.brain_root),
                        heart_root=args.heart_root, break_step=args.break_step)
    _dump(payload)
    # 日志说什么，退出码就是什么：有一步失败就回非 0
    print("退出码 %d" % (0 if payload["ok"] else 1))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
