"""engage 与 ports 的验收：硬门先于软门、软门全算、归因不变式、只读端口。

时间一律用带显式偏移的固定串（+08:00），跑在哪个时区都同一个结论；
数据一律落 helpers.make_root 的临时目录，不碰仓库里的真 data。
"""
from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from organs.heart import engage, events, model, ports, store
from organs.heart.tests import helpers

# 固定时刻。
NIGHT = "2024-05-06T23:30:00+08:00"
NIGHT_LATE = "2024-05-07T03:10:00+08:00"
DAY = "2024-05-06T14:00:00+08:00"


def make_input(**kw: Any) -> "engage.EngageInput":
    """全绿为底的输入：只改想测的那一项。"""
    base: Dict[str, Any] = {
        "mood_now": 60.0,
        "energy": 80.0,
        "familiarity": 3.0,
        "today_count": 3,
        "now": DAY,
    }
    base.update(kw)
    return engage.EngageInput(**base)


def minutes_ago(minutes: float, base: str = DAY) -> str:
    """base 往前 minutes 分钟的时刻串。"""
    return model.iso(model.parse_ts(base) - timedelta(minutes=minutes))


def feed(root: Any, kinds: List[str], gaps: List[float], base: str = DAY) -> None:
    """按 gaps（距 base 几分钟）往临时账本投递一串事件。"""
    for kind, gap in zip(kinds, gaps):
        events.deliver(kind, root=root, now=minutes_ago(gap, base))


class HardGatesTest(unittest.TestCase):
    """验收 1：三道硬门各自命中，且命中时软门确实没被算。"""

    def test_night_proactive_is_silent(self) -> None:
        res = engage.decide(make_input(now=NIGHT, intent=model.INTENT_PROACTIVE))
        self.assertEqual(res["state"], model.STATE_SILENT)
        self.assertEqual(res["reason"], model.REASON_NIGHT)
        self.assertEqual(res["hits"], [model.REASON_NIGHT])
        self.assertEqual(res["hard"], [model.REASON_NIGHT])
        self.assertTrue(res["soft_skipped"])
        self.assertEqual(res["soft"], [])

    def test_night_passive_still_answers(self) -> None:
        # 夜间被动仍回：少说，而不是完全不出声。
        res = engage.decide(make_input(now=NIGHT, intent=model.INTENT_PASSIVE))
        self.assertEqual(res["state"], model.STATE_LESS)
        self.assertEqual(res["reason"], model.REASON_NIGHT)
        self.assertTrue(res["soft_skipped"])
        self.assertEqual(res["soft"], [])

    def test_night_window_edges(self) -> None:
        # 23:00 进、08:00 出；22:59 与 08:00 都不算夜里。
        for hour, expect_night in ((23, True), (0, True), (7, True),
                                   (8, False), (22, False), (14, False)):
            stamp = "2024-05-06T%02d:30:00+08:00" % hour
            res = engage.decide(make_input(now=stamp))
            self.assertEqual(model.REASON_NIGHT in res["hits"], expect_night,
                             "%s 的夜间判定错了：%s" % (stamp, res["hits"]))

    def test_insulted_today(self) -> None:
        res = engage.decide(make_input(insulted_today=True))
        self.assertEqual(res["state"], model.STATE_LESS)
        self.assertEqual(res["reason"], model.REASON_INSULTED)
        self.assertEqual(res["hits"], [model.REASON_INSULTED])
        self.assertTrue(res["soft_skipped"])
        self.assertEqual(res["soft"], [])

    def test_mood_and_energy_double_low(self) -> None:
        res = engage.decide(make_input(mood_now=30.0, energy=30.0))
        self.assertEqual(res["state"], model.STATE_SILENT)
        self.assertEqual(res["reason"], model.REASON_DOUBLE_LOW)
        self.assertEqual(res["hits"], [model.REASON_DOUBLE_LOW])
        self.assertTrue(res["soft_skipped"])

    def test_single_low_is_not_double_low(self) -> None:
        # 只有一头低不算双低：mood 30 / energy 40 与 mood 40 / energy 30 都不闭口。
        for mood, energy in ((30.0, 40.0), (40.0, 30.0), (31.0, 30.0)):
            res = engage.decide(make_input(mood_now=mood, energy=energy))
            self.assertNotIn(model.REASON_DOUBLE_LOW, res["hits"])
            self.assertEqual(res["state"], model.STATE_SPEAK)

    def test_hard_gate_skips_soft_entirely(self) -> None:
        # 三道硬门全中 + 三道软门也全都该中的输入：软门一条都不许算。
        res = engage.decide(make_input(
            now=NIGHT, insulted_today=True, mood_now=20.0, energy=20.0,
            today_count=50, familiarity=0.0,
            last_interrupted_at="2024-05-06T23:25:00+08:00"))
        self.assertEqual(res["hard"],
                         [model.REASON_NIGHT, model.REASON_INSULTED,
                          model.REASON_DOUBLE_LOW])
        self.assertEqual(res["soft"], [])
        self.assertTrue(res["soft_skipped"])
        self.assertEqual(res["hits"], res["hard"])
        # 次序即优先级：夜里先说话，被动时是「少说」。
        self.assertEqual(res["reason"], model.REASON_NIGHT)
        self.assertEqual(res["state"], model.STATE_LESS)


