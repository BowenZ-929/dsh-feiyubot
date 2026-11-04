"""脑线第二轮验收：status 退役消失、装包 dropped(remaining/need)、α/β 与来源、evidence_key。"""
from __future__ import annotations

import json
import unittest

from organs.brain.model import PackLimits
from organs.brain.pack import pack
from organs.brain.tests.helpers import fresh_store, ingest


class StatusFilterTest(unittest.TestCase):
    def test_retired_memory_appears_then_disappears(self):
        """验收①：先复现一条已退役记忆出现在回想里，再让它消失。"""
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "主人", "我换新工作了", "2026-11-03T10:00:00+00:00")
        entry_id = result["entry"]["id"]
        before = store.recall("新工作", now_ts="2026-11-03T10:01:00+00:00")
        self.assertTrue(before["has_material"], "改前必须能复现它出现在回想里")
        store.retire(entry_id, ts="2026-11-03T10:02:00+00:00")
        after = store.recall("新工作", now_ts="2026-11-03T10:03:00+00:00")
        self.assertFalse(after["has_material"], "退役后必须从回想里消失")
        on_disk = json.loads((store.root / "today.json").read_text(encoding="utf-8"))
        self.assertEqual("stale", on_disk["entries"][0]["status"], "行还在盘上，只打状态位")


class PackDroppedTest(unittest.TestCase):
    def test_dropped_records_remaining_and_need(self):
        """验收②的一部分：超预算跳过时 dropped 带 remaining/need，不静默丢。"""
        limits = PackLimits(slots=2, per_item_cap=160, today_quota=1, longterm_quota=1)
        out = pack([
            {"id": "t1", "source": "today", "text": "一", "score": 3.0},
            {"id": "t2", "source": "today", "text": "二", "score": 2.0},
            {"id": "l1", "source": "longterm", "text": "三", "score": 1.0},
        ], limits=limits)
        self.assertEqual(2, out["slots_used"])
        dropped = out["skipped"][0]
        self.assertEqual("quota_full", dropped["reason"])
        self.assertEqual({"slots": 1, "quota": 0}, dropped["remaining"])
        self.assertEqual({"slots": 1}, dropped["need"])

    def test_long_item_capped_not_dropped(self):
        limits = PackLimits(slots=1, per_item_cap=5, today_quota=1, longterm_quota=1)
        out = pack([{"id": "t1", "source": "today", "text": "一二三四五六"}], limits=limits)
        self.assertTrue(out["items"][0]["capped"])
        self.assertEqual(5, len(out["items"][0]["text"]))


class CredibilityTest(unittest.TestCase):
    def test_scheduled_memory_is_talk_not_testimony(self):
        """验收③：来源类型亲历/日程模拟；虚构只作谈资、不作证词。"""
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.ingest("主人", "我去买菜了", ts="2026-11-03T10:00:00+00:00",
                     source_type="日程模拟")
        store.ingest("主人", "我换了新工作", ts="2026-11-03T10:15:00+00:00",
                     source_type="亲历")
        result = store.recall("买菜", now_ts="2026-11-03T10:16:00+00:00")
        self.assertTrue(result["has_material"])
        item = result["items"][0]
        self.assertEqual("日程模拟", item["source_type"])
        self.assertFalse(item["testimony"], "虚构只作谈资、不作证词")
        result2 = store.recall("新工作", now_ts="2026-11-03T10:16:00+00:00")
        self.assertTrue(result2["items"][0]["testimony"])

    def test_scheduled_ranks_after_lived(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.ingest("主人", "我去买菜了", ts="2026-11-03T10:00:00+00:00",
                     source_type="日程模拟")
        store.ingest("主人", "我去买菜的路上遇到你", ts="2026-11-03T10:15:00+00:00",
                     source_type="亲历")
        result = store.recall("买菜", now_ts="2026-11-03T10:16:00+00:00")
        sources = [item["source_type"] for item in result["items"]]
        self.assertEqual("亲历", sources[0], "亲历排在日程模拟之前")
        self.assertIn("日程模拟", sources)


class EvidenceKeyTest(unittest.TestCase):
    def test_lived_has_evidence_key_and_scheduled_is_unbacked(self):
        """验收④：回想每条带 evidence_key，无出处显式标「无记录背书」。"""
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        lived = store.ingest("主人", "我换了新工作", ts="2026-11-03T10:00:00+00:00")
        store.ingest("主人", "我去买菜了", ts="2026-11-03T10:15:00+00:00",
                     source_type="日程模拟")
        item = store.recall("新工作", now_ts="2026-11-03T10:16:00+00:00")["items"][0]
        self.assertEqual(lived["entry"]["id"], item["evidence_key"])
        self.assertTrue(item["backed"])
        self.assertEqual("", item["backing_note"])
        sim = store.recall("买菜", now_ts="2026-11-03T10:16:00+00:00")["items"][0]
        self.assertEqual("", sim["evidence_key"])
        self.assertFalse(sim["backed"])
        self.assertEqual("无记录背书", sim["backing_note"])


class Pack3000Test(unittest.TestCase):
    def test_3000_chars_packed_within_budget_with_dropped_and_cap(self):
        """验收③：3000 字量级下装包不超预算，能看到 cap 与 dropped(remaining/need)。"""
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        for index in range(5):
            text = ("重点内容%d" % index) + ("字" * 590)   # 每条约 600 字，共 3000 字量级
            store.ingest("群友%d" % index, text,
                         ts="2026-11-03T10:%02d:00+00:00" % index)
        result = store.recall("重点", now_ts="2026-11-03T10:06:00+00:00")
        self.assertTrue(result["has_material"])
        self.assertLessEqual(result["slots_used"], result["budget"]["slots"])
        for item in result["items"]:
            self.assertLessEqual(len(item["text"]), result["budget"]["per_item_cap"],
                                 "每项必须被 cap")
            self.assertTrue(item["capped"])
        self.assertTrue(result["skipped"], "5 条候选、今日桶配额 3：必须看到 dropped")
        for dropped in result["skipped"]:
            self.assertIn("remaining", dropped)
            self.assertIn("need", dropped)


class BackfillTest(unittest.TestCase):
    def test_import_rows_normalizes_legacy_rows(self):
        """顺带：数据导入器把旧存量行补齐 status/来源/α/β/evidence_key。"""
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = store.import_rows([
            {"id": "old-1", "ts": "2026-11-01T10:00:00+00:00", "who": "主人",
             "text": "旧数据一条", "source": "today"},
        ])
        self.assertEqual(1, result["imported"])
        rows = store.longterm_active_entries()
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("active", row["status"])
        self.assertEqual("亲历", row["source_type"])
        self.assertEqual(1, row["alpha"])
        self.assertEqual(0, row["beta"])
        self.assertEqual("old-1", row["evidence_key"])


if __name__ == "__main__":
    unittest.main()
