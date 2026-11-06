"""主动开口（日子线）：动念的判定、理由码、一行账、计划、到点再判一次。

造数据，不等真实夜里：夜里那一条把 now 推到 23:30（换日点之前），双低那一条只测
理由码的短名（情绪与精力落到双低要造心的读数，那属于心的测试）。
"""
from __future__ import annotations

import json
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from organs.brain import BrainStore
from organs.day import delivery, model, plan, proactive, proactive_ask
from organs.heart import model as heart_model
from organs.heart import events as heart_events
from organs.heart import proactive as heart_proactive

from .helpers import at, temp_root

PERSON = "qq:3425635473"
OTHER = "qq:9876543210"
NOW = "2026-11-05T10:30:00"
NIGHT = "2026-11-05T23:30:00"


def heart_root(root: Path) -> Path:
    target = Path(root) / "heart"
    target.mkdir(parents=True, exist_ok=True)
    return target


def open_thought(heart: Path, now_iso: str = NOW, kind: str = "wanted_praise") -> None:
    """往心的念头文件里放一条 open 念头（真话由 pending.open_thoughts 读出来）。"""
    stamp = model.parse_ts(at(now_iso))
    row = {"id": "p-test", "kind": kind, "axis": None,
           "created_at": at(now_iso),
           "expires_at": model.iso(stamp + timedelta(minutes=180)),
           "attempts": 0, "state": "open", "settled_at": None, "faded_at": None,
           "source": "ignored-window"}
    (Path(heart) / "pending.json").write_text(
        json.dumps({"version": 1, "config": {}, "thoughts": [row]}, ensure_ascii=False),
        encoding="utf-8")


def schedule_a_day(root: Path, day: str = "2026-11-05") -> None:
    """日程日志里落一条当天的：这样"今天还没定下要做什么"这条由头就不再成立。"""
    line = {"at": at("2026-11-05T09:00:00"), "day": day, "slot": "morning"}
    path = Path(root) / "schedule_log.jsonl"
    path.write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")


def caps(per_who: int = 3) -> dict:
    """闸门配置（与 limits.json 同一形状）：每人每天几条，全从配置来。"""
    return {"day_cutover_hour": 4, "min_gap_minutes": 10, "daily_cap": 20,
            "kinds": {"proactive": {"required": False, "min_gap_minutes": 10}},
            "proactive": {"per_who_daily_cap": per_who}}


class ProactiveCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root(self)
        self.heart = heart_root(self.root)
        self.brain = BrainStore(Path(self.root) / "brain")

    def consider(self, now: str = NOW, **kw):
        return proactive.consider(at(now), root=self.root, heart_root=self.heart,
                                  brain=self.brain, **kw)

    def heard(self, text: str = "晚上有空吗", now: str = NOW, who: str = PERSON) -> None:
        out = self.brain.ingest(who=who, text=text, ts=at(now))
        self.assertTrue(out["ok"], out)


