"""rhythm：tick 先排当天（幂等）再数到点、换日只数一次、下一次是纯算术、线程唯一且默认关。"""
from __future__ import annotations

import json
import unittest
from typing import Any

from organs.day import night, plan, rhythm
from organs.day.tests.helpers import at, brain, candidates, temp_root


def night_adapter_steps():
    """夜的四步名单只有一处定义（night.STEPS），测试不另抄一份。"""
    return night.STEPS

NOW = "2026-11-05T09:30:00"
# 关掉自动排程的那份配置：验证"tick 只数到点"这条纯算术判据要靠它
NO_KINDS = {"tick_seconds": 60, "min_gap_minutes": 10, "daily_cap": 12,
            "day_cutover_hour": 4, "kinds": {}}


class TickTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root(self)
        self.addCleanup(rhythm.stop, 2.0)

    def test_same_now_twice_adds_nothing(self):
        """同一个 now 连跑两次：计划条数不变、id 集合不变、consumed 数不变。"""
        plan.add(candidates(("diary", "2026-11-05T09:00:00"), ("diary", "2026-11-05T10:00:00")),
                 root=self.root, now_ts=at("2026-11-05T08:00:00"))
        first = rhythm.tick(at(NOW), root=self.root)
        second = rhythm.tick(at(NOW), root=self.root)
        self.assertEqual(first["plan_ids"], second["plan_ids"])
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(0, second["counts"]["consumed"])
        self.assertEqual(first["due"], second["due"])
        self.assertEqual([], second["ensured"]["added"], "第二次排程一条都不新增")
        self.assertEqual(1, len(second["ensured"]["duplicates"]), "排过的落 duplicates")

    def test_first_tick_schedules_todays_night(self):
        """空账第一次 tick 就给"当天"排一条 night（03:30），排的是明天凌晨、现在不到点。"""
        payload = rhythm.tick(at(NOW), root=self.root)
        self.assertEqual(["night"], payload["ensured"]["kinds"])
        self.assertEqual(1, len(payload["ensured"]["added"]))
        pending = plan.listing(self.root)["pending"]
        self.assertEqual(1, len(pending))
        self.assertEqual("night", pending[0]["kind"])
        self.assertEqual(at("2026-11-06T03:30:00"), pending[0]["at"])
        self.assertEqual("2026-11-05", pending[0]["day"], "03:30 归它前面那一天")
        self.assertEqual(plan.STATUS_PENDING, pending[0]["status"])
        self.assertEqual([], payload["due"], "排的是明天凌晨，不是现在")
        self.assertEqual(at("2026-11-06T03:30:00"), payload["next_at"])

    def test_next_day_rerolls_and_retires_yesterdays_night(self):
        """跨天重排新的一天；前一天那条没人跑过 → 标 expired（不补跑），内容不改写。"""
        rhythm.tick(at(NOW), root=self.root)
        before = plan.listing(self.root)["pending"][0]
        payload = rhythm.tick(at("2026-11-06T05:00:00"), root=self.root)
        self.assertEqual(1, payload["rollovers"])
        self.assertEqual(1, len(payload["ensured"]["added"]))
        listing = plan.listing(self.root)
        self.assertEqual(1, len(listing["pending"]), "只留新一天那条")
        kept = [row for row in listing["expired"] if row["id"] == before["id"]][0]
        self.assertEqual(plan.STATUS_EXPIRED, kept["status"])
        self.assertIn("过期不补", kept["reason"])
        for field in ("kind", "at", "day", "created_at"):
            self.assertEqual(before[field], kept[field], "过期只是换状态，不改写原来的时刻")
        fresh = listing["pending"][0]
        self.assertEqual(at("2026-11-07T03:30:00"), fresh["at"])
        self.assertEqual("2026-11-06", fresh["day"])

    def test_no_kinds_means_no_automatic_plan(self):
        """kinds 空着就不自动排：这时 tick 只数到点、不造任何计划。"""
        payload = rhythm.tick(at(NOW), root=self.root, config=NO_KINDS)
        self.assertEqual([], payload["plan_ids"])
        self.assertEqual({"pending": 0, "consumed": 0, "blocked": 0, "expired": 0,
                          "never_ran": 0, "total": 0}, payload["counts"])
        self.assertIsNone(payload["next_at"])

    def test_first_tick_only_remembers_today(self):
        payload = rhythm.tick(at("2026-11-05T23:59:00"), root=self.root)
        self.assertEqual("2026-11-05", payload["day"])
        self.assertEqual(0, payload["rollovers"], "第一次 tick 只是记下今天，不算换日")
        self.assertFalse(payload["rolled_over"])

    def test_rollover_counts_once_and_only_once(self):
        rhythm.tick(at("2026-11-05T23:59:00"), root=self.root)
        after_midnight = rhythm.tick(at("2026-11-06T05:00:00"), root=self.root)
        again = rhythm.tick(at("2026-11-06T05:00:00"), root=self.root)
        self.assertEqual(1, after_midnight["rollovers"])
        self.assertTrue(after_midnight["rolled_over"])
        self.assertEqual(1, again["rollovers"])
        self.assertFalse(again["rolled_over"])
        self.assertEqual("2026-11-06", again["day"])

    def test_due_and_next_at_are_plain_arithmetic(self):
        plan.add(candidates(("diary", "2026-11-05T09:00:00"), ("diary", "2026-11-05T10:00:00")),
                 root=self.root, now_ts=at("2026-11-05T08:00:00"))
        payload = rhythm.tick(at(NOW), root=self.root)
        self.assertEqual(1, len(payload["due"]))
        self.assertEqual(at("2026-11-05T10:00:00"), payload["next_at"])
        self.assertEqual(at("2026-11-05T09:31:00"), payload["next_tick_at"])

    def test_tick_interval_comes_from_limits(self):
        config = dict(NO_KINDS, tick_seconds=30)
        payload = rhythm.tick(at(NOW), root=self.root, config=config)
        self.assertEqual(30, payload["interval_seconds"])
        self.assertEqual(at("2026-11-05T09:30:30"), payload["next_tick_at"])

    def test_state_and_ledger_are_the_only_things_written(self):
        rhythm.tick(at(NOW), root=self.root)
        self.assertEqual(["plans.json", rhythm.STATE_FILE],
                         sorted(path.name for path in self.root.iterdir()))