class SoftGatesTest(unittest.TestCase):
    """验收 2：软门各档命中，组合时最强胜出且 hits 全列。"""

    def test_today_volume_soft_line(self) -> None:
        res = engage.decide(make_input(today_count=model.ENGAGE_TODAY_SOFT))
        self.assertEqual(res["soft"], [model.REASON_TODAY_VOLUME])
        self.assertEqual(res["hits"], [model.REASON_TODAY_VOLUME])
        self.assertEqual(res["state"], model.STATE_LESS)
        self.assertEqual(res["reason"], model.REASON_TODAY_VOLUME)
        self.assertFalse(res["soft_skipped"])

    def test_today_volume_hard_line(self) -> None:
        res = engage.decide(make_input(today_count=model.ENGAGE_TODAY_HARD))
        self.assertEqual(res["hits"], [model.REASON_TODAY_VOLUME])
        self.assertEqual(res["state"], model.STATE_SILENT)
        self.assertFalse(res["soft_skipped"])

    def test_low_familiarity(self) -> None:
        res = engage.decide(make_input(familiarity=0.99))
        self.assertEqual(res["hits"], [model.REASON_FAMILIARITY])
        self.assertEqual(res["state"], model.STATE_LESS)

    def test_familiarity_at_line_is_enough(self) -> None:
        res = engage.decide(make_input(familiarity=model.ENGAGE_FAMILIARITY_LOW))
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["state"], model.STATE_SPEAK)

    def test_recently_interrupted(self) -> None:
        res = engage.decide(make_input(
            last_interrupted_at="2024-05-06T13:50:00+08:00"))
        self.assertEqual(res["hits"], [model.REASON_INTERRUPTED])
        self.assertEqual(res["state"], model.STATE_LESS)

    def test_interrupt_cooldown_expired(self) -> None:
        res = engage.decide(make_input(
            last_interrupted_at="2024-05-06T13:15:00+08:00"))
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["state"], model.STATE_SPEAK)

    def test_combination_strongest_wins_and_all_hits_listed(self) -> None:
        res = engage.decide(make_input(
            today_count=model.ENGAGE_TODAY_HARD, familiarity=0.2,
            last_interrupted_at="2024-05-06T13:55:00+08:00"))
        self.assertEqual(res["hits"],
                         [model.REASON_TODAY_VOLUME, model.REASON_FAMILIARITY,
                          model.REASON_INTERRUPTED])
        self.assertEqual(res["soft"], res["hits"])
        self.assertEqual(res["state"], model.STATE_SILENT)
        self.assertEqual(res["reason"], model.REASON_TODAY_VOLUME)

    def test_combination_soft_only_stays_less(self) -> None:
        res = engage.decide(make_input(today_count=20, familiarity=0.2))
        self.assertEqual(res["hits"],
                         [model.REASON_TODAY_VOLUME, model.REASON_FAMILIARITY])
        self.assertEqual(res["state"], model.STATE_LESS)

    def test_all_green_speaks_with_no_reason(self) -> None:
        res = engage.decide(make_input())
        self.assertEqual(res["state"], model.STATE_SPEAK)
        self.assertIsNone(res["reason"])
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["hard"], [])
        self.assertEqual(res["soft"], [])
        self.assertFalse(res["soft_skipped"])

    def test_score_is_readout_only(self) -> None:
        # 分数只做读数：energy 变了分数跟着变，三态不变。
        low = engage.decide(make_input(energy=10.0))
        high = engage.decide(make_input(energy=90.0))
        self.assertNotEqual(low["score"], high["score"])
        self.assertEqual(low["state"], high["state"])

    def test_readouts_round_trip(self) -> None:
        res = engage.decide(make_input(mood_now=42.0, energy=63.0,
                                       familiarity=2.5, today_count=7,
                                       intent=model.INTENT_PROACTIVE))
        self.assertEqual(res["readouts"], {
            "mood_now": 42.0, "energy": 63.0, "familiarity": 2.5,
            "today_count": 7, "intent": model.INTENT_PROACTIVE})


