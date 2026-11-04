"""评测最小集：20 题 + 6 锚题 + 闭卷两臂，净增益为正，锚题全过。"""
from __future__ import annotations

import unittest

from organs.brain.bench.eval import run_eval


class EvalTest(unittest.TestCase):
    def test_net_gain_positive_and_anchors_pass(self):
        report = run_eval()
        self.assertGreater(report["net_gain"], 0, report)
        self.assertEqual(0, report["closed_hits"], "闭卷臂不能有命中，否则基准是脏的")
        self.assertEqual(20, report["open_hits"], "20 题在给记忆臂应全部命中")
        self.assertTrue(report["anchors_ok"], report["anchors"])

    def test_human_spotcheck_sheet_has_26_lines(self):
        from organs.brain.bench.eval import render_for_human
        sheet = render_for_human()
        self.assertEqual(26, len([line for line in sheet.splitlines()
                                  if line[:2].strip().rstrip(".").isdigit()]))


if __name__ == "__main__":
    unittest.main()
