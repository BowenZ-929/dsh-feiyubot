"""今日：逐条保存原文、换日降级进长期字面档；保留来源与证据编号。"""
from __future__ import annotations

import unittest

from organs.brain.tests.helpers import fresh_store, ingest


class TodayTest(unittest.TestCase):
    def test_turn_link_survives_capacity_demotion_and_rollover(self):
        from unittest.mock import patch
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        tiers = dict(store._tiers(), today_max=2, today_demote_count=1)
        with patch.object(store, '_tiers', return_value=tiers):
            for text in ('一', '二', '三'):
                result = ingest(store, '3425635473', text, '2026-11-03T10:00:00+00:00', turn_id='round-one')
                self.assertTrue(result['ok'])
            self.assertEqual('round-one', store.longterm_entries()[0]['turn_id'])
            store.today_entries('2026-11-04T10:00:00+00:00')
            self.assertEqual(['round-one'] * 3, [r['turn_id'] for r in store.longterm_entries()])
        result = ingest(store, '3425635473', '旧式无回合编号', '2026-11-04T10:00:00+00:00')
        self.assertNotIn('turn_id', result['entry'])

    def test_ingest_appends_one_entry(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "3425635473", "今天第一句", "2026-11-03T10:00:00+00:00")
        self.assertTrue(result["ok"])
        self.assertFalse(result["merged"])
        self.assertEqual(1, result["entry"]["count"])
        self.assertEqual(1, len(store.today_entries("2026-11-03T10:01:00+00:00")))

    def test_same_person_followup_preserves_both_originals(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        first = ingest(store, "3425635473", "第一句", "2026-11-03T10:00:00+00:00")
        second = ingest(store, "3425635473", "第二句", "2026-11-03T10:09:59+00:00")
        self.assertFalse(second["merged"])
        self.assertEqual("第二句", second["entry"]["text"])
        self.assertNotEqual(first["entry"]["id"], second["entry"]["id"])
        self.assertEqual(["第一句", "第二句"], [row["quote"] for row in
                         store.today_entries("2026-11-03T10:10:00+00:00")])

    def test_followup_does_not_erase_material_before_longterm_recall(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        first = ingest(store, "3425635473", "我妈住院了", "2026-11-03T10:00:00+00:00")
        ingest(store, "3425635473", "谢谢，先不聊了", "2026-11-03T10:01:00+00:00")
        recalled = store.recall("以前说过什么我妈", now_ts="2026-11-04T05:00:00+00:00")
        self.assertTrue(recalled["has_material"])
        self.assertIn(first["entry"]["id"], [row["id"] for row in recalled["items"]])
        self.assertIn("我妈住院了", [row["text"] for row in recalled["items"]])

    def test_followup_keeps_its_own_source_and_visibility(self):
        from organs.brain import model
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        first = ingest(store, "3425635473", "私人记录", "2026-11-03T10:00:00+00:00",
                       audience=model.AUDIENCE_PRIVATE, scene=model.SCENE_PRIVATE,
                       source_type=model.SOURCE_SCHEDULED, note="前一条的理解")
        second = ingest(store, "3425635473", "群里的新发言", "2026-11-03T10:01:00+00:00",
                        scene=model.SCENE_GROUP)
        rows = store.today_entries("2026-11-03T10:02:00+00:00")
        self.assertEqual(first["entry"], rows[0])
        self.assertEqual(model.SOURCE_LIVED, second["entry"]["source_type"])
        self.assertEqual(second["entry"]["id"], second["entry"]["evidence_key"])
        self.assertEqual("", second["entry"]["note"])

    def test_different_person_does_not_merge(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "第一句", "2026-11-03T10:00:00+00:00")
        second = ingest(store, "567890123", "另一句", "2026-11-03T10:05:00+00:00")
        self.assertFalse(second["merged"])
        self.assertEqual(2, len(store.today_entries("2026-11-03T10:06:00+00:00")))

    def test_rollover_archives_to_longterm_and_clears_today(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "我妈住院了", "2026-11-03T18:00:00+00:00")
        ingest(store, "567890123", "别太冷淡", "2026-11-03T18:05:00+00:00")
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
        ingest(store, "3425635473", "第一句", "2026-11-03T10:00:00+00:00")
        store.today_entries("2026-11-04T05:00:00+00:00")   # 换日已发生
        store.today_entries("2026-11-03T23:00:00+00:00")    # 旧时间戳不回滚
        view = store.today_view("2026-11-04T05:01:00+00:00")
        self.assertEqual("2026-11-04", view["day"])

    def test_today_over_60_demotes_oldest_20_into_hot(self):
        """第三轮：今日 >60 条时，最旧 20 条全文进长期档、压成 ≤3 行薄索引进热，并记录被挤掉的是什么。"""
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        for index in range(65):
            ingest(store, "9900%02d" % index, "第 %d 句" % index,
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
