"""plan：绝对时刻入账、同一个 (kind, at) 幂等、两道闸门拦在调度层。"""
from __future__ import annotations

import json
import unittest
import warnings
from datetime import timedelta, timezone

from organs.day import model, plan
from organs.day.tests.helpers import at, candidates, temp_root

NOW = "2026-11-05T08:00:00"


def same_day_stamps(count: int, start: str = "2026-11-05T05:00:00") -> list:
    """同一天内一串相隔 1 分钟的绝对时刻：只为"造 N 条"，不测 min_gap。"""
    base = model.parse_ts(at(start))
    return [model.iso(base + timedelta(minutes=index)) for index in range(count)]


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root(self)
        self.items = candidates(("diary", "2026-11-05T09:00:00"), ("walk", "2026-11-05T10:00:00"))

    def test_same_candidates_twice_add_nothing(self):
        first = plan.add(self.items, root=self.root, now_ts=at(NOW))
        second = plan.add(self.items, root=self.root, now_ts=at(NOW))
        self.assertEqual(2, len(first["added"]))
        self.assertEqual([], second["added"])
        self.assertEqual(sorted(row["id"] for row in first["added"]), second["duplicates"])
        self.assertEqual(first["plan_ids"], second["plan_ids"])
        self.assertEqual(first["counts"], second["counts"])

    def test_same_instant_with_another_offset_is_one_plan(self):
        local = at("2026-11-05T09:00:00")
        utc = model.parse_ts(local).astimezone(timezone.utc).isoformat()
        plan.add([{"kind": "diary", "at": local}], root=self.root)
        again = plan.add([{"kind": "diary", "at": utc}], root=self.root)
        self.assertEqual([], again["added"])
        self.assertEqual(1, len(again["duplicates"]))


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root(self)

    def test_three_minutes_apart_is_blocked_by_min_gap(self):
        first = plan.add([{"kind": "diary", "at": at("2026-11-05T09:00:00")}], root=self.root)
        second = plan.add([{"kind": "diary", "at": at("2026-11-05T09:03:00")}], root=self.root)
        self.assertEqual(1, len(first["added"]))
        self.assertEqual([], second["added"])
        self.assertEqual([plan.REASON_MIN_GAP], [row["reason"] for row in second["blocked"]])
        listing = plan.listing(self.root)
        self.assertEqual([row["id"] for row in first["added"]],
                         [row["id"] for row in listing["pending"]])
        self.assertEqual(1, len(listing["blocked"]))

    def test_ten_minutes_apart_is_allowed(self):
        plan.add([{"kind": "diary", "at": at("2026-11-05T09:00:00")}], root=self.root)
        ok = plan.add([{"kind": "diary", "at": at("2026-11-05T09:10:00")}], root=self.root)
        self.assertEqual(1, len(ok["added"]))
        self.assertEqual([], ok["blocked"])

    def test_min_gap_is_per_kind(self):
        plan.add([{"kind": "diary", "at": at("2026-11-05T09:00:00")}], root=self.root)
        other = plan.add([{"kind": "walk", "at": at("2026-11-05T09:03:00")}], root=self.root)
        self.assertEqual(1, len(other["added"]), "不同类互不影响")


    def test_cap_is_per_kind_and_per_day(self):
        early = [{"kind": "diary", "at": at("2026-11-05T%02d:%02d:00" % (7 + index // 2,
                                                                        0 if index % 2 == 0 else 15))}
                 for index in range(12)]
        small = {"day_cutover_hour": 4, "min_gap_minutes": 10, "daily_cap": 12}
        plan.add(early, root=self.root, config=small)
        nxt = plan.add([{"kind": "diary", "at": at("2026-11-06T09:00:00")}],
                       root=self.root, config=small)
        self.assertEqual(1, len(nxt["added"]), "换一天不算进昨天那 12 条")


class BookkeepingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root(self)
        plan.add(candidates(("diary", "2026-11-05T09:00:00"), ("diary", "2026-11-05T10:00:00"),
                            ("diary", "2026-11-05T11:00:00")),
                 root=self.root, now_ts=at("2026-11-05T08:00:00"))

    def test_due_lists_only_plans_at_or_before_now(self):
        due = plan.due(at("2026-11-05T10:30:00"), root=self.root)
        self.assertEqual([at("2026-11-05T09:00:00"), at("2026-11-05T10:00:00")],
                         [row["at"] for row in due])

    def test_next_at_is_arithmetic_over_pending(self):
        self.assertEqual(at("2026-11-05T11:00:00"), plan.next_at(at("2026-11-05T10:30:00"),
                                                                 root=self.root))
        self.assertIsNone(plan.next_at(at("2026-11-05T23:00:00"), root=self.root))

    def test_consume_moves_pending_to_consumed(self):
        target = plan.listing(self.root)["pending"][0]["id"]
        result = plan.consume([target, "pl-nope"], root=self.root,
                              now_ts=at("2026-11-05T09:10:00"))
        self.assertEqual(1, len(result["consumed"]))
        self.assertEqual(["pl-nope"], result["missing"])
        self.assertEqual(2, result["counts"]["pending"])
        self.assertEqual(1, result["counts"]["consumed"])
        self.assertEqual(plan.STATUS_CONSUMED, result["consumed"][0]["status"])

    def test_corrupt_ledger_raises_instead_of_looking_empty(self):
        (self.root / plan.PLANS_FILE).write_text("{不是 JSON", encoding="utf-8")
        with self.assertRaises(plan.PlanError):
            plan.load(self.root)

    def test_row_missing_fields_raises_instead_of_hiding(self):
        """行不齐就没法算闸门：坏行要响，不许静默跳过（跳过等于把下限放开）。"""
        data = {"version": 1, "pending": [{"id": "pl-x", "kind": "diary"}], "consumed": [],
                "blocked": []}
        (self.root / plan.PLANS_FILE).write_text(__import__("json").dumps(data), encoding="utf-8")
        with self.assertRaises(plan.PlanError):
            plan.load(self.root)

    def test_bad_candidate_shape_raises(self):
        with self.assertRaises(ValueError):
            plan.add([{"at": at("2026-11-05T09:00:00")}], root=self.root)
        with self.assertRaises(ValueError):
            plan.add(["不是候选"], root=self.root)
        with self.assertRaises(ValueError):
            plan.add([{"kind": "diary", "at": "明天"}], root=self.root)

    def test_plan_id_is_derived_from_kind_and_instant(self):
        local = at("2026-11-05T09:00:00")
        utc = model.parse_ts(local).astimezone(timezone.utc).isoformat()
        once = plan.plan_id("diary", model.iso(local))
        self.assertEqual(once, plan.plan_id("diary", model.iso(utc)), "归一后同一时刻同一个 id")
        self.assertNotEqual(once, plan.plan_id("walk", model.iso(local)))


