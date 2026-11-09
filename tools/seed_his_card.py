#!/usr/bin/env python3
"""把主人的名片写回人名录（幂等）。清场之后重放这一支，名片就回来了。

清场（tools/reset_experience.py）会清掉 people.json——测试期的人名录是脏的（"人"、"江迟"
这些不是人）。但主人这张卡是**迁移成果**，不该跟着丢。所以做成可重放的种子：
清场之后跑一次这个脚本，名片就回来，而且不会连带把脏卡带回来。

用法：python3 tools/seed_his_card.py [--who 3425635473] [--actor bowen]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from organs.brain.store import BrainStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "home" / "organs" / "brain"

CARD = {
    "关系": "主人",
    "叫法": "老大",
    "称呼": "老大（可以叫主人）",
    "生日": "9 月 29 日",
    "备注": "私聊按主人模式。他叫她的外号：小肥鱼、哦鲸鲸、大肥鱼、傻鱼（后两个她不爱听）",
    "雷点": "",  # 人明确说过：他没有"别提白饭"这种雷点
}


def main(argv: list[str]) -> int:
    who = argv[argv.index("--who") + 1] if "--who" in argv else "3425635473"
    actor = argv[argv.index("--actor") + 1] if "--actor" in argv else "bowen"
    store = BrainStore(BRAIN)
    store.interact(who, display=CARD["称呼"])
    out = store.patch_card(who, actor, CARD)
    if not out.get("ok"):
        print("写名片失败：", out.get("message"))
        return 1
    card = store.card_view(who) or {}
    print("名片已写回：", {k: card.get(k) for k in ("display", "birthday", "notes")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
