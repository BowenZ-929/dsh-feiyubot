"""两个动作 + 一个只读查询：回执信封、码表、手账两行、以及「拒绝不是失败」。"""
from __future__ import annotations

import tempfile
import unittest

from organs.hand import actions

from . import helpers

WHO = "1000xxxx"


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)


class AddTaskTest(BaseCase):
    def test_success_writes_card_and_two_ledger_rows(self):
        out = actions.add_task(who=WHO, title="交房租", when="明天中午", now=helpers.NOW,
                               root=self.root)
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["code"], "OK")
        self.assertEqual(out["action"], "add_task")
        self.assertEqual(out["who"], WHO)
        self.assertIsNotNone(out["card_id"])
        self.assertIn("记下了", out["say"])
        self.assertIsNotNone(out["ledger_id"])

        rows = helpers.ledger_lines(self.root)
        self.assertEqual([row["state"] for row in rows], ["reserved", "done"])
        self.assertEqual(rows[0]["ledger_id"], rows[1]["ledger_id"])
        self.assertEqual(rows[0]["ledger_id"], out["ledger_id"])
        self.assertEqual(rows[1]["code"], "OK")
        self.assertIsNone(rows[1]["grant_id"])

        written = helpers.tasks(self.root)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["name"], "交房租")
        self.assertEqual(written[0]["params"]["due"], helpers.TOMORROW_NOON)

    def test_no_time_at_all_is_a_question_not_a_card(self):
        out = actions.add_task(who=WHO, title="交房租", now=helpers.NOW, root=self.root)
        self.assertEqual(out["state"], "refused")
        self.assertEqual(out["code"], "MISSING_DUE")
        self.assertIn("？", out["say"])
        self.assertIsNone(out["card_id"])
        self.assertEqual(helpers.tasks(self.root), [])
        self.assertEqual([row["state"] for row in helpers.ledger_lines(self.root)],
                         ["reserved", "refused"])

    def test_unreadable_when_is_refused_and_no_card(self):
        out = actions.add_task(who=WHO, title="交房租", when="下周再说", now=helpers.NOW,
                               root=self.root)
        self.assertEqual(out["state"], "refused")
        self.assertEqual(out["code"], "MISSING_DUE")
        self.assertIn("？", out["say"])
        self.assertIn("下周再说", out["say"])
        self.assertEqual(helpers.tasks(self.root), [])

    def test_due_without_timezone_is_refused_not_guessed(self):
        out = actions.add_task(who=WHO, title="交房租", due="2026-11-07T12:00:00",
                               now=helpers.NOW, root=self.root)
        self.assertEqual(out["state"], "refused")
        self.assertEqual(out["code"], "MISSING_DUE")
        self.assertEqual(helpers.tasks(self.root), [])

    def test_due_iso_with_offset_goes_through_untouched(self):
        out = actions.add_task(who=WHO, title="开会", due="2026-11-08T09:00:00+08:00",
                               now=helpers.NOW, root=self.root)
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["detail"]["due"], "2026-11-08T09:00:00+08:00")

    def test_home_rejection_is_failed_with_original_code(self):
        # 名字过不了家的路径形状（".." 是上级目录符号）：家拒，手记一条 failed，并保留家的原始码
        out = actions.add_task(who=WHO, title="..", when="明天中午", now=helpers.NOW,
                               root=self.root)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["code"], "HOME_WRITE_FAILED")
        self.assertEqual(out["say"], actions.HOME_FAILED_SAY)
        self.assertNotEqual(out["detail"]["home"], "OK")
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(rows[-1]["state"], "failed")
        self.assertNotEqual(rows[-1]["origin"], "OK")

    def test_quota_exhausted_says_how_many_are_left(self):
        helpers.write_limits(self.root, 0)
        out = actions.add_task(who=WHO, title="交房租", when="明天中午", now=helpers.NOW,
                               root=self.root)
        self.assertEqual(out["state"], "refused")
        self.assertEqual(out["code"], "QUOTA_EXCEEDED")
        self.assertIn("还能", out["say"])
        self.assertIn("还能 0 次", out["say"])
        self.assertEqual(helpers.tasks(self.root), [])

    def test_quota_counts_down_from_the_ledger_itself(self):
        helpers.write_limits(self.root, 1)
        first = actions.add_task(who=WHO, title="交房租", when="明天中午", now=helpers.NOW,
                                 root=self.root)
        self.assertEqual(first["state"], "done")
        self.assertEqual(first["detail"]["quota"]["remaining"], 0)
        second = actions.add_task(who=WHO, title="买菜", when="后天早上", now=helpers.NOW,
                                  root=self.root)
        self.assertEqual(second["state"], "refused")
        self.assertEqual(second["code"], "QUOTA_EXCEEDED")
        self.assertEqual(len(helpers.tasks(self.root)), 1)

    def test_limit_file_is_the_source_and_says_which_level_it_came_from(self):
        out = actions.add_task(who=WHO, title="交房租", when="明天中午", now=helpers.NOW,
                               root=self.root)
        self.assertEqual(out["detail"]["quota"]["source"], "factory")
        helpers.write_limits(self.root, 7)
        again = actions.add_task(who=WHO, title="买菜", when="明天早上", now=helpers.NOW,
                                 root=self.root)
        self.assertEqual(again["detail"]["quota"]["limit"], 7)
        self.assertEqual(again["detail"]["quota"]["source"], "live")


