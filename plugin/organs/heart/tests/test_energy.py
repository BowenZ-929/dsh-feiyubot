"""精力的验收：一条时间轴、切片积分、读时结算、连发与长回复有界、重启不重复扣。

判据：下文的数都是默认值派生的期望，不是验收线——改 model 里的数，期望跟着改。
所有用例都在 helpers.make_root 的临时目录里跑，绝不写真 data。
时刻一律用本机时区造：14:00 醒着、01:00 在睡眠窗口里。
"""
from __future__ import annotations

import importlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from organs.heart import energy, model
from organs.heart.tests import helpers


def _local(hour: int = 14) -> datetime:
    tz = datetime.now().astimezone().tzinfo
    return datetime(2025, 1, 15, hour, 0, 0, tzinfo=tz)


def _at(hours: float = 0.0, base: Optional[datetime] = None) -> str:
    return model.iso((base or _local()) + timedelta(hours=hours))


def _slice_walk(
    value: float, start_iso: str, end_iso: str, window: Any = None
) -> float:
    """参照实现：关掉夹断、一片一片地走完整个 Δt。

    刻意不经过 _settle（那条路现在会按整天折算），而是直接按 ENERGY_SLICE_MIN 逐片
    调 energy._step——这就是"没有步数上限时必须走的那条路"，用来钉住折算不改变结果。
    """
    cursor = model.parse_ts(start_iso)
    end = model.parse_ts(end_iso)
    remaining = (end - cursor).total_seconds() / 60.0
    slice_min = float(model.ENERGY_SLICE_MIN)
    band = energy.DEFAULT_SLEEP_WINDOW if window is None else window
    while remaining > 1e-9:
        take = min(slice_min, remaining)
        value = energy._step(value, cursor, take, band)
        cursor = cursor + timedelta(minutes=take)
        remaining -= take
    return value


class EnergyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)
        energy._SEEN_BURSTS.clear()

    # ---- 小工具 ----------------------------------------------------------

    def _seed(
        self,
        value: float,
        updated_at: str,
        deducted: float = 0.0,
        day: Optional[str] = None,
        last_burst_at: Optional[str] = None,
    ) -> None:
        data = {
            "version": model.VERSION,
            "value": value,
            "updated_at": updated_at,
            "day": day or model.day_key(updated_at),
            "deducted": deducted,
            "last_burst_at": last_burst_at,
        }
        helpers.write_raw(Path(self.root) / energy.ENERGY_FILE, json.dumps(data))

    def _state(self) -> Dict[str, Any]:
        return helpers.read_json_file(Path(self.root) / energy.ENERGY_FILE)

    def _raw(self) -> str:
        return (Path(self.root) / energy.ENERGY_FILE).read_text(encoding="utf-8")

    # ---- 验收 1：时间前推一天 --------------------------------------------

    def test_day_gap_stays_in_range_and_explains_itself(self) -> None:
        """验收 1：updated_at 前推 24 小时、value=80。

        14:00 起算：醒 14:00–23:00 共 9h × (−4/h) = −36 → 44；
        睡 23:00–08:00 共 9h × (+1/min) 一路顶到 100；
        醒 08:00–14:00 共 6h × (−4/h) = −24 → 76。
        """
        start = _at()
        old = _at(-24)
        self._seed(80.0, old)
        got = energy.read(self.root, now=start)
        self.assertAlmostEqual(76.0, got["value"], places=1)
        self.assertGreaterEqual(got["value"], float(model.ENERGY_MIN))
        self.assertLessEqual(got["value"], float(model.ENERGY_MAX))
        self.assertFalse(got["capped"])
        self.assertAlmostEqual(
            float(model.ENERGY_MAX_SETTLE_H), got["hours"], places=2
        )
        self.assertEqual(old, got["settled_from"])
        self.assertEqual(start, got["settled_to"])
        self.assertEqual(start, self._state()["updated_at"], "结算没把起点推到 now")

    # ---- 验收 2：读时算，不是跑出来的 ------------------------------------

    def test_same_now_twice_is_the_same_value(self) -> None:
        """验收 2：同一个 now 连读两次，值不变（差 < 0.05）。"""
        start = _at()
        self._seed(80.0, _at(-6))
        first = energy.read(self.root, now=start)
        second = energy.read(self.root, now=start)
        self.assertLess(abs(first["value"] - second["value"]), 0.05)
        self.assertAlmostEqual(0.0, second["hours"], places=2)

    def test_reimport_rereads_the_same_value(self) -> None:
        """验收 2 的后半：把模块重新 import 一遍，同一个 now 还是同一个值。"""
        start = _at()
        self._seed(80.0, _at(-6))
        first = energy.read(self.root, now=start)
        reloaded = importlib.reload(energy)
        self.addCleanup(importlib.reload, energy)
        second = reloaded.read(self.root, now=start)
        self.assertLess(abs(first["value"] - second["value"]), 0.05)
        self.assertEqual(first["value"], second["value"])

    # ---- 验收 3：重启不重复扣 --------------------------------------------

    def test_restart_deducts_only_the_new_gap(self) -> None:
        """验收 3：只留 30 分钟差 → 只扣 2；再读一次 → 不再扣。"""
        start = _at()
        self._seed(80.0, _at(-0.5))
        first = energy.read(self.root, now=start)
        self.assertAlmostEqual(78.0, first["value"], places=1)
        self.assertAlmostEqual(0.5, first["hours"], places=2)

        second = energy.read(self.root, now=start)
        self.assertAlmostEqual(78.0, second["value"], places=1)
        self.assertAlmostEqual(0.0, second["hours"], places=2)
        self.assertAlmostEqual(78.0, self._state()["value"], places=1)

    def test_gap_below_min_does_not_touch_the_file(self) -> None:
        """Δ 小于 ENERGY_MIN_GAP_MIN 连文件都不写。"""
        start = _at()
        self._seed(55.0, _at(-0.05))  # 3 分钟
        before = self._raw()
        got = energy.read(self.root, now=start)
        self.assertAlmostEqual(55.0, got["value"], places=1)
        self.assertAlmostEqual(0.0, got["hours"], places=2)
        self.assertEqual(before, self._raw(), "没到最小间隔却改了文件")

    def test_sleep_window_recovers(self) -> None:
        """睡着的两小时按分钟回血，封顶 100。"""
        base = _local(23)
        start = _at(2, base)
        self._seed(20.0, _at(0, base))
        got = energy.read(self.root, now=start)
        self.assertAlmostEqual(float(model.ENERGY_MAX), got["value"], places=1)

    def test_thirty_days_ago_is_settled_by_folding(self) -> None:
        """30 天前：一天逐片、其余整天折算——整段算得完，步数有上限，结果与逐片相同。

        步数上限 ENERGY_STEP_CAP 就是一天的量，所以 steps ⇒ 一天；剩下的 29 天整天
        折算。逐片走 30 天要 8640 片，夹断后只走 288 片。
        """
        start = _at()
        old = _at(-24 * 30)
        self._seed(80.0, old)
        naive_steps = int(30 * 24 * 60 / float(model.ENERGY_SLICE_MIN))

        began = time.perf_counter()
        got = energy.read(self.root, now=start)
        elapsed_ms = (time.perf_counter() - began) * 1000.0

        self.assertTrue(got["capped"])
        self.assertEqual(energy.ENERGY_STEP_CAP, got["steps"], "步进没停在步数上限")
        self.assertEqual(29, got["folded_days"], "剩下的整天没折算")
        self.assertAlmostEqual(30 * 24.0, got["hours"], places=2)
        self.assertEqual(start, self._state()["updated_at"])
        self.assertLess(
            abs(got["value"] - _slice_walk(80.0, old, start)),
            0.05,
            "折算出来的值与逐片积分不一致",
        )
        self.assertLess(got["steps"] * 10, naive_steps, "夹断没把步数压下来")
        self.assertLess(elapsed_ms, 100.0, "夹断后读一次仍要 %.2f ms" % elapsed_ms)

        follow = energy.read(self.root, now=start)
        self.assertAlmostEqual(0.0, follow["hours"], places=2)
        self.assertEqual(0, follow["folded_days"])

    def test_folding_matches_slice_by_slice(self) -> None:
        """1 天 / 1.5 天 / 3 天：夹断后的 read 与关掉夹断的逐片积分差 < 0.05。

        1 天正好一天，不许走进折算（folded_days=0）；1.5 天 = 一天 + 半天尾巴；
        51 小时 = 一天 + 一天整 + 3 小时尾巴（折算与切片两种走法混在一次调用里）；
        3 天 = 一天 + 两天整。默认窗口一天就顶到 100 附近，算术压不出差别，所以再补
        一组 (1, 2)（一天只睡一小时）——值落在中间、还会撞 0 的下限，折算里那道上下限
        才真被压到。
        """
        for hours in (24, 36, 51, 72):
            for band, seeds in (
                (None, (5.0, 50.0, 80.0)),
                ((1, 2), (5.0, 60.0, 95.0)),
            ):
                for seed in seeds:
                    start = _at()
                    old = _at(-hours)
                    self._seed(seed, old)
                    got = energy.read(self.root, now=start, sleep_window=band)
                    want = _slice_walk(seed, old, start, band)
                    where = "%s 小时、起点 %s、窗口 %r" % (hours, seed, band)
                    self.assertLess(
                        abs(got["value"] - want), 0.05, "%s 折算偏了" % where
                    )
                    if hours == 24:
                        self.assertEqual(0, got["folded_days"], "%s 不该折算" % where)
                        self.assertEqual(
                            energy.ENERGY_STEP_CAP, got["steps"], where
                        )
                    if hours == 51:
                        self.assertEqual(1, got["folded_days"], where)
                        tail = int(3 * 60 / float(model.ENERGY_SLICE_MIN))
                        self.assertEqual(
                            energy.ENERGY_STEP_CAP + tail, got["steps"], where
                        )

    def test_tier_words_move_with_the_value(self) -> None:
        """四档：累 / 偏累 / 一般 / 充沛。"""
        cases = ((10.0, "累"), (40.0, "偏累"), (60.0, "一般"), (90.0, "充沛"))
        for value, word in cases:
            start = _at()
            self._seed(value, start)
            self.assertEqual(word, energy.read(self.root, now=start)["tier"])

    # ---- 连发 / 长回复 ---------------------------------------------------

    def test_burst_dedupes_one_event_id(self) -> None:
        """一次连发只扣一次：同一个 event_id 再来不算。"""
        start = _at()
        self._seed(80.0, start)
        first = energy.apply_burst(self.root, now=start, event_id="evt-1")
        self.assertTrue(first["ok"])
        self.assertAlmostEqual(float(model.ENERGY_BURST_COST), first["cost"], places=1)
        second = energy.apply_burst(self.root, now=start, event_id="evt-1")
        self.assertFalse(second["ok"])
        self.assertEqual("duplicate", second["skipped"])
        self.assertAlmostEqual(0.0, second["cost"], places=1)
        self.assertAlmostEqual(
            float(model.ENERGY_BURST_COST), self._state()["deducted"], places=2
        )

    def test_burst_dedupes_within_the_same_minute(self) -> None:
        """没有 event_id 时，同一分钟里的第二次连发不再扣。"""
        start = _at()
        self._seed(80.0, start)
        energy.apply_burst(self.root, now=start)
        later = model.iso(_local() + timedelta(seconds=40))
        second = energy.apply_burst(self.root, now=later)
        self.assertEqual("duplicate", second["skipped"])
        self.assertAlmostEqual(
            float(model.ENERGY_BURST_COST), self._state()["deducted"], places=2
        )

    def test_burst_respects_the_daily_cap(self) -> None:
        """单日封顶：2 + 2 + 2 第三次只落 1（正好扣满 ENERGY_DAILY_CAP）。"""
        start = _at()
        self._seed(80.0, start)
        costs = []
        for index in range(3):
            got = energy.apply_burst(
                self.root, now=_at(index), event_id="burst-%d" % index
            )
            costs.append(got["cost"])
        self.assertAlmostEqual(2.0, costs[0], places=1)
        self.assertAlmostEqual(2.0, costs[1], places=1)
        self.assertAlmostEqual(1.0, costs[2], places=1, msg="没有扣到封顶就停了")
        self.assertAlmostEqual(
            float(model.ENERGY_DAILY_CAP), self._state()["deducted"], places=2
        )
        exhausted = energy.apply_burst(self.root, now=_at(4), event_id="burst-x")
        self.assertEqual("capped", exhausted["skipped"])

    def test_long_reply_threshold(self) -> None:
        """过了门槛才扣，短回复一分不扣。"""
        start = _at()
        self._seed(80.0, start)
        short = energy.apply_long_reply(
            int(model.ENERGY_LONG_REPLY_CHARS), self.root, now=start
        )
        self.assertAlmostEqual(0.0, short["cost"], places=1)
        long_one = energy.apply_long_reply(
            int(model.ENERGY_LONG_REPLY_CHARS) + 1, self.root, now=start
        )
        self.assertAlmostEqual(
            float(model.ENERGY_LONG_REPLY_COST), long_one["cost"], places=1
        )
        self.assertAlmostEqual(
            float(model.ENERGY_WAKE_DEFAULT) - float(model.ENERGY_LONG_REPLY_COST),
            long_one["value"],
            places=1,
        )

    def test_long_reply_respects_the_daily_cap(self) -> None:
        """长回复也进同一天的封顶账。"""
        start = _at()
        self._seed(80.0, start, deducted=float(model.ENERGY_DAILY_CAP))
        got = energy.apply_long_reply(
            int(model.ENERGY_LONG_REPLY_CHARS) + 50, self.root, now=start
        )
        self.assertEqual("capped", got["skipped"])
        self.assertAlmostEqual(80.0, got["value"], places=1)

    def test_day_rollover_resets_deducted(self) -> None:
        """昨天的扣减不带进今天。"""
        start = _at()
        self._seed(
            80.0,
            start,
            deducted=float(model.ENERGY_DAILY_CAP),
            day=model.day_key(_at(-24)),
        )
        got = energy.apply_burst(self.root, now=start, event_id="rollover")
        self.assertTrue(got["ok"], "跨日了还按昨天的扣除额封顶")
        self.assertAlmostEqual(
            float(model.ENERGY_BURST_COST), self._state()["deducted"], places=2
        )

    # ---- 其它 ------------------------------------------------------------

    def test_overview_adds_deducted(self) -> None:
        start = _at()
        self._seed(80.0, start, deducted=3.0)
        got = energy.overview(self.root, now=start)
        self.assertIn("deducted", got)
        self.assertAlmostEqual(3.0, got["deducted"], places=2)
        for key in (
            "value",
            "tier",
            "settled_from",
            "settled_to",
            "hours",
            "capped",
            "steps",
            "folded_days",
        ):
            self.assertIn(key, got)

    def test_bad_json_falls_back_to_defaults(self) -> None:
        """坏 JSON 回默认：不炸，也不把文件当成"没有过"再扣一遍。"""
        helpers.write_raw(Path(self.root) / energy.ENERGY_FILE, "{不是 JSON")
        got = energy.read(self.root, now=_at())
        self.assertAlmostEqual(
            float(model.ENERGY_WAKE_DEFAULT), got["value"], places=1
        )

    def test_missing_file_is_initialised_once(self) -> None:
        """第一次读：初值 ENERGY_WAKE_DEFAULT，并把时钟起点记下来。"""
        start = _at()
        got = energy.read(self.root, now=start)
        self.assertAlmostEqual(
            float(model.ENERGY_WAKE_DEFAULT), got["value"], places=1
        )
        self.assertEqual(start, self._state()["updated_at"])
        self.assertAlmostEqual(0.0, got["hours"], places=2)


