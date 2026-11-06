"""第十二轮：人名录主键 = QQ 号；昵称是别名；旧日记反填骨架。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from organs.brain import BrainStore
from organs.brain.old_home import backfill_people
from organs.brain.tests.helpers import fresh_store, ingest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "old_home_participants.json"


class WhoKeyTest(unittest.TestCase):
    def test_same_qq_two_nicknames_one_card_two_aliases(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "第一句", "2026-11-03T10:00:00+00:00",
               display="老大")
        ingest(store, "3425635473", "第二句", "2026-11-03T10:02:00+00:00",
               display="主人")
        people = store.people_list("2026-11-03T10:05:00+00:00")
        self.assertEqual(1, len(people))
        self.assertEqual("3425635473", people[0]["id"])
        self.assertEqual("主人", people[0]["display"])
        self.assertEqual(["老大", "主人"], people[0]["aliases"])

    def test_nickname_as_who_rejected_with_reason(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "小明", "一句话", "2026-11-03T10:00:00+00:00")
        self.assertFalse(result["ok"])
        self.assertIn("昵称不是身份", result["message"])
        self.assertEqual([], store.people_list("2026-11-03T10:01:00+00:00"))

    def test_bot_does_not_build_card(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = store.interact("2284938721", display="bot",
                                ts="2026-11-03T10:00:00+00:00", is_bot=True)
        self.assertEqual("bot", result["reason"])
        self.assertEqual([], store.people_list("2026-11-03T10:01:00+00:00"))


class OldHomeBackfillTest(unittest.TestCase):
    def test_backfill_builds_seven_cards_and_skips_bots_low(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        records = json.loads(FIXTURE.read_text(encoding="utf-8"))
        # 主号已有卡：只补别名
        store.interact("3425635473", display="老大（可以叫主人）",
                       ts="2026-11-03T09:00:00+00:00")
        report = backfill_people(store, records)
        self.assertTrue(report["ok"])
        people = store.people_list("2026-11-03T10:00:00+00:00")
        self.assertEqual(7, len(people), [p["id"] for p in people])
        skipped = {(s["sender_id"], s["reason"]) for s in report["skipped"]}
        self.assertIn(("2284938721", "bot"), skipped)
        self.assertIn(("astrbot", "bot"), skipped)
        self.assertIn(("2398539930", "low-count"), skipped)
        self.assertIn(("248900410", "low-count"), skipped)
        self.assertIn(("1056283825", "low-count"), skipped)
        by_id = {p["id"]: p for p in people}
        self.assertTrue(by_id["970782150"]["from_old_home"])
        self.assertTrue(by_id["970782150"]["familiarity_estimated"])
        self.assertEqual("张云鹏", by_id["970782150"]["display"])
        self.assertEqual(["Ben Brode", "canxueqq"], by_id["970782150"]["aliases"])
        self.assertEqual(["老大（可以叫主人）", "主人"], by_id["3425635473"]["aliases"])


if __name__ == "__main__":
    unittest.main()