class CompleteTaskTest(BaseCase):
    def _add(self, title="交房租", due="2026-11-06T08:00:00+09:00"):
        return actions.add_task(who=WHO, title=title, due=due, now=helpers.NOW, root=self.root)

    def test_due_sees_it_before_done_and_stops_seeing_it_after(self):
        added = self._add()
        ready = actions.due(now=helpers.NOW, root=self.root)
        self.assertEqual([card["card_id"] for card in ready], [added["card_id"]])

        out = actions.complete_task(who=WHO, card_id=added["card_id"], now=helpers.NOW,
                                    root=self.root)
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["code"], "OK")
        self.assertIn("做完了", out["say"])
        self.assertEqual(actions.due(now=helpers.NOW, root=self.root), [])

    def test_due_ignores_cards_not_yet_due(self):
        self._add(due="2026-12-01T08:00:00+09:00")
        self.assertEqual(actions.due(now=helpers.NOW, root=self.root), [])

    def test_due_is_read_only(self):
        self._add()
        before = len(helpers.ledger_lines(self.root))
        actions.due(now=helpers.NOW, root=self.root)
        self.assertEqual(len(helpers.ledger_lines(self.root)), before)

    def test_completing_unknown_card_is_failed_with_home_code(self):
        out = actions.complete_task(who=WHO, card_id="工作台-99", now=helpers.NOW, root=self.root)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["code"], "HOME_WRITE_FAILED")
        self.assertEqual(out["detail"]["home"], "CARD_NOT_FOUND")

    def test_empty_card_id_is_failed_with_home_code(self):
        out = actions.complete_task(who=WHO, card_id="  ", now=helpers.NOW, root=self.root)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["code"], "HOME_WRITE_FAILED")
        self.assertEqual(out["detail"]["home"], "BAD_ARGUMENT")

    def test_completing_twice_is_idempotent_and_says_which_time(self):
        added = self._add()
        first = actions.complete_task(who=WHO, card_id=added["card_id"], now=helpers.NOW,
                                      root=self.root)
        second = actions.complete_task(who=WHO, card_id=added["card_id"], now=helpers.NOW,
                                       root=self.root)
        self.assertEqual(first["state"], "done")
        self.assertEqual(second["state"], "done")
        self.assertEqual(second["detail"]["done_count"], 2)
        self.assertIn("第 2 次", second["say"])


if __name__ == "__main__":
    unittest.main()
