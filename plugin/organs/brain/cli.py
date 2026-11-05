"""脑的命令行入口：本地验收用，不碰任何插件层。

用法（在 plugin/ 下）：
    python3 -m organs.brain.cli ingest --who 主人 --text "今天说的话"
    python3 -m organs.brain.cli today
    python3 -m organs.brain.cli people
    python3 -m organs.brain.cli recall --q "以前说过什么"
    python3 -m organs.brain.cli self
    python3 -m organs.brain.cli proposals
    python3 -m organs.brain.cli night
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional, Sequence

from .store import BrainStore

ROOT = Path(__file__).resolve().parent / "data"


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python3 -m organs.brain.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ingester = sub.add_parser("ingest", help="一条消息进：今日追加或合并 + 人名录认人")
    ingester.add_argument("--who", required=True)
    ingester.add_argument("--text", required=True)
    ingester.add_argument("--display", default=None)
    ingester.add_argument("--mood", default=None)
    ingester.add_argument("--ts", default=None)

    sub.add_parser("today", help="打印今日条目")
    sub.add_parser("people", help="打印人名录与熟稔读数")
    sub.add_parser("longterm", help="打印长期字面档")
    sub.add_parser("proposals", help="打印当前提案框")
    sub.add_parser("self", help="打印最新一条自述")

    recaller = sub.add_parser("recall", help="回想：原文 + 字面检索")
    recaller.add_argument("--q", required=True)
    recaller.add_argument("--who", default=None)
    recaller.add_argument("--limit", type=int, default=3)
    recaller.add_argument("--now", default=None)

    recorder = sub.add_parser("self-record", help="追加一条自述（只记录，不解释）")
    recorder.add_argument("--text", required=True)
    recorder.add_argument("--note", default="")

    nighter = sub.add_parser("night", help="夜结算：换日降级 + 产提案")
    nighter.add_argument("--now", default=None)

    retirer = sub.add_parser("retire", help="退役一条记忆（stale）：行还在，回想不再返回")
    retirer.add_argument("--id", required=True)
    retirer.add_argument("--ts", default=None)

    backfiller = sub.add_parser("backfill", help="存量回填：补齐 status/来源/α/β/evidence_key")
    importer = sub.add_parser("import", help="数据导入器：旧存量行归一化后追加进长期档")
    importer.add_argument("--json", required=True, help="JSON 数组，每项至少带 text")

    evaler = sub.add_parser("eval", help="评测最小集：20 题 + 6 锚题，闭卷两臂，人抽检")
    evaler.add_argument("--now", default=None)

    sub.add_parser("three-arm", help="三臂对照：冷启动 / 朴素字面基线 / 我们的方案（跑 fixture）")
    sub.add_parser("status-writers", help="状态枚举 → 写者断言：未接线的会打印「未接线」")
    sub.add_parser("check", help="结构边守卫 + 状态写者断言")
    neighbors_p = sub.add_parser("neighbors", help="给一个节点查邻居（人—事—话题边）")
    neighbors_p.add_argument("--node", required=True)
    cold_p = sub.add_parser("cold", help="冷仓读回：按 id 取原文（不看过期状态）")
    cold_p.add_argument("--id", required=True)
    sub.add_parser("preflight", help="接库前只读检查：只报不修")
    return parser.parse_args(argv)


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    store = BrainStore(ROOT)
    if args.cmd == "ingest":
        _print(store.ingest(args.who, args.text, mood=args.mood,
                            display=args.display, ts=args.ts))
    elif args.cmd == "today":
        _print(store.today_entries())
    elif args.cmd == "people":
        _print(store.people_list())
    elif args.cmd == "longterm":
        _print(store.longterm_entries())
    elif args.cmd == "proposals":
        _print(store.proposals())
    elif args.cmd == "self":
        _print(store.selfnarration_latest())
    elif args.cmd == "self-record":
        _print(store.record_selfnarration(args.text, note=args.note))
    elif args.cmd == "recall":
        _print(store.recall(args.q, who=args.who, now_ts=args.now, limit=args.limit))
    elif args.cmd == "night":
        _print(store.run_night(args.now))
    elif args.cmd == "retire":
        _print(store.retire(args.id, ts=args.ts))
    elif args.cmd == "backfill":
        _print(store.backfill())
    elif args.cmd == "import":
        _print(store.import_rows(json.loads(args.json)))
    elif args.cmd == "eval":
        from .bench import eval as eval_mod

        print(eval_mod.render_for_human(args.now or "2026-11-03T12:00:00+00:00"))
    elif args.cmd == "three-arm":
        from .bench import three_arm

        print(three_arm.render(three_arm.run()))
    elif args.cmd == "status-writers":
        from .model import status_writer_report

        for row in status_writer_report():
            writer = row["writer"] if row["wired"] else "None（未接线）"
            print("%s -> %s" % (row["status"], writer))
    elif args.cmd == "check":
        health = store.edge_health()
        print("结构边守卫: %s" % ("通过" if health["ok"] else "FAIL"))
        print("边: total=%d by_type=%s structural_ratio=%s"
              % (health["total"], health["by_type"], health["structural_ratio"]))
        from .model import status_writer_report

        for row in status_writer_report():
            writer = row["writer"] if row["wired"] else "None（未接线）"
            print("%s -> %s" % (row["status"], writer))
    elif args.cmd == "neighbors":
        _print(store.neighbors(args.node))
    elif args.cmd == "cold":
        _print(store.cold_lookup(args.id))
    elif args.cmd == "preflight":
        from .preflight import preflight

        _print(preflight(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
