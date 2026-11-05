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


class NightRetryScopeTest(unittest.TestCase):
    """重试只重跑指定的步骤：其余原样沿用上一次的结果，日志里看得出这一次跑了哪几步。"""

    def setUp(self) -> None:
        self.root = temp_root(self)
        self.heart = self.root / "heart"
        self.heart.mkdir()
        self.brain = brain(self.root)

    def _run(self, **kwargs):
        params = {"root": self.root, "brain": self.brain, "heart_root": self.heart}
        params.update(kwargs)
        return night.run(at(NOW), **params)

    def test_only_reruns_listed_steps_and_carries_the_rest(self):
        first = self._run(break_step=2)
        second = self._run(break_step=2, only=["brain_decay"], carry=first["steps"],
                           attempt=1)
        self.assertEqual(list(night.STEPS), list(second["steps"]), "四步名单与顺序不变")
        self.assertEqual(["brain_decay"], second["retried_steps"])
        self.assertEqual(1, second["attempt"])
        for name in ("brain_compact", "home_diary", "heart_settle"):
            self.assertEqual(first["steps"][name], second["steps"][name],
                             "%s 不重跑：结果原样沿用" % name)
        self.assertEqual("failed", second["steps"]["brain_decay"]["status"])

    def test_carried_step_without_a_previous_record_is_not_faked(self):
        payload = self._run(only=["heart_settle"], carry={})
        self.assertEqual(night.STATUS_CARRIED, payload["steps"]["brain_compact"]["status"])
        self.assertIn("没跑", payload["steps"]["brain_compact"]["detail"])
        self.assertEqual("ok", payload["steps"]["heart_settle"]["status"])

    def test_each_attempt_is_one_log_line_with_attempt_and_retried_steps(self):
        first = self._run(break_step=2)
        self._run(break_step=2, only=["brain_decay"], carry=first["steps"], attempt=1)
        rows = log_lines(self.root)
        self.assertEqual(2, len(rows))
        self.assertEqual(0, rows[0]["attempt"])
        self.assertEqual(list(night.STEPS), rows[0]["retried_steps"])
        self.assertEqual(1, rows[1]["attempt"])
        self.assertEqual(["brain_decay"], rows[1]["retried_steps"])
        self.assertEqual(["brain_decay"], rows[1]["failed"])

    def test_never_ran_audit_is_one_line_in_the_same_log(self):
        self._run()
        out = night.audit_never_ran(at(NOW), root=self.root, day="2026-11-05", attempt=3,
                                   failed_steps=["brain_decay"],
                                   reason="该跑没跑成：brain_decay 在 4 次尝试里都没成功")
        rows = log_lines(self.root)
        self.assertEqual(2, len(rows), "审计与夜跑共用 night_log.jsonl，不另开文件")
        line = rows[-1]
        self.assertEqual("never_ran", line["event"])
        self.assertEqual(3, line["attempt"])
        self.assertEqual(["brain_decay"], line["failed_steps"])
        self.assertIn("brain_decay", line["reason"])
        self.assertEqual(out["log_path"], str(self.root / "night_log.jsonl"))


class NightBackoffTest(unittest.TestCase):
    """退避算术只在 night.run 这一处：失败写下一次几点，成功或额度用完就不写。"""

    def setUp(self) -> None:
        self.root = temp_root(self)
        self.heart = self.root / "heart"
        self.heart.mkdir()
        self.brain = brain(self.root)

    def _run(self, **kwargs):
        params = {"root": self.root, "brain": self.brain, "heart_root": self.heart,
                  "backoff_minutes": [1, 5, 15], "max_retries": 3}
        params.update(kwargs)
        return night.run(at(NOW), **params)

    def test_first_failure_points_one_minute_ahead(self):
        payload = self._run(break_step=2, attempt=0)
        expected = at("2026-11-06T05:01:00")
        self.assertEqual(expected, payload["next_retry_at"])
        self.assertEqual(expected, log_lines(self.root)[-1]["next_retry_at"],
                         "日志行也要看得见下一次几点")
        self.assertEqual(expected, payload["log_line"]["next_retry_at"])

    def test_second_and_third_failures_take_five_and_fifteen_minutes(self):
        """按尝试序号取退避时长：第 2 次失败等 5 分钟、第 3 次等 15 分钟。

        累加成 T / T+1 / T+6 / T+21 由 rhythm 的 tick 时刻决定（那是真跑出来的）。
        """
        self.assertEqual(at("2026-11-06T05:05:00"),
                         self._run(break_step=2, attempt=1)["next_retry_at"])
        self.assertEqual(at("2026-11-06T05:15:00"),
                         self._run(break_step=2, attempt=2)["next_retry_at"])

    def test_budget_used_up_writes_no_next_retry_at(self):
        payload = self._run(break_step=2, attempt=3)
        self.assertIsNone(payload["next_retry_at"], "不再重试就不写一个不会发生的时刻")
        self.assertIsNone(log_lines(self.root)[-1]["next_retry_at"])

    def test_success_writes_no_next_retry_at(self):
        self.assertIsNone(self._run(attempt=0)["next_retry_at"])

    def test_without_a_backoff_table_nothing_is_promised(self):
        self.assertIsNone(self._run(break_step=2, attempt=0, backoff_minutes=None)["next_retry_at"])

    def test_audit_line_carries_the_attempt_and_the_boundary_key(self):
        night.audit_never_ran(at(NOW), root=self.root, day="2026-11-05", attempt=3,
                              failed_steps=["brain_decay"], reason="试满了")
        line = log_lines(self.root)[-1]
        self.assertEqual(3, line["attempt"])
        self.assertIn("next_retry_at", line, "审计行也要能看出下一次几点（这里是没有）")
        self.assertIsNone(line["next_retry_at"])


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
