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
        ingest(store, "3425635473", "我妈住院了", "2026-11-03T10:00:00+00:00")
        before = store.lookup("3425635473", "2026-11-03T10:01:00+00:00")
        result = store.patch_card("3425635473", "panel", {"称呼": "老板", "雷点": ["别提工作"]})
        self.assertTrue(result["ok"])
        self.assertEqual(2, len(result["history_lines"]))
        after = store.lookup("3425635473", "2026-11-03T10:02:00+00:00")
        self.assertEqual(before["familiarity"], after["familiarity"],
                         "熟稔是读数，人改名片不得动它")
        self.assertEqual("老板", after["display"])
        self.assertEqual(["别提工作"], after["prefs"])
        self.assertEqual(2, len(store.card_history("3425635473")))

    def test_relationship_and_address_survive_real_card_storage(self):
        from organs.mind import conversation
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.interact('3425635473', display='普通群昵称')
        result = store.patch_card('3425635473', 'owner-confirmed', {'关系':'主人', '叫法':'老大'})
        self.assertTrue(result['ok'], result)
        reloaded = type(store)(store._root)
        card = reloaded.card_view('3425635473')
        self.assertEqual('主人', card['relationship'])
        self.assertEqual('老大', card['address'])
        text = conversation._caller_text(conversation.load_assembly(), reloaded, '3425635473')
        self.assertIn('主人（你叫老大）', text)
        reloaded.interact('10000421', display='主人')
        stranger = conversation._caller_text(conversation.load_assembly(), reloaded, '10000421')
        self.assertIn('关系尚未确认', stranger)
        self.assertNotIn('一个熟人', stranger)
        self.assertNotIn('来的是主人', stranger)
        self.assertEqual(2, len(reloaded.card_history('3425635473')))
        for value in (None, {}, 3, 'x'*81):
            self.assertFalse(reloaded.patch_card('3425635473','panel',{'关系':value})['ok'])
        self.assertEqual('主人', reloaded.card_view('3425635473')['relationship'])

    def test_caller_uses_exact_number_not_another_persons_numeric_alias(self):
        from organs.mind import conversation
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.interact('10000011', display='旧主人')
        store.patch_card('10000011', 'fixture', {'关系':'主人', '叫法':'老大'})
        store.add_alias('10000011', '10000022')
        store.interact('10000022', display='另一个人')
        text = conversation._caller_text(conversation.load_assembly(), store, '10000022')
        self.assertNotIn('来的是主人', text)
        self.assertIn('另一个人', text)
        store.add_alias('10000011', '10000033')
        missing = conversation._caller_text(conversation.load_assembly(), store, '10000033')
        self.assertIn('不认识的号', missing)

    def test_patch_card_rejects_familiarity_and_unknown_fields(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "在吗", "2026-11-03T10:00:00+00:00")
        self.assertFalse(store.patch_card("3425635473", "panel", {"familiarity": 99})["ok"])
        self.assertFalse(store.patch_card("3425635473", "panel", {"熟稔": 99})["ok"])
        self.assertFalse(store.patch_card("3425635473", "panel", {"爱好": ["猫"]})["ok"])
        self.assertEqual([], store.card_history("3425635473"))


class RetentionAnnotationTest(unittest.TestCase):
    def test_today_and_people_annotations_match_settle_params(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "我妈住院了", "2026-11-03T10:00:00+00:00")
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
        ingest(store, "3425635473", "我妈住院了", "2026-11-03T10:00:00+00:00")
        summary = store.tiers_summary("2026-11-03T10:05:00+00:00")
        self.assertEqual(1, summary["today"]["count"])
        self.assertEqual(0, summary["hot"]["count"])
        self.assertEqual(0, summary["longterm"]["count"])
        self.assertEqual(0, summary["cold"]["count"])

    def test_cold_page_annotates_retention_and_clarity(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "3425635473", "我妈住院了", "2026-10-01T10:00:00+00:00")
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
