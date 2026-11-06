"""心情的验收：两个数、两把尺、有界事件、渐近线 + 热度、读时采样、日结。

判据：下文的数都是默认值派生的期望，不是验收线——改 model 里的数，期望跟着改。
所有用例都在 helpers.make_root 的临时目录里跑，绝不写真 data。
时间一律用本机时区造，把 now 往前推就是"等了一段时间"。
"""
from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from organs.day import model as day_model
from organs.heart import events, model, mood
from organs.heart.tests import helpers

BASE_HOUR = 14

# 上一轮那两条的复刻，只给倒挂回归测试用：复刻的是"假如保留日上限"的世界，
# 不是本模块的现行规则（现行规则里没有这两个数了）。
_LEGACY_DIMINISH = 0.5


def _local(hour: int = BASE_HOUR) -> datetime:
    """本机时区的一个固定时刻：2025-01-15 <hour>:00。"""
    tz = datetime.now().astimezone().tzinfo
    return datetime(2025, 1, 15, hour, 0, 0, tzinfo=tz)


def _at(hours: float = 0.0, base: Optional[datetime] = None) -> str:
    return model.iso((base or _local()) + timedelta(hours=hours))


def _at_min(minutes: float = 0.0, base: Optional[datetime] = None) -> str:
    return model.iso((base or _local()) + timedelta(minutes=minutes))


def _at_sec(seconds: float = 0.0, base: Optional[datetime] = None) -> str:
    return model.iso((base or _local()) + timedelta(seconds=seconds))


class MoodTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)

    # ---- 小工具 ----------------------------------------------------------

    def _seed_into(
        self,
        root: Any,
        now_value: float,
        baseline: float,
        at: str,
        conflict_used: int = 0,
        window: Optional[Dict[str, str]] = None,
        day: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """种一份状态；extra 用来伪造旧文件里那两栏已退役的字段。"""
        data = {
            "version": model.VERSION,
            "now": now_value,
            "baseline": baseline,
            "updated_at": at,
            "day": day or day_model.day_key(at, cutover_hour=0),
            "conflict_used": conflict_used,
            "window": dict(window or {}),
        }
        if extra:
            data.update(extra)
        helpers.write_raw(Path(root) / mood.MOOD_FILE, json.dumps(data))

    def _seed(
        self,
        now_value: float,
        baseline: float,
        at: str,
        conflict_used: int = 0,
        window: Optional[Dict[str, str]] = None,
        day: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._seed_into(
            self.root,
            now_value,
            baseline,
            at,
            conflict_used=conflict_used,
            window=window,
            day=day,
            extra=extra,
        )

    def _state(self) -> Dict[str, Any]:
        return helpers.read_json_file(Path(self.root) / mood.MOOD_FILE)

    def _raw(self) -> str:
        return (Path(self.root) / mood.MOOD_FILE).read_text(encoding="utf-8")

    def _fresh_root(self) -> Any:
        """另开一个临时根目录（对比实验各用一份，互不串味）。"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return helpers.make_root(temp.name)

    # ---- 两把尺 ----------------------------------------------------------

    def test_now_decays_toward_baseline_in_three_hours(self) -> None:
        """now 那把尺：90 往基线 60 走，3 小时后 75（正好一半）。"""
        start = _at()
        self._seed(90, model.MOOD_BASELINE_DEFAULT, start)
        got = mood.read(self.root, now=_at(3))
        self.assertAlmostEqual(75.0, got["now"], places=1)
        self.assertAlmostEqual(60.0, got["baseline"], places=1)

    def test_baseline_has_its_own_seven_day_ruler(self) -> None:
        """baseline 那把尺：90 漂七天回一半到 75；now 早被 3 小时那把尺拉到基线附近。"""
        start = _at()
        self._seed(90, 90, start)
        got = mood.read(self.root, now=_at(float(model.MOOD_BASELINE_HALF_LIFE_H)))
        self.assertAlmostEqual(75.0, got["baseline"], places=1)
        self.assertAlmostEqual(75.0, got["now"], places=1)

    def test_read_never_writes_state(self) -> None:
        """读不写状态；没有文件也不会被读出一个 mood.json 来（只有轨迹账本）。"""
        got = mood.read(self.root, now=_at())
        self.assertFalse((Path(self.root) / mood.MOOD_FILE).exists())
        self.assertTrue((Path(self.root) / mood.TRAJ_FILE).exists())
        self.assertAlmostEqual(float(model.MOOD_BASELINE_DEFAULT), got["now"], places=1)

        start = _at()
        self._seed(80, 60, start)
        before = self._raw()
        mood.read(self.root, now=start)
        self.assertEqual(before, self._raw(), "read() 动了状态文件")

    # ---- 验收第 1 条：渐近线 + 热度 --------------------------------------

    def test_ten_praises_climb_along_the_ceiling(self) -> None:
        """验收第 1 条：同一自然日连夸 10 次（每分钟一次、每次换人绕开一小时窗口），
        增量一头大一头小，永远顶不满 100。"""
        start = _local()
        self._seed(60, 60, model.iso(start))
        rows = [
            mood.apply_event("praise", self.root, now=_at_min(index, start), who="人%d" % index)
            for index in range(10)
        ]
        for row in rows:
            self.assertTrue(row["ok"])
            self.assertGreater(row["delta"], 0.0)
            self.assertLess(row["headroom"], 1.0, "余量系数没生效，还没到天花板的加成")
            self.assertGreater(row["heat"], 0.0)
            self.assertLessEqual(row["heat"], 1.0)
        deltas = [row["delta"] for row in rows]
        self.assertEqual(sorted(deltas, reverse=True), deltas, "增量不是一路缩水")
        self.assertGreater(deltas[0], deltas[-1] * 1.5, "头几次没有明显更大")
        self.assertGreater(rows[-1]["after"], rows[0]["after"], "夸了十次还不涨")
        self.assertLess(rows[-1]["after"], float(model.MOOD_MAX), "一顿夸就顶满了")
        self.assertAlmostEqual(
            float(model.MOOD_UNIT) * (1.0 - 60.0 / float(model.MOOD_CEILING)),
            rows[0]["delta"],
            places=1,
            msg="第一次增量应是名义档 × 余量",
        )

    def test_heat_wears_off_after_the_window(self) -> None:
        """热度只看窗口：隔过 MOOD_HEAT_WINDOW_MIN 分钟再夸，折扣回到 1。"""
        start = _local()
        self._seed(60, 60, model.iso(start))
        mood.apply_event("praise", self.root, now=model.iso(start), who="甲")
        after_window = mood.apply_event(
            "praise",
            self.root,
            now=_at_min(float(model.MOOD_HEAT_WINDOW_MIN) + 1, start),
            who="乙",
        )
        self.assertTrue(after_window["ok"])
        self.assertAlmostEqual(1.0, after_window["heat"], places=4, msg="出了窗口还打折")
        inside = mood.apply_event(
            "praise",
            self.root,
            now=_at_min(float(model.MOOD_HEAT_WINDOW_MIN) + 2, start),
            who="丙",
        )
        self.assertAlmostEqual(
            1.0 / (1.0 + 1.0 / float(model.MOOD_HEAT_K)), inside["heat"], places=4
        )

    # ---- 验收第 2 条：30 条的热度曲线 ------------------------------------

    def test_thirty_events_in_one_window_shrink_monotonically(self) -> None:
        """验收第 2 条：同一类型 10 分钟内连来 30 条（每 10 秒一条、每条换人），
        增量单调不增、恒 ≥ 0，累计只涨不缩。"""
        start = _local()
        self._seed(60, 60, model.iso(start))
        rows = [
            mood.apply_event("praise", self.root, now=_at_sec(10 * index, start), who="人%d" % index)
            for index in range(30)
        ]
        for row in rows:
            self.assertTrue(row["ok"])
        deltas = [row["delta"] for row in rows]
        self.assertEqual(sorted(deltas, reverse=True), deltas, "增量曲线不是单调不增")
        for row in rows:
            self.assertGreaterEqual(row["delta"], 0.0, "出现了负增量")
        totals: list = []
        running = 60.0
        for row, delta in zip(rows, deltas):
            running += delta
            totals.append(running)
        self.assertEqual(sorted(totals), totals, "累计不是只涨不缩")
        self.assertLess(totals[-1], float(model.MOOD_MAX), "30 条就顶满了")
        self.assertGreater(totals[-1], totals[2], "第 30 条不比第 3 条高，曲线是平的")

    # ---- 验收第 3 条：倒挂回归 -------------------------------------------

    def _legacy_value(self, cap_tiers: float, count: int) -> float:
        """把"假如保留日上限"复刻一遍：第 2 次起半档 + 单日同向封顶 cap 档。

        全部落在同一时刻，单看上限本身会不会把曲线掰回去；这是对**已退役规则**的
        仿真，不是对现行 mood 的调用。
        """
        value = 60.0
        used = 0.0
        unit = float(model.MOOD_UNIT)
        for index in range(count):
            allowed = cap_tiers - used
            if allowed <= 0.0:
                break
            nominal = 1.0 if index == 0 else _LEGACY_DIMINISH
            tiers = min(nominal, allowed)
            value = min(float(model.MOOD_MAX), value + tiers * unit)
            used += tiers
        return value

    def _real_value(self, count: int) -> float:
        """现行规则真跑 count 条同向事件（同一时刻，避开衰减），返回落账后的值。"""
        root = self._fresh_root()
        at = model.iso(_local())
        self._seed_into(root, 60, 60, at)
        value = 60.0
        for index in range(count):
            got = mood.apply_event("praise", root, now=at, who="人%d" % index)
            self.assertTrue(got["ok"], "第 %d 条被挡了：%r" % (index, got.get("skipped")))
            value = got["after"]
        return value

    def test_no_threshold_makes_thirty_worse_than_three(self) -> None:
        """验收第 3 条：把"假如保留的日上限"从 1 档扫到 20 档，每档都跑 30 条同向事件，
        断言不存在任何阈值让 值(30 条) < 值(3 条)——上限只会把曲线掰平，不会掰回去。"""
        for cap in range(1, 21):
            three = self._legacy_value(float(cap), 3)
            thirty = self._legacy_value(float(cap), 30)
            self.assertGreaterEqual(
                thirty, three, "阈值 %d 档时 30 条（%.1f）反而低于 3 条（%.1f）" % (cap, thirty, three)
            )
        # 现行规则里已经没有阈值了，但同一句断言照样得成立。
        self.assertGreaterEqual(
            self._real_value(30), self._real_value(3), "新引擎 30 条比 3 条还低"
        )

    # ---- 验收第 4 条：负向不缩放 -----------------------------------------

    def test_negative_is_never_scaled(self) -> None:
        """验收第 4 条：rejected / insult 各连打 3 次（间隔 20 秒 < 热度窗口），
        delta 恒为 −20：不随次数缩水、也不被天花板折算（只受负向日上限与地板限制）。"""
        from unittest import mock

        start = _local()
        for kind, want in (("rejected", -20.0), ("insult", -20.0)):
            root = self._fresh_root()
            self._seed_into(root, 60, 60, model.iso(start))
            with mock.patch.object(model, "MOOD_NEGATIVE_DAILY_CAP", 10):
                rows = [
                    mood.apply_event(kind, root, now=_at_sec(20 * index, start), who="人%d" % index)
                    for index in range(3)
                ]
            self.assertEqual([True, True, True], [row["ok"] for row in rows])
            self.assertEqual([want, want, want], [row["delta"] for row in rows])
            self.assertEqual([1.0, 1.0, 1.0], [row["heat"] for row in rows], "负向被热度打折了")
            self.assertEqual(
                [1.0, 1.0, 1.0], [row["headroom"] for row in rows], "负向被天花板缩了"
            )
            # 20 秒的衰减把 20 往上抬了一点点，所以第三次落在 0 上而不是恰好 0。
            self.assertEqual([40.0, 20.0, 0.1], [row["after"] for row in rows])

    def test_negative_still_stops_at_the_floor(self) -> None:
        """不缩放的负增量照样被地板挡住：5 分上挨一句骂只扣到 0，不穿底。"""
        start = _local()
        self._seed(5, 5, model.iso(start))
        got = mood.apply_event("insult", self.root, now=model.iso(start), who="甲")
        self.assertAlmostEqual(float(model.MOOD_MIN), got["after"], places=1)
        self.assertAlmostEqual(-5.0, got["delta"], places=1, msg="delta 应是真实落账差")

    # ---- 旧闸 ------------------------------------------------------------

    def test_rejected_hits_twice_then_the_daily_gate_closes(self) -> None:
        """负向的"档位上限"退役了，只剩负向日上限（默认 2 次/日）：
        前两次各 −20，第三次 capped，被挡时不落账。"""
        start = _local()
        self._seed(60, 60, model.iso(start))
        results = [
            mood.apply_event("rejected", self.root, now=model.iso(start), who=who)
            for who in ("甲", "乙", "丙")
        ]
        self.assertEqual([True, True, False], [row["ok"] for row in results])
        self.assertEqual("capped", results[2]["skipped"])
        self.assertAlmostEqual(40.0, results[0]["after"], places=1)
        self.assertAlmostEqual(20.0, results[1]["after"], places=1)
        self.assertEqual(2, self._state()["conflict_used"], "只有两次真的落账")

    def test_insults_never_bottom_out(self) -> None:
        """被骂两次各 −20 到 20，第三次被日上限挡下；地板始终没碰。"""
        start = _local()
        self._seed(60, 60, model.iso(start))
        results = [
            mood.apply_event("insult", self.root, now=model.iso(start), who=who)
            for who in ("甲", "乙", "丙")
        ]
        self.assertAlmostEqual(40.0, results[0]["after"], places=1)
        self.assertAlmostEqual(20.0, results[1]["after"], places=1)
        self.assertFalse(results[2]["ok"])
        self.assertGreater(results[1]["after"], float(model.MOOD_MIN))

    def test_rejected_count_gate_still_fires(self) -> None:
        """MOOD_NEGATIVE_DAILY_CAP 那道旧闸没拆：配额用满就 capped。"""
        start = _at()
        self._seed(60, 60, start, conflict_used=int(model.MOOD_NEGATIVE_DAILY_CAP))
        got = mood.apply_event("rejected", self.root, now=start, who="甲")
        self.assertFalse(got["ok"])
        self.assertEqual("capped", got["skipped"])

    def test_unknown_event_raises(self) -> None:
        """kind 不在枚举里就抛，不静默当没事发生。"""
        with self.assertRaises(ValueError):
            mood.apply_event("bribe", self.root, now=_at())

    def test_same_who_same_hour_is_windowed(self) -> None:
        """同一人同一小时只算一次；被挡时状态一个字节都不改。"""
        start = _at()
        self._seed(60, 60, start)
        first = mood.apply_event("praise", self.root, now=start, who="主人")
        self.assertTrue(first["ok"])
        before = self._raw()
        second = mood.apply_event("praise", self.root, now=start, who="主人")
        self.assertFalse(second["ok"])
        self.assertEqual("windowed", second["skipped"])
        self.assertEqual(before, self._raw(), "被窗口挡掉还动了状态")

        next_hour = mood.apply_event("praise", self.root, now=_at(1), who="主人")
        self.assertTrue(next_hour["ok"], "换了一小时还挡着")

    def test_daily_cap_resets_with_a_new_day(self) -> None:
        """昨天用满的配额不带进今天。"""
        start = _at()
        yesterday = _at(-24)
        self._seed(60, 60, start, conflict_used=2, day=day_model.day_key(yesterday, cutover_hour=0))
        got = mood.apply_event("rejected", self.root, now=start, who="甲")
        self.assertTrue(got["ok"], "跨日了还按昨天的 conflict_used 封顶")

    # ---- 验收第 5 条：旧状态兼容 -----------------------------------------

    def test_old_state_with_retired_columns_still_works(self) -> None:
        """验收第 5 条：手写一份带旧 day_events / day_delta_tiers 的 mood.json，
        读到不炸、下一次夸照常落账，回写时两栏一起消失（不搬进新字段）。"""
        start = _local()
        self._seed(
            60,
            60,
            model.iso(start),
            extra={
                "day_events": {"praise": 3, "cold": 1, "rejected": 1, "insult": 0},
                "day_delta_tiers": {"positive": 2.0, "negative": 1.0},
            },
        )
        self.assertAlmostEqual(60.0, mood.read(self.root, now=model.iso(start))["now"], places=1)
        got = mood.apply_event("praise", self.root, now=_at_min(1, start), who="甲")
        self.assertTrue(got["ok"], "旧文件读炸了")
        self.assertAlmostEqual(1.0, got["heat"], places=4, msg="旧账不该再压热度")
        self.assertAlmostEqual(64.0, got["after"], places=1)
        state = self._state()
        self.assertNotIn("day_events", state)
        self.assertNotIn("day_delta_tiers", state)

    def test_retired_columns_with_garbage_are_ignored(self) -> None:
        """退役的两栏就算值坏了也不许炸：静默忽略，下一次夸照样按余量落账。"""
        start = _local()
        self._seed(
            60,
            60,
            model.iso(start),
            extra={"day_events": {"praise": "坏"}, "day_delta_tiers": {"positive": None}},
        )
        got = mood.apply_event("praise", self.root, now=_at_min(1, start), who="甲")
        self.assertTrue(got["ok"])
        self.assertAlmostEqual(64.0, got["after"], places=1)
        self.assertNotIn("day_events", self._state())

    # ---- 事件账本落账 ----------------------------------------------------

    def test_every_landed_event_leaves_one_ledger_row(self) -> None:
        """落一次账就写一行：at / kind / who / delta / after 都在；被挡的不写。"""
        start = _local()
        self._seed(60, 60, model.iso(start))
        first = mood.apply_event("praise", self.root, now=model.iso(start), who="甲")
        self.assertTrue(first["ok"])
        self.assertFalse(
            mood.apply_event("praise", self.root, now=model.iso(start), who="甲")["ok"]
        )
        rows = events.recent(self.root, now=_at_min(1, start), window_min=60.0)
        self.assertEqual(1, len(rows), "被窗口挡掉的那次也落了账")
        self.assertEqual("praise", rows[0]["kind"])
        self.assertEqual("甲", rows[0]["who"])
        self.assertAlmostEqual(4.0, rows[0]["delta"], places=2)
        self.assertAlmostEqual(64.0, rows[0]["after"], places=2)

    def test_positive_scaled_negative_not(self) -> None:
        """正向按余量缩（+4.0），负向全额扣（−10.0）：两条规矩各管一半。"""
        start = _local()
        self._seed(60, 60, model.iso(start))
        praise = mood.apply_event("praise", self.root, now=model.iso(start), who="甲")
        cold = mood.apply_event("cold", self.root, now=_at_min(1, start), who="乙")
        self.assertAlmostEqual(4.0, praise["delta"], places=1)
        self.assertAlmostEqual(-10.0, cold["delta"], places=1)
        self.assertAlmostEqual(54.0, cold["after"], places=1)

    # ---- 档位词与方向：公开函数 ------------------------------------------

    def test_tier_of_boundaries_unchanged(self) -> None:
        """五档的界与改动前一致（默认基线 60、一档 10、冰点 30）。"""
        cases = [
            (29, "冰点"),
            (30, "偏冷"),
            (49, "偏冷"),
            (50, "平常"),
            (69, "平常"),
            (70, "偏热"),
            (89, "偏热"),
            (90, "滚烫"),
        ]
        for value, want in cases:
            self.assertEqual(want, mood.tier_of(float(value)), "%s 的档界变了" % value)

    def test_public_tier_and_direction_functions(self) -> None:
        """tier_of / direction_of 是公开的（别的模块要用），私有别名不再留。"""
        self.assertEqual("平常", mood.tier_of(60.0))
        self.assertEqual("偏热", mood.direction_of(70.0, 60.0))
        self.assertEqual("偏冷", mood.direction_of(50.0, 60.0))
        self.assertEqual("中性", mood.direction_of(62.0, 60.0))
        self.assertFalse(hasattr(mood, "_tier"))
        self.assertFalse(hasattr(mood, "_direction"))

    def test_bad_json_falls_back_to_defaults(self) -> None:
        """坏 JSON 回默认值，不炸调用方。"""
        helpers.write_raw(Path(self.root) / mood.MOOD_FILE, "{不是 JSON")
        got = mood.read(self.root, now=_at())
        self.assertAlmostEqual(float(model.MOOD_BASELINE_DEFAULT), got["now"], places=1)
        self.assertAlmostEqual(
            float(model.MOOD_BASELINE_DEFAULT), got["baseline"], places=1
        )

    # ---- 日结 ------------------------------------------------------------

    def test_settle_day_keeps_half_the_deviation(self) -> None:
        """日结：80 对 60 的差一半进基线（70），另一半不给；配额与日期重置。"""
        start = _at()
        self._seed(80, 60, start, conflict_used=2, day=_at(-24)[:10])
        got = mood.settle_day(self.root, now=start)
        self.assertAlmostEqual(60.0, got["baseline_before"], places=1)
        self.assertAlmostEqual(70.0, got["baseline_after"], places=1)
        self.assertAlmostEqual(10.0, got["absorbed"], places=1)
        self.assertAlmostEqual(80.0, got["now"], places=1)
        state = self._state()
        self.assertAlmostEqual(70.0, state["baseline"], places=1)
        self.assertEqual(0, state["conflict_used"])
        self.assertEqual(day_model.day_key(start, cutover_hour=0), state["day"])

    def test_settle_day_keeps_baseline_in_range(self) -> None:
        """基线永远夹在刻度里，哪怕种子数据已经在边上。"""
        for seed in ((100.0, 100.0), (0.0, 0.0), (100.0, 0.0)):
            now_value, baseline = seed
            start = _at()
            self._seed(now_value, baseline, start)
            mood.settle_day(self.root, now=start)
            state = self._state()
            self.assertGreaterEqual(state["baseline"], float(model.MOOD_MIN))
            self.assertLessEqual(state["baseline"], float(model.MOOD_MAX))

    def test_settle_day_signature_has_no_final(self) -> None:
        """签名里不许冒出 final——这一格归别处。"""
        names = list(inspect.signature(mood.settle_day).parameters)
        self.assertNotIn("final", names)

    # ---- 轨迹账本 --------------------------------------------------------

    def test_trajectory_sampling_obeys_the_min_gap(self) -> None:
        """读时顺手记：隔不到 MOOD_SAMPLE_MIN_GAP_MIN 不记，隔够了才记。"""
        start = _at()
        mood.read(self.root, now=start)
        mood.read(self.root, now=_at(0.05))  # 3 分钟，太挤
        self.assertEqual(1, len(mood.trajectory(self.root)))

        mood.read(self.root, now=_at(0.5))  # 30 分钟，够
        rows = mood.trajectory(self.root)
        self.assertEqual(2, len(rows))
        self.assertEqual(["at", "baseline", "now", "tier"], sorted(rows[0].keys()))
        self.assertEqual(1, len(mood.trajectory(self.root, limit=1)))
        self.assertEqual(rows[-1]["at"], _at(0.5))

    def test_trajectory_limit_is_the_tail(self) -> None:
        """limit 取最后几条，面板画曲线用。"""
        for hours in (0.0, 0.5, 1.0, 1.5):
            mood.read(self.root, now=_at(hours))
        self.assertEqual(4, len(mood.trajectory(self.root, limit=10)))
        tail = mood.trajectory(self.root, limit=2)
        self.assertEqual(2, len(tail))
        self.assertEqual(_at(1.5), tail[-1]["at"])


if __name__ == "__main__":
    unittest.main()