class SourcesTest(ProactiveCase):
    """三个驱动源各一个理由码；三个都成立时按 REASON_ORDER 取第一个。"""

    def test_no_source_means_no_thought_and_no_ledger_line(self):
        schedule_a_day(self.root)
        out = self.consider()
        self.assertFalse(out["fired"])
        self.assertEqual("no-source", out["skipped"])
        self.assertEqual([], proactive.log_lines(self.root), "没有由头就不落账")

    def test_thought_driver_reads_the_heart(self):
        open_thought(self.heart)
        fired = proactive.sources(at(NOW), root=self.root, heart_root=self.heart,
                                  brain=self.brain)
        self.assertEqual(proactive.REASON_THOUGHT, fired[0]["reason_code"],
                         "念头那条排在第一个")
        self.assertIn("wanted_praise", fired[0]["because"])
        self.assertEqual("p-test", fired[0]["thought_id"])

    def test_unfinished_driver_reads_the_last_hand_in_brain_today(self):
        self.heard("晚上有空吗")
        fired = proactive.sources(at(NOW), root=self.root, heart_root=self.heart,
                                  brain=self.brain)
        codes = [row["reason_code"] for row in fired]
        self.assertIn(proactive.REASON_UNFINISHED, codes)
        row = [item for item in fired if item["reason_code"] == proactive.REASON_UNFINISHED][0]
        self.assertEqual("晚上有空吗", row["because"])

    def test_she_answered_so_nothing_is_unfinished(self):
        self.heard("晚上有空吗", now="2026-11-05T10:00:00")
        self.brain.ingest(who="她说", text="有", ts=at(NOW), side="她说")
        fired = proactive.sources(at(NOW), root=self.root, heart_root=self.heart,
                                  brain=self.brain)
        self.assertNotIn(proactive.REASON_UNFINISHED, [row["reason_code"] for row in fired])

    def test_unscheduled_driver_disappears_once_the_day_is_set(self):
        fired = proactive.sources(at(NOW), root=self.root, heart_root=self.heart,
                                  brain=self.brain)
        self.assertIn(proactive.REASON_UNSCHEDULED, [row["reason_code"] for row in fired])
        schedule_a_day(self.root)
        again = proactive.sources(at(NOW), root=self.root, heart_root=self.heart,
                                  brain=self.brain)
        self.assertNotIn(proactive.REASON_UNSCHEDULED, [row["reason_code"] for row in again])

    def test_unscheduled_only_exists_inside_its_window(self):
        """常驻条件不算由头：窗外的"今天还没定下"不 fire，窗里才 fire。"""
        self.assertIn(proactive.REASON_UNSCHEDULED,
                      [row["reason_code"] for row in
                       proactive.sources(at("2026-11-05T10:30:00"), root=self.root,
                                         heart_root=self.heart, brain=self.brain)])
        for stamp in ("2026-11-05T08:30:00", "2026-11-05T15:00:00", "2026-11-05T20:00:00"):
            self.assertNotIn(proactive.REASON_UNSCHEDULED,
                             [row["reason_code"] for row in
                              proactive.sources(at(stamp), root=self.root,
                                                heart_root=self.heart, brain=self.brain)],
                             "%s 在窗外，不该有这条由头" % stamp)

    def test_the_window_comes_from_the_config(self):
        config = dict(proactive.load_config(self.root))
        config["unscheduled_window"] = {"from": 14, "to": 18}
        self.assertFalse(proactive.in_unscheduled_window(at("2026-11-05T10:30:00"), config))
        self.assertTrue(proactive.in_unscheduled_window(at("2026-11-05T15:00:00"), config))
        fired = proactive.sources(at("2026-11-05T15:00:00"), root=self.root,
                                  heart_root=self.heart, brain=self.brain, config=config)
        self.assertEqual([proactive.REASON_UNSCHEDULED],
                         [row["reason_code"] for row in fired])

    def test_reason_order_puts_the_thought_first(self):
        open_thought(self.heart)
        self.heard("晚上有空吗")
        fired = proactive.sources(at(NOW), root=self.root, heart_root=self.heart,
                                  brain=self.brain)
        self.assertEqual(list(proactive.REASON_ORDER[:3]),
                         [row["reason_code"] for row in fired])


