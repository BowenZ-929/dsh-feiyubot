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
        # pending_task 只在真有快到点的任务卡时才成立（这里家是空的）；
        # own_doing 只在独处做完一件事、由头池里挂着一条时才成立（这里池子是空的）——
        # 所以成立的是中间那三条
        self.assertEqual(list(proactive.REASON_ORDER[1:-1]),
                         [row["reason_code"] for row in fired])
        self.assertEqual("pending_task", proactive.REASON_ORDER[0],
                         "快到点的事比她想说更硬：它排第一")


class ConsiderTest(ProactiveCase):
    """动念 → 一行账 + 一条计划（priority 与 who 都在），次序：静音 > 额度 > engage。"""

    def test_a_reason_becomes_one_plan_with_priority_and_who(self):
        self.heard("晚上有空吗")
        out = self.consider()
        self.assertTrue(out["fired"])
        self.assertEqual("planned", out["action"])
        self.assertEqual(PERSON, out["who"], "未完话题回应原作者")
        self.assertEqual("source-author", out["who_source"])
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

    def test_recent_contact_survives_day_cutover_and_keeps_priority_lower(self):
        self.heard("昨晚聊过", now="2026-11-05T23:00:00")
        now = at("2026-11-06T09:00:00")
        self.assertEqual([], self.brain.today_entries(now))
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        view = proactive.overview(now, root=self.root, heart_root=self.heart, brain=self.brain)
        self.assertEqual(PERSON, view['who'])
        self.assertTrue(view['lately_talked'])
        self.assertEqual(at("2026-11-05T23:00:00"), view['lately_talked_at'])
        self.assertEqual(20, view['downgrade'])
        self.assertEqual({'before': 60, 'after': 40}, view['priority']['thought'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        open_thought(self.heart, now_iso="2026-11-06T09:00:00")
        result = self.consider(now="2026-11-06T09:00:00")
        self.assertEqual(40, result['priority'])
        self.assertEqual(at("2026-11-05T23:00:00"), proactive.log_tail(self.root, 1)[0]['lately_talked'])

    def test_recent_contact_window_does_not_extend_from_own_reply_or_other_person(self):
        self.heard("之前聊过", now="2026-11-05T09:00:00")
        self.brain.ingest(who=PERSON, text="她自己说的话", side="她说", ts=at("2026-11-06T08:00:00"))
        self.heard("另一个人来了", who="10000421", now="2026-11-06T08:30:00")
        config = dict(proactive.load_config(self.root), lately_talked_hours=24)
        for stamp, expected in (("2026-11-06T09:00:00", True), ("2026-11-06T09:00:01", False)):
            with self.subTest(stamp=stamp):
                result = proactive._lately_talked(PERSON, at(stamp), brain=self.brain, config=config)
                self.assertEqual(expected, result['lately_talked'])
                self.assertEqual(20 if expected else 0, result['downgrade'])
        config['lately_talked_hours'] = 2
        self.assertFalse(proactive._lately_talked(PERSON, at("2026-11-06T09:00:00"),
                                               brain=self.brain, config=config)['lately_talked'])

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
        self.assertEqual("queued", out["action"], out)
        self.assertEqual(1, len(calls))
        self.assertIn("晚上有空吗", calls[0][1]["content"], "由头要进提示词")
        items = delivery.load(self.root)["items"]
        self.assertEqual(1, len(items), "先落盘：待说队列里有这一条")
        self.assertEqual(PERSON, items[0]["to"])
        self.assertIn("你睡了吗", items[0]["text"])
        self.assertIn("刚才想起", items[0]["text"], "几段合并成一条")
        state = heart_proactive.overview(self.heart, at("2026-11-06T09:00:00"))
        self.assertFalse(state["pending"], "入队未送达，不开始等待回复")
        self.assertEqual([], plan.listing(self.root)["pending"])
        self.assertEqual("queued", proactive.log_tail(self.root, 1)[0]["action"])

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
        self.assertEqual({"queued": 1}, payload["proactive"]["actions"])
        self.assertEqual(1, len(delivery.load(self.root)["items"]))
        ledger = [item for item in proactive.log_lines(self.root)
                  if item["stage"] == "due"][-1]
        self.assertEqual("queued", ledger["action"])
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


# ---- 第四类由头：pending_task（家的工作台上那张快到点的任务卡） -----------------


def home_at(root: Path) -> Path:
    """临时家：十间建好 + access.json（工作台可写），且就在 <root>/home 那一棵树下。

    放在这个位置是为了让 rhythm.tick 自己按 <root>/home 派生——测试不替它传 home_root，
    派生这一条也就被走到了。
    """
    from organs.home import rooms
    home = Path(root) / "home"
    for name in rooms.ROOM_NAMES:
        (home / name).mkdir(parents=True, exist_ok=True)
    (home / "access.json").write_text(
        json.dumps({"version": 1, "writable": ["玄关", "工作台", "仓库"]}, ensure_ascii=False),
        encoding="utf-8")
    return home


class PendingTaskCase(unittest.TestCase):
    """造一张任务卡，走 tick 那条 60 秒的路：由头 → 额度 → engage → 计划。"""

    def setUp(self) -> None:
        self.root = temp_root(self)
        self.heart = heart_root(self.root)
        self.brain = BrainStore(Path(self.root) / "brain")
        self.home = home_at(self.root)
        schedule_a_day(self.root)      # 把"今天还没定下做什么"关掉：只测任务卡这一条

    def card(self, name: str = "整理抽屉", due: str = "2026-11-05T10:40:00",
             task: Any = True, room: str = "工作台",
             due_raw: Any = None, **params: Any) -> str:
        """放一张任务卡进工作台：kind=物件、params.task / params.due（真源就是这两个字段）。

        due_raw 是"卡上写着一个不是时刻的值"那种造数据（越界保护要测的就是它）。
        """
        from organs.home import cards
        body = {"task": task, "due": at(due) if due_raw is None else due_raw}
        body.update(params)
        out = cards.append_cards(root=self.home, role="agent", by="agent", cards=[
            {"kind": "物件", "room": room, "name": name, "note": "", "params": body}])
        return out["cards"][0]["card_id"]

    def tick(self, now: str = NOW, **kw: Any):
        from organs.day import rhythm
        return rhythm.tick(at(now), root=self.root, brain=self.brain,
                           heart_root=self.heart, **kw)

    def considered(self, now: str = NOW, **kw: Any):
        return proactive.consider(at(now), root=self.root, heart_root=self.heart,
                                  brain=self.brain, home_root=self.home, **kw)

    def proactive_plans(self) -> list:
        return [row for row in plan.load(self.root)["pending"] if row["kind"] == "proactive"]

    def card_params(self) -> dict:
        """卡上的 name / params 原文（给"because 逐字来自卡"那一条做对照）。"""
        from organs.home import cards
        item = cards.list_room(root=self.home, room="工作台")["items"][0]
        return {"name": item["name"], "params": item["params"]}


class PendingTaskConsiderTest(PendingTaskCase):
    """due 在 10 分钟后 → 生成 pending_task 计划；夜里 → held；同一张卡不提醒两次。"""

    def test_task_after_first_room_page_is_seen_and_planned(self):
        from organs.home import cards
        (self.home / 'limits.json').write_text(json.dumps({'writes_per_room_per_day': 500}))
        for batch in range(10):
            cards.append_cards(root=self.home, role='agent', by='agent', cards=[
                {'kind': '物件', 'room': '工作台', 'name': f'旧作品{batch * 20 + i}',
                 'note': '', 'params': {}} for i in range(20)])
        ident = self.card()
        first = cards.list_room(root=self.home, room='工作台')
        self.assertEqual(201, first['total'])
        self.assertTrue(first['truncated'])
        before = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        view = proactive.overview(at(NOW), root=self.root, heart_root=self.heart, brain=self.brain)
        self.assertEqual(ident, view['pending_task']['fired'])
        considered = self.considered()
        self.assertEqual('pending_task', considered['reason_code'])
        self.assertEqual(ident, considered['plan']['task']['card_id'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_a_card_due_in_ten_minutes_becomes_a_pending_task_plan(self):
        card_id = self.card()
        payload = self.tick()
        considered = payload["proactive_consider"]
        self.assertEqual(proactive.REASON_PENDING_TASK, considered["reason_code"])
        self.assertEqual("planned", considered["action"], considered)

        rows = self.proactive_plans()
        self.assertEqual(1, len(rows), "计划出现")
        row = rows[0]
        self.assertEqual("pending_task", row["reason_code"])
        self.assertEqual(card_id, row["task"]["card_id"], "计划上带着那张卡")
        self.assertEqual(70, row["priority"], "reason_priority 默认 70（快到点的事更硬）")

        # because 只用卡上的标题与 due 的本地时刻拼出来：逐字对得上
        card = self.card_params()
        self.assertEqual("整理抽屉", card["name"])
        self.assertEqual(at("2026-11-05T10:40:00"), card["params"]["due"])
        self.assertEqual("10:40 要交的那件事：整理抽屉", row["note"])
        self.assertIn(card["name"], row["note"])
        self.assertIn("10:40", row["note"])

        ledger = proactive.log_tail(self.root, 1)[0]
        self.assertEqual("pending_task", ledger["reason_code"])
        self.assertEqual("planned", ledger["action"])
        self.assertEqual(row["note"], ledger["because"], "动念账与计划上那句是同一句")
        self.assertEqual(card_id, ledger["card_id"])

        task_row = proactive.task_log_tail(self.root, 1)[0]
        self.assertEqual("planned", task_row["action"])
        self.assertEqual(card_id, task_row["card_id"])
        self.assertEqual("整理抽屉", task_row["name"])
        self.assertEqual(at("2026-11-05T10:40:00"), task_row["due"])
        self.assertEqual(0, task_row["reminded_count"], "这一行之前没提醒过（这一次之后才记 1）")

    def test_the_reason_comes_from_both_data_files(self):
        self.card(due="2026-11-05T10:40:00")
        (Path(self.root) / "proactive.json").write_text(json.dumps(
            {"task_because": "【{when}】{name}", "task_when_format": "%H点%M分"},
            ensure_ascii=False), encoding="utf-8")
        out = self.considered()
        self.assertTrue(out["fired"], out)
        self.assertEqual("【10点40分】整理抽屉", out["because"], "拼接模板与时时刻刻法都从配置来")

    def test_three_in_the_morning_plans_but_never_speaks(self):
        self.card(due="2026-11-05T03:10:00")
        payload = self.tick(now="2026-11-05T03:00:00")
        considered = payload["proactive_consider"]
        self.assertEqual(proactive.REASON_PENDING_TASK, considered["reason_code"])
        self.assertEqual("held", considered["action"], considered)
        self.assertEqual(heart_model.STATE_SILENT, considered["engage"]["state"])
        self.assertEqual(heart_model.REASON_NIGHT, considered["engage"]["reason"])
        row = self.proactive_plans()[0]
        self.assertTrue(row["held"], "计划照生，标 held")
        self.assertEqual(heart_model.REASON_NIGHT, row["held_reason"])
        self.assertEqual([], delivery.load(self.root)["items"], "夜里不发送：待说队列空")
        self.assertEqual("held", proactive.task_log_tail(self.root, 1)[0]["action"])

    def test_the_same_card_is_never_reminded_twice(self):
        self.card()
        first = self.tick()
        self.assertEqual("planned", first["proactive_consider"]["action"])
        second = self.tick(now="2026-11-05T10:31:00")
        self.assertFalse(second["proactive_consider"]["fired"], second["proactive_consider"])
        self.assertEqual(1, len(self.proactive_plans()), "第二条计划没有出现")

        rows = proactive.task_log_lines(self.root)
        self.assertEqual(2, len(rows))
        self.assertEqual("planned", rows[0]["action"])
        self.assertEqual("skipped", rows[1]["action"])
        self.assertIn("今天已经提醒过这张卡", rows[1]["reason"])
        self.assertEqual(1, rows[1]["reminded_count"])

        # 再 consider 一次（不走 tick）：照样不排第二条
        again = self.considered(now="2026-11-05T10:32:00")
        self.assertFalse(again["fired"], again)

    def reminded_yesterday(self, card_id: str) -> None:
        """在 task 账里记一行"昨天提醒过这张卡"：跨天的额度就是这么数出来的。"""
        proactive.append_task_log(self.root, {
            "at": at("2026-11-04T10:30:00"), "card_id": card_id, "name": "整理抽屉",
            "due": at("2026-11-04T10:40:00"), "progress": 0, "action": "planned",
            "reason": "", "reminded_count": 0})

    def test_a_card_already_reminded_costs_quota_across_days(self):
        card_id = self.card()
        self.reminded_yesterday(card_id)
        out = self.considered()
        self.assertEqual("quota", out["action"], out)
        self.assertEqual("card", out["quota"]["blocked_by"]["scope"])
        self.assertEqual(1, out["quota"]["blocked_by"]["cap"], "默认每条卡只提醒一次")
        self.assertEqual(card_id, out["quota"]["blocked_by"]["card_id"])
        self.assertEqual([], self.proactive_plans(), "超了额度就不排计划")
        self.assertEqual("quota", proactive.task_log_tail(self.root, 1)[0]["action"])

    def test_the_per_card_cap_comes_from_limits(self):
        card_id = self.card()
        self.reminded_yesterday(card_id)
        (Path(self.root) / "limits.json").write_text(json.dumps(
            {"proactive": {"task_remind_per_card": 2}}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(2, proactive.task_remind_cap(self.root), "这个数只许从 limits.json 来")
        out = self.considered()
        self.assertEqual("planned", out["action"], "上限 2：第二次仍然可以提醒")


class PendingTaskSkipTest(PendingTaskCase):
    """没该提醒的那几种：都是一行 skipped，且都不抛崩 tick。"""

    def test_a_finished_card_is_skipped(self):
        from organs.home import cards
        card_id = self.card()
        cards.set_progress(root=self.home, role="agent", by="agent", room="工作台",
                           card_id=card_id, progress=100, reason="做完了")
        payload = self.tick()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["proactive_consider"]["fired"])
        row = proactive.task_log_tail(self.root, 1)[0]
        self.assertEqual("skipped", row["action"])
        self.assertIn("已经完成", row["reason"])
        self.assertEqual(100, row["progress"])

    def test_a_consumed_card_is_gone_from_the_room(self):
        from organs.home import cards
        card_id = self.card()
        cards.consume(root=self.home, role="agent", by="agent", room="工作台",
                      card_id=card_id, reason="收走")
        payload = self.tick()
        self.assertFalse(payload["proactive_consider"]["fired"])
        self.assertEqual([], proactive.task_log_lines(self.root), "看不见的卡连一行都没有")

    def test_a_card_that_is_not_due_yet_is_skipped(self):
        self.card(due="2026-11-05T18:00:00")
        payload = self.tick()
        self.assertFalse(payload["proactive_consider"]["fired"])
        row = proactive.task_log_tail(self.root, 1)[0]
        self.assertEqual("skipped", row["action"])
        self.assertIn("还没到提醒的时候", row["reason"])

    def test_a_card_overdue_past_the_grace_is_skipped(self):
        self.card(due="2026-11-05T06:00:00")
        payload = self.tick()
        self.assertFalse(payload["proactive_consider"]["fired"])
        self.assertIn("已经过期", proactive.task_log_tail(self.root, 1)[0]["reason"])

    def test_a_card_overdue_inside_the_grace_still_fires(self):
        self.card(due="2026-11-05T09:30:00")     # 过期 60 分钟，宽限 120 分钟
        payload = self.tick()
        self.assertEqual(proactive.REASON_PENDING_TASK,
                         payload["proactive_consider"]["reason_code"])

    def test_the_same_skip_is_one_line_with_a_count(self):
        self.card(due="2026-11-05T18:00:00")
        for minute in range(5):
            self.tick(now="2026-11-05T10:3%d:00" % minute)
        rows = proactive.task_log_lines(self.root)
        self.assertEqual(1, len(rows), "同日同卡同因只留一行")
        self.assertEqual(5, rows[0]["count"], "这一天因同一原因被判过 5 次")
        self.assertEqual(at("2026-11-05T10:34:00"), rows[0]["at"], "at 就地更新到最近一次")
        self.assertIn("还没到提醒的时候", rows[0]["reason"], "为什么没提醒仍然查得到")
        self.assertEqual(0, len(self.proactive_plans()), "skipped 行不计额度、也不排计划")

    def test_the_dedupe_switch_comes_from_the_config(self):
        self.card(due="2026-11-05T18:00:00")
        (Path(self.root) / "proactive.json").write_text(
            json.dumps({"task_log_dedupe_skipped": False}, ensure_ascii=False),
            encoding="utf-8")
        for minute in range(5):
            self.tick(now="2026-11-05T10:3%d:00" % minute)
        rows = proactive.task_log_lines(self.root)
        self.assertEqual(5, len(rows), "关掉去重就逐条落")
        self.assertEqual([1] * 5, [row["count"] for row in rows])

    def test_a_different_day_or_reason_is_a_new_line(self):
        self.card(due="2026-11-05T18:00:00")
        self.tick(now="2026-11-05T10:30:00")
        self.assertEqual(1, len(proactive.task_log_lines(self.root)))
        (Path(self.root) / "schedule_log.jsonl").write_text(
            json.dumps({"day": "2026-11-06"}, ensure_ascii=False) + chr(10), encoding="utf-8")
        self.tick(now="2026-11-06T09:00:00")
        rows = proactive.task_log_lines(self.root)
        self.assertEqual(2, len(rows), "换一天就是另一件事，该新起一行")

    def test_planned_rows_are_never_merged(self):
        row = {"at": at("2026-11-05T10:30:00"), "card_id": "工作台-1", "name": "整理抽屉",
               "due": at("2026-11-05T10:40:00"), "progress": 0, "action": "planned",
               "reason": "", "reminded_count": 0}
        proactive.append_task_log(self.root, dict(row))
        proactive.append_task_log(self.root, dict(row))
        rows = proactive.task_log_lines(self.root)
        self.assertEqual(2, len(rows), "planned 一次一行，任何配置下都不合并")
        self.assertNotIn("count", rows[0])

    def test_a_broken_due_is_skipped_with_a_reason(self):
        self.card(due_raw="明天吧")
        payload = self.tick()
        self.assertTrue(payload["ok"], "坏值不许崩掉 tick")
        self.assertFalse(payload["proactive_consider"]["fired"])
        row = proactive.task_log_tail(self.root, 1)[0]
        self.assertEqual("skipped", row["action"])
        self.assertIn("params.due 不是合法时刻", row["reason"])

    def test_a_card_without_the_task_flag_is_skipped(self):
        self.card(task=False)
        payload = self.tick()
        self.assertFalse(payload["proactive_consider"]["fired"])
        self.assertIn("没有任务标记", proactive.task_log_tail(self.root, 1)[0]["reason"])

    def test_a_wrong_room_is_reported_and_never_crashes_the_tick(self):
        self.card()
        (Path(self.root) / "proactive.json").write_text(
            json.dumps({"task_room": "杂物间"}, ensure_ascii=False), encoding="utf-8")
        payload = self.tick()
        self.assertTrue(payload["ok"], "房名不对也不许崩掉 tick")
        view = payload["proactive_consider"]["pending_task"]
        self.assertFalse(view["ok"])
        self.assertIn("读不了", view["error"])
        self.assertEqual([], self.proactive_plans())

        seen = proactive.overview(at(NOW), root=self.root, heart_root=self.heart,
                                  brain=self.brain, home_root=self.home)
        self.assertFalse(seen["pending_task"]["ok"])
        self.assertEqual("杂物间", seen["pending_task"]["room"])


class PendingTaskDueTest(PendingTaskCase):
    """到点执行：提示词里写明"只能用卡上的标题与时间"，回执对不上就 offtask。"""

    def _planned(self) -> dict:
        self.card()
        self.tick()
        return self.proactive_plans()[0]

    def test_the_prompt_says_only_the_card_may_be_used(self):
        self._planned()
        prompts = []

        def complete(messages, **kwargs):
            prompts.append(messages)
            return json.dumps({"send": True, "chunks": ["10:40 那件事别忘了：整理抽屉"]},
                              ensure_ascii=False)

        row = self._planned()
        out = proactive.run_due(row, at("2026-11-05T10:45:00"), root=self.root,
                                heart_root=self.heart, brain=self.brain,
                                complete=complete)
        self.assertEqual("queued", out["action"], out)
        body = prompts[0][1]["content"]
        self.assertIn("整理抽屉", body)
        self.assertIn("10:40", body)
        self.assertIn("不许添加卡上没有的事", body, "规矩要进提示词")
        items = delivery.load(self.root)["items"]
        self.assertEqual(1, len(items), "先落盘再记账")

    def test_a_reply_about_something_else_is_offtask_and_not_sent(self):
        row = self._planned()
        out = proactive.run_due(row, at("2026-11-05T10:45:00"), root=self.root,
                                heart_root=self.heart, brain=self.brain,
                                complete=lambda messages, **kw: json.dumps(
                                    {"send": True, "chunks": ["窗外的风好大"]},
                                    ensure_ascii=False))
        self.assertEqual("error", out["action"], out)
        self.assertEqual("offtask", out["error_kind"])
        self.assertEqual([], delivery.load(self.root)["items"], "不发")
        self.assertEqual([], self.proactive_plans(), "consume，不重试")
        ledger = proactive.log_tail(self.root, 1)[0]
        self.assertEqual("error", ledger["action"])
        self.assertEqual("offtask", ledger["error_kind"])

    def test_a_reply_that_echoes_the_hour_alone_is_enough(self):
        row = self._planned()
        out = proactive.run_due(row, at("2026-11-05T10:45:00"), root=self.root,
                                heart_root=self.heart, brain=self.brain,
                                complete=lambda messages, **kw: json.dumps(
                                    {"send": True, "chunks": ["10:40 那件别忘了"]},
                                    ensure_ascii=False))
        self.assertEqual("queued", out["action"], out)


if __name__ == "__main__":
    unittest.main()