class AttributionTest(unittest.TestCase):
    """验收 3：连判 20 次，每次非「说」的判决都能说出为什么。"""

    CASES: List[Dict[str, Any]] = [
        dict(label="深夜主动·全绿", now=NIGHT, intent=model.INTENT_PROACTIVE),
        dict(label="深夜被动·全绿", now=NIGHT, intent=model.INTENT_PASSIVE),
        dict(label="白天被骂", insulted_today=True),
        dict(label="双低 30/30", mood_now=30.0, energy=30.0),
        dict(label="被骂+双低", insulted_today=True, mood_now=25.0, energy=25.0),
        dict(label="今日 20 条", today_count=20),
        dict(label="今日 40 条", today_count=40),
        dict(label="熟稔 0.4", familiarity=0.4),
        dict(label="5 分钟前被打断",
             last_interrupted_at="2024-05-06T13:55:00+08:00"),
        dict(label="今日 20+熟稔 0.4", today_count=20, familiarity=0.4),
        dict(label="40 条+熟稔 0.4+刚被打断", today_count=40, familiarity=0.4,
             last_interrupted_at="2024-05-06T13:55:00+08:00"),
        dict(label="全绿"),
        dict(label="energy 20 但 mood 60", energy=20.0),
        dict(label="mood 30 但 energy 50", mood_now=30.0, energy=50.0),
        dict(label="打断 29 分钟（冷却内）",
             last_interrupted_at="2024-05-06T13:31:00+08:00"),
        dict(label="打断 31 分钟（冷却外）",
             last_interrupted_at="2024-05-06T13:29:00+08:00"),
        dict(label="凌晨 3 点被动", now=NIGHT_LATE, intent=model.INTENT_PASSIVE),
        dict(label="被骂+今日 40（硬门压软门）", insulted_today=True,
             today_count=40),
        dict(label="熟稔 0+50 分钟前被打断", familiarity=0.0,
             last_interrupted_at="2024-05-06T13:10:00+08:00"),
        dict(label="深夜+全绿+主动", now=NIGHT, intent=model.INTENT_PROACTIVE),
    ]

    def test_twenty_judgments_are_all_attributed(self) -> None:
        self.assertEqual(len(self.CASES), 20)
        rows: List[Dict[str, Any]] = []
        for index, case in enumerate(self.CASES, 1):
            kw = dict(case)
            kw.pop("label")
            res = engage.decide(make_input(**kw))
            state, hits = res["state"], res["hits"]
            if state != model.STATE_SPEAK:
                self.assertTrue(hits, "第 %d 次判成「%s」却没有理由码" % (index, state))
                self.assertNotIn(model.REASON_UNATTRIBUTED, hits,
                                 "第 %d 次落到了 unattributed" % index)
                self.assertIn(res["reason"], model.REASONS)
                self.assertIn(res["reason"], hits)
            else:
                self.assertEqual(hits, [])
                self.assertIsNone(res["reason"])
            rows.append({"state": state, "reason": res["reason"],
                         "hits": list(hits)})
        report = engage.attribution_report(rows)
        self.assertEqual(report["total"], 20)
        self.assertEqual(report["unattributed"], 0)
        self.assertEqual(sum(report["by_reason"].values()), 20)
        silent = [r for r in rows if r["state"] == model.STATE_SILENT]
        self.assertTrue(silent, "20 次里应该有判「不说」的")

    # 上一轮那 20 次判决的逐行快照（state, reason, hits）：
    # 加第 4 道软门之后必须一模一样——硬门次序、软门三条、命中即停都没动。
    EXPECTED: List[Tuple[str, Optional[str], List[str]]] = [
        (model.STATE_SILENT, model.REASON_NIGHT, [model.REASON_NIGHT]),
        (model.STATE_LESS, model.REASON_NIGHT, [model.REASON_NIGHT]),
        (model.STATE_LESS, model.REASON_INSULTED, [model.REASON_INSULTED]),
        (model.STATE_SILENT, model.REASON_DOUBLE_LOW, [model.REASON_DOUBLE_LOW]),
        (model.STATE_LESS, model.REASON_INSULTED,
         [model.REASON_INSULTED, model.REASON_DOUBLE_LOW]),
        (model.STATE_LESS, model.REASON_TODAY_VOLUME, [model.REASON_TODAY_VOLUME]),
        (model.STATE_SILENT, model.REASON_TODAY_VOLUME, [model.REASON_TODAY_VOLUME]),
        (model.STATE_LESS, model.REASON_FAMILIARITY, [model.REASON_FAMILIARITY]),
        (model.STATE_LESS, model.REASON_INTERRUPTED, [model.REASON_INTERRUPTED]),
        (model.STATE_LESS, model.REASON_TODAY_VOLUME,
         [model.REASON_TODAY_VOLUME, model.REASON_FAMILIARITY]),
        (model.STATE_SILENT, model.REASON_TODAY_VOLUME,
         [model.REASON_TODAY_VOLUME, model.REASON_FAMILIARITY,
          model.REASON_INTERRUPTED]),
        (model.STATE_SPEAK, None, []),
        (model.STATE_SPEAK, None, []),
        (model.STATE_SPEAK, None, []),
        (model.STATE_LESS, model.REASON_INTERRUPTED, [model.REASON_INTERRUPTED]),
        (model.STATE_SPEAK, None, []),
        (model.STATE_LESS, model.REASON_NIGHT, [model.REASON_NIGHT]),
        (model.STATE_LESS, model.REASON_INSULTED, [model.REASON_INSULTED]),
        (model.STATE_LESS, model.REASON_FAMILIARITY, [model.REASON_FAMILIARITY]),
        (model.STATE_SILENT, model.REASON_NIGHT, [model.REASON_NIGHT]),
    ]

    def test_twenty_judgments_unchanged_by_window_gate(self) -> None:
        self.assertEqual(len(self.EXPECTED), len(self.CASES))
        for index, (case, want) in enumerate(zip(self.CASES, self.EXPECTED), 1):
            kw = dict(case)
            label = kw.pop("label")
            res = engage.decide(make_input(**kw))
            self.assertEqual((res["state"], res["reason"], res["hits"]), want,
                             "第 %d 次（%s）的判决变了" % (index, label))
            # 没传窗口 → 第 4 道软门不参与，判决与被加之前逐行一致。
            self.assertIsNone(res["window"])

    def test_invariant_stamps_unattributed_and_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            reason, hits = engage._attributed(model.STATE_LESS, None, [])
        self.assertEqual(reason, model.REASON_UNATTRIBUTED)
        self.assertEqual(hits, [model.REASON_UNATTRIBUTED])
        self.assertEqual(len(caught), 1)
        self.assertIn("判了但不知道为什么", str(caught[0].message))

    def test_report_counts_unattributed_rows(self) -> None:
        rows = [
            {"state": model.STATE_SPEAK, "reason": None, "hits": []},
            {"state": model.STATE_LESS, "reason": None, "hits": []},
            {"state": model.STATE_SILENT, "reason": model.REASON_NIGHT,
             "hits": [model.REASON_NIGHT]},
        ]
        report = engage.attribution_report(rows)
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["unattributed"], 1)
        self.assertEqual(report["by_reason"]["(none)"], 2)
        self.assertEqual(report["by_reason"][model.REASON_NIGHT], 1)

    def test_report_tolerates_empty_and_junk(self) -> None:
        self.assertEqual(engage.attribution_report([]),
                         {"total": 0, "unattributed": 0, "by_reason": {}})
        self.assertEqual(engage.attribution_report([None, "x"]),  # type: ignore[list-item]
                         {"total": 0, "unattributed": 0, "by_reason": {}})