class NightLoopTest(unittest.TestCase):
    """到点就真跑夜并结账：跑一次、不重跑、过期不补、单飞不撞车。"""

    def setUp(self) -> None:
        self.root = temp_root(self)
        self.heart = temp_root(self)
        self.brain = brain(self.root)
        self.addCleanup(rhythm.stop, 2.0)

    def _tick(self, now: str = "2026-11-05T03:31:00"):
        return rhythm.tick(at(now), root=self.root, brain=self.brain, heart_root=self.heart)

    def _log_lines(self):
        target = self.root / "night_log.jsonl"
        return target.read_text(encoding="utf-8").splitlines() if target.exists() else []

    def test_due_night_runs_once_and_is_consumed(self):
        first = self._tick()
        self.assertEqual(1, len(first["night"]["executed"]))
        executed = first["night"]["executed"][0]
        self.assertTrue(executed["ok"])
        self.assertEqual({"ok": 2, "stubbed": 2}, executed["summary"]["statuses"])
        self.assertEqual([], executed["summary"]["failed"])
        self.assertEqual(1, len(self._log_lines()))
        listing = plan.listing(self.root)
        self.assertEqual([], listing["pending"])
        self.assertEqual(1, len(listing["consumed"]))
        row = listing["consumed"][0]
        self.assertEqual(plan.STATUS_CONSUMED, row["status"])
        self.assertTrue(row["consumed_at"])
        # 账是一份会落盘的表，键按字典序存；四步的顺序由 night_log 那一行守着
        self.assertEqual(set(night_adapter_steps()),
                         set(row["result"]["steps"]))
        self.assertEqual({"ok": 2, "stubbed": 2}, row["result"]["statuses"])

    def test_same_now_twice_does_not_run_the_night_again(self):
        self._tick()
        second = self._tick()
        self.assertEqual([], second["due"])
        self.assertEqual([], second["night"]["executed"])
        self.assertEqual(1, len(self._log_lines()), "同一 now 再跑不许写第二行夜日志")

    def test_expired_night_is_not_made_up(self):
        """关机错过的夜：标 expired、不补跑、日志一行都不加（只跑今天这条）。"""
        rhythm.tick(at("2026-11-05T05:00:00"), root=self.root, brain=self.brain,
                    heart_root=self.heart)   # 05:00 只排不跑：那条夜在明天凌晨
        self.assertEqual([], self._log_lines())
        later = self._tick("2026-11-08T03:31:00")
        self.assertEqual(1, len(later["night"]["expired"]))
        gone = later["night"]["expired"][0]
        self.assertEqual(plan.STATUS_EXPIRED, gone["status"])
        self.assertIn("过期不补", gone["reason"])
        self.assertEqual(1, len(self._log_lines()), "只跑今天这条，前面那条不补")
        self.assertEqual(1, len(later["night"]["executed"]))

    def test_lock_taken_means_skip_not_double_run(self):
        """单飞：另一个 tick 正在跑夜时，这一次直接跳过并回 night_running。"""
        self.assertTrue(rhythm.NIGHT_LOCK.acquire(blocking=False))
        self.addCleanup(rhythm.NIGHT_LOCK.release)
        payload = self._tick()
        self.assertEqual("night_running", payload["night"]["skipped"])
        self.assertEqual([], payload["night"]["executed"])
        self.assertEqual([], self._log_lines())
        self.assertEqual(1, len(plan.listing(self.root)["pending"]),
                         "被跳过的计划留在 pending，等下一 tick")

    def test_no_execution_without_brain_stays_pending_not_consumed(self):
        """没有脑也照跑（第 1 步记 failed），但**不下账**：留 pending 等重试，不许静默 consume。"""
        payload = rhythm.tick(at("2026-11-05T03:31:00"), root=self.root,
                              heart_root=self.heart)
        self.assertEqual([], payload["night"]["executed"], "失败的那次不许算跑过")
        self.assertEqual(1, len(payload["night"]["retried"]))
        retried = payload["night"]["retried"][0]
        self.assertFalse(retried["ok"])
        self.assertEqual(["brain_compact"], retried["failed_steps"])
        self.assertEqual(0, retried["attempt"])
        self.assertEqual(1, retried["next_attempt"])
        listing = plan.listing(self.root)
        self.assertEqual([], listing["consumed"])
        self.assertEqual(1, len(listing["pending"]))
        self.assertEqual(0, listing["pending"][0]["night"]["attempts"])
        self.assertEqual(["brain_compact"], listing["pending"][0]["night"]["failed_steps"])