class ConsiderTest(ProactiveCase):
    """动念 → 一行账 + 一条计划（priority 与 who 都在），次序：静音 > 额度 > engage。"""

    def test_a_reason_becomes_one_plan_with_priority_and_who(self):
        self.heard("晚上有空吗")
        out = self.consider()
        self.assertTrue(out["fired"])
        self.assertEqual("planned", out["action"])
        self.assertEqual(PERSON, out["who"], "24h 内说过话的人里熟稔最高的那个")
        self.assertEqual("recent-talk", out["who_source"])
        row = proactive.log_tail(self.root, 1)[0]
        for key in ("at", "reason_code", "who", "because", "priority", "engage", "action"):
            self.assertIn(key, row, "账行缺 %s" % key)
        self.assertEqual(proactive.REASON_UNFINISHED, row["reason_code"])
        self.assertIn("晚上有空吗", row["because"])
        self.assertEqual("planned", row["action"])
        listing = plan.listing(self.root)
        self.assertEqual(1, len(listing["pending"]))
        planned = listing["pending"][0]
        self.assertEqual("proactive", planned["kind"])
        self.assertEqual(PERSON, planned["who"])
        self.assertEqual(row["priority"], planned["priority"])
        self.assertFalse(planned["held"])
        self.assertEqual(proactive.REASON_UNFINISHED, planned["reason_code"])

    def test_night_keeps_the_plan_but_marks_it_held(self):
        self.heard("晚上有空吗", now=NIGHT)
        with mock.patch.object(proactive_ask, "ask",
                               side_effect=AssertionError("夜里不许调核")) as asked:
            out = self.consider(NIGHT)
        self.assertEqual(0, asked.call_count, "夜里静音：一个请求都不发")
        self.assertEqual("held", out["action"])
        self.assertEqual(heart_model.STATE_SILENT, out["engage"]["state"])
        self.assertEqual(heart_model.REASON_NIGHT, out["engage"]["reason"])
        row = proactive.log_tail(self.root, 1)[0]
        self.assertEqual("held", row["action"])
        self.assertEqual({"state": heart_model.STATE_SILENT,
                          "reason": heart_model.REASON_NIGHT}, row["engage"])
        listing = plan.listing(self.root)
        self.assertEqual(1, len(listing["pending"]), "判 SILENT 仍然生成计划")
        self.assertTrue(listing["pending"][0]["held"])
        self.assertEqual(heart_model.REASON_NIGHT, listing["pending"][0]["held_reason"])
        self.assertEqual([], delivery.load(self.root)["items"], "没发送")

    def test_muted_beats_wanting_to_talk(self):
        for _ in range(3):
            heart_proactive.sent(self.heart, at(NOW))
            heart_proactive.outcome("ignored", self.heart, at(NOW))
        self.assertTrue(heart_proactive.is_muted(self.heart, at(NOW))["muted"])
        self.heard("晚上有空吗")
        out = self.consider()
        self.assertEqual("muted", out["action"])
        self.assertEqual([], plan.listing(self.root)["pending"], "静音不排计划")
        row = proactive.log_tail(self.root, 1)[0]
        self.assertEqual("muted", row["action"])
        self.assertEqual(heart_model.REASON_PROACTIVE_MUTED, row["engage"]["reason"])
        self.assertTrue(row["muted_until"])

    def test_per_who_cap_comes_from_limits_and_stops_the_plan(self):
        self.heard("晚上有空吗")
        out = self.consider(config=None, limits=caps(per_who=1))
        self.assertEqual("planned", out["action"])
        again = self.consider("2026-11-05T11:00:00", limits=caps(per_who=1))
        self.assertEqual("quota", again["action"])
        self.assertEqual(1, again["quota"]["used"])
        self.assertEqual(1, len(plan.listing(self.root)["pending"]), "超限不排计划")
        row = proactive.log_tail(self.root, 1)[0]
        self.assertEqual("quota", row["action"])
        self.assertEqual({"scope": "who", "used": 1, "cap": 1}, row["quota"],
                         "账上要能看出是被哪条额度挡的")

    def test_the_cap_is_read_from_the_config_not_from_the_code(self):
        self.assertEqual(1, proactive.per_who_cap(limits=caps(per_who=1)))
        self.assertEqual(5, proactive.per_who_cap(limits=caps(per_who=5)))
        # 写在 kinds.proactive 那一栏里也认（两份写法同一把尺）
        self.assertEqual(2, proactive.per_who_cap(
            limits={"kinds": {"proactive": {"per_who_daily_cap": 2}}}))

    def test_idle_only_does_not_stack_plans(self):
        self.heard("晚上有空吗")
        self.assertEqual("planned", self.consider()["action"])
        again = self.consider("2026-11-05T11:00:00", idle_only=True)
        self.assertFalse(again["fired"])
        self.assertEqual("pending-plan", again["skipped"])
        self.assertEqual(1, len(plan.listing(self.root)["pending"]))
        self.assertEqual(1, len(proactive.log_lines(self.root)), "没动念就不落账")

    def test_reason_cap_stops_the_same_reason_twice_a_day(self):
        """同一个由头一天只用几次：unscheduled 默认 1，排过一条之后就被额度挡下。"""
        # 这条测试只用 unscheduled（日程没抽过 + 在 9-12 点窗里）
        first = self.consider()
        self.assertEqual(proactive.REASON_UNSCHEDULED, first["reason_code"])
        self.assertEqual("planned", first["action"])
        self.assertEqual(40, first["priority"])
        # 那条计划被用掉（consume）之后再来一次：idle_only 不再是理由，由头额度才是
        plan.consume([first["plan"]["id"]], root=self.root, now_ts=at("2026-11-05T10:40:00"))
        second = self.consider("2026-11-05T11:00:00")
        self.assertEqual("quota", second["action"])
        self.assertEqual("reason", second["quota"]["scope"])
        self.assertEqual(1, second["quota"]["reason"]["used"])
        self.assertEqual(1, second["quota"]["reason"]["cap"])
        self.assertIsNone(second["plan"], "超了额度不排计划")
        self.assertEqual(1, len(plan.listing(self.root)["consumed"]), "没有第二条计划")
        row = proactive.log_tail(self.root, 1)[0]
        self.assertEqual("quota", row["action"])
        self.assertEqual({"scope": "reason", "reason_code": proactive.REASON_UNSCHEDULED,
                          "used": 1, "cap": 1}, row["quota"])

    def test_the_reason_cap_comes_from_the_config(self):
        config = dict(proactive.load_config(self.root))
        config["per_reason_daily_cap"] = {proactive.REASON_UNSCHEDULED: 2}
        first = self.consider(config=config)
        self.assertEqual("planned", first["action"])
        plan.consume([first["plan"]["id"]], root=self.root, now_ts=at("2026-11-05T10:40:00"))
        second = self.consider("2026-11-05T11:00:00", config=config)
        self.assertEqual("planned", second["action"], "额度调到 2 就还能再排一条")
        plan.consume([second["plan"]["id"]], root=self.root, now_ts=at("2026-11-05T11:10:00"))
        third = self.consider("2026-11-05T11:20:00", config=config)
        self.assertEqual("quota", third["action"])
        self.assertEqual(2, third["quota"]["reason"]["used"])

    def test_a_zero_cap_means_this_reason_is_off(self):
        config = dict(proactive.load_config(self.root))
        config["per_reason_daily_cap"] = {proactive.REASON_UNSCHEDULED: 0}
        out = self.consider(config=config)
        self.assertEqual("quota", out["action"])
        self.assertIsNone(out["plan"])
        self.assertEqual(0, out["quota"]["reason"]["cap"])

    def test_double_low_has_a_short_code_on_the_ledger(self):
        self.assertEqual("double-low",
                         proactive.hold_code({"reason": heart_model.REASON_DOUBLE_LOW}))
        self.assertEqual(heart_model.REASON_NIGHT,
                         proactive.hold_code({"reason": heart_model.REASON_NIGHT}))