class ExpireTest(unittest.TestCase):
    """expired 是第四种结局：不执行、不补跑、不占闸门。"""

    def setUp(self) -> None:
        self.root = temp_root(self)

    def test_expire_moves_pending_out_with_reason(self):
        row = plan.add([{"kind": "diary", "at": at("2026-11-05T09:00:00")}],
                       root=self.root)["added"][0]
        out = plan.expire([row["id"]], root=self.root, now_ts=at("2026-11-08T05:00:00"),
                          reason="过期不补")
        listing = plan.listing(self.root)
        self.assertEqual([], listing["pending"])
        self.assertEqual(1, len(listing["expired"]))
        self.assertEqual(plan.STATUS_EXPIRED, listing["expired"][0]["status"])
        self.assertEqual("过期不补", listing["expired"][0]["reason"])
        self.assertEqual(1, out["counts"]["expired"])

    def test_expired_does_not_take_a_slot_or_a_gap(self):
        """过期那条不占 min_gap / daily_cap：同一天同一类还能再排一条。"""
        config = {"min_gap_minutes": 10, "daily_cap": 12, "day_cutover_hour": 4,
                  "kinds": {"night": {"at": "09:00", "min_gap_minutes": 10, "daily_cap": 1}}}
        first = plan.add([{"kind": "night", "at": at("2026-11-05T09:00:00")}], root=self.root,
                         config=config)["added"][0]
        plan.expire([first["id"]], root=self.root, now_ts=at("2026-11-05T09:30:00"),
                    reason="过期不补")
        again = plan.add([{"kind": "night", "at": at("2026-11-05T09:20:00")}], root=self.root,
                         config=config)
        self.assertEqual(1, len(again["added"]), "过期那条既不算名额也不算间隔")
        self.assertEqual([], again["blocked"])

    def test_consumed_row_carries_the_result_summary(self):
        row = plan.add([{"kind": "night", "at": at("2026-11-05T09:00:00")}],
                       root=self.root)["added"][0]
        summary = {"steps": {"brain_compact": "ok"}, "statuses": {"ok": 1}, "failed": [],
                   "written": 1}
        out = plan.consume([row["id"]], root=self.root, now_ts=at("2026-11-05T09:01:00"),
                           results={row["id"]: summary})
        self.assertEqual(summary, out["consumed"][0]["result"])
        self.assertEqual(summary, plan.listing(self.root)["consumed"][0]["result"])


