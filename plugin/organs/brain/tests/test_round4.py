"""第四轮追加件：名片白名单写、历史、熟稔不动；retention 现算；四档与冷仓只读。"""
from __future__ import annotations

import json
import unittest

from organs.brain import retention as retention_mod
from organs.brain.store import BrainReadError
from organs.brain.tests.helpers import fresh_store, ingest


class CardTest(unittest.TestCase):
    def test_patch_card_whitelist_history_and_familiarity_unchanged(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "我妈住院了", "2026-11-03T10:00:00+00:00")
        before = store.lookup("主人", "2026-11-03T10:01:00+00:00")
        result = store.patch_card("主人", "panel", {"称呼": "老板", "雷点": ["别提工作"]})
        self.assertTrue(result["ok"])
        self.assertEqual(2, len(result["history_lines"]))
        after = store.lookup("主人", "2026-11-03T10:02:00+00:00")
        self.assertEqual(before["familiarity"], after["familiarity"],
                         "熟稔是读数，人改名片不得动它")
        self.assertEqual("老板", after["display"])
        self.assertEqual(["别提工作"], after["prefs"])
        self.assertEqual(2, len(store.card_history("主人")))

    def test_patch_card_rejects_familiarity_and_unknown_fields(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "在吗", "2026-11-03T10:00:00+00:00")
        self.assertFalse(store.patch_card("主人", "panel", {"familiarity": 99})["ok"])
        self.assertFalse(store.patch_card("主人", "panel", {"熟稔": 99})["ok"])
        self.assertFalse(store.patch_card("主人", "panel", {"爱好": ["猫"]})["ok"])
        self.assertEqual([], store.card_history("主人"))


class RetentionAnnotationTest(unittest.TestCase):
    def test_today_and_people_annotations_match_settle_params(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "我妈住院了", "2026-11-03T10:00:00+00:00")
        now = "2026-11-03T10:05:00+00:00"
        view = store.today_view(now)
        entry = view["entries"][0]
        params = store._retention_params()
        self.assertEqual(retention_mod.retention(entry, params, now), entry["retention"])
        self.assertEqual(retention_mod.clarity(entry["retention"], params), entry["clarity"])
        people = store.people_list(now)
        self.assertTrue(people[0]["memories"])
        self.assertTrue(all("ts" in m and "retention" in m and "clarity" in m
                            for m in people[0]["memories"]))


class TiersAndColdTest(unittest.TestCase):
    def test_tiers_summary_counts_and_recent(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "我妈住院了", "2026-11-03T10:00:00+00:00")
        summary = store.tiers_summary("2026-11-03T10:05:00+00:00")
        self.assertEqual(1, summary["today"]["count"])
        self.assertEqual(0, summary["hot"]["count"])
        self.assertEqual(0, summary["longterm"]["count"])
        self.assertEqual(0, summary["cold"]["count"])

    def test_cold_page_annotates_retention_and_clarity(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "主人", "我妈住院了", "2026-10-01T10:00:00+00:00")
        entry_id = result["entry"]["id"]
        store.today_entries("2026-11-20T10:00:00+00:00")
        found = store._latest_longterm(entry_id)
        updated = dict(found)
        updated["ts"] = "2026-10-01T10:00:00+00:00"
        updated["last_recalled_at"] = "2026-10-02T10:00:00+00:00"
        updated["recall_count"] = 0
        store._append_longterm([updated])
        store.recall("住院", now_ts="2026-11-20T10:05:00+00:00")
        page = store.cold_page(now_ts="2026-11-20T10:06:00+00:00")
        self.assertEqual(1, len(page["items"]))
        self.assertIn("retention", page["items"][0])
        self.assertIn("clarity", page["items"][0])
        self.assertLess(page["items"][0]["retention"], 0.08)

    def test_cold_page_empty_is_empty_array_and_bad_line_raises(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        self.assertEqual({"items": [], "next_cursor": None}, store.cold_page())
        with open(store.root / "longterm.jsonl", "a", encoding="utf-8") as f:
            f.write("{bad json\n")
        with self.assertRaises(BrainReadError):
            store.cold_page()
        with self.assertRaises(BrainReadError):
            store.tiers_summary()


if __name__ == "__main__":
    unittest.main()
