"""召回装包：分桶配额 + 总预算 + 每项 cap + 超预算跳过。"""
from __future__ import annotations

import unittest

from organs.brain.pack import pack
from organs.brain.model import PackLimits


def _candidate(cid: str, source: str, text: str, score: float = 1.0,
               kind: str = "fact") -> dict:
    return {"id": cid, "source": source, "kind": kind, "who": "3425635473",
            "ts": "2026-11-03T10:00:00+00:00", "text": text, "score": score}


class PackTest(unittest.TestCase):
    def test_fills_within_quota_and_budget(self):
        limits = PackLimits(slots=4, per_item_cap=160, today_quota=2, longterm_quota=2)
        out = pack([
            _candidate("t1", "today", "今天第一句"),
            _candidate("t2", "today", "今天第二句"),
            _candidate("t3", "today", "今天第三句"),
            _candidate("l1", "longterm", "长期第一句"),
            _candidate("l2", "longterm", "长期第二句"),
        ], limits=limits)
        self.assertEqual(4, out["slots_used"])
        self.assertEqual(["t1", "t2", "l1", "l2"], [item["id"] for item in out["items"]])
        self.assertEqual("quota_full", out["skipped"][0]["reason"])
        self.assertEqual("t3", out["skipped"][0]["id"])
        self.assertEqual(0, out["skipped"][0]["remaining"]["quota"])
        self.assertEqual(1, out["skipped"][0]["need"]["slots"])

    def test_spare_slots_fill_by_rank_without_stealing_other_buckets(self):
        limits = PackLimits(slots=4, today_quota=1, longterm_quota=1, hot_quota=0)
        candidates = [_candidate('t1', 'today', '一'), _candidate('t2', 'today', '二'),
                      _candidate('t3', 'today', '三'), _candidate('t4', 'today', '四'),
                      _candidate('h1', 'hot', '关闭桶'), _candidate('l1', 'longterm', '旧背景')]
        out = pack(candidates, limits=limits)
        self.assertEqual(['t1', 't2', 't3', 'l1'], [r['id'] for r in out['items']])
        self.assertEqual([False, True, True, False], [r['borrowed_slot'] for r in out['items']])
        self.assertEqual([1, 2, 3, 6], [r['rank'] for r in out['items']])
        self.assertEqual(4, out['slots_used'])
        self.assertTrue(all(r['remaining']['quota'] >= 0 for r in out['skipped']))
        ample = pack(candidates, limits=PackLimits(slots=10, today_quota=1, longterm_quota=1, hot_quota=0))
        self.assertNotIn('h1', {r['id'] for r in ample['items']})
        self.assertEqual(5, ample['slots_used'])

    def test_item_cap_truncates_and_marks_capped(self):
        limits = PackLimits(slots=1, per_item_cap=5, today_quota=1, longterm_quota=1)
        out = pack([_candidate("t1", "today", "一二三四五六七八")], limits=limits)
        self.assertEqual(1, len(out["items"]))
        self.assertTrue(out["items"][0]["capped"])
        self.assertEqual(5, len(out["items"][0]["text"]))
        self.assertTrue(out["items"][0]["text"].endswith("…"))

    def test_budget_full_skips_rest(self):
        limits = PackLimits(slots=2, per_item_cap=160, today_quota=5, longterm_quota=5)
        out = pack([
            _candidate("t1", "today", "一", 3.0),
            _candidate("t2", "today", "二", 2.0),
            _candidate("t3", "today", "三", 1.0),
        ], limits=limits)
        self.assertEqual(2, out["slots_used"])
        self.assertEqual("budget_full", out["skipped"][0]["reason"])
        self.assertEqual("t3", out["skipped"][0]["id"])
        self.assertEqual(0, out["skipped"][0]["remaining"]["slots"])
        self.assertEqual(1, out["skipped"][0]["need"]["slots"])

    def test_outline_occupies_one_slot_like_fact(self):
        limits = PackLimits(slots=2, per_item_cap=160, today_quota=1, longterm_quota=1)
        out = pack([
            _candidate("o1", "longterm", "一个纲：两个目都在这", kind="outline"),
            _candidate("f1", "longterm", "普通事实"),
        ], limits=limits)
        self.assertEqual(2, out["slots_used"])
        self.assertEqual({"o1", "f1"}, {item["id"] for item in out["items"]})
        self.assertTrue(all(item["slot"] == 1 for item in out["items"]),
                        "一条纲占 1 个名额，普通条目 1 条占 1 个名额")

    def test_empty_pack_reports_no_material(self):
        out = pack([])
        self.assertFalse(out["has_material"])
        self.assertEqual(0, out["slots_used"])
        self.assertIn("不硬凑", out["empty_reason"])


if __name__ == "__main__":
    unittest.main()
