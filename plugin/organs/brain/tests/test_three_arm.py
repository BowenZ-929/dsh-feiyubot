"""三臂对照：冷启动 / 朴素字面 / 我们的方案，跑 fixture。

判定函数唯一（judge）：answer 子串是否出现在该臂"被选中的条目"的完整正文里。
refuse 题正确拒答得分：答案不得出现在被选中的条目里。
"""
from __future__ import annotations

import unittest

from organs.brain.bench.fixture import build_store, load_fixture
from organs.brain.bench.three_arm import arm_naive, arm_ours, judge, run

NOW = "2026-11-03T12:00:00+00:00"


class ThreeArmTest(unittest.TestCase):
    def test_judge_single_function(self):
        self.assertTrue(judge({"answer": "住院", "kind": "answer"}, ["我妈住院了"]))
        self.assertFalse(judge({"answer": "住院", "kind": "answer"}, ["别的"]))
        self.assertTrue(judge({"answer": "新耳机", "kind": "refuse"}, ["别的"]),
                        "refuse 题：没把答案递出去才算对")
        self.assertFalse(judge({"answer": "新耳机", "kind": "refuse"}, ["我买了个新耳机"]))

    def test_three_arms_report(self):
        report = run()
        self.assertEqual(26, report["total"])
        self.assertEqual(9, report["cold"], "冷启动：只有拒答题得分（什么都不答）")
        self.assertEqual(20, report["naive"], "朴素：17 道应答题 + 3 道语料没有的拒答")
        self.assertEqual(26, report["ours"], "我们的方案：status/来源把退役与虚构也拒对")
        self.assertGreater(report["net_vs_cold"], 0)
        self.assertGreater(report["net_vs_naive"], 0)

    def test_probe_categories_recorded_not_in_acceptance(self):
        report = run()
        probes = report["probe_scores"]
        self.assertEqual({"换说法", "指代", "时间", "下坡基线"}, set(probes))
        self.assertEqual(4, probes["换说法"]["total"])
        self.assertEqual(4, probes["指代"]["total"])
        self.assertEqual(4, probes["时间"]["total"])
        self.assertEqual(1, probes["下坡基线"]["total"])
        # 这三类只记录分数与标注，不进验收线；不钉具体分数，
        # 但每一类现在都该显著低于满分（字面档答不好）。
        for category in ("换说法", "指代", "时间"):
            self.assertLess(probes[category]["ours"], probes[category]["total"])
        self.assertEqual(0, probes["下坡基线"]["ours"], "下坡基线现在必须不过")

    def test_downhill_baseline_must_fail_now(self):
        fixture = load_fixture()
        store, tmp = build_store(fixture)
        self.addCleanup(tmp.cleanup)
        downhill = [q for q in fixture["probe_questions"]
                    if q["category"] == "下坡基线"][0]
        result = store.recall(downhill["q"], now_ts=NOW)
        selected = [item.get("full_text", item.get("text", ""))
                    for item in result.get("items", [])
                    if item.get("testimony") is True]
        self.assertFalse(judge(downhill, selected),
                         "下坡基线现在必须不过；哪天过了先查题是不是被改软了")

    def test_naive_answers_retired_but_ours_refuses(self):
        fixture = load_fixture()
        store, tmp = build_store(fixture)
        self.addCleanup(tmp.cleanup)
        retired_q = [q for q in fixture["questions"]
                     if q["kind"] == "refuse" and "退役" in q["note"]][0]
        self.assertFalse(judge(retired_q, [self._naive_text(fixture, retired_q)]),
                         "朴素基线会把退役记忆当答案递出去")
        self.assertTrue(judge(retired_q, self._ours_texts(store, retired_q)),
                        "我们的方案正确拒答退役记忆")

    def test_naive_answers_scheduled_but_ours_refuses(self):
        fixture = load_fixture()
        store, tmp = build_store(fixture)
        self.addCleanup(tmp.cleanup)
        scheduled_q = [q for q in fixture["questions"]
                       if q["kind"] == "refuse" and "日程模拟" in q["note"]][0]
        self.assertFalse(judge(scheduled_q, [self._naive_text(fixture, scheduled_q)]))
        self.assertTrue(judge(scheduled_q, self._ours_texts(store, scheduled_q)))

    @staticmethod
    def _naive_text(fixture, question):
        from organs.brain.bench.three_arm import _naive_top1
        return _naive_top1(fixture, question)

    @staticmethod
    def _ours_texts(store, question):
        result = store.recall(question["q"], now_ts=NOW)
        return [item.get("full_text", item.get("text", ""))
                for item in result.get("items", [])
                if item.get("testimony") is True]


if __name__ == "__main__":
    unittest.main()
