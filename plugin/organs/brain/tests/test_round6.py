"""第六轮：写入分两栏——quote（原话）与 note（她的理解）分开。"""
from __future__ import annotations

import json
import unittest

from organs.brain.tests.helpers import fresh_store, ingest


class QuoteNoteTest(unittest.TestCase):
    def test_unclear_note_empty_recalls_quote_only(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.ingest("主人", "他说明天上午九点做手术", note="", unclear=True,
                     ts="2026-11-03T10:00:00+00:00")
        result = store.recall("手术", now_ts="2026-11-03T10:05:00+00:00")
        self.assertTrue(result["has_material"])
        item = result["items"][0]
        self.assertTrue(item["unclear"])
        self.assertEqual("", item["note"])
        self.assertEqual("（unclear，未理解）", item["note_display"])
        self.assertEqual("他说明天上午九点做手术", item["full_text"])

    def test_quote_and_note_distinguishable_and_testimony_only_quote(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.ingest("主人", "这顿我请", note="她以为这顿该他请",
                     ts="2026-11-03T10:00:00+00:00")
        result = store.recall("这顿", now_ts="2026-11-03T10:05:00+00:00")
        item = result["items"][0]
        self.assertEqual("这顿我请", item["full_text"])
        self.assertEqual("她以为这顿该他请", item["note_full"])
        self.assertEqual("她理解：她以为这顿该他请", item["note_display"])
        self.assertTrue(item["testimony"], "testimony 只认 quote")
        self.assertFalse(item["note_testimony"], "note 永不作证词")

    def test_old_text_only_rows_read_as_quote_without_note(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.import_rows([{"id": "1000101", "who": "主人",
                            "ts": "2026-11-02T10:00:00+00:00",
                            "text": "旧数据只有 text", "source": "today"}])
        rows = store.longterm_active_entries()
        self.assertEqual("旧数据只有 text", rows[0]["quote"])
        self.assertEqual("", rows[0]["note"])
        result = store.recall("旧数据", now_ts="2026-11-03T10:05:00+00:00")
        self.assertTrue(result["has_material"])
        self.assertEqual("（无理解）", result["items"][0]["note_display"])

    def test_edit_note_keeps_quote_verbatim_and_logs_history(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "主人", "他说明天上午九点做手术",
                        note="旧理解", ts="2026-11-03T10:00:00+00:00")
        entry_id = result["entry"]["id"]
        edited = store.edit_note(entry_id, "panel", "新理解")
        self.assertTrue(edited["ok"])
        self.assertEqual("他说明天上午九点做手术", edited["quote"])
        today = store.today_entries("2026-11-03T10:06:00+00:00")
        self.assertEqual("新理解", today[0]["note"])
        self.assertEqual("他说明天上午九点做手术", today[0]["quote"])
        history = [row for row in store.root.joinpath("note_history.jsonl").read_text(
            encoding="utf-8").splitlines() if row.strip()]
        self.assertEqual(1, len(history))


if __name__ == "__main__":
    unittest.main()