class DowngradeTest(ProactiveCase):
    """对方最近 24h 内找过她 → priority 减降权值，账上给出降权前后两个数。

    两组同一个由头（念头那条：60 分），只换"那个人最近找过她没有"：
    找过 → 减 downgrade；没找过（连名录都没有，退回 owner）→ 不减。
    """

    def _consider_with(self, heard_at):  # type: ignore[no-untyped-def]
        root = temp_root(self)
        heart = heart_root(root)
        brain = BrainStore(Path(root) / "brain")
        open_thought(heart)
        if heard_at:
            brain.ingest(who=PERSON, text="晚上有空吗", ts=at(heard_at))
        out = proactive.consider(at(NOW), root=root, heart_root=heart, brain=brain)
        return out, proactive.log_tail(root, 1)[0]

    def test_lately_talked_lowers_the_priority_and_both_numbers_are_on_the_ledger(self):
        recent, recent_row = self._consider_with("2026-11-05T09:30:00")
        quiet, quiet_row = self._consider_with(None)

        self.assertEqual(proactive.REASON_THOUGHT, recent["reason_code"])
        self.assertEqual(proactive.REASON_THOUGHT, quiet["reason_code"], "同一个由头")
        self.assertEqual(PERSON, recent["who"])
        self.assertEqual("主人", quiet["who"], "24h 内没人说过话 → 退回 owner")
        self.assertTrue(recent["lately_talked"])
        self.assertFalse(quiet["lately_talked"])

        self.assertEqual(quiet["priority"], recent["priority_before"],
                         "降权前只由理由码定：同一把尺")
        self.assertEqual(0, quiet["downgrade"])
        self.assertGreater(recent["downgrade"], 0)
        self.assertLess(recent["priority"], quiet["priority"])
        self.assertEqual(recent["priority"], recent["priority_before"] - recent["downgrade"])
        self.assertEqual(recent["priority_before"], recent_row["priority_before"])
        self.assertEqual(recent["priority_after"], recent_row["priority_after"])
        self.assertNotEqual(quiet_row["priority"], recent_row["priority"])


