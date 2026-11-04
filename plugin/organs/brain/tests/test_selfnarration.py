"""自述：只记录不解释，留版本；心去读，脑不解读。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from organs.brain.tests.helpers import fresh_store


class SelfNarrationTest(unittest.TestCase):
    def test_record_appends_versions_and_latest_wins(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(store.selfnarration_latest())
        first = store.record_selfnarration("我最近像变了个人", note="首条")
        second = store.record_selfnarration("今天比昨天更松了", note="第二条")
        self.assertTrue(first["ok"])
        self.assertEqual(1, first["record"]["version"])
        self.assertEqual(2, second["record"]["version"])
        self.assertEqual("今天比昨天更松了", store.selfnarration_latest()["text"])
        self.assertEqual(2, len(store.selfnarration_versions()))

    def test_record_only_stores_text_not_interpretation(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        record = store.record_selfnarration("我最近像变了个人")["record"]
        self.assertEqual({"id", "version", "ts", "text", "note"}, set(record))

    def test_seed_data_has_her_words_verbatim(self):
        data = Path(__file__).resolve().parents[1] / "data" / "selfnarration.jsonl"
        rows = [json.loads(line) for line in data.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(1, len(rows))
        self.assertEqual("我最近像变了个人", rows[0]["text"])
        self.assertEqual(1, rows[0]["version"])


if __name__ == "__main__":
    unittest.main()
