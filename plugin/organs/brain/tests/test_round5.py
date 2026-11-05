"""第五轮：judge、usage 账、重建不丢、preflight、day_key 收敛、回归断言。"""
from __future__ import annotations

import json
import unittest

from organs.brain import judge as judge_mod
from organs.brain.judge import JudgeInput, decide
from organs.brain.preflight import preflight
from organs.brain.tests.helpers import fresh_store, ingest


def _input(mood=3.0, energy=30.0, familiarity=2.0, has_material=True,
           used=1, limit=20, **kwargs):
    return JudgeInput(
        text=kwargs.pop("text", "在吗"),
        heart_state={"mood": {"now": mood}, "energy": energy},
        familiarity=familiarity,
        material={"has_material": has_material, "slots_used": 1},
        daily_quota={"used": used, "limit": limit},
        now=kwargs.pop("now", "2026-11-03T12:00:00+00:00"),
        **kwargs)


class JudgeTest(unittest.TestCase):
    def test_mood_changes_conclusion_and_attribution(self):
        good = decide(_input(mood=5.0, energy=40.0))
        bad = decide(_input(mood=0.0, energy=0.0))
        self.assertEqual("回", good["reply"])
        self.assertEqual("不回", bad["reply"])
        self.assertIn("mood-and-energy-low", bad["codes"]["reply"])

    def test_named_forces_reply_even_when_engage_silent(self):
        result = decide(_input(mood=0.0, energy=0.0, named=True))
        self.assertEqual("回", result["reply"])
        self.assertIn("named", result["codes"]["reply"])

    def test_no_unattributed_silence_in_20_judgments(self):
        for _ in range(20):
            result = decide(_input())
            if result["reply"] == "不回":
                self.assertNotIn("unattributed", result["codes"]["reply"])

    def test_act_request_detected(self):
        result = decide(_input(text="帮我查一下"))
        self.assertEqual("动手", result["act"])
        self.assertIn("act-requested", result["codes"]["act"])


class UsageLedgerTest(unittest.TestCase):
    def test_usage_survives_index_rebuild(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        for index in range(65):
            ingest(store, "群友%d" % index,
                   "第 %d 句：这是一段足够长的原文，用来证明薄索引里不放全文" % index,
                   "2026-11-03T12:00:00+00:00")
        store.recall("第 0 句", now_ts="2026-11-03T13:00:00+00:00")
        usage_before = store.usage_entries()
        rebuilt = store.rebuild_index("2026-11-03T13:05:00+00:00")
        self.assertTrue(rebuilt["ok"])
        usage_after = store.usage_entries()
        self.assertEqual(usage_before, usage_after,
                         "重建索引后 usage 账逐字不变（计数与上次命中时间不丢）")


class PreflightTest(unittest.TestCase):
    def test_preflight_reports_corruption_with_human_message(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        self.assertTrue(preflight(store.root)["ok"])
        with open(store.root / "longterm.jsonl", "a", encoding="utf-8") as f:
            f.write("{bad json\n")
        report = preflight(store.root)
        self.assertFalse(report["ok"])
        self.assertTrue(any("第" in issue["message"] and "不是合法 JSON" in issue["message"]
                            for issue in report["issues"]))


class DayKeyConvergenceTest(unittest.TestCase):
    def test_brain_has_no_day_key_definition_and_imports_day_model(self):
        import inspect

        from organs.brain import model as brain_model
        from organs.day import model as day_model

        self.assertIs(day_model.day_key, brain_model.day_key,
                      "brain 的 day_key 必须是 organs/day/model 那一份")
        source = inspect.getsource(brain_model)
        self.assertNotIn("def day_key", source, "brain/model.py 不许再定义 day_key")


class RegressionTests(unittest.TestCase):
    """每个修过的 bug 一条命名断言：改坏了要能一眼看出是哪条回来的。"""

    def test_bug_retired_memory_not_in_recall(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "主人", "我买了个新耳机", "2026-11-03T10:00:00+00:00")
        store.retire(result["entry"]["id"], ts="2026-11-03T10:01:00+00:00")
        self.assertFalse(store.recall("新耳机", now_ts="2026-11-03T10:02:00+00:00")["has_material"])

    def test_bug_archived_has_writer(self):
        from organs.brain.model import STATUS_ARCHIVED, STATUS_WRITERS
        self.assertIn("_settle_retention", STATUS_WRITERS[STATUS_ARCHIVED])

    def test_bug_chinese_name_unquote(self):
        import tempfile
        from pathlib import Path
        from api import api
        from api.tests.helpers import copy_fixture
        from organs.brain import BrainStore
        from organs.soul import load

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        soul = load(copy_fixture("valid", tmp.name))
        brain = BrainStore(Path(tmp.name) / "brain")
        brain.ingest("主人", "在吗")
        status, payload = api.handle(
            "GET", "/brain/people/%E4%B8%BB%E4%BA%BA/card", None, soul, brain=brain)
        self.assertEqual(200, status, payload)

    def test_bug_empty_array_not_collapsed_to_zero(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        self.assertEqual([], store.cold_page()["items"])
        self.assertIn("count", store.tiers_summary()["cold"])

    def test_bug_duplicate_edges_not_created(self):
        from organs.brain import edges as edges_mod
        graph = {"nodes": {}, "edges": []}
        edges_mod.add_event_edges(graph, "m1", "主人", ["医院"], "2026-11-03T10:00:00+00:00")
        edges_mod.add_event_edges(graph, "m1", "主人", ["医院"], "2026-11-03T10:00:00+00:00")
        ids = [e["id"] for e in graph["edges"]]
        self.assertEqual(len(ids), len(set(ids)), "同一条边不许重复建")


if __name__ == "__main__":
    unittest.main()