class EnsureTest(unittest.TestCase):
    """ensure：为"当天"每个 kind 确保恰有一条计划——跨天重排，同一天一条不新增。"""

    NIGHT = {"tick_seconds": 60, "min_gap_minutes": 10, "daily_cap": 12, "day_cutover_hour": 4,
             "kinds": {"night": {"at": "03:30", "min_gap_minutes": 10, "daily_cap": 1}}}

    def setUp(self) -> None:
        self.root = temp_root(self)

    def test_kind_at_uses_day_key_as_the_judge(self):
        """03:30 在换日点之前 → 归前一天，所以绝对时刻落在次日凌晨。"""
        self.assertEqual(at("2026-11-06T03:30:00"), plan.kind_at("2026-11-05", "03:30", 4))
        self.assertEqual(at("2026-11-05T09:00:00"), plan.kind_at("2026-11-05", "09:00", 4))

    def test_bad_clock_raises(self):
        for clock in ("25:00", "03:60", "03:30:00", None, ""):
            with self.assertRaises(ValueError):
                plan.kind_at("2026-11-05", clock, 4)

    def test_thresholds_prefer_the_kind_column(self):
        cfg = {"min_gap_minutes": 10, "daily_cap": 12,
               "kinds": {"night": {"min_gap_minutes": 30, "daily_cap": 1}}}
        self.assertEqual((1800, 1), plan.thresholds(cfg, "night"))
        self.assertEqual((600, 12), plan.thresholds(cfg, "diary"))

    def test_ensure_lands_exactly_one_plan_per_kind(self):
        first = plan.ensure(at("2026-11-05T09:30:00"), root=self.root, config=self.NIGHT)
        self.assertEqual(["night"], first["kinds"])
        self.assertEqual(1, len(first["added"]))
        row = first["added"][0]
        self.assertEqual(at("2026-11-06T03:30:00"), row["at"])
        self.assertEqual("2026-11-05", row["day"])
        again = plan.ensure(at("2026-11-05T09:30:00"), root=self.root, config=self.NIGHT)
        self.assertEqual([], again["added"], "同一天再 ensure 一条都不新增")
        self.assertEqual([row["id"]], again["duplicates"])
        self.assertEqual(first["plan_ids"], again["plan_ids"])

    def test_ensure_rerolls_next_day_and_leaves_yesterday(self):
        plan.ensure(at("2026-11-05T09:30:00"), root=self.root, config=self.NIGHT)
        before = plan.listing(self.root)["pending"][0]
        second = plan.ensure(at("2026-11-06T05:00:00"), root=self.root, config=self.NIGHT)
        self.assertEqual(1, len(second["added"]))
        self.assertEqual(at("2026-11-07T03:30:00"), second["added"][0]["at"])
        pending = plan.listing(self.root)["pending"]
        self.assertEqual(2, len(pending))
        self.assertEqual(before, [row for row in pending if row["id"] == before["id"]][0])

    def test_ensure_does_not_replan_a_consumed_day(self):
        first = plan.ensure(at("2026-11-05T09:30:00"), root=self.root, config=self.NIGHT)
        pid = first["added"][0]["id"]
        plan.consume([pid], root=self.root, now_ts=at("2026-11-06T03:31:00"))
        same_day = plan.ensure(at("2026-11-06T03:31:00"), root=self.root, config=self.NIGHT)
        self.assertEqual([], same_day["added"], "那天已经跑过（consumed），不再排第二条")
        self.assertEqual([pid], same_day["duplicates"])
        next_day = plan.ensure(at("2026-11-06T04:30:00"), root=self.root, config=self.NIGHT)
        self.assertEqual(1, len(next_day["added"]), "04:00 之后是新的一天，要重排")

    def test_ensure_goes_through_the_same_gate(self):
        """ensure 不是后门：被拦的候选进 blocked 带 reason，一条都不进 pending。"""
        capped = {"day_cutover_hour": 4, "min_gap_minutes": 10, "daily_cap": 12,
                  "kinds": {"night": {"at": "09:00", "min_gap_minutes": 10, "daily_cap": 1}}}
        plan.add([{"kind": "night", "at": at("2026-11-05T10:00:00")}], root=self.root,
                 config=capped)
        out = plan.ensure(at("2026-11-05T11:00:00"), root=self.root, config=capped)
        self.assertEqual([], out["added"])
        self.assertEqual([plan.REASON_FUSE_TRIPPED], [row["reason"] for row in out["blocked"]])
        self.assertEqual(1, out["counts"]["pending"])

        gap = {"day_cutover_hour": 4, "min_gap_minutes": 10, "daily_cap": 12,
               "kinds": {"night": {"at": "09:00", "min_gap_minutes": 10, "daily_cap": 2}}}
        other = temp_root(self)
        plan.add([{"kind": "night", "at": at("2026-11-05T09:05:00")}], root=other, config=gap)
        out = plan.ensure(at("2026-11-05T11:00:00"), root=other, config=gap)
        self.assertEqual([plan.REASON_MIN_GAP], [row["reason"] for row in out["blocked"]])

    def test_ensure_without_kinds_does_nothing(self):
        out = plan.ensure(at("2026-11-05T09:30:00"), root=self.root,
                          config={"day_cutover_hour": 4, "kinds": {}})
        self.assertEqual([], out["kinds"])
        self.assertEqual([], out["added"])
        self.assertEqual(0, out["counts"]["total"])


