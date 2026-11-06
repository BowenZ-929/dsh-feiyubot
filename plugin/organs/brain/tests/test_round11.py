"""第十一轮：认人——建档、别名、冷热、话题、who 归属。"""
from __future__ import annotations

import unittest

from organs.brain.tests.helpers import fresh_store, ingest


class RecognitionTest(unittest.TestCase):
    def test_three_speakers_build_cards_with_cold_and_counts(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        for i in range(3):
            ingest(store, "234567890", "我妈住院了", "2026-11-03T10:%02d:00+00:00" % i)
        for i in range(2):
            ingest(store, "123456789", "我换工作了", "2026-11-03T10:%02d:00+00:00" % (3 + i))
        ingest(store, "345678901", "在吗", "2026-11-03T10:05:00+00:00")
        people = {p["id"]: p for p in store.people_list("2026-11-03T10:10:00+00:00")}
        self.assertEqual({"234567890", "123456789", "345678901"}, set(people))
        self.assertTrue(people["345678901"]["cold"], "只聊过一次 = cold")
        self.assertFalse(people["234567890"]["cold"])
        self.assertFalse(people["123456789"]["cold"])
        self.assertEqual(3, people["234567890"]["total_chats"])
        self.assertEqual(2, people["123456789"]["total_chats"])
        self.assertEqual(1, people["345678901"]["total_chats"])
        self.assertTrue(people["234567890"]["last_interaction"])
        self.assertIn("医院", people["234567890"]["topics"])

    def test_self_message_does_not_add_card(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "123456789", "在吗", "2026-11-03T10:00:00+00:00")
        before = len(store.people_list("2026-11-03T10:01:00+00:00"))
        store.ingest("江迟", "我自己的话", ts="2026-11-03T10:02:00+00:00",
                     side="她说")
        after = len(store.people_list("2026-11-03T10:03:00+00:00"))
        self.assertEqual(before, after, "她以江迟身份说话不新增卡")

    def test_nickname_change_same_card_alias_accumulates(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "123456789", "第一句", "2026-11-03T10:00:00+00:00",
               display="小明")
        ingest(store, "123456789", "第二句", "2026-11-03T10:01:00+00:00",
               display="小明同学")
        people = store.people_list("2026-11-03T10:02:00+00:00")
        self.assertEqual(1, len(people))
        self.assertEqual("", people[0]["display"], "display 归人：通道不许盖")
        self.assertEqual("小明同学", people[0]["nickname"])
        self.assertEqual(["小明", "小明同学"], people[0]["aliases"])

    def test_memories_hang_under_real_sender(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "123456789", "小明说他爱吃辣", "2026-11-03T10:00:00+00:00")
        ingest(store, "234567890", "阿黄说他怕狗", "2026-11-03T10:02:00+00:00")
        spicy = store.recall("吃辣", now_ts="2026-11-03T10:05:00+00:00")
        dog = store.recall("怕狗", now_ts="2026-11-03T10:05:00+00:00")
        self.assertEqual("123456789", spicy["items"][0]["who"])
        self.assertEqual("234567890", dog["items"][0]["who"])


if __name__ == "__main__":
    unittest.main()
