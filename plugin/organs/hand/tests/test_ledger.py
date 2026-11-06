"""手账：先落 reserved 再动手；**账写不进去就算这次动作失败**；只增不改、读时折叠。"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from organs.hand import actions, ledger

from . import helpers

WHO = "1000xxxx"


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)


class WriteFailureTest(BaseCase):
    def test_unwritable_reserved_row_means_nothing_happens(self):
        # 把 ledger.jsonl 做成一个目录：追加必然失败（这是最朴素的故障注入）。
        (self.root / "ledger.jsonl").mkdir()
        out = actions.add_task(who=WHO, title="交房租", when="明天中午", now=helpers.NOW,
                               root=self.root)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["code"], "LEDGER_WRITE_FAILED")
        self.assertEqual(out["detail"]["stage"], "reserved")
        self.assertIsNone(out["ledger_id"])
        # 家那边零写入：卡没落、工作台连目录都没建
        self.assertEqual(helpers.tasks(self.root), [])
        self.assertFalse((helpers.home_root(self.root) / "工作台" / "index.jsonl").exists())

    def test_unwritable_result_row_turns_success_into_failure(self):
        real = ledger.append
        calls = {"count": 0}

        def flaky(entry, *, root=None):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise OSError("盘满了（测试注入）")
            return real(entry, root=root)

        with mock.patch.object(ledger, "append", flaky):
            out = actions.add_task(who=WHO, title="交房租", when="明天中午", now=helpers.NOW,
                                   root=self.root)

        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["code"], "LEDGER_WRITE_FAILED")
        self.assertEqual(out["detail"]["stage"], "result")
        self.assertEqual(out["detail"]["wanted"]["state"], "done")
        # 卡其实落下去了（家那边动了手），但对外绝不说是成功
        self.assertNotEqual(out["state"], "done")
        self.assertEqual(len(helpers.tasks(self.root)), 1)


class FoldTest(BaseCase):
    def test_latest_row_wins_when_reading(self):
        first = ledger.append(ledger.make_entry(who=WHO, action="add_task", state="reserved",
                                                at=helpers.NOW), root=self.root)
        ledger.append(ledger.make_entry(who=WHO, action="add_task", state="done", code="OK",
                                        at=helpers.NOW, ledger_id=first["ledger_id"]),
                      root=self.root)
        self.assertEqual(len(helpers.ledger_lines(self.root)), 2)      # 盘上两行，一个都不删
        folded = ledger.read_all(root=self.root)
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0]["state"], "done")

    def test_read_tail_counts_actions_not_lines(self):
        for index in range(3):
            row = ledger.append(ledger.make_entry(who=WHO, action="add_task", at=helpers.NOW),
                                root=self.root)
            ledger.append(ledger.make_entry(ledger_id=row["ledger_id"], who=WHO,
                                             action="add_task", state="done", at=helpers.NOW),
                          root=self.root)
        self.assertEqual(len(helpers.ledger_lines(self.root)), 6)
        self.assertEqual(len(ledger.read_tail(limit=2, root=self.root)), 2)
        self.assertEqual(ledger.read_tail(limit=0, root=self.root), [])

    def test_bad_line_does_not_break_the_whole_ledger(self):
        ledger.append(ledger.make_entry(who=WHO, action="add_task", state="done",
                                        at=helpers.NOW), root=self.root)
        with open(str(helpers.ledger_path(self.root)), "a", encoding="utf-8") as handle:
            handle.write("{半截行\n")
        rows = ledger.read_all(root=self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "done")

    def test_long_params_are_truncated_in_the_summary(self):
        row = ledger.append(ledger.make_entry(who=WHO, action="add_task", state="reserved",
                                              params={"note": "很长" * 80}, at=helpers.NOW),
                            root=self.root)
        self.assertLessEqual(len(row["params"]["note"]), ledger.SUMMARY_CHARS + 1)
        self.assertTrue(row["params"]["note"].endswith("…"))


if __name__ == "__main__":
    unittest.main()