class DueTest(ProactiveCase):
    """到点再判一次：仍 SILENT 就只记账；否则交给核，回执四路各落各的账。"""

    def _planned(self, now: str = NOW) -> dict:
        self.heard("晚上有空吗", now=now)
        out = self.consider(now)
        self.assertTrue(out["fired"], out)
        return plan.load(self.root)["pending"][0]

    def test_still_silent_at_due_is_booked_and_never_retried(self):
        row = self._planned(NIGHT)
        self.assertTrue(row["held"])
        calls = []

        def complete(messages, **kwargs):
            calls.append(messages)
            return json.dumps({"send": True, "chunks": ["喂"]})

        out = proactive.run_due(row, at("2026-11-05T23:36:00"), root=self.root,
                                heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual("held", out["action"])
        self.assertEqual(0, len(calls), "仍然 SILENT：不许调核")
        self.assertEqual([], plan.listing(self.root)["pending"], "consume，不重试")
        self.assertEqual([], delivery.load(self.root)["items"])
        ledger = proactive.log_tail(self.root, 1)[0]
        self.assertEqual("held", ledger["action"])
        self.assertEqual("due", ledger["stage"])
        self.assertEqual(heart_model.REASON_NIGHT, ledger["reason"])

    def test_she_says_it_when_the_gate_opens(self):
        row = self._planned(NIGHT)
        calls = []

        def complete(messages, **kwargs):
            calls.append(messages)
            return json.dumps({"send": True, "chunks": ["你睡了吗", "刚才想起你说的那件事"]},
                              ensure_ascii=False)

        out = proactive.run_due(row, at("2026-11-06T09:00:00"), root=self.root,
                                heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual("sent", out["action"], out)
        self.assertEqual(1, len(calls))
        self.assertIn("晚上有空吗", calls[0][1]["content"], "由头要进提示词")
        items = delivery.load(self.root)["items"]
        self.assertEqual(1, len(items), "先落盘：待说队列里有这一条")
        self.assertEqual(PERSON, items[0]["to"])
        self.assertIn("你睡了吗", items[0]["text"])
        self.assertIn("刚才想起", items[0]["text"], "几段合并成一条")
        state = heart_proactive.overview(self.heart, at("2026-11-06T09:00:00"))
        self.assertTrue(state["pending"], "再记一条'她主动开口了'的 pending")
        self.assertEqual([], plan.listing(self.root)["pending"])
        self.assertEqual("sent", proactive.log_tail(self.root, 1)[0]["action"])

    def test_send_false_is_declined_and_nothing_is_queued(self):
        row = self._planned()
        out = proactive.run_due(row, at("2026-11-05T10:36:00"), root=self.root,
                                heart_root=self.heart, brain=self.brain,
                                complete=lambda messages, **kw: '{"send": false, "chunks": []}')
        self.assertEqual("declined", out["action"])
        self.assertEqual([], delivery.load(self.root)["items"])
        self.assertEqual([], plan.listing(self.root)["pending"], "consume，不重试")
        self.assertEqual("declined", proactive.log_tail(self.root, 1)[0]["action"])
        self.assertFalse(heart_proactive.overview(self.heart)["pending"], "她没说，就不记账")

    def test_empty_chunks_are_booked_as_empty_and_not_retried(self):
        row = self._planned()
        out = proactive.run_due(row, at("2026-11-05T10:36:00"), root=self.root,
                                heart_root=self.heart, brain=self.brain,
                                complete=lambda messages, **kw: '{"send": true, "chunks": ["  ", ""]}')
        self.assertEqual("empty", out["action"])
        self.assertEqual([], delivery.load(self.root)["items"])
        ledger = proactive.log_tail(self.root, 1)[0]
        self.assertEqual("empty", ledger["action"])
        remaining = plan.load(self.root)
        self.assertEqual([], remaining["pending"])
        self.assertEqual(1, len(remaining["consumed"]), "consume 掉，不重试")
        lines = len(proactive.log_lines(self.root))
        again = self._again(row, at("2026-11-05T10:40:00"))
        self.assertEqual("not-pending", again["skipped"], "已经 consumed 的不许再跑一遍")
        self.assertEqual(lines, len(proactive.log_lines(self.root)), "账上不多一行")

    def _again(self, row: dict, now: str) -> dict:
        """对同一条再跑一次：计划已经 consumed，什么都不该再发生。"""
        return proactive.run_due(row, now, root=self.root, heart_root=self.heart,
                                 brain=self.brain,
                                 complete=lambda messages, **kw: '{"send": true, "chunks": []}')

    def test_llm_failure_is_an_error_with_its_kind_and_no_retry(self):
        from organs.mind import LLMError
        row = self._planned()

        def boom(messages, **kwargs):
            raise LLMError("timeout", "模型没在时限内回话")

        out = proactive.run_due(row, at("2026-11-05T10:36:00"), root=self.root,
                               heart_root=self.heart, brain=self.brain, complete=boom)
        self.assertFalse(out["ok"])
        self.assertEqual("error", out["action"])
        self.assertEqual("timeout", out["error_kind"])
        ledger = proactive.log_tail(self.root, 1)[0]
        self.assertEqual("error", ledger["action"])
        self.assertEqual("timeout", ledger["error_kind"])
        self.assertEqual([], delivery.load(self.root)["items"])
        self.assertEqual([], plan.load(self.root)["pending"], "主动开口不做退避重试")

    def test_unparseable_reply_is_a_parse_error_not_a_message(self):
        row = self._planned()
        out = proactive.run_due(row, at("2026-11-05T10:36:00"), root=self.root,
                               heart_root=self.heart, brain=self.brain,
                               complete=lambda messages, **kw: "我今晚不想说话")
        self.assertEqual("error", out["action"])
        self.assertEqual("parse", out["error_kind"])
        self.assertEqual("parse", proactive.log_tail(self.root, 1)[0]["error_kind"])
        self.assertEqual([], delivery.load(self.root)["items"], "解析不出就不发")


class OutcomeTest(ProactiveCase):
    """连三次没人接：streak=3、静音、mood 账上多一行 cold，且只多一行。"""

    def _round(self, result: str, now: str) -> dict:
        heart_proactive.sent(self.heart, at(now))
        return proactive.settle(result, at(now), root=self.root, heart_root=self.heart,
                                brain=self.brain, who=PERSON)

    def test_third_ignored_mutes_her_and_lands_exactly_one_cold(self):
        out = None
        for stamp in ("2026-11-05T10:00:00", "2026-11-05T11:00:00", "2026-11-05T12:00:00"):
            out = self._round("ignored", stamp)
            self.assertTrue(out["ok"], out)
        self.assertEqual(3, out["readings"]["streak"])
        self.assertTrue(out["readings"]["muted"])
        self.assertTrue(out["readings"]["muted_until"])
        self.assertIsNotNone(out["cold"], "刚进静音那一次要落 cold")
        colds = heart_events.recent(self.heart, at("2026-11-05T12:00:00"),
                                    window_min=600, kinds=(heart_model.EVENT_COLD,))
        self.assertEqual(1, len(colds), "mood 账上刚好一行 cold")
        ledger = proactive.log_tail(self.root, 1)[0]
        self.assertEqual("cold", ledger["action"])
        self.assertEqual(out["readings"]["muted_until"], ledger["muted_until"])

    def test_a_fourth_ignored_does_not_land_another_cold(self):
        for stamp in ("2026-11-05T10:00:00", "2026-11-05T11:00:00", "2026-11-05T12:00:00",
                      "2026-11-05T13:00:00"):
            out = self._round("ignored", stamp)
        self.assertEqual(4, out["readings"]["streak"])
        self.assertIsNone(out["cold"], "同一段静音只落一次 cold")
        colds = heart_events.recent(self.heart, at("2026-11-05T13:00:00"),
                                    window_min=600, kinds=(heart_model.EVENT_COLD,))
        self.assertEqual(1, len(colds))
        self.assertEqual(1, len([row for row in proactive.log_lines(self.root)
                                 if row["action"] == "cold"]))

    def test_replied_clears_the_streak(self):
        self._round("ignored", "2026-11-05T10:00:00")
        out = self._round("replied", "2026-11-05T11:00:00")
        self.assertEqual(0, out["readings"]["streak"])
        self.assertFalse(out["readings"]["muted"])
        self.assertIsNone(out["cold"])

    def test_unknown_result_is_a_400_not_a_silent_edit(self):
        with self.assertRaises(proactive.ProactiveError):
            proactive.settle("whatever", at(NOW), root=self.root, heart_root=self.heart)


class TickIntegrationTest(ProactiveCase):
    """到点那一条由 tick 执行：夜里排的计划到白天 tick 时才真开口（同一个人过几小时就不一样了）。"""

    def test_tick_runs_the_due_plan_and_consumes_it(self):
        from organs.day import rhythm
        self.heard("晚上有空吗", now=NIGHT)
        out = self.consider(NIGHT)
        self.assertEqual("held", out["action"])
        row = plan.load(self.root)["pending"][0]
        asked, mined = [], []

        def complete(messages, **kwargs):
            mined.append(messages)
            return json.dumps({"send": True, "chunks": ["早上好"]}, ensure_ascii=False)

        original = proactive_ask.ask

        def fake_ask(ctx, **kwargs):
            asked.append(ctx)
            return original(ctx, complete=complete, config=kwargs.get("config"))

        with mock.patch.object(proactive_ask, "ask", side_effect=fake_ask):
            payload = rhythm.tick(at("2026-11-06T09:00:00"), root=self.root,
                                  brain=self.brain, heart_root=self.heart,
                                  config={"tick_seconds": 60, "min_gap_minutes": 10,
                                          "daily_cap": 20, "day_cutover_hour": 4,
                                          "kinds": {}})
        self.assertEqual(1, len(asked), "白天这一次放行：问了一次")
        self.assertEqual(1, len(mined), "核被调了一次")
        self.assertEqual({"sent": 1}, payload["proactive"]["actions"])
        self.assertEqual(1, len(delivery.load(self.root)["items"]))
        ledger = [item for item in proactive.log_lines(self.root)
                  if item["stage"] == "due"][-1]
        self.assertEqual("sent", ledger["action"])
        self.assertEqual(row["id"], ledger["plan_id"])
        # 到点那一条已经 consume；这一 tick 末尾的动念又排了一条新的（由头还在，
        # 见 TickAutoConsiderTest：那不是重试，是下一次"她想说"）
        consumed = [item for item in plan.load(self.root)["consumed"]
                    if item["kind"] == "proactive"]
        self.assertEqual([row["id"]], [item["id"] for item in consumed])


def settled(root, *days: str) -> None:
    """把这几天的日程先"定下来"：tick 里那次动念就没有"今天还没定"这个由头。"""
    path = Path(root) / "schedule_log.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for day in days:
            handle.write(json.dumps({"day": day}, ensure_ascii=False) + chr(10))


class TickAutoConsiderTest(ProactiveCase):
    """tick 自己会动念：空 root + 造一个由头 → 计划出现；再 tick 一次不叠第二条。"""

    def _tick(self, now: str = NOW):
        from organs.day import rhythm
        return rhythm.tick(at(now), root=self.root, brain=self.brain,
                           heart_root=self.heart)

    def test_tick_considers_and_does_not_stack(self):
        self.heard("晚上有空吗")          # 由头：脑 today 最后一手是别人说的
        first = self._tick()
        self.assertTrue(first["proactive_consider"]["fired"], first["proactive_consider"])
        self.assertEqual(proactive.REASON_UNFINISHED,
                         first["proactive_consider"]["reason_code"])
        self.assertEqual("planned", first["proactive_consider"]["action"])
        counted = first["counts"]
        rows = [row for row in plan.load(self.root)["pending"] if row["kind"] == "proactive"]
        self.assertEqual(1, len(rows), "tick 之后计划出现")
        self.assertEqual(1, len(proactive.log_lines(self.root)), "动念账多一行")
        self.assertEqual(at(NOW), proactive.log_tail(self.root, 1)[0]["at"])

        second = self._tick()
        self.assertFalse(second["proactive_consider"]["fired"])
        self.assertEqual("pending-plan", second["proactive_consider"]["skipped"])
        self.assertEqual(counted, second["counts"], "再 tick 一次不叠第二条")
        self.assertEqual(1, len(proactive.log_lines(self.root)), "没动念就不落账")

    def test_without_a_reason_tick_does_not_consider_at_all(self):
        settled(self.root, "2026-11-05")
        payload = self._tick()
        self.assertFalse(payload["proactive_consider"]["fired"])
        self.assertEqual("no-source", payload["proactive_consider"]["skipped"])
        self.assertEqual([], [row for row in plan.load(self.root)["pending"]
                              if row["kind"] == "proactive"])
        self.assertEqual([], proactive.log_lines(self.root), "没有由头就一行账都不落")

    def test_a_broken_consider_does_not_take_the_tick_down(self):
        from organs.day import rhythm
        self.heard("晚上有空吗")
        with mock.patch.object(proactive, "consider", side_effect=RuntimeError("账坏了")):
            with self.assertWarns(RuntimeWarning):
                payload = self._tick()
        self.assertTrue(payload["ok"], "动念坏了 tick 照常返回")
        self.assertFalse(payload["proactive_consider"]["ok"])
        self.assertIn("账坏了", payload["proactive_consider"]["error"])
        self.assertIn("账坏了", rhythm.load_state(self.root)["last_error"],
                      "错记进 state.last_error，不静默吞掉")


class PlanRowTest(ProactiveCase):
    """ensure 跳过没有 at 的 kind；priority / who / held 要落在计划行上，/day/plan 读得出来。"""

    def test_ensure_skips_the_kind_without_a_clock(self):
        config = {"day_cutover_hour": 4, "min_gap_minutes": 10, "daily_cap": 20,
                  "kinds": {"night": {"at": "03:30", "required": True},
                            "proactive": {"required": False}}}
        out = plan.ensure(at(NOW), root=self.root, config=config)
        self.assertEqual(["night", "proactive"], out["kinds"])
        self.assertEqual(["proactive"], out["skipped"], "没有 at 的 kind 不由钟点排")
        self.assertEqual(1, len(out["added"]))
        self.assertEqual("night", out["added"][0]["kind"])

    def test_extras_survive_into_the_listing(self):
        plan.add([{"kind": "proactive", "at": at(NOW), "note": "由头", "priority": 42,
                   "who": PERSON, "held": True, "held_reason": "night",
                   "reason_code": "thought", "thought_id": "p-test"}],
                 root=self.root, now_ts=at(NOW), config=caps())
        row = plan.listing(self.root)["pending"][0]
        self.assertEqual(42, row["priority"])
        self.assertEqual(PERSON, row["who"])
        self.assertTrue(row["held"])
        self.assertEqual("night", row["held_reason"])
        self.assertEqual("thought", row["reason_code"])
        self.assertEqual("由头", row["note"])


class OverviewTest(ProactiveCase):
    """GET /day/proactive 的读数：账尾 + 降权前后 + 额度 + 静音 / streak，全是只读。"""

    def test_overview_reads_without_writing(self):
        self.heard("晚上有空吗")
        self.consider()
        before = sorted(path.name for path in Path(self.root).iterdir())
        out = proactive.overview(at(NOW), root=self.root, heart_root=self.heart,
                                 brain=self.brain)
        self.assertEqual(before, sorted(path.name for path in Path(self.root).iterdir()),
                         "GET 不落盘")
        self.assertEqual(1, len(out["log"]))
        self.assertEqual(PERSON, out["who"])
        self.assertEqual({"before", "after"},
                         set(out["priority"][proactive.REASON_THOUGHT]))
        self.assertEqual(1, out["quota"]["used"])
        self.assertEqual(2, out["quota"]["remaining"])
        self.assertFalse(out["muted"]["muted"])
        self.assertEqual(0, out["streak"])
        self.assertEqual({"planned": 1}, out["counts"])


if __name__ == "__main__":
    unittest.main()

