"""心的人可读自检：python3 -m organs.heart.cli <card|energy-day|mood-praise3|gates|all>。

只做演示与取证，不是产品入口：每条命令都把数据根指到 tempfile.mkdtemp() 的临时目录，
第一行就把这个路径打印出来——**仓里的 data 一个字节都不碰**。
（mood.read 读时会往轨迹账本追加一行、energy.read 读时会把结算写回，这是它们的设计，
所以拿默认根跑一次就会留下运行时文件；这条铁律就是为了不再发生那件事。）

输出是给人看的：轴名、档位词、状态与命中门都直接排成表格/列表，不做花活。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from organs.heart import card as card_module
from organs.heart import energy as energy_module
from organs.heart import engage as engage_module
from organs.heart import model
from organs.heart import mood as mood_module

SECTIONS = ("card", "energy-day", "mood-praise3", "gates")


def _stamp(day: int, hour: int, minute: int = 0) -> str:
    """造一个带 +09:00 的固定时刻：演示不看"现在几点"，也不依赖本机时区。"""
    return "2026-11-%02dT%02d:%02d:00+09:00" % (day, hour, minute)


def _root_note(root: Path) -> None:
    print("  数据根（临时目录）：%s" % root)


# ---- card：六轴档位 + 待定两轴标灰 + 拒载一张坏卡 --------------------------


def demo_card(root: Path) -> None:
    """四对轴读成档位词、两轴标灰、再拿一张缺轴坏卡当场拒载。"""
    _root_note(root)
    (root / card_module.CARD_FILE).write_bytes(card_module.DEFAULT_CARD_PATH.read_bytes())
    card = card_module.load_card(root / card_module.CARD_FILE)
    view = card_module.view(card)
    print("  载入四对轴：%s" % "、".join(view["loaded"]))
    for axis in view["loaded"]:
        row = view["tiers"][axis]
        print("    %-4s 心里 %s（第 %d 档） / 面上 %s（第 %d 档）"
              % (axis, row["inner_word"], row["inner_tier"],
                 row["shown_word"], row["shown_tier"]))
    print("  待定两轴（greyed=True，跳过注入，不写数也不写 0）：")
    for item in view["pending"]:
        print("    %-4s greyed=%s" % (item["axis"], item["greyed"]))
    print("  档位词文本（render_text：只讲档位，不含阿拉伯数字）：")
    for line in card_module.render_text(card).splitlines():
        print("    " + line)

    bad = {
        "scale": {"min": 0, "max": 100},
        "axes": [
            {"id": "服气", "inner": 90, "shown": 20},
            {"id": "心疼", "inner": 70, "shown": 60},
            {"id": "耐心", "pending": True},
            {"id": "记性", "pending": True},
        ],
    }
    bad_path = root / "bad-card.json"
    bad_path.write_text(json.dumps(bad, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        card_module.load_card(bad_path)
    except card_module.CardError as exc:
        print("  缺轴坏卡被拒：code=%s，共 %d 条理由" % (exc.code, len(exc.issues)))
        for issue in exc.issues:
            print("    %-18s %-4s %s"
                  % (issue["code"], issue["axis"] or "-", issue["message"]))
    else:  # pragma: no cover - 走不到才是对的
        print("  !! 坏卡居然加载成功：这是缺陷，不是演示失误")


# ---- energy-day：80 往前推一天 + 连读两次同值 -----------------------------


def demo_energy_day(root: Path) -> None:
    """起点 80，前推一天看结算到多少；同一时刻连读两次必须同值。"""
    _root_note(root)
    start = _stamp(5, 10)
    first = energy_module.read(root, start)
    print("  起点 %s：value=%.1f（%s）" % (start, first["value"], first["tier"]))
    later = _stamp(6, 10)
    after = energy_module.read(root, later)
    print("  前推一天 %s：value=%.1f（%s），补算 %.2f 小时（capped=%s）"
          % (later, after["value"], after["tier"], after["hours"], after["capped"]))
    again = energy_module.read(root, later)
    print("  同一时刻再读一次：value=%.1f，两次同值=%s"
          % (again["value"], again["value"] == after["value"]))
    print("  第二次的结算区间：%s → %s（Δt 小于 10 分钟就不动，连文件都不碰）"
          % (again["settled_from"], again["settled_to"]))


# ---- mood-praise3：60 → 70 → 80 → 90 → 3 小时后 75.0 ----------------------


def demo_mood_praise3(root: Path) -> None:
    """三连夸把 now 推到 90，再等一个半衰（3 小时）读回 75.0。

    同一人同一小时只算一次（mood 的 window），所以三次夸来自三个不同的人；
    这是设计里的防连珠炮，不是演示的手脚。
    """
    _root_note(root)
    at = _stamp(5, 9)
    before = mood_module.read(root, at)
    print("  %-22s now=%5.1f  baseline=%5.1f  %s"
          % ("起点（%s）" % at, before["now"], before["baseline"], before["tier"]))
    for index, who in enumerate(("甲", "乙", "丙"), start=1):
        result = mood_module.apply_event("praise", root=root, now=at, who=who)
        print("  %-22s now %.1f → %.1f（delta %+.1f）"
              % ("第 %d 次夸（%s）" % (index, who), result["before"], result["after"],
                 result["delta"]))
    later = _stamp(5, 12)
    final = mood_module.read(root, later)
    print("  %-22s now=%5.1f（%s）  期望 75.0（90 与基线 60 的中点）"
          % ("3 小时后（%s）" % later, final["now"], final["tier"]))
    rows = mood_module.trajectory(root, limit=200)
    print("  轨迹账本：%d 行；最后一行 %s"
          % (len(rows), json.dumps(rows[-1], ensure_ascii=False) if rows else "（空）"))


# ---- gates：20 次判决 + 归因账 -------------------------------------------


def _gate_input(index: int) -> engage_module.EngageInput:
    """第 index 组判决输入：白天/夜里、被动/主动、被骂、被打断都覆盖到。"""
    day = 5 + index % 10
    night = index % 2 == 1
    now = _stamp(day, 23 if night else 9 + index % 5)
    familiarity = 2.5 if index % 3 == 0 else 0.4
    today_count = 24 if index % 3 == 0 else index * 2
    return engage_module.EngageInput(
        mood_now=65.0 - index,
        energy=60.0 - 2 * index,
        familiarity=familiarity,
        today_count=today_count,
        intent=model.INTENT_PASSIVE if index % 4 < 2 else model.INTENT_PROACTIVE,
        now=now,
        insulted_today=index % 7 == 3,
        last_interrupted_at=_stamp(day, 8, 40) if index % 5 == 4 else None,
    )


def demo_gates(root: Path) -> None:
    """20 次判决逐条列出 state 与 hits，再用 attribution_report 算归因账。"""
    _root_note(root)
    print("  %-3s %-9s %-4s %-6s %s" % ("#", "intent", "时段", "state", "hits"))
    rows: List[Dict[str, Any]] = []
    for index in range(20):
        judgement = _gate_input(index)
        result = engage_module.decide(judgement)
        rows.append({"state": result["state"], "reason": result["reason"],
                     "hits": list(result["hits"])})
        print("  %-3d %-9s %-4s %-6s %s"
              % (index + 1, judgement.intent,
                 "夜里" if index % 2 else "白天", result["state"],
                 "、".join(result["hits"]) or "（无）"))
    report = engage_module.attribution_report(rows)
    print("  归因账：total=%d unattributed=%d" % (report["total"], report["unattributed"]))
    for reason in sorted(report["by_reason"]):
        print("    %-22s %d 次" % (reason, report["by_reason"][reason]))


DEMOS = {"card": demo_card, "energy-day": demo_energy_day,
         "mood-praise3": demo_mood_praise3, "gates": demo_gates}


def _run_section(index: int, total: int, name: str) -> None:
    """一段演示：自己的临时根 + 自己的标题（第一行永远是临时根）。"""
    root = Path(tempfile.mkdtemp(prefix="heart-cli-%s-" % name))
    print("临时根：%s" % root)
    print("【%d/%d】%s" % (index, total, name))
    DEMOS[name](root)
    print("")


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m organs.heart.cli",
        description="心的人可读自检：每段都跑在自己的临时根上，绝不写仓里的 data。")
    parser.add_argument("cmd", choices=tuple(SECTIONS) + ("all",),
                        help="card / energy-day / mood-praise3 / gates / all")
    args = parser.parse_args(argv)
    if args.cmd == "all":
        total = len(SECTIONS) + 1
        for index, name in enumerate(SECTIONS, start=1):
            _run_section(index, total, name)
        print("【%d/%d】小结：以上 %d 段各自用了一个临时根，仓里的 data 一个字节没动。"
              % (total, total, len(SECTIONS)))
        return 0
    _run_section(1, 1, args.cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