class WindowSignalTest(unittest.TestCase):
    """验收 5：近期事件窗口现算加权——读账本、不落状态、窗口滑走自然归零。"""

    def test_three_consecutive_ignored_judges_less(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = helpers.make_root(temp)
            feed(root, ["ignored"] * 3, [4, 2, 0])
            sig = engage.window_signal(root, now=DAY)
            self.assertEqual(sig["rows"], 3)
            self.assertEqual(sig["consecutive_ignored"], 3)
            self.assertEqual((sig["harsh"], sig["replied"]), (0, 0))
            self.assertEqual(sig["window_min"], engage.WINDOW_MIN)
            self.assertAlmostEqual(sig["weight"], 2.95, places=6)
            res = engage.decide(make_input(window=sig))
            self.assertEqual(res["state"], model.STATE_LESS)
            self.assertEqual(res["hits"], [model.REASON_IGNORED])
            self.assertEqual(res["reason"], model.REASON_IGNORED)
            self.assertEqual(res["soft"], [model.REASON_IGNORED])
            self.assertEqual(res["window"], sig)
            self.assertIn(model.REASON_IGNORED, model.SOFT_REASONS)

    def test_normal_reply_restores_speak(self) -> None:
        # 连喂 3 条 ignored 判少说；补一次正常互动，下一次判定回到「说」。
        with tempfile.TemporaryDirectory() as temp:
            root = helpers.make_root(temp)
            feed(root, ["ignored"] * 3, [6, 4, 2])
            before = engage.window_signal(root, now=DAY)
            before_res = engage.decide(make_input(window=before))
            self.assertEqual(before_res["state"], model.STATE_LESS)
            self.assertAlmostEqual(before["weight"], 2.9, places=6)
            events.deliver("replied", root=root, now=DAY)
            after = engage.window_signal(root, now=DAY)
            after_res = engage.decide(make_input(window=after))
            self.assertAlmostEqual(after["weight"], 1.9, places=6)
            self.assertEqual(after["replied"], 1)
            self.assertEqual(after["consecutive_ignored"], 0)
            self.assertEqual(after_res["state"], model.STATE_SPEAK)
            self.assertEqual(after_res["hits"], [])
            self.assertIsNone(after_res["reason"])

    def test_window_slides_away_and_weights_return_to_zero(self) -> None:
        # 同一批事件、只把 now 推后 3 小时：weight 归 0、判「说」——证明没留状态。
        with tempfile.TemporaryDirectory() as temp:
            root = helpers.make_root(temp)
            feed(root, ["ignored"] * 3, [4, 2, 0])
            later = model.iso(model.parse_ts(DAY) + timedelta(hours=3))
            sig = engage.window_signal(root, now=later)
            self.assertEqual(sig["rows"], 0)
            self.assertEqual(sig["weight"], 0.0)
            self.assertEqual(sig["consecutive_ignored"], 0)
            res = engage.decide(make_input(now=later, window=sig))
            self.assertEqual(res["state"], model.STATE_SPEAK)
            self.assertEqual(res["hits"], [])
            # 账本一行没动：回到原来那一刻，窗口里还是那 3 条。
            self.assertEqual(engage.window_signal(root, now=DAY)["rows"], 3)

    def test_weight_ladder_grows_with_ignored(self) -> None:
        # 「越来越冷」长在加权里：条数越多、越贴 now，weight 单调爬，跨线就降态。
        expect = {1: model.STATE_SPEAK, 2: model.STATE_SPEAK,
                  3: model.STATE_LESS, 5: model.STATE_LESS,
                  8: model.STATE_SILENT}
        weights: List[float] = []
        for n in (1, 2, 3, 5, 8):
            with tempfile.TemporaryDirectory() as temp:
                root = helpers.make_root(temp)
                feed(root, ["ignored"] * n,
                     [2.0 * (n - 1 - index) for index in range(n)])
                sig = engage.window_signal(root, now=DAY)
                res = engage.decide(make_input(window=sig))
                self.assertEqual(sig["consecutive_ignored"], n)
                self.assertEqual(res["state"], expect[n],
                                 "%d 条 ignored 该判「%s」，实际「%s」"
                                 % (n, expect[n], res["state"]))
                if expect[n] == model.STATE_SPEAK:
                    self.assertEqual(res["hits"], [])
                else:
                    self.assertEqual(res["hits"], [model.REASON_IGNORED])
                weights.append(sig["weight"])
        self.assertEqual(weights, sorted(weights), "weight 没有随条数单调爬：%s" % weights)
        self.assertAlmostEqual(weights[0], 1.0, places=6)
        self.assertAlmostEqual(weights[1], 1.98333, places=4)
        self.assertAlmostEqual(weights[2], 2.95, places=6)
        self.assertAlmostEqual(weights[3], 4.83333, places=4)
        self.assertAlmostEqual(weights[4], 7.53333, places=4)

    def test_harsh_weighs_harder_than_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = helpers.make_root(temp)
            feed(root, ["harsh"] * 3, [4, 2, 0])
            harsh_sig = engage.window_signal(root, now=DAY)
            self.assertEqual(harsh_sig["harsh"], 3)
            self.assertAlmostEqual(harsh_sig["weight"], 4.425, places=6)
            res = engage.decide(make_input(window=harsh_sig))
            self.assertEqual(res["state"], model.STATE_LESS)
            self.assertEqual(res["hits"], [model.REASON_IGNORED])
        with tempfile.TemporaryDirectory() as temp:
            root = helpers.make_root(temp)
            feed(root, ["ignored"] * 3, [4, 2, 0])
            ignored_sig = engage.window_signal(root, now=DAY)
        self.assertGreater(harsh_sig["weight"], ignored_sig["weight"],
                           "同样的条数与间隔，harsh 该压得比 ignored 更狠")

    def test_harsh_and_ignored_mix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = helpers.make_root(temp)
            feed(root, ["harsh", "ignored", "harsh", "ignored"], [6, 4, 2, 0])
            sig = engage.window_signal(root, now=DAY)
            self.assertEqual((sig["harsh"], sig["rows"]), (2, 4))
            # 最新一条是 ignored、上一条是 harsh：连续数就停在 1。
            self.assertEqual(sig["consecutive_ignored"], 1)
            self.assertAlmostEqual(sig["weight"], 4.86667, places=4)
            res = engage.decide(make_input(window=sig))
            self.assertEqual(res["state"], model.STATE_LESS)
            self.assertEqual(res["hits"], [model.REASON_IGNORED])

    def test_window_edges_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = helpers.make_root(temp)
            events.deliver("ignored", root=root, now=minutes_ago(121))
            events.deliver("ignored", root=root, now=minutes_ago(119))
            sig = engage.window_signal(root, now=DAY)
            self.assertEqual(sig["rows"], 1)
            self.assertEqual(sig["consecutive_ignored"], 1)
            self.assertAlmostEqual(sig["weight"], 1.0 - 119.0 / 120.0, places=6)

    def test_no_window_signal_is_no_hit(self) -> None:
        res = engage.decide(make_input())
        self.assertIsNone(res["window"])
        self.assertNotIn(model.REASON_IGNORED, res["hits"])
        self.assertEqual(res["state"], model.STATE_SPEAK)

    def test_broken_window_signal_degrades_to_no_hit(self) -> None:
        res = engage.decide(make_input(window={"weight": "坏", "consecutive_ignored": None}))
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["state"], model.STATE_SPEAK)
        empty = engage.decide(make_input(window={}))
        self.assertEqual(empty["hits"], [])

    def test_hard_gate_still_wins_over_window(self) -> None:
        # 硬门次序一个字不改：夜里主动依旧「不说」，窗口再响也进不了 soft。
        with tempfile.TemporaryDirectory() as temp:
            root = helpers.make_root(temp)
            feed(root, ["ignored"] * 8,
                 [2.0 * (7 - index) for index in range(8)], base=NIGHT)
            sig = engage.window_signal(root, now=NIGHT)
            self.assertGreater(sig["weight"], engage.WINDOW_SILENT_WEIGHT)
            res = engage.decide(make_input(now=NIGHT, window=sig,
                                           intent=model.INTENT_PROACTIVE))
            self.assertEqual(res["hard"], [model.REASON_NIGHT])
            self.assertTrue(res["soft_skipped"])
            self.assertEqual(res["soft"], [])
            self.assertEqual(res["hits"], [model.REASON_NIGHT])
            self.assertEqual(res["state"], model.STATE_SILENT)
            self.assertEqual(res["window"], sig)