class NightRetryTest(unittest.TestCase):
    """失败的那次不下账：留 pending、只重跑失败步、试满次数进 never_ran 并写审计。"""

    def setUp(self) -> None:
        self.root = temp_root(self)
        self.heart = temp_root(self)
        self.brain = brain(self.root)
        self.addCleanup(rhythm.stop, 2.0)

    def _tick(self, now: str = "2026-11-05T03:31:00", break_step: Any = 2, **kwargs):
        params = {"root": self.root, "brain": self.brain, "heart_root": self.heart,
                  "break_step": break_step}
        params.update(kwargs)
        return rhythm.tick(at(now), **params)

    def _lines(self):
        target = self.root / "night_log.jsonl"
        if not target.exists():
            return []
        return [json.loads(text) for text in target.read_text(encoding="utf-8").splitlines()
                if text.strip()]

    def test_first_failure_keeps_pending_and_logs_the_step(self):
        payload = self._tick()
        self.assertEqual([], payload["night"]["executed"])
        self.assertEqual(1, len(payload["night"]["retried"]))
        row = plan.listing(self.root)["pending"][0]
        self.assertEqual(plan.STATUS_PENDING, row["status"])
        self.assertEqual(0, row["night"]["attempts"], "首跑失败：用掉 0 次重试")
        self.assertEqual(["brain_decay"], row["night"]["failed_steps"])
        self.assertEqual({"brain_compact": "ok", "brain_decay": "failed",
                          "home_diary": "stubbed", "heart_settle": "ok"},
                         {name: item["status"] for name, item in row["night"]["steps"].items()})
        lines = self._lines()
        self.assertEqual(1, len(lines), "一次尝试一行夜日志")
        self.assertEqual(0, lines[0]["attempt"])
        self.assertEqual(list(night.STEPS), lines[0]["retried_steps"], "首跑四步都跑")
        self.assertEqual(["brain_decay"], lines[0]["failed"])
        self.assertNotIn("event", lines[0], "跑过的那一行不是审计行")

    def test_retry_only_reruns_the_failed_step_and_keeps_other_results(self):
        self._tick()
        first = plan.listing(self.root)["pending"][0]["night"]["steps"]
        payload = self._tick()
        self.assertEqual(1, len(payload["night"]["retried"]))
        row = plan.listing(self.root)["pending"][0]
        self.assertEqual(1, row["night"]["attempts"], "第二次 tick 用掉 1 次重试")
        lines = self._lines()
        self.assertEqual(1, lines[1]["attempt"])
        self.assertEqual(["brain_decay"], lines[1]["retried_steps"], "只重跑失败的那一步")
        self.assertEqual(["brain_decay"], lines[1]["failed"])
        for name in ("brain_compact", "home_diary", "heart_settle"):
            self.assertEqual(first[name], lines[1]["steps"][name],
                             "%s 沿用上一次的结果，不重跑也不重写" % name)
        self.assertEqual(first["brain_compact"]["detail"],
                         row["night"]["steps"]["brain_compact"]["detail"])

    def test_two_attempts_then_success_consumes_with_all_four_steps(self):
        self._tick()
        payload = self._tick(break_step=None)      # 第二次尝试治好，四步全成
        self.assertEqual(1, len(payload["night"]["executed"]))
        listing = plan.listing(self.root)
        self.assertEqual([], listing["pending"])
        self.assertEqual(1, len(listing["consumed"]))
        self.assertEqual(set(night.STEPS), set(listing["consumed"][0]["result"]["steps"]))
        self.assertEqual({"ok": 2, "stubbed": 2},
                         listing["consumed"][0]["result"]["statuses"])

    def test_four_attempts_then_never_ran_with_one_audit_line(self):
        for _ in range(3):
            self._tick()
        last = self._tick()
        self.assertEqual(1, len(last["night"]["never_ran"]))
        self.assertEqual([], last["night"]["executed"])
        listing = plan.listing(self.root)
        self.assertEqual([], listing["pending"], "试满次数不再留 pending")
        self.assertEqual(1, len(listing["never_ran"]))
        row = listing["never_ran"][0]
        self.assertEqual(plan.STATUS_NEVER_RAN, row["status"])
        self.assertEqual(3, row["night"]["attempts"])
        self.assertIn("brain_decay", row["reason"])
        self.assertIn("4 次尝试", row["reason"])
        audit = [line for line in self._lines() if line.get("event") == "never_ran"]
        self.assertEqual(1, len(audit), "never-ran 只写一条审计，不另开第二个文件")
        self.assertEqual(["brain_decay"], audit[0]["failed_steps"])
        self.assertEqual(3, audit[0]["attempt"])
        self.assertIn("brain_decay", audit[0]["reason"])
        self.assertEqual(5, len(self._lines()), "四次尝试四行 + 一条 never_ran")

    def test_after_never_ran_the_next_tick_runs_nothing(self):
        for _ in range(4):
            self._tick()
        before = plan.listing(self.root)
        lines = len(self._lines())
        again = self._tick()
        self.assertEqual([], again["night"]["retried"])
        self.assertEqual([], again["night"]["never_ran"])
        self.assertEqual([], again["due"])
        after = plan.listing(self.root)
        self.assertEqual(before["plan_ids"], after["plan_ids"])
        self.assertEqual(before["counts"], after["counts"])
        self.assertEqual(1, len(after["never_ran"]), "同一天不许再排第二条夜")
        self.assertEqual(lines, len(self._lines()), "不再跑夜就不再写日志")

    def test_retry_budget_comes_from_limits_not_from_code(self):
        config = {"tick_seconds": 60, "min_gap_minutes": 10, "daily_cap": 12,
                  "day_cutover_hour": 4, "kinds": {"night": {"at": "03:30"}},
                  "night": {"max_retries": 1}}
        first = self._tick(config=config)
        self.assertEqual(1, len(first["night"]["retried"]))
        second = self._tick(config=config)
        self.assertEqual(1, len(second["night"]["never_ran"]), "max_retries=1：两次就试满")
        self.assertIn("2 次尝试", plan.listing(self.root)["never_ran"][0]["reason"])

    def test_stale_pending_with_attempts_expires_not_never_ran(self):
        self._tick()
        lines = len(self._lines())
        later = self._tick(now="2026-11-08T03:31:00")
        expired = later["night"]["expired"]
        self.assertTrue(expired, "换日点已过的那条仍标 expired")
        self.assertIn("过期不补", expired[0]["reason"])
        self.assertEqual([], [line for line in self._lines() if line.get("event")],
                         "过期那条不补跑、也不写 never-ran")
        self.assertEqual(lines + 1, len(self._lines()), "只多了今天那条夜的日志")
        listing = plan.listing(self.root)
        self.assertEqual([], listing["never_ran"])
        self.assertEqual(1, len([row for row in listing["expired"]
                                 if row["night"]["attempts"] == 0]))


class ThreadTest(unittest.TestCase):
    """线程只验启停与"不起第二个"；节律本身的判据一律靠直接调 tick()。"""

    def setUp(self) -> None:
        rhythm.stop(2.0)
        self.addCleanup(rhythm.stop, 2.0)

    def test_default_is_off(self):
        self.assertFalse(rhythm.status()["running"], "没人开就不许有循环")

    def test_start_is_singleton_and_stop_joins(self):
        first = rhythm.start(interval=3600)
        second = rhythm.start(interval=3600)
        self.assertTrue(first["started"])
        self.assertFalse(second["started"], "已经在跑就不许起第二个")
        self.assertTrue(rhythm.status()["running"])
        stopped = rhythm.stop(2.0)
        self.assertTrue(stopped["stopped"])
        self.assertFalse(rhythm.status()["running"])


if __name__ == "__main__":
    unittest.main()