# ---- 作息窗口：可注入，默认值逐字不变 ------------------------------------

_TZ = datetime.now().astimezone().tzinfo


def _ts(day: int, hour: int, minute: int = 0) -> str:
    """golden 表的固定时刻：2025-01 某天某点，只有挂钟小时参与结算。"""
    return model.iso(datetime(2025, 1, day, hour, minute, 0, tzinfo=_TZ))


def _seed_state(
    root: Any, value: float, updated_at: str, deducted: float = 0.0
) -> None:
    """按给定的 value / updated_at 造一份状态文件（golden 与窗口用例共用）。"""
    helpers.write_raw(
        Path(root) / energy.ENERGY_FILE,
        json.dumps(
            {
                "version": model.VERSION,
                "value": value,
                "updated_at": updated_at,
                "day": model.day_key(updated_at),
                "deducted": deducted,
                "last_burst_at": None,
            }
        ),
    )


# 12 组 golden：改窗口注入之前，用采集脚本在同一批旧数据上跑出来的期望值，
# 逐字段固化（当时两版逐字段 diff 的差异处数是 0）。以后谁把结算改坏了，这里就红。
GOLDEN = (
    {
        "name": "01_forward_24h_from_14",
        "seed": (80.0, _ts(14, 14)),
        "steps": (
            (
                "read",
                {"now": _ts(15, 14)},
                {"value": 76.0, "tier": "充沛", "hours": 24.0, "capped": False},
            ),
        ),
    },
    {
        "name": "02_forward_24h_from_10",
        "seed": (80.0, _ts(14, 10)),
        "steps": (
            (
                "read",
                {"now": _ts(15, 10)},
                {"value": 92.0, "tier": "充沛", "hours": 24.0, "capped": False},
            ),
        ),
    },
    {
        "name": "03_gap_30min",
        "seed": (80.0, _ts(15, 13, 30)),
        "steps": (
            (
                "read",
                {"now": _ts(15, 14)},
                {"value": 78.0, "tier": "充沛", "hours": 0.5, "capped": False},
            ),
        ),
    },
    {
        "name": "04_across_midnight",
        "seed": (80.0, _ts(15, 21)),
        "steps": (
            (
                "read",
                {"now": _ts(16, 2)},
                {"value": 100.0, "tier": "充沛", "hours": 5.0, "capped": False},
            ),
        ),
    },
    {
        "name": "05_sleep_window_repeated_reads",
        "seed": (20.0, _ts(15, 23)),
        "steps": (
            (
                "read",
                {"now": _ts(15, 23, 30)},
                {"value": 50.0, "tier": "一般", "hours": 0.5, "capped": False},
            ),
            (
                "read",
                {"now": _ts(16, 0)},
                {"value": 80.0, "tier": "充沛", "hours": 0.5, "capped": False},
            ),
            (
                "read",
                {"now": _ts(16, 1)},
                {"value": 100.0, "tier": "充沛", "hours": 1.0, "capped": False},
            ),
        ),
    },
    {
        "name": "06_burst_twice",
        "seed": (80.0, _ts(15, 14)),
        "steps": (
            (
                "apply_burst",
                {"now": _ts(15, 14), "event_id": "evt-1"},
                {"value": 78.0, "tier": "充沛", "hours": 0.0, "capped": False,
                 "cost": 2.0, "skipped": None, "ok": True, "deducted": 2.0},
            ),
            (
                "apply_burst",
                {"now": _ts(15, 14), "event_id": "evt-1"},
                {"value": 78.0, "tier": "充沛", "hours": 0.0, "capped": False,
                 "cost": 0.0, "skipped": 'duplicate', "ok": False, "deducted": 2.0},
            ),
            (
                "apply_burst",
                {"now": _ts(15, 14), "event_id": "evt-2"},
                {"value": 78.0, "tier": "充沛", "hours": 0.0, "capped": False,
                 "cost": 0.0, "skipped": 'duplicate', "ok": False, "deducted": 2.0},
            ),
        ),
    },
    {
        "name": "07_long_reply",
        "seed": (80.0, _ts(15, 14)),
        "steps": (
            (
                "apply_long_reply",
                {"chars": 201, "now": _ts(15, 14)},
                {"value": 79.0, "tier": "充沛", "hours": 0.0, "capped": False,
                 "cost": 1.0, "skipped": None, "ok": True, "deducted": 1.0},
            ),
            (
                "apply_long_reply",
                {"chars": 200, "now": _ts(15, 14)},
                {"value": 79.0, "tier": "充沛", "hours": 0.0, "capped": False,
                 "cost": 0.0, "skipped": None, "ok": True, "deducted": 1.0},
            ),
        ),
    },
    {
        "name": "08_daily_cap",
        "seed": (80.0, _ts(15, 14)),
        "steps": (
            (
                "apply_burst",
                {"now": _ts(15, 14), "event_id": "evt-a"},
                {"value": 78.0, "tier": "充沛", "hours": 0.0, "capped": False,
                 "cost": 2.0, "skipped": None, "ok": True, "deducted": 2.0},
            ),
            (
                "apply_burst",
                {"now": _ts(15, 15), "event_id": "evt-b"},
                {"value": 72.0, "tier": "一般", "hours": 1.0, "capped": False,
                 "cost": 2.0, "skipped": None, "ok": True, "deducted": 4.0},
            ),
            (
                "apply_burst",
                {"now": _ts(15, 16), "event_id": "evt-c"},
                {"value": 67.0, "tier": "一般", "hours": 1.0, "capped": False,
                 "cost": 1.0, "skipped": None, "ok": True, "deducted": 5.0},
            ),
            (
                "apply_burst",
                {"now": _ts(15, 17), "event_id": "evt-d"},
                {"value": 63.0, "tier": "一般", "hours": 1.0, "capped": False,
                 "cost": 0.0, "skipped": 'capped', "ok": False, "deducted": 5.0},
            ),
        ),
    },
    {
        "name": "09_start_at_23",
        "seed": (80.0, _ts(15, 23)),
        "steps": (
            (
                "read",
                {"now": _ts(16, 0)},
                {"value": 100.0, "tier": "充沛", "hours": 1.0, "capped": False},
            ),
        ),
    },
    {
        "name": "10_start_at_08",
        "seed": (80.0, _ts(15, 8)),
        "steps": (
            (
                "read",
                {"now": _ts(15, 9)},
                {"value": 76.0, "tier": "充沛", "hours": 1.0, "capped": False},
            ),
        ),
    },
    {
        "name": "11_start_at_100",
        "seed": (100.0, _ts(15, 14)),
        "steps": (
            (
                "read",
                {"now": _ts(15, 16)},
                {"value": 92.0, "tier": "充沛", "hours": 2.0, "capped": False},
            ),
            (
                "overview",
                {"now": _ts(15, 16)},
                {"value": 92.0, "tier": "充沛", "hours": 0.0, "capped": False,
                 "deducted": 0.0},
            ),
        ),
    },
    {
        "name": "12_start_at_0",
        "seed": (0.0, _ts(15, 14)),
        "steps": (
            (
                "read",
                {"now": _ts(15, 16)},
                {"value": 0.0, "tier": "累", "hours": 2.0, "capped": False},
            ),
            (
                "overview",
                {"now": _ts(15, 17)},
                {"value": 0.0, "tier": "累", "hours": 1.0, "capped": False,
                 "deducted": 0.0},
            ),
        ),
    },
)


