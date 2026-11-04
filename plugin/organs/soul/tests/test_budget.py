"""六件预算：单件超建议值只 warn（形象、怕什么以外），合计超 1000 硬线才拦（改 fixture 副本，不动真数据）。"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from unittest import mock

from organs.soul import model
from organs.soul.store import load, read_blocks
from organs.soul.tests.helpers import copy_fixture, edit_card, read_bytes


class BudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)

    def _issues(self) -> list:
        _, issues = read_blocks(self.root)
        return issues

    def _assert_warn_only_over_suggested(self) -> None:
        """单件超建议值：有 SUGGESTED_BUDGET_EXCEEDED warn，没有 error，卡照样载得进来。"""
        issues = self._issues()
        warned = [issue for issue in issues if issue.code == "SUGGESTED_BUDGET_EXCEEDED"]
        self.assertTrue(warned, [issue.code for issue in issues])
        self.assertEqual({"warn"}, {issue.level for issue in warned})
        self.assertEqual([], [issue for issue in issues if issue.level == model.ERROR])
        load(self.root)

    def test_clean_fixture_loads_and_six_items_fit(self):
        store = load(self.root)
        snapshot = store.snapshot()
        parts = model.body_parts(snapshot.identity, snapshot.manner, snapshot.bounds)
        self.assertEqual(6, len(parts))
        for _, chars, limit, _ in parts:
            self.assertLessEqual(chars, limit)
        self.assertLessEqual(model.body_chars(snapshot.identity, snapshot.manner, snapshot.bounds),
                             model.limits.body_total)

    def test_name_over_forty_only_warns(self):
        edit_card(self.root, "identity", lambda data: data["self_names"].update({"display": "鱼" * 41}))
        self._assert_warn_only_over_suggested()

    def test_statements_over_sixty_only_warns(self):
        edit_card(self.root, "identity",
                  lambda data: data.update({"self_statements": ["字" * 21, "字" * 21, "字" * 21]}))
        self._assert_warn_only_over_suggested()

    def test_manner_over_one_hundred_only_warns(self):
        edit_card(self.root, "manner", lambda data: data.update({"stance": "字" * 101}))
        self._assert_warn_only_over_suggested()

    def test_bounds_over_two_hundred_fifty_only_warns(self):
        edit_card(self.root, "bounds", lambda data: data["items"][0].update({"rule": "禁" * 250}))
        self._assert_warn_only_over_suggested()

    def test_total_over_one_thousand_rejected(self):
        """单件都放行，加起来过 1000 就拒载：合计才是硬线。"""
        edit_card(self.root, "bounds", lambda data: data["items"][0].update({"rule": "禁" * 1000}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        messages = [issue.message for issue in caught.exception.issues if issue.code == "BUDGET_OVER"]
        self.assertTrue(any("合计" in message for message in messages), messages)

    def test_total_gate_reads_body_total(self):
        """把 body_total 压到 20，37 字的干净卡就该被合计闸拦下。"""
        tighter = dataclasses.replace(model.limits, body_total=20)
        with mock.patch.object(model, "limits", tighter):
            with self.assertRaises(model.SoulLoadError) as caught:
                load(self.root)
        messages = [issue.message for issue in caught.exception.issues if issue.code == "BUDGET_OVER"]
        self.assertTrue(any("合计" in message for message in messages), messages)

    def test_suggested_values_stay_below_the_hard_line(self):
        """四个建议值加起来 450，硬线 1000：留下的余量给红线占大头（plan §2）。

        形象（60）不在其中：它不是建议值，是用户指定的硬线。
        """
        suggested = (model.limits.self_names + model.limits.self_statements
                     + model.limits.manner + model.limits.bounds)
        self.assertEqual(450, suggested)
        self.assertEqual(60, model.limits.appearance)
        self.assertEqual(1000, model.limits.body_total)

    def test_over_hard_line_card_is_refused_not_trimmed(self):
        edit_card(self.root, "bounds", lambda data: data["items"][0].update({"rule": "禁" * 1000}))
        before = read_bytes(self.root / "bounds.json")
        with self.assertRaises(model.SoulLoadError):
            load(self.root)
        self.assertEqual(before, read_bytes(self.root / "bounds.json"))


if __name__ == "__main__":
    unittest.main()
