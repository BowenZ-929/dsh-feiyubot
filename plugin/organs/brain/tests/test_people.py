"""人名录：认人、熟稔现算不写回、别名合并、冷处理。"""
from __future__ import annotations

import unittest

from organs.brain.tests.helpers import fresh_store, ingest


class PeopleTest(unittest.TestCase):
    def test_interact_creates_person_and_updates_familiarity(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "在吗", "2026-11-03T10:00:00+00:00")
        people = store.people_list("2026-11-03T10:01:00+00:00")
        self.assertEqual(1, len(people))
        self.assertEqual("主人", people[0]["id"])
        self.assertEqual(1.0, people[0]["familiarity"])

    def test_familiarity_decays_on_read_not_write_back(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "在吗", "2026-11-03T10:00:00+00:00")
        view = store.lookup("主人", "2026-11-24T10:00:00+00:00")   # 21 天后
        self.assertAlmostEqual(0.5, view["familiarity"], places=3)
        data = store._people_data()
        self.assertNotIn("familiarity", data["people"]["主人"],
                         "熟稔是读数，不写回人名录")

    def test_alias_lookup_and_cold_mark(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "在吗", "2026-11-03T10:00:00+00:00")
        self.assertTrue(store.add_alias("主人", "老板")["ok"])
        self.assertIsNotNone(store.lookup("老板", "2026-11-03T10:01:00+00:00"))
        self.assertTrue(store.mark_cold("主人", True)["ok"])
        self.assertTrue(store.lookup("老板", "2026-11-03T10:02:00+00:00")["cold"])

    def test_merge_people_keeps_aliases_and_earlier_first_seen(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "第一句", "2026-11-01T10:00:00+00:00")
        ingest(store, "新号", "第二句", "2026-11-03T10:00:00+00:00")
        result = store.merge_people("主人", "新号")
        self.assertTrue(result["ok"])
        self.assertEqual("2026-11-01T10:00:00+00:00", result["person"]["first_seen"])
        self.assertEqual(["新号"], result["person"]["aliases"])
        self.assertIsNotNone(store.lookup("新号", "2026-11-03T10:01:00+00:00"))
        self.assertEqual(1, len(store.people_list()))


if __name__ == "__main__":
    unittest.main()