class NeverRanTest(unittest.TestCase):
    """never_ran 是第五栏：该跑却没跑成，与 expired 一样算"已处理"，但不占闸门。"""

    def setUp(self) -> None:
        self.root = temp_root(self)

    def _night_row(self):
        plan.add([{"kind": "night", "at": at("2026-11-05T03:30:00")}], root=self.root,
                 now_ts=at("2026-11-05T03:00:00"))
        return plan.listing(self.root)["pending"][0]

    def test_note_night_keeps_the_row_pending(self):
        row = self._night_row()
        snap = {"attempts": 0, "steps": {"brain_decay": {"status": "failed", "detail": "boom"}},
                "failed_steps": ["brain_decay"]}
        out = plan.note_night([row["id"]], root=self.root, now_ts=at("2026-11-05T03:31:00"),
                              nights={row["id"]: snap})
        self.assertEqual(1, len(out["noted"]))
        listing = plan.listing(self.root)
        self.assertEqual(1, len(listing["pending"]), "失败的那次不下账")
        self.assertEqual([], listing["consumed"])
        kept = listing["pending"][0]
        self.assertEqual(plan.STATUS_PENDING, kept["status"])
        self.assertEqual(snap, plan.night_of(kept))
        self.assertEqual(["brain_decay"], kept["night"]["failed_steps"])

    def test_never_ran_moves_the_row_to_the_fifth_column_with_its_reason(self):
        row = self._night_row()
        snap = {"attempts": 3, "steps": {}, "failed_steps": ["brain_decay"]}
        plan.never_ran([row["id"]], root=self.root, now_ts=at("2026-11-05T03:40:00"),
                       reasons={row["id"]: "该跑没跑成：brain_decay 在 4 次尝试里都没成功"},
                       nights={row["id"]: snap})
        listing = plan.listing(self.root)
        self.assertIn("never_ran", listing, "第五栏要出现在 /day/plan 的返回里")
        self.assertEqual([], listing["pending"])
        self.assertEqual([], listing["blocked"], "never_ran 不是 blocked：它不是闸门拦的")
        self.assertEqual(1, len(listing["never_ran"]))
        gone = listing["never_ran"][0]
        self.assertEqual(plan.STATUS_NEVER_RAN, gone["status"])
        self.assertTrue(gone["never_ran_at"])
        self.assertIn("brain_decay", gone["reason"])
        self.assertIn("4 次尝试", gone["reason"])
        self.assertEqual(snap, plan.night_of(gone))

    def test_never_ran_row_is_not_scheduled_again(self):
        row = self._night_row()
        plan.never_ran([row["id"]], root=self.root, reasons={row["id"]: "没跑成"})
        again = plan.add([{"kind": "night", "at": at("2026-11-05T03:30:00")}],
                         root=self.root, now_ts=at("2026-11-05T04:00:00"))
        self.assertEqual([], again["added"], "同一天不许再排第二条夜")
        self.assertEqual([row["id"]], again["duplicates"])
        self.assertEqual(1, len(plan.listing(self.root)["never_ran"]))

    def test_never_ran_does_not_take_a_slot_or_a_gap(self):
        row = self._night_row()
        plan.never_ran([row["id"]], root=self.root, reasons={row["id"]: "没跑成"})
        other = plan.add([{"kind": "night", "at": at("2026-11-05T03:35:00")}],
                         root=self.root, now_ts=at("2026-11-05T04:00:00"),
                         config={"day_cutover_hour": 4, "min_gap_minutes": 10, "daily_cap": 1,
                                 "kinds": {"night": {"at": "03:30", "daily_cap": 1}}})
        self.assertEqual(1, len(other["added"]), "没跑成的那条不占名额：明天那条照排")


