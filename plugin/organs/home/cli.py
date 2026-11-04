"""家的命令行入口：人在终端里看房、对账、登记指针卡。

用法（在 plugin/ 下）：
    python3 -m organs.home.cli rooms
    python3 -m organs.home.cli verify
    python3 -m organs.home.cli seed

三条子命令都不建房、不改房名表、不手改账：

- rooms：十间房一张表——房名、slug、放什么、结构可写与否、她（role=agent）今天被白名单点名了哪几间。
  白名单读不出来就响亮报并回非 0（读不到不等于谁都能写，也不等于谁都不能写，见 access.load_whitelist）。
- verify：审计行数与序号 vs 高水位。变少只报不修（docs/modules/home.md:110），补行等于把事故抹平。
- seed：把嘴那三份数据文件登记成仓库里的指针卡（只记路径与指纹，不搬本体），并打印抽屉模板路径。
  **往抽屉一个字节都不写**：抽屉是只读房，资料卡只有人直接在盘上改。缺哪份就如实报哪份、不静默跳过。

日志说什么，退出码就是什么（CONTRIBUTING.md §1.1 第二条）：每条命令最后一行都写「退出码 N」。
"""
from __future__ import annotations

import argparse
import json
import unicodedata
from typing import Dict, Optional, Sequence

from . import access, audit, cards, residents, rooms, store

# source 的头一段是哪一级解析出来的：构造参数 > 环境变量 > home_root()/access.json
_SOURCE_KINDS = {"explicit": "构造参数", "env": "环境变量 %s" % access.ENV_ACCESS,
                 "root": "home_root()/access.json"}


def _pad(text: str, width: int) -> str:
    """按终端显示宽度补空格：中文占两格，%6s 数的是字数，十间房的表会歪。"""
    shown = sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)
    return text + " " * max(0, width - shown)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python3 -m organs.home.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    helps = {
        "rooms": "十间房：房名/slug/放什么/结构可写/她现在可写（白名单现状）",
        "verify": "审计行数与高水位：变少只报不修，退出码非 0",
        "seed": "把嘴那三份登记成仓库里的指针卡，并打印抽屉模板路径（不写抽屉）",
    }
    for name, text in helps.items():
        child = sub.add_parser(name, help=text)
        child.add_argument("--root", default=None,
                           help="家的根（默认 HOME_ROOT > 包内 data/）")
    return parser.parse_args(argv)


def _rooms(root=None) -> int:
    """十间房一张表 + 白名单现状：两栏分开答，别把「房不放东西」与「她今天被收回」混成一栏。"""
    print("家：%s" % store.home_root(root))
    try:
        whitelist = access.load_whitelist(root)
    except access.AccessDenied as exc:
        print("白名单读不出来：%s %s（detail=%s）"
              % (exc.code, exc.message, json.dumps(exc.detail, ensure_ascii=False)))
        print("读不到不等于谁都能写，也不等于谁都不能写 → 不是「她一间都不能写」→ 退出码 1")
        return 1

    kind, _, path = whitelist.source.partition(":")
    named = "、".join(sorted(whitelist.writable)) or "（空集）"
    print("白名单：%s" % named)
    print("解析自：%s（%s）——改这份文件就是改「她今天能写哪几间」这个决定"
          % (_SOURCE_KINDS.get(kind, kind), path))

    writable = [room.name for room in rooms.ROOMS if room.writable_default]
    readonly = [room.name for room in rooms.ROOMS if not room.writable_default]
    print("")
    print("%s%s%s%s%s" % (_pad("房名", 8), _pad("slug", 12), _pad("结构可写", 10),
                          _pad("她现在", 8), "放什么"))
    for room in rooms.ROOMS:
        structural = "可写" if room.writable_default else "只读"
        if not room.writable_default:
            now = "—"          # 只读房不进白名单：这一栏空着，不是「收回了」
        else:
            now = "可写" if room.name in whitelist.writable else "收回"
        print("%s%s%s%s%s" % (_pad(room.name, 8), _pad(room.slug, 12), _pad(structural, 10),
                              _pad(now, 8), room.purpose))
    print("")
    print("结构可写五间：%s" % "、".join(writable))
    print("结构只读五间：%s（三个角色一律拒；改它等于改建房，要人改代码）" % "、".join(readonly))
    print("退出码 0")
    return 0


def _verify(root=None) -> int:
    """审计对账：行数与序号 vs 高水位。少了只报不修——补行与重写高水位都不许做。"""
    report = audit.verify(root)
    mark = report.get("mark") or {}
    print("审计：%s" % (store.home_root(root) / audit.AUDIT_NAME))
    print("现在：%d 行，最后序号 %d，读不出来的坏行 %d 行"
          % (report["lines"], report["seq"], report.get("bad", 0)))
    if mark:
        print("高水位：%s 行，序号 %s（%s 记的；%s）"
              % (mark.get("lines"), mark.get("seq"), mark.get("at"), audit.HIGH_WATER_NAME))
    else:
        print("高水位：不存在（%s 还没写过）" % audit.HIGH_WATER_NAME)

    if report["ok"]:
        print("%s：账目一致，只报不修 → 退出码 0" % report["code"])
        return 0
    if report["code"] == audit.CODE_SHRUNK:
        print("%s：账比高水位短（现在 %d 行／序号 %d，高水位记的是 %s 行／序号 %s）"
              % (audit.CODE_SHRUNK, report["lines"], report["seq"],
                 mark.get("lines"), mark.get("seq")))
        print("只报不修：不补行、不重写高水位——少行要么是盘坏了、要么是有人在改账 → 退出码 1")
        return 1
    print("%s：账目对不上（原因见上两行），只报不修 → 退出码 1" % report["code"])
    return 1


def _seed(root=None) -> int:
    """登记嘴那三份的指针卡，并打印抽屉模板路径；抽屉一个字节都不写。"""
    outcome = residents.seed_mouth_pointers(root)
    print("嘴的数据目录：%s" % outcome["data_root"])
    print("对齐口径：%s" % outcome["aligned"])
    print("落房：%s（只记名字、现路径、sha256、bytes；不搬文件本体）" % outcome["room"])
    for card in outcome["seeded"]:
        print("已登记：%s  card_id=%s  sha256=%s" % (card["name"], card["card_id"],
                                                   card["sha256"][:12]))
    if outcome["missing"]:
        print("缺这几份，没有登记：%s" % "、".join(outcome["missing"]))
    print("抽屉模板：%s" % residents.DRAWER_TEMPLATE)
    print("抽屉：一字未写——资料卡只由人直接在盘上改，程序侧只读（缺件时如实报未接入）")
    if outcome["ok"]:
        print("三份都在，指针卡已登记 → 退出码 0")
        return 0
    print("缺份：没有全登记 → 退出码 1")
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    handlers: Dict[str, object] = {"rooms": _rooms, "verify": _verify, "seed": _seed}
    try:
        return handlers[args.cmd](args.root)          # type: ignore[operator]
    except (access.AccessDenied, cards.CardError, store.StoreError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        detail = getattr(exc, "detail", "")
        print("%s：%s（detail=%s）" % (code, exc, json.dumps(detail, ensure_ascii=False,
                                                            default=str)))
        print("家这边拒绝了这条命令 → 退出码 2")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
