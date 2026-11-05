"""第七轮：人名录挡住两类"人"——占位符不建卡，她自己不建卡。"""
from __future__ import annotations

import json
import unittest
import warnings

from organs.brain.tests.helpers import fresh_store, ingest


class PeopleBlockTest(unittest.TestCase):
    def test_placeholders_and_self_do_not_create_cards(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        for who in ("人", "", "江迟", "本鱼"):
            store.interact(who, ts="2026-11-03T10:00:00+00:00")
        people = store.people_list("2026-11-03T10:01:00+00:00")
        self.assertEqual([], people, "占位符与她自己的自称都不该建卡：%r" % people)

    def test_normal_person_creates_card(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.interact("小明", ts="2026-11-03T10:00:00+00:00")
        self.assertEqual(1, len(store.people_list("2026-11-03T10:01:00+00:00")))

    def test_self_interact_logs_warn(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            store.interact("江迟", ts="2026-11-03T10:00:00+00:00")
        self.assertTrue(any("不是通讯录里的人" in str(w.message) for w in caught),
                        "她自己的自称要记一条 warn")

    def test_empty_who_ingest_still_records_memory_as_placeholder(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "", "来源不明的一句话", "2026-11-03T10:00:00+00:00")
        self.assertTrue(result["ok"])
        self.assertEqual("人", result["entry"]["who"], "记忆照记，who 记成占位符")
        self.assertEqual([], store.people_list("2026-11-03T10:01:00+00:00"))

    def test_clean_dirty_cards_backs_up_and_keeps_real_card(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        data = {"people": {
            "人": {"id": "人", "display": "人", "interactions": {}},
            "江迟": {"id": "江迟", "display": "江迟", "interactions": {}},
            "小明": {"id": "小明", "display": "小明", "interactions": {}},
        }}
        (store.root / "people.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        result = store.clean_dirty_cards()
        self.assertEqual(["人", "江迟"], result["removed"])
        self.assertTrue(result["backup"])
        remaining = store._people_data()["people"]
        self.assertEqual(["小明"], list(remaining), "真卡不许动")


if __name__ == "__main__":
    unittest.main()