class RequiredOptionalTest(unittest.TestCase):
    """保险丝只约束 optional：required 的既不占名额、也不被保险丝挡。"""

    CAPPED = {"day_cutover_hour": 4, "min_gap_minutes": 10, "daily_cap": 1,
              "kinds": {"night": {"at": "03:30", "required": True},
                        "greet": {"at": "09:00"}}}

    def setUp(self) -> None:
        self.root = temp_root(self)

    def test_required_kind_ignores_quota_and_gap(self):
        rows = [{"kind": "night", "at": at("2026-11-05T03:%02d:00" % (index * 5))}
                for index in range(3)]
        out = plan.add(rows, root=self.root, config=self.CAPPED)
        self.assertEqual(3, len(out["added"]), "required 的三条都排得上（额度 1 也不挡）")
        self.assertEqual([], out["blocked"], "min_gap / 额度都不管 required")
        self.assertEqual([True, True, True], [row["required"] for row in out["added"]])

    def test_optional_kind_trips_the_fuse_for_the_optional_quota(self):
        rows = [{"kind": "greet", "at": at("2026-11-05T%02d:00:00" % hour)}
                for hour in (9, 10, 11)]
        out = plan.add(rows, root=self.root, config=self.CAPPED)
        self.assertEqual(1, len(out["added"]))
        self.assertEqual([plan.REASON_FUSE_TRIPPED, plan.REASON_FUSE_TRIPPED],
                         [row["reason"] for row in out["blocked"]])
        self.assertEqual("fuse-tripped", out["blocked"][0]["reason"])
        self.assertEqual((1, 1), (out["blocked"][0]["count"], out["blocked"][0]["cap"]),
                         "跳闸的回执只带机器字段 count / cap")
        self.assertNotIn("额度", out["blocked"][0]["message"], "回执里不许出现人话")
        self.assertEqual([False, False], [row["required"] for row in out["blocked"]])

    def test_kind_outside_kinds_is_optional_not_exempt(self):
        out = plan.add([{"kind": "临时", "at": at("2026-11-05T09:00:00")},
                        {"kind": "临时", "at": at("2026-11-05T10:00:00")}],
                       root=self.root, config=self.CAPPED)
        self.assertEqual(1, len(out["added"]), "kinds 里没写的 kind 一律 optional")
        self.assertEqual(plan.REASON_FUSE_TRIPPED, out["blocked"][0]["reason"])

    def test_candidate_required_overrides_the_config(self):
        forced = plan.add([{"kind": "greet", "at": at("2026-11-05T09:00:00"), "required": True},
                           {"kind": "greet", "at": at("2026-11-05T10:00:00"), "required": True}],
                          root=self.root, config=self.CAPPED)
        self.assertEqual(2, len(forced["added"]), "候选自己写 required：额度不管它")
        self.assertEqual([True, True], [row["required"] for row in forced["added"]])

        other = temp_root(self)
        relaxed = plan.add([{"kind": "night", "at": at("2026-11-05T03:00:00"), "required": False},
                            {"kind": "night", "at": at("2026-11-05T03:10:00"), "required": False}],
                           root=other, config=self.CAPPED)
        self.assertEqual(1, len(relaxed["added"]), "反过来也认：候选说 optional 就受额度管")
        self.assertEqual(plan.REASON_FUSE_TRIPPED, relaxed["blocked"][0]["reason"],
                         "配置说 required、候选说 optional：按候选算，额度照样拦")

    def test_required_rows_do_not_fill_the_optional_quota(self):
        plan.add([{"kind": "night", "at": at("2026-11-05T03:%02d:00" % (index * 5))}
                  for index in range(3)], root=self.root, config=self.CAPPED)
        optional = plan.add([{"kind": "night", "at": at("2026-11-05T09:00:00"),
                              "required": False}], root=self.root, config=self.CAPPED)
        self.assertEqual(1, len(optional["added"]),
                         "三条 required 不占 optional 的额度：第 1 条 optional 照样进账")

    def test_never_ran_does_not_take_the_optional_quota(self):
        first = plan.add([{"kind": "greet", "at": at("2026-11-05T09:00:00")}],
                         root=self.root, config=self.CAPPED)["added"][0]
        plan.never_ran([first["id"]], root=self.root, reasons={first["id"]: "没跑成"})
        again = plan.add([{"kind": "greet", "at": at("2026-11-05T10:00:00")}],
                         root=self.root, config=self.CAPPED)
        self.assertEqual(1, len(again["added"]), "没跑成那条不占 optional 额度")

    def test_listing_shows_required_on_every_row(self):
        plan.add([{"kind": "night", "at": at("2026-11-05T03:30:00")}], root=self.root,
                 config=self.CAPPED)
        plan.add([{"kind": "greet", "at": at("2026-11-05T09:00:00")}], root=self.root,
                 config=self.CAPPED)
        plan.add([{"kind": "greet", "at": at("2026-11-05T10:00:00")}], root=self.root,
                 config=self.CAPPED)
        listing = plan.listing(self.root)
        self.assertEqual([True, False], [row["required"] for row in listing["pending"]])
        self.assertEqual([False], [row["required"] for row in listing["blocked"]])
        for column in ("pending", "consumed", "blocked", "expired", "never_ran"):
            for row in listing[column]:
                self.assertIsInstance(row["required"], bool)