class EnergyGoldenTest(unittest.TestCase):
    """不注入时的结算必须与旧版逐字段一致：12 组场景全部复算一遍。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)

    def test_golden_table_matches(self) -> None:
        for scn in GOLDEN:
            energy._SEEN_BURSTS.clear()
            _seed_state(self.root, scn["seed"][0], scn["seed"][1])
            for index, (fn, kwargs, expect) in enumerate(scn["steps"]):
                call = dict(kwargs)
                chars = call.pop("chars", None)
                if fn == "apply_long_reply":
                    got = energy.apply_long_reply(chars, self.root, **call)
                elif fn == "apply_burst":
                    got = energy.apply_burst(self.root, **call)
                else:
                    got = getattr(energy, fn)(self.root, **call)
                where = "%s 第 %d 步 %s" % (scn["name"], index + 1, fn)
                for key, want in expect.items():
                    if isinstance(want, float):
                        self.assertAlmostEqual(
                            want, got[key], places=4, msg="%s 的 %s 变了" % (where, key)
                        )
                    else:
                        self.assertEqual(want, got[key], "%s 的 %s 变了" % (where, key))


# 模块级大写名字的白名单：除 DEFAULT_SLEEP_WINDOW 与派生量 ENERGY_STEP_CAP 外与
# 改动前完全相同；去重不设过期时间，所以这里也不许再出现 _SEEN_BURSTS_TTL_S 那种
# 第二个时钟。ENERGY_STEP_CAP 不是新常数，是 ENERGY_MAX_SETTLE_H / ENERGY_SLICE_MIN
# 的派生量（下面有用例钉住它确实由既有常数算出）。
_ALLOWED_UPPER_NAMES = (
    "DEFAULT_SLEEP_WINDOW",
    "ENERGY_FILE",
    "ENERGY_STEP_CAP",
    "_ENERGY_TIERS",
    "_SEEN_BURSTS",
    "_SEEN_BURSTS_MAX",
)


class SleepWindowTest(unittest.TestCase):
    """作息窗口可注入：默认值不动，注进来的是什么档就按什么结。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)
        energy._SEEN_BURSTS.clear()

    def _four_callables(self, bad: Any) -> Any:
        now = _ts(16, 4)
        return (
            lambda: energy.read(self.root, now=now, sleep_window=bad),
            lambda: energy.overview(self.root, now=now, sleep_window=bad),
            lambda: energy.apply_burst(self.root, now=now, sleep_window=bad),
            lambda: energy.apply_long_reply(999, self.root, now=now, sleep_window=bad),
        )

    def test_default_window_is_the_model_default(self) -> None:
        """默认值仍然只有一个出处：model 的两个小时数。"""
        self.assertEqual(
            (int(model.ENERGY_SLEEP_START_H), int(model.ENERGY_SLEEP_END_H)),
            energy.DEFAULT_SLEEP_WINDOW,
        )

    def test_none_means_the_default_window(self) -> None:
        """不传 / 传 None / 传默认值，三条路必须同一个数。"""
        seen = []
        for extra in ({}, {"sleep_window": None}, {"sleep_window": (23, 8)}):
            _seed_state(self.root, 80.0, _ts(15, 20))
            seen.append(energy.read(self.root, now=_ts(16, 9), **extra)["value"])
        self.assertEqual(1, len(set(seen)), "不传和传默认值结果不一样：%r" % (seen,))

    def test_injected_window_moves_the_settlement(self) -> None:
        """同一个起点、同一段时长，换档结算就变。

        20:00→次日 09:00：默认 (23,8)：醒 3h → 68、睡 9h 顶到 100、醒 1h → 96；
        (22,6)：醒 2h → 72、睡 8h → 100、醒 3h → 88；
        (1,5)：醒 5h → 60、睡 4h → 100、醒 4h → 84。
        """
        for window, want in ((None, 96.0), ((23, 8), 96.0), ((22, 6), 88.0), ((1, 5), 84.0)):
            _seed_state(self.root, 80.0, _ts(15, 20))
            got = energy.read(self.root, now=_ts(16, 9), sleep_window=window)
            self.assertAlmostEqual(want, got["value"], places=1, msg=str(window))

    def test_same_span_over_a_whole_day(self) -> None:
        """12:00→次日 12:00 整整一天：默认 84、(22,6) 76、(1,5) 72。"""
        for window, want in ((None, 84.0), ((22, 6), 76.0), ((1, 5), 72.0)):
            _seed_state(self.root, 80.0, _ts(15, 12))
            got = energy.read(self.root, now=_ts(16, 12), sleep_window=window)
            self.assertAlmostEqual(want, got["value"], places=1, msg=str(window))

    def test_window_that_does_not_cross_midnight(self) -> None:
        """start < end 时窗口就是 [start, end)，不是"全天"。

        (13,15) 的 13:00–15:00 才睡：50 + 30 分钟 = 80；默认窗口下这段时间是醒的 → 48。
        """
        for window, want in (((13, 15), 80.0), (None, 48.0)):
            _seed_state(self.root, 50.0, _ts(15, 13, 30))
            got = energy.read(self.root, now=_ts(15, 14), sleep_window=window)
            self.assertAlmostEqual(want, got["value"], places=1, msg=str(window))

    def test_burst_long_reply_and_overview_take_the_window_too(self) -> None:
        """四个公开函数都收这个档：连发 / 长回复 / 总览的结算段跟着窗口走。

        (22,6)：21:00–22:00 醒 1h → 76，22:00–22:30 睡 +30 → 100，再 −2 → 98；
        默认窗口（23 点才睡）：21:00–22:30 一直醒 → −6 → 74，再 −2 → 72。
        总览 / 长回复：(1,5) 时 06:00–07:00 是醒的 → 76（长回复再 −1 → 75）；
        默认窗口 06:00 还在睡 → 100。
        """
        _seed_state(self.root, 80.0, _ts(15, 21))
        got = energy.apply_burst(
            self.root, now=_ts(15, 22, 30), sleep_window=(22, 6), event_id="win-1"
        )
        self.assertAlmostEqual(98.0, got["value"], places=1)

        _seed_state(self.root, 80.0, _ts(15, 21))
        got = energy.apply_burst(self.root, now=_ts(15, 22, 30), event_id="win-2")
        self.assertAlmostEqual(72.0, got["value"], places=1)

        _seed_state(self.root, 80.0, _ts(15, 6))
        overview = energy.overview(self.root, now=_ts(15, 7), sleep_window=(1, 5))
        self.assertAlmostEqual(76.0, overview["value"], places=1)

        _seed_state(self.root, 80.0, _ts(15, 6))
        overview = energy.overview(self.root, now=_ts(15, 7))
        self.assertAlmostEqual(float(model.ENERGY_MAX), overview["value"], places=1)

        _seed_state(self.root, 80.0, _ts(15, 6))
        reply = energy.apply_long_reply(999, self.root, now=_ts(15, 7), sleep_window=(1, 5))
        self.assertAlmostEqual(
            76.0 - float(model.ENERGY_LONG_REPLY_COST), reply["value"], places=1
        )

    def test_bad_window_raises_value_error(self) -> None:
        """坏窗口不许糊过去：说清该传什么，并把坏值原样带出来。"""
        for bad in ((24, 8), (8, 8), "23-8", (23,)):
            for call in self._four_callables(bad):
                with self.assertRaises(ValueError) as ctx:
                    call()
                message = str(ctx.exception)
                self.assertIn(repr(bad), message, "消息没原样带出坏值：%s" % message)
                self.assertIn("(start_h, end_h)", message)
                self.assertIn("0–23", message)

    def test_no_new_module_level_constants(self) -> None:
        """大写名字只许增 DEFAULT_SLEEP_WINDOW：不许冒出第二个半衰 / delay / 间隔常数。"""
        upper = sorted(n for n in dir(energy) if n.isupper())
        self.assertEqual(sorted(_ALLOWED_UPPER_NAMES), upper)
        for name in upper:
            self.assertNotIn("TTL", name.upper(), "又是过期常数：%s" % name)
            self.assertNotIn("EXPIRE", name.upper(), "又是过期常数：%s" % name)

    def test_step_cap_is_derived_not_hardcoded(self) -> None:
        """步数上限是派生量：= ENERGY_MAX_SETTLE_H 小时 ÷ 每片 ENERGY_SLICE_MIN 分钟。"""
        self.assertEqual(
            int(
                float(model.ENERGY_MAX_SETTLE_H)
                * 60.0
                / float(model.ENERGY_SLICE_MIN)
            ),
            energy.ENERGY_STEP_CAP,
        )
        self.assertEqual(
            int(model.ENERGY_MAX_SETTLE_H * 60 / model.ENERGY_SLICE_MIN),
            energy.ENERGY_STEP_CAP,
            "换成整数算也必须是同一个数",
        )

    def test_one_event_id_never_deducts_twice(self) -> None:
        """幂等键不随时间失效：同一 event_id 隔 30 分钟再调仍然是 duplicate。

        本模块只有 settle 一把时间尺，没有"多久之后忘掉它"的第二个时钟；
        换一个 event_id 才算新的一次连发。
        """
        _seed_state(self.root, 80.0, _ts(15, 14))
        first = energy.apply_burst(self.root, now=_ts(15, 14), event_id="keep-1")
        self.assertTrue(first["ok"])
        later = energy.apply_burst(self.root, now=_ts(15, 14, 30), event_id="keep-1")
        self.assertEqual("duplicate", later["skipped"], "隔了 30 分钟又把同一个 id 扣了一遍")
        self.assertAlmostEqual(0.0, later["cost"], places=1)
        self.assertAlmostEqual(
            float(first["deducted"]), later["deducted"], places=2, msg="deducted 变了"
        )
        fresh = energy.apply_burst(self.root, now=_ts(15, 14, 30), event_id="keep-2")
        self.assertTrue(fresh["ok"], "换了 event_id 还算重复？")
        self.assertAlmostEqual(
            float(model.ENERGY_BURST_COST), fresh["cost"], places=1
        )


if __name__ == "__main__":
    unittest.main()