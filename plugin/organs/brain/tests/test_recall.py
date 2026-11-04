"""回想：第一档原文 + 字面检索；空就返回空不硬凑；向量只留接口。"""
from __future__ import annotations

import unittest

from organs.brain.tests.helpers import fresh_store, ingest


class RecallTest(unittest.TestCase):
    def test_recall_today_literal_hit_with_source(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "你今天有点不理人", "2026-11-03T10:00:00+00:00")
        result = store.recall("今天不理人", now_ts="2026-11-03T10:01:00+00:00")
        self.assertTrue(result["has_material"])
        self.assertEqual(1, len(result["items"]))
        self.assertEqual("today", result["items"][0]["source"])
        self.assertEqual("主人", result["items"][0]["who"])

    def test_recall_past_finds_original_text_in_longterm(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "主人", "我妈住院了", "2026-11-03T10:00:00+00:00")
        result = store.recall("以前说过什么我妈", now_ts="2026-11-04T05:00:00+00:00")
        self.assertTrue(result["has_material"])
        self.assertEqual("longterm", result["items"][0]["source"])
        self.assertEqual("我妈住院了", result["items"][0]["text"])

    def test_recall_empty_returns_empty_not_fabricated(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = store.recall("一个从来没出现过的话题", now_ts="2026-11-03T10:00:00+00:00")
        self.assertFalse(result["has_material"])
        self.assertEqual([], result["items"])
        self.assertIn("不硬凑", result["empty_reason"])

    def test_recall_empty_query_is_empty_material(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = store.recall("  ")
        self.assertFalse(result["has_material"])
        self.assertEqual([], result["items"])

    def test_vector_rerank_is_rerank_only_never_gate(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        candidates = [{"id": "a", "text": "候选一"}, {"id": "b", "text": "候选二"}]
        result = store.vector_rerank("随便什么", candidates)
        self.assertFalse(result["connected"])
        self.assertEqual(candidates, result["candidates"], "未接入时原样返回，不删任何候选")
        self.assertIn("永不 gate", result["note"])

    def test_forget_marks_status_and_drops_from_recall(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "主人", "我妈住院了", "2026-11-03T10:00:00+00:00")
        entry_id = result["entry"]["id"]
        self.assertEqual("active", result["entry"]["status"])
        store.forget(entry_id, ts="2026-11-03T10:05:00+00:00")
        self.assertFalse(store.recall("我妈", now_ts="2026-11-03T10:06:00+00:00")["has_material"])
        raw = store.today_view("2026-11-03T10:06:00+00:00")
        self.assertEqual(0, len(raw["entries"]), "检索面不再返回已遗忘条目")
        # 物理没删：今日文件里那行还在，只是 status=stale
        import json
        on_disk = json.loads((store.root / "today.json").read_text(encoding="utf-8"))
        self.assertEqual("stale", on_disk["entries"][0]["status"])


if __name__ == "__main__":
    unittest.main()