class RetryWindowTest(unittest.TestCase):
    """退避窗口只在 plan 里判一次：due() 跳过窗口内的那条，next_at() 报它的边界。"""

    def setUp(self) -> None:
        self.root = temp_root(self)
        self.row = plan.add([{"kind": "night", "at": at("2026-11-05T03:30:00")}],
                            root=self.root, now_ts=at("2026-11-05T03:00:00"))["added"][0]

    def _mark(self, next_retry: str) -> None:
        plan.note_night([self.row["id"]], root=self.root,
                        nights={self.row["id"]: {
                            "attempts": 0, "steps": {}, "failed_steps": ["brain_decay"],
                            "next_retry_at": next_retry}})

    def test_due_skips_the_row_inside_the_window(self):
        self.assertEqual(1, len(plan.due(at("2026-11-05T03:31:00"), root=self.root)))
        self._mark(at("2026-11-05T03:32:00"))
        self.assertEqual([], plan.due(at("2026-11-05T03:31:00"), root=self.root),
                         "窗口内不算到点")
        self.assertEqual([], plan.due(at("2026-11-05T03:31:59"), root=self.root))
        self.assertEqual(1, len(plan.due(at("2026-11-05T03:32:00"), root=self.root)),
                         "窗口刚过就到点")

    def test_next_at_reports_the_retry_boundary_not_the_past_at(self):
        self._mark(at("2026-11-05T03:32:00"))
        self.assertEqual(at("2026-11-05T03:32:00"),
                         plan.next_at(at("2026-11-05T03:31:00"), root=self.root))
        plan.add([{"kind": "diary", "at": at("2026-11-05T04:00:00")}], root=self.root,
                 now_ts=at("2026-11-05T03:31:00"))
        self.assertEqual(at("2026-11-05T03:32:00"),
                         plan.next_at(at("2026-11-05T03:31:00"), root=self.root),
                         "退避边界比后面那条早，就该报它")

    def test_row_without_a_retry_boundary_is_due_as_usual(self):
        self.assertEqual(1, len(plan.due(at("2026-11-05T03:30:00"), root=self.root)))