class FakeBrain(object):
    """只暴露 ports 允许的两条读数的脑：形状随便，读数随便。"""

    def __init__(self, people: List[Dict[str, Any]],
                 entries: List[Dict[str, Any]]) -> None:
        self._people = people
        self._entries = entries
        self.calls: List[Any] = []

    def people_list(self, now: Optional[str] = None) -> List[Dict[str, Any]]:
        self.calls.append(("people_list", now))
        return [dict(p) for p in self._people]

    def today_view(self, now: Optional[str] = None) -> Dict[str, Any]:
        self.calls.append(("today_view", now))
        return {"day": "2024-05-06", "entries": [dict(e) for e in self._entries]}


class RecordingBrain(object):
    """碰任何第三个属性就炸的脑：用来钉死「只许读两个数」。"""

    def __init__(self) -> None:
        self.touched: List[str] = []
        object.__setattr__(self, "_people", [
            {"id": "u1", "display": "主人", "aliases": ["boss"],
             "familiarity": 4.0}])
        object.__setattr__(self, "_entries", [{"who": "u1"}, {"who": "u1"},
                                              {"who": "别人"}])

    def __getattr__(self, name: str) -> Any:
        self.touched.append(name)

        def call(now: Optional[str] = None) -> Any:
            if name == "people_list":
                return [dict(p) for p in self._people]
            if name == "today_view":
                return {"day": "2024-05-06",
                        "entries": [dict(e) for e in self._entries]}
            raise AssertionError("ports 碰了脑的私有面：%s" % name)

        return call


