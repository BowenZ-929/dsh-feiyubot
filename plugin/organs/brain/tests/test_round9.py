"""第九轮：recall 打分/去重读 note；conversation 用脑的 display_text。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from organs.brain import BrainStore

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "migration_baseline.jsonl"


class NoteScoringTest(unittest.TestCase):
    def _store(self, tmp) -> BrainStore:
        store = BrainStore(Path(tmp.name) / "brain")
        rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        store.import_rows(rows)
        return store

    def test_note_only_migration_hits_by_note(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = self._store(tmp)
        result = store.recall("你为什么叫江迟", now_ts="2026-11-03T10:00:00+00:00")
        self.assertTrue(result["has_material"])
        self.assertGreaterEqual(len(result["items"]), 1)

    def test_seven_rows_not_merged_on_recall(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = self._store(tmp)
        result = store.recall("江迟", now_ts="2026-11-03T10:00:00+00:00")
        # 长期桶配额 3：7 条里取 3 条；若去重键还在用空 text，这里会并成 1 条
        self.assertEqual(3, len(result["items"]), "空 quote 的迁移记忆不许在召回侧被并成一条")
        self.assertEqual(3, len({item["id"] for item in result["items"]}))

    def test_display_and_note_lines_in_prompt(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = self._store(tmp)
        from organs.mind import conversation

        recalled, text = conversation._recall(store, "你为什么叫江迟", "1000001")
        self.assertIn("（无原话，来自旧家记录）", text)
        self.assertIn("她理解：", text)


if __name__ == "__main__":
    unittest.main()