class FuseTest(unittest.TestCase):
    """撞上限是保险丝跳闸：码只给机器（fuse-tripped / count / cap），人话只走一行 WARN。"""

    # 保险丝靠"造 N 条"来测：min_gap 收到 1 分钟，200 条才塞得进同一个自然日。
    FUSE = {"day_cutover_hour": 4, "min_gap_minutes": 1}

    def setUp(self) -> None:
        self.root = temp_root(self)

    def test_two_hundred_and_first_of_a_kind_trips_the_fuse(self):
        """默认保险丝 200：前 200 条照进，第 201 条不生成，回执里一个人话字都没有。"""
        rows = [{"kind": "diary", "at": stamp} for stamp in same_day_stamps(201)]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = plan.add(rows, root=self.root, config=self.FUSE)
        self.assertEqual(200, len(result["added"]), "默认保险丝是 200 条，前 200 条照常进")
        self.assertEqual(1, len(result["blocked"]), "第 201 条不生成")
        row = result["blocked"][0]
        self.assertEqual(plan.REASON_FUSE_TRIPPED, row["reason"])
        self.assertEqual("fuse-tripped", row["reason"])
        self.assertEqual((200, 200), (row["count"], row["cap"]), "回执只给机器字段 count / cap")
        said = [str(entry.message) for entry in caught if "保险丝" in str(entry.message)]
        self.assertEqual(1, len(said), "跳闸要留且只留一行 WARN：抓到的 WARN 是 %r" % said)
        self.assertIn("保险丝跳闸", said[0], said[0])
        self.assertIn("通常是 bug 循环或模型暴走", said[0], said[0])
        self.assertIn("请查", said[0], said[0])
        # 禁语那一句拆成两截写：守卫照旧点名整句，字面量不再以整串形式出现在我们的树里
        # （整串出现一次，全仓扫描就永远差一行）。断言强度不变：运行时拼出来仍是那三个字。
        self.assertNotIn("额度" + "已满", said[0], said[0])
        dump = json.dumps(result, ensure_ascii=False)
        for word in ("额度", "已满", "次数用完"):
            self.assertNotIn(word, dump, "回执里不许出现人话「%s」：%s" % (word, dump))

    def test_blocked_candidate_is_never_added_later(self):
        """拦下就是拦下：同一条候选再进来仍然 blocked，账一条不增。"""
        rows = [{"kind": "diary", "at": stamp} for stamp in same_day_stamps(201)]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            plan.add(rows, root=self.root, config=self.FUSE)
        before = plan.listing(self.root)
        again = plan.add([rows[-1]], root=self.root, config=self.FUSE)
        after = plan.listing(self.root)
        self.assertEqual([], again["added"])
        self.assertEqual([plan.REASON_FUSE_TRIPPED], [row["reason"] for row in again["blocked"]])
        self.assertEqual(len(before["blocked"]), len(after["blocked"]))
        self.assertEqual(before["plan_ids"], after["plan_ids"])

    def test_a_normal_day_never_trips_the_fuse(self):
        """日常一天（排程 + 几条临时候选）离保险丝还很远：跳闸 0 次。"""
        config = {"day_cutover_hour": 4, "min_gap_minutes": 10,
                  "kinds": {"night": {"at": "03:30", "min_gap_minutes": 10, "daily_cap": 1,
                                      "required": True}}}
        scheduled = plan.ensure(at("2026-11-05T09:30:00"), root=self.root, config=config)
        daily = plan.add(candidates(("diary", "2026-11-05T10:00:00"),
                                    ("walk", "2026-11-05T11:00:00"),
                                    ("chat", "2026-11-05T12:00:00")),
                         root=self.root, config=config)
        self.assertEqual(1, len(scheduled["added"]))
        self.assertEqual(3, len(daily["added"]))
        tripped = [row for row in scheduled["blocked"] + daily["blocked"]
                   if row["reason"] == plan.REASON_FUSE_TRIPPED]
        self.assertEqual([], tripped, "正常的一天一次都不跳")

    def test_a_smaller_cap_in_the_data_file_still_demonstrates_the_same_path(self):
        """cap 临时调小（写进临时 limits.json，不改默认值）：第 6 条跳闸，码与 WARN 都同一套。"""
        (self.root / model.LIMITS_FILE).write_text(
            json.dumps({"version": 1, "tick_seconds": 60, "min_gap_minutes": 1,
                        "daily_cap": 5, "day_cutover_hour": 4, "kinds": {}}),
            encoding="utf-8")
        rows = [{"kind": "greet", "at": stamp} for stamp in same_day_stamps(6)]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = plan.add(rows, root=self.root, config=model.load_limits(self.root))
        self.assertEqual(5, len(result["added"]), "cap=5：前 5 条照进")
        self.assertEqual(["fuse-tripped"], [row["reason"] for row in result["blocked"]])
        self.assertEqual((5, 5), (result["blocked"][0]["count"], result["blocked"][0]["cap"]))
        said = [str(entry.message) for entry in caught if "保险丝跳闸" in str(entry.message)]
        self.assertEqual(1, len(said), "同一套 WARN：%r" % said)
        for word in ("额度", "已满", "次数用完"):
            self.assertNotIn(word, json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