class PortsTest(unittest.TestCase):
    """验收 4：两个读数都给得出来；缺脑不抛；源码里没有越界通道。"""

    def test_static_readout(self) -> None:
        port = ports.StaticReadout(familiarity=2.5, today_count=7)
        self.assertEqual(port.familiarity("主人"), 2.5)
        self.assertEqual(port.today_count("主人"), 7)
        self.assertEqual(port.familiarity("谁", now=NIGHT), 2.5)
        empty = ports.StaticReadout()
        self.assertEqual((empty.familiarity("主人"), empty.today_count("主人")),
                         (0.0, 0))
        self.assertIsInstance(port, ports.BrainReadout)

    def test_from_brain_gives_both_numbers(self) -> None:
        brain = FakeBrain(
            people=[{"id": "主人", "display": "主人", "familiarity": 3.5}],
            entries=[{"who": "主人"}, {"who": "主人"}, {"who": "主人"},
                     {"who": "别人"}])
        port = ports.from_brain(brain)
        self.assertAlmostEqual(port.familiarity("主人", now=DAY), 3.5)
        self.assertEqual(port.today_count("主人", now=DAY), 3)
        self.assertAlmostEqual(port.familiarity("陌生人", now=DAY), 0.0)
        self.assertEqual(port.today_count("陌生人", now=DAY), 0)
        # now 被原样透传给脑，心自己不现算任何时间量。
        self.assertTrue(all(call[1] == DAY for call in brain.calls))

    def test_from_brain_recognises_alias_and_display(self) -> None:
        brain = FakeBrain(
            people=[{"id": "u1", "display": "主人", "aliases": ["boss"],
                     "familiarity": 2.0}],
            entries=[{"who": "u1"}, {"who": "u1"}])
        port = ports.from_brain(brain)
        self.assertAlmostEqual(port.familiarity("主人", now=DAY), 2.0)
        self.assertEqual(port.today_count("主人", now=DAY), 2)
        self.assertEqual(port.today_count("boss", now=DAY), 2)

    def test_from_brain_none_is_safe_and_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            port = ports.from_brain(None)
        self.assertEqual((port.familiarity("主人", now=DAY),
                          port.today_count("主人", now=DAY)), (0.0, 0))
        self.assertEqual(len(caught), 1)
        self.assertIn("brain=None", str(caught[0].message))

    def test_from_brain_broken_brain_degrades(self) -> None:
        class Broken(object):
            def people_list(self, now: Optional[str] = None) -> Any:
                raise RuntimeError("脑坏了")

            def today_view(self, now: Optional[str] = None) -> Any:
                raise RuntimeError("脑坏了")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            port = ports.from_brain(Broken())
            self.assertEqual((port.familiarity("主人"), port.today_count("主人")),
                             (0.0, 0))
            self.assertEqual(port.familiarity("主人"), 0.0)
        self.assertTrue(caught, "读数降级要留一行 WARN")

    def test_duck_adapter_only_touches_two_readouts(self) -> None:
        brain = RecordingBrain()
        port = ports.from_brain(brain)
        self.assertAlmostEqual(port.familiarity("主人", now=DAY), 4.0)
        self.assertEqual(port.today_count("主人", now=DAY), 2)
        self.assertTrue(set(brain.touched) <= {"people_list", "today_view"},
                        "碰了边界外的东西：%s" % sorted(set(brain.touched)))

    def test_ports_source_has_no_brain_import_or_storage_names(self) -> None:
        text = (Path(ports.__file__).read_text(encoding="utf-8"))
        for banned in ("import organs.brain", "from organs.brain",
                       "from organs import brain", "import brain",
                       "today.json", "longterm.jsonl", "people.json"):
            self.assertNotIn(banned, text, "ports.py 里出现了越界串：%s" % banned)

    def test_engage_source_has_no_random_or_probability(self) -> None:
        text = Path(engage.__file__).read_text(encoding="utf-8")
        for banned in ("random", "概率", "import organs.brain", "from organs.brain"):
            self.assertNotIn(banned, text, "engage.py 里出现了越界串：%s" % banned)


