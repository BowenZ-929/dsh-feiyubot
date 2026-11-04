"""闭卷对照：同一批题跑两臂（不给记忆 / 给记忆），只报净增益。

只有「给记忆时答对」不算通过——两臂分数都要报，净增益 = 给记忆命中 − 不给记忆命中。
"""
from __future__ import annotations

import json
import unittest

from organs.brain.tests.helpers import fresh_store, ingest

# (题面, 答案在记忆原文里必须逐字包含的子串)
QUESTIONS = (
    ("我妈怎么了", "住院"),          # 「我妈」是原文子串，字面档该命中
    ("最近喜欢吃什么", "小鱼干"),     # 「最近」二字重合，字面档该命中
    ("那个 bug 出在哪", "登录页"),    # 「bug」是原文子串
    ("那天难过吗", "有点难过"),       # 「难过」是原文子串
    ("谁说过医院好吵", "医院好吵"),   # 整句原文子串
    ("她家里发生了什么", "住院"),     # 转述题：和原文没有字面重合，第一档诚实答不了
)

FACTS = (
    ("主人", "我妈住院了", "2026-11-03T10:00:00+00:00"),
    ("主人", "我最近喜欢吃小鱼干", "2026-11-03T10:15:00+00:00"),
    ("主人", "那个 bug 在登录页", "2026-11-03T10:30:00+00:00"),
    ("主人", "我今天有点难过", "2026-11-03T10:45:00+00:00"),
    ("朋友", "医院好吵", "2026-11-03T11:00:00+00:00"),
)


def _hit(store, question: str, answer: str, now_ts: str) -> bool:
    result = store.recall(question, now_ts=now_ts)
    return any(answer in item.get("text", "") for item in result.get("items", []))


def run_closed_book(now_ts: str = "2026-11-03T11:05:00+00:00") -> dict:
    closed_store, closed_tmp = fresh_store()
    try:
        closed_hits = sum(1 for q, a in QUESTIONS if _hit(closed_store, q, a, now_ts))
    finally:
        closed_tmp.cleanup()

    open_store, open_tmp = fresh_store()
    try:
        for who, text, ts in FACTS:
            ingest(open_store, who, text, ts)
        open_hits = sum(1 for q, a in QUESTIONS if _hit(open_store, q, a, now_ts))
    finally:
        open_tmp.cleanup()

    total = len(QUESTIONS)
    return {
        "total": total,
        "closed_hits": closed_hits,
        "open_hits": open_hits,
        "net_gain": open_hits - closed_hits,
        "verdict": "通过（净增益 > 0）" if open_hits - closed_hits > 0
                   else "不通过（只有给记忆答对不算数）",
    }


class ClosedBookTest(unittest.TestCase):
    def test_net_gain_positive(self):
        report = run_closed_book()
        self.assertGreater(report["net_gain"], 0, json.dumps(report, ensure_ascii=False))
        self.assertEqual(0, report["closed_hits"],
                         "闭卷臂（空记忆）不该有任何命中，否则基准就是脏的")


if __name__ == "__main__":
    print(json.dumps(run_closed_book(), ensure_ascii=False, indent=2))
