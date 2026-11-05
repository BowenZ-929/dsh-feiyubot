"""遗忘曲线：读时结算。三态复现 + 清晰度渲染 + 参数从数据文件读。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from organs.brain import retention as retention_mod
from organs.brain.tests.helpers import fresh_store, ingest

DATA = Path(__file__).resolve().parents[1] / "data"


class RetentionTest(unittest.TestCase):
    def test_three_states_recall_then_forget_then_cold_lookup(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "主人", "我妈住院了，最近常去医院",
                        "2026-10-01T10:00:00+00:00")
        entry_id = result["entry"]["id"]
        # 状态一：今天能想起
        before = store.recall("我妈", now_ts="2026-10-01T10:05:00+00:00")
        self.assertTrue(before["has_material"])
        # 换日：今日全文进长期档，再改时间制造低 retention + 闲置超限
        store.today_entries("2026-11-20T10:00:00+00:00")
        store._append_usage({"id": entry_id, "ts": "2026-10-02T10:00:00+00:00",
                             "recall_count": 0, "last_recalled_at": "2026-10-02T10:00:00+00:00",
                             "hit_count": 0})
        # 状态二：读时结算 → 落冷仓，回想想不起来
        after = store.recall("我妈", now_ts="2026-11-20T10:05:00+00:00")
        self.assertFalse(after["has_material"], "该想不起来了")
        cold = store.cold_entries()
        self.assertEqual(1, len(cold))
        self.assertEqual(entry_id, cold[0]["id"])
        self.assertEqual("archived", cold[0]["status"])
        # 状态三：原文仍能从冷仓读回
        original = store.cold_lookup(entry_id)
        self.assertIn("我妈住院了", original["text"])

    def test_fuzzy_and_trace_rendering(self):
        params = json.loads((DATA / "retention.json").read_text(encoding="utf-8"))
        entry = {"text": "我妈住院了，最近常去医院，医生说要多休息，别太累，我每天下班都过去陪她。这是一段足够长的原文，用来验证 fuzzy 只保留前四十二个字，后面的内容都要被细节记不清了替代掉。"}
        fuzzy = retention_mod.render(entry, 0.3, params)
        trace = retention_mod.render(entry, 0.05, params)
        self.assertIn("（细节记不清了）", fuzzy)
        self.assertEqual(params["fuzzy_chars"], len(fuzzy) - len("（细节记不清了）"))
        self.assertIn("（想不起来了）", trace)
        self.assertLess(len(trace), len(fuzzy))

    def test_params_come_from_data_file_not_code(self):
        params = json.loads((DATA / "retention.json").read_text(encoding="utf-8"))
        self.assertEqual(0.35, params["decay_rate_normal"])
        self.assertEqual(0.12, params["decay_rate_owner"])
        self.assertEqual(0.08, params["threshold"])
        self.assertEqual(30, params["idle_days"])


if __name__ == "__main__":
    unittest.main()
