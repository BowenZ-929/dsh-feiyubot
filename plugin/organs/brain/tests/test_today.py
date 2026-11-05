"""今日：追加、合并、换日降级进长期字面档；热记忆形状待定，本轮不触发热降级。"""
from __future__ import annotations

import unittest

from organs.brain.tests.helpers import fresh_store, ingest


class TodayTest(unittest.TestCase):
    def test_ingest_appends_one_entry(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "主人", "今天第一句", "2026-11-03T10:00:00+00:00")
        self.assertTrue(result["ok"])
        self.assertFalse(result["merged"])
        self.assertEqual(1, result["entry"]["count"])
        self.assertEqual(1, len(store.today_entries("2026-11-03T10:01:00+00:00")))

    def test_same_person_within_ten_minutes_merges(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "第一句", "2026-11-03T10:00:00+00:00")
        second = ingest(store, "主人", "第二句", "2026-11-03T10:09:59+00:00")
        self.assertTrue(second["merged"])
        self.assertEqual("第二句", second["entry"]["text"])
        self.assertEqual(2, second["entry"]["count"])
        self.assertEqual(1, len(store.today_entries("2026-11-03T10:10:00+00:00")))

    def test_different_person_does_not_merge(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "第一句", "2026-11-03T10:00:00+00:00")
        second = ingest(store, "朋友", "另一句", "2026-11-03T10:05:00+00:00")
        self.assertFalse(second["merged"])
        self.assertEqual(2, len(store.today_entries("2026-11-03T10:06:00+00:00")))

    def test_rollover_archives_to_longterm_and_clears_today(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "我妈住院了", "2026-11-03T18:00:00+00:00")
        ingest(store, "朋友", "别太冷淡", "2026-11-03T18:05:00+00:00")
        # 04:00 换日点：05:00 第一次读触发换日
        self.assertEqual(0, len(store.longterm_entries()))
        today = store.today_entries("2026-11-04T05:00:00+00:00")
        self.assertEqual([], today)
        self.assertEqual("2026-11-04", store.today_view("2026-11-04T05:00:00+00:00")["day"])
        longterm = store.longterm_entries()
        self.assertEqual(2, len(longterm))
        self.assertTrue(all(entry["source"] == "today" for entry in longterm))
        self.assertIn("我妈住院了", [entry["text"] for entry in longterm])
        self.assertEqual(0, len(store.today_entries("2026-11-04T05:01:00+00:00")))

    def test_out_of_order_timestamp_does_not_roll_back(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "第一句", "2026-11-03T10:00:00+00:00")
        store.today_entries("2026-11-04T05:00:00+00:00")   # 换日已发生
        store.today_entries("2026-11-03T23:00:00+00:00")    # 旧时间戳不回滚
        view = store.today_view("2026-11-04T05:01:00+00:00")
        self.assertEqual("2026-11-04", view["day"])

    def test_today_over_60_demotes_oldest_20_into_hot(self):
        """第三轮：今日 >60 条时，最旧 20 条全文进长期档、压成 ≤3 行薄索引进热，并记录被挤掉的是什么。"""
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        for index in range(65):
            ingest(store, "群友%d" % index, "第 %d 句" % index,
                   "2026-11-03T12:00:00+00:00")
        self.assertEqual(45, len(store.today_entries("2026-11-03T13:00:00+00:00")))
        self.assertEqual(20, len(store.longterm_active_entries()))
        self.assertLessEqual(len(store.hot_rows()), 3)
        evictions = store.evictions()
        self.assertEqual(1, len(evictions))
        self.assertEqual("today_over_60", evictions[0]["reason"])
        self.assertEqual(20, len(evictions[0]["demoted_ids"]))


if __name__ == "__main__":
    unittest.main()
