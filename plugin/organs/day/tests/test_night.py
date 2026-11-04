"""night：四步固定顺序、一步失败继续下一步、每次跑完落一行日志。"""
from __future__ import annotations

import unittest

from organs.day import night
from organs.day.tests.helpers import at, brain, log_lines, temp_root

NOW = "2026-11-06T05:00:00"


class NightRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root(self)
        self.heart = self.root / "heart"
        self.heart.mkdir()
        self.brain = brain(self.root)

    def _run(self, **kwargs):
        params = {"root": self.root, "brain": self.brain, "heart_root": self.heart}
        params.update(kwargs)
        return night.run(at(NOW), **params)

    def test_four_steps_in_fixed_order_with_expected_statuses(self):
        payload = self._run()
        self.assertEqual(list(night.STEPS), list(payload["steps"]), "四步顺序是契约，不许重排")
        self.assertEqual({"brain_compact": "ok", "brain_decay": "stubbed",
                          "home_diary": "stubbed", "heart_settle": "ok"},
                         {name: row["status"] for name, row in payload["steps"].items()})
        self.assertEqual([], payload["failed"])
        self.assertTrue(payload["ok"])

    def test_brain_compact_really_calls_run_night(self):
        payload = self._run()
        detail = payload["steps"]["brain_compact"]["detail"]
        self.assertIn("night_of=", detail)
        self.assertIn(at(NOW)[:10], detail)
        self.assertIsInstance(payload["steps"]["brain_compact"]["proposals"], int)

    def test_stubs_say_why_they_are_empty(self):
        payload = self._run()
        self.assertIn("_settle_retention", payload["steps"]["brain_decay"]["detail"])
        self.assertIn("home/rooms.py:34", payload["steps"]["home_diary"]["detail"])

    def test_heart_settle_reports_value_and_tier(self):
        payload = self._run()
        step = payload["steps"]["heart_settle"]
        self.assertIn("value", step)
        self.assertIn("tier", step)
        self.assertIsInstance(step["value"], float)

    def test_break_step_fails_one_step_and_keeps_going(self):
        payload = self._run(break_step=2)
        self.assertEqual("failed", payload["steps"]["brain_decay"]["status"])
        self.assertIn("RuntimeError", payload["steps"]["brain_decay"]["detail"])
        self.assertEqual("stubbed", payload["steps"]["home_diary"]["status"])
        self.assertEqual("ok", payload["steps"]["heart_settle"]["status"], "第 4 步必须仍然跑完")
        self.assertEqual(["brain_decay"], payload["failed"])
        self.assertFalse(payload["ok"])
        line = log_lines(self.root)[-1]
        self.assertEqual("failed", line["steps"]["brain_decay"]["status"])

    def test_brain_missing_is_one_failed_step_not_a_dead_night(self):
        payload = self._run(brain=None)
        self.assertEqual("failed", payload["steps"]["brain_compact"]["status"])
        self.assertIn("脑未载入", payload["steps"]["brain_compact"]["detail"])
        self.assertEqual("ok", payload["steps"]["heart_settle"]["status"])

    def test_heart_root_failure_does_not_stop_the_log(self):
        payload = self._run(heart_root=self.root / "no-such-dir" / ".." / "nope")
        self.assertIn(payload["steps"]["heart_settle"]["status"], ("ok", "failed"))
        self.assertEqual(1, len(log_lines(self.root)))

    def test_break_step_out_of_range_changes_nothing(self):
        payload = self._run(break_step=9)
        self.assertEqual([], payload["failed"])

    def test_rerun_same_night_is_idempotent(self):
        self._run()
        again = self._run()
        self.assertTrue(again["steps"]["brain_compact"]["rerun"])


class NightLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root(self)
        self.heart = self.root / "heart"
        self.heart.mkdir()
        self.brain = brain(self.root)

    def _run(self, **kwargs):
        params = {"root": self.root, "brain": self.brain, "heart_root": self.heart}
        params.update(kwargs)
        return night.run(at(NOW), **params)

    def test_one_line_per_run(self):
        self._run()
        self._run()
        rows = log_lines(self.root)
        self.assertEqual(2, len(rows), "一次夜一行，不许把上一次改写掉")
        for row in rows:
            for key in ("ts", "day", "written", "steps", "failed"):
                self.assertIn(key, row)
            self.assertEqual(1, row["written"])
            self.assertEqual(list(night.STEPS), list(row["steps"]))

    def test_log_tail_limit_and_raw(self):
        for _ in range(3):
            self._run()
        tail = night.log_tail(2, root=self.root)
        self.assertEqual(2, tail["count"])
        self.assertEqual(2, len(tail["raw"]))
        self.assertIn("steps", tail["lines"][-1])

    def test_missing_log_reads_as_empty_not_as_error(self):
        tail = night.log_tail(5, root=self.root)
        self.assertEqual(0, tail["count"])
        self.assertEqual([], tail["raw"])

    def test_day_root_only_gets_the_log(self):
        self._run()
        names = sorted(path.name for path in self.root.iterdir() if path.is_file())
        self.assertEqual(["night_log.jsonl"], names, "夜不动计划账与节律状态")

    def test_heart_state_lands_in_heart_root(self):
        self._run()
        self.assertTrue((self.heart / "energy.json").exists(), "心的结算写在心自己的根下")
        self.assertFalse((self.root / "energy.json").exists())


if __name__ == "__main__":
    unittest.main()