class DecideAndLogTest(unittest.TestCase):
    """decide_and_log 落一行日志；日志失败不影响结论。"""

    def test_logs_one_row_into_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = helpers.make_root(temp)
            res = engage.decide_and_log(make_input(today_count=20), root=root)
            path = Path(root) / engage.LOG_FILE
            self.assertTrue(path.exists())
            rows = store.read_jsonl(path)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["state"], res["state"])
            self.assertEqual(row["reason"], res["reason"])
            self.assertEqual(row["hits"], res["hits"])
            self.assertEqual(row["readouts"], res["readouts"])
            self.assertEqual(row["at"], DAY)
            report = engage.attribution_report(rows)
            self.assertEqual(report, {"total": 1, "unattributed": 0,
                                      "by_reason": {model.REASON_TODAY_VOLUME: 1}})
            # 落的是临时根，不是真 data。
            self.assertEqual(path.parent, Path(root))
            self.assertNotEqual(Path(root), helpers.heart_data())

    def test_log_failure_does_not_change_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            blocker = Path(temp) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            inp = make_input(insulted_today=True)
            expected = engage.decide(inp)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                actual = engage.decide_and_log(inp, root=blocker)
            self.assertEqual(actual, expected)
            self.assertTrue(caught, "日志写失败要留一行 WARN")


class IntentGateTest(unittest.TestCase):
    """验收 4：意图排序层作为第 5 道软门——只否决，不授予。

    intents 是 intent.rank() 的返回，调用方传进来；decide 不读权重表、不读 pending。
    pass=False → 主张少说；pass=True → 一个字都不加；没传 → 与没有这道门时逐行一致。
    """

    def test_below_threshold_adds_less(self) -> None:
        low = {"pass": False, "score": 0.42, "threshold": 0.55}
        res = engage.decide(make_input(intents=low))
        self.assertEqual(res["state"], model.STATE_LESS)
        self.assertEqual(res["reason"], model.REASON_INTENT_BELOW)
        self.assertIn(model.REASON_INTENT_BELOW, res["hits"])
        self.assertEqual(res["soft"], [model.REASON_INTENT_BELOW])
        self.assertFalse(res["soft_skipped"])
        self.assertEqual(res["intents"], low)

    def test_below_threshold_is_in_soft_reasons(self) -> None:
        self.assertIn(model.REASON_INTENT_BELOW, model.SOFT_REASONS)
        self.assertIn(model.REASON_INTENT_BELOW, model.REASONS)

    def test_pass_true_adds_no_gate(self) -> None:
        ok = {"pass": True, "score": 0.77, "threshold": 0.55}
        res = engage.decide(make_input(intents=ok))
        self.assertEqual(res["state"], model.STATE_SPEAK)
        self.assertEqual(res["hits"], [])
        self.assertIsNone(res["reason"])
        self.assertEqual(res["intents"], ok, "原样带回")

    def test_no_intents_means_no_fifth_gate(self) -> None:
        res = engage.decide(make_input())
        self.assertEqual(res["hits"], [])
        self.assertIsNone(res["intents"])

    def test_intents_without_pass_key_do_not_gate(self) -> None:
        res = engage.decide(make_input(intents={"score": 0.1, "order": []}))
        self.assertEqual(res["hits"], [])

    def test_intent_gate_stacks_with_other_soft_gates(self) -> None:
        res = engage.decide(make_input(today_count=20,
                                       intents={"pass": False}))
        self.assertEqual(res["state"], model.STATE_LESS)
        self.assertEqual(res["hits"],
                         [model.REASON_TODAY_VOLUME, model.REASON_INTENT_BELOW])
        self.assertEqual(res["reason"], model.REASON_TODAY_VOLUME)

    def test_hard_gate_still_skips_the_intent_gate(self) -> None:
        res = engage.decide(make_input(now=NIGHT, intent=model.INTENT_PROACTIVE,
                                       intents={"pass": False}))
        self.assertEqual(res["state"], model.STATE_SILENT)
        self.assertEqual(res["hits"], [model.REASON_NIGHT])
        self.assertTrue(res["soft_skipped"])
        self.assertEqual(res["soft"], [])

    def test_broken_intents_value_is_ignored(self) -> None:
        res = engage.decide(make_input())
        self.assertIsNone(res["intents"])
        res = engage.decide(make_input(intents={}))  # type: ignore[arg-type]
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["state"], model.STATE_SPEAK)

    def test_decide_does_not_read_the_weights_table(self) -> None:
        text = Path(engage.__file__).read_text(encoding="utf-8")
        for banned in ("engage_weights", "import intent", "from organs.heart import intent"):
            self.assertNotIn(banned, text, "decide 该保持纯函数：%s" % banned)


