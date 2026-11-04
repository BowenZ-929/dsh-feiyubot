"""rhythm：tick 先排当天（幂等）再数到点、换日只数一次、下一次是纯算术、线程唯一且默认关。"""
from __future__ import annotations

import unittest

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
        self.assertEqual({"pending": 0, "consumed": 0, "blocked": 0, "expired": 0, "total": 0},
                         payload["counts"])
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

    def test_no_execution_without_brain_still_consumes_and_reports(self):
        """没有脑也照跑（第 1 步记 failed），并如实 consume：不许留死账。"""
        payload = rhythm.tick(at("2026-11-05T03:31:00"), root=self.root,
                              heart_root=self.heart)
        self.assertEqual(1, len(payload["night"]["executed"]))
        self.assertFalse(payload["night"]["executed"][0]["ok"])
        self.assertEqual(["brain_compact"], payload["night"]["executed"][0]["summary"]["failed"])
        self.assertEqual(1, len(plan.listing(self.root)["consumed"]))


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
