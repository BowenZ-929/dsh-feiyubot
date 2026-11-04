"""四档分层：今日>60 降级进热薄索引；热满挤最久未命中并记录；薄索引不含全文。"""
from __future__ import annotations

import json
import unittest

from organs.brain.tests.helpers import fresh_store, ingest


class TiersTest(unittest.TestCase):
    def test_hot_row_has_no_full_text_and_expands_on_hit(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        for index in range(65):
            ingest(store, "群友%d" % index,
                   "第 %d 句：这是一段足够长的原文，用来证明薄索引里不放全文" % index,
                   "2026-11-03T12:00:00+00:00")
        rows = store.hot_rows()
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("条", row["line"])
            self.assertNotIn("这是一段足够长的原文，用来证明薄索引里不放全文",
                             row["line"], "薄索引不含全文，只留可查指针")
        expanded = store.hot_expand(rows[0]["id"], now_ts="2026-11-03T13:00:00+00:00")
        self.assertTrue(expanded["ok"])
        self.assertTrue(expanded["full_texts"])
        self.assertIn("第", expanded["full_texts"][0]["text"])

    def test_hot_full_evicts_oldest_unhit_and_records_it(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        tiers = json.loads((store.root / "tiers.json").read_text(encoding="utf-8"))
        tiers["hot_max"] = 4
        (store.root / "tiers.json").write_text(json.dumps(tiers, ensure_ascii=False), encoding="utf-8")
        # 每次 65 条 → 降级 20 条 → 约 3 行热；三次后热超过 4 行触发挤掉
        for batch in range(3):
            for index in range(65):
                ingest(store, "群友%d-%d" % (batch, index), "第 %d-%d 句" % (batch, index),
                       "2026-11-03T%02d:%02d:00+00:00" % (12 + batch, index % 60))
            store.today_entries("2026-11-03T%02d:30:00+00:00" % (12 + batch))
        evictions = store.evictions()
        hot_full = [e for e in evictions if e["reason"] == "hot_full"]
        self.assertTrue(hot_full, "热满必须挤最久未命中并记录被挤掉的是什么")
        self.assertIn("evicted_hot_id", hot_full[0])
        self.assertIn("evicted_line", hot_full[0])
        self.assertLessEqual(len(store.hot_rows()), 4)


if __name__ == "__main__":
    unittest.main()