class ProactiveMutedGateTest(unittest.TestCase):
    """第 6 道软门：主动开口连着没人理 → 只关**主动**那一侧。

    muted 是 proactive.is_muted() 的返回，调用方传进来；decide 仍然不读盘、不改 streak。
    这一道是单向的：被动回复永不受它影响——被叫了就得答，她安静的是"没人理还继续说"。
    """

    MUTED = {"muted": True, "remaining_min": 720.0, "streak": 3, "muted_until": None}
    FREE = {"muted": False, "remaining_min": 0.0, "streak": 0, "muted_until": None}

    def test_proactive_is_silent_while_muted(self) -> None:
        res = engage.decide(make_input(intent=model.INTENT_PROACTIVE, muted=self.MUTED))
        self.assertEqual(res["state"], model.STATE_SILENT)
        self.assertEqual(res["reason"], model.REASON_PROACTIVE_MUTED)
        self.assertEqual(res["hits"], [model.REASON_PROACTIVE_MUTED])
        self.assertEqual(res["soft"], [model.REASON_PROACTIVE_MUTED])
        self.assertFalse(res["soft_skipped"])
        self.assertEqual([], res["hard"])

    def test_passive_is_never_touched_in_the_same_moment(self) -> None:
        res = engage.decide(make_input(intent=model.INTENT_PASSIVE, muted=self.MUTED))
        self.assertEqual(res["state"], model.STATE_SPEAK, "被叫了就得答")
        self.assertEqual(res["hits"], [])
        self.assertIsNone(res["reason"])

    def test_unmuted_proactive_speaks(self) -> None:
        res = engage.decide(make_input(intent=model.INTENT_PROACTIVE, muted=self.FREE))
        self.assertEqual(res["state"], model.STATE_SPEAK)
        self.assertEqual(res["hits"], [])

    def test_no_muted_readout_means_no_sixth_gate(self) -> None:
        res = engage.decide(make_input(intent=model.INTENT_PROACTIVE))
        self.assertEqual(res["state"], model.STATE_SPEAK)
        self.assertEqual(res["hits"], [])

    def test_broken_muted_shapes_are_ignored(self) -> None:
        for broken in ({}, {"muted": "yes"}, {"muted": 1}, [], "muted", None):
            res = engage.decide(make_input(intent=model.INTENT_PROACTIVE, muted=broken))
            self.assertEqual(res["hits"], [], broken)
            self.assertEqual(res["state"], model.STATE_SPEAK, broken)

    def test_reason_is_in_soft_reasons(self) -> None:
        self.assertIn(model.REASON_PROACTIVE_MUTED, model.SOFT_REASONS)
        self.assertIn(model.REASON_PROACTIVE_MUTED, model.REASONS)
        self.assertNotIn(model.REASON_PROACTIVE_MUTED, model.HARD_REASONS)

    def test_it_stacks_with_other_soft_gates_and_wins_as_the_strongest(self) -> None:
        res = engage.decide(make_input(today_count=20, intent=model.INTENT_PROACTIVE,
                                       muted=self.MUTED))
        self.assertEqual(res["state"], model.STATE_SILENT)
        self.assertEqual(res["reason"], model.REASON_PROACTIVE_MUTED)
        self.assertEqual(res["hits"],
                         [model.REASON_TODAY_VOLUME, model.REASON_PROACTIVE_MUTED])

    def test_hard_gate_still_short_circuits_the_sixth_gate(self) -> None:
        res = engage.decide(make_input(now=NIGHT, intent=model.INTENT_PROACTIVE,
                                       muted=self.MUTED))
        self.assertEqual(res["state"], model.STATE_SILENT)
        self.assertEqual(res["hits"], [model.REASON_NIGHT])
        self.assertTrue(res["soft_skipped"])
        self.assertEqual(res["soft"], [])

    def test_caller_supplied_readout_comes_back_unchanged(self) -> None:
        res = engage.decide(make_input(intent=model.INTENT_PROACTIVE, muted=self.MUTED))
        self.assertEqual(res["readouts"]["intent"], model.INTENT_PROACTIVE)

    def test_decide_does_not_read_the_proactive_file(self) -> None:
        text = Path(engage.__file__).read_text(encoding="utf-8")
        for banned in ("import proactive", "PROACTIVE_FILE", "proactive.sent",
                       "proactive.outcome", "proactive.the_other_side_spoke"):
            self.assertFalse(banned in text, "decide 该保持纯函数：%s" % banned)


if __name__ == "__main__":
    unittest.main()
