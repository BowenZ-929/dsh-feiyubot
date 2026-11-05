"""六轴偏移：日事件 ±1、周结算覆盖 ±2、无事件按天向 0 落，且永不写卡。

回落尺是**天**：连续 model.OFFSET_IDLE_DAYS（3）天无事件 → 向 0 回落 1。
判据在 docs/modules/heart.md §2 offset「会被什么改变」；6h / 4h 那对半衰是
情绪量的尺，本文件一条都不许再断言它。
"""
from __future__ import annotations

import hashlib
import inspect
import unittest
import tempfile
from pathlib import Path
from typing import Any

from organs.day import model as day_model
from organs.heart import card, model, offset, store
from organs.heart.tests import helpers

T0 = "2026-11-03T12:00:00+08:00"
# +1 天：还没攒够一个整 idle 窗口。
T_DAY = "2026-11-04T12:00:00+08:00"
# +2.9 天：只差 2.4 小时就满 3 天，仍然一格不落。
T_2_9D = "2026-11-06T09:36:00+08:00"
# +3 天：判据上"连续 3 天无事件"，回落 1。
T_3D = "2026-11-06T12:00:00+08:00"
# +6 天：两个整窗口，回落 2。
T_6D = "2026-11-09T12:00:00+08:00"


def sha256_of(path: Any) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ledger_kinds(root: Path) -> list:
    rows = store.read_jsonl(root / offset.LEDGER_FILE)
    return [row.get("kind") for row in rows]


class OffsetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)

    def test_daily_cap_keeps_one_and_ledger_records_truncate(self):
        results = [offset.apply_daily("想黏人", 1.0, root=self.root, now=T0) for _ in range(3)]
        self.assertAlmostEqual(results[0]["value"], 1.0)
        self.assertAlmostEqual(results[-1]["value"], model.OFFSET_DAILY_PER_AXIS)
        self.assertTrue(results[-1]["truncated"])
        self.assertEqual(results[-1]["applied"], 0.0, "第三下已经顶到单日上限，实加为零")
        snap = offset.read(root=self.root, now=T0)
        self.assertAlmostEqual(snap["axes"]["想黏人"]["value"], model.OFFSET_DAILY_PER_AXIS)
        self.assertIn(offset.KIND_TRUNCATE, ledger_kinds(self.root))
        rows = [row for row in store.read_jsonl(self.root / offset.LEDGER_FILE)
                if row.get("kind") == offset.KIND_TRUNCATE]
        self.assertEqual(rows[0]["axis"], "想黏人")
        self.assertEqual(rows[0]["cap"], model.OFFSET_DAILY_PER_AXIS)
        self.assertGreater(rows[0]["requested"], model.OFFSET_DAILY_PER_AXIS)

    def test_daily_cap_resets_on_a_new_day(self):
        offset.apply_daily("想黏人", 1.0, root=self.root, now=T0)
        next_day = offset.apply_daily("想黏人", 1.0, root=self.root, now=T_DAY)
        self.assertFalse(next_day["truncated"], "换一天，单日额度重新算")
        self.assertAlmostEqual(next_day["day_delta"], model.OFFSET_DAILY_PER_AXIS)
        self.assertAlmostEqual(next_day["value"], 2 * model.OFFSET_DAILY_PER_AXIS,
                               msg="才过一天不算闲置，两天的日偏移先叠着，等周结算覆盖")

    def test_daily_event_after_a_three_day_gap_only_counts_today(self):
        offset.apply_daily("想黏人", 1.0, root=self.root, now=T0)
        after_idle = offset.apply_daily("想黏人", 1.0, root=self.root, now=T_3D)
        self.assertFalse(after_idle["truncated"], "换一天，单日额度重新算")
        self.assertAlmostEqual(after_idle["decayed_value"], 0.0, "满 3 天那一格先回落掉")
        self.assertAlmostEqual(after_idle["value"], model.OFFSET_DAILY_PER_AXIS,
                               msg="三天的残余已按天尺回落进死区，今天只算今天那一格")

    def test_week_settle_overwrites_instead_of_adding(self):
        offset.apply_daily("想黏人", 1.0, root=self.root, now=T0)
        settled = offset.settle_week({"想黏人": 2.0}, root=self.root, now=T0)
        self.assertAlmostEqual(settled["axes"]["想黏人"]["value"], model.OFFSET_WEEK_MAX)
        self.assertAlmostEqual(settled["axes"]["想黏人"]["daily_before"], 1.0)
        snap = offset.read(root=self.root, now=T0)
        self.assertAlmostEqual(snap["axes"]["想黏人"]["value"], model.OFFSET_WEEK_MAX,
                               msg="覆盖式：日 +1 与周 +2 不许叠成 +3")
        overwrites = [row for row in store.read_jsonl(self.root / offset.LEDGER_FILE)
                      if row.get("kind") == offset.KIND_OVERWRITE]
        self.assertEqual(len(overwrites), 1)
        self.assertEqual(overwrites[0]["axis"], "想黏人")
        self.assertAlmostEqual(overwrites[0]["daily_before"], 1.0)
        self.assertAlmostEqual(overwrites[0]["week_value"], model.OFFSET_WEEK_MAX)
        self.assertTrue(snap["week"].startswith("2026-W"))

    def test_week_settle_clamps_out_of_band_values(self):
        settled = offset.settle_week({"服气": -9.0, "怕失去": 9.0}, root=self.root, now=T0)
        self.assertAlmostEqual(settled["axes"]["服气"]["week_value"], -model.OFFSET_WEEK_MAX)
        self.assertAlmostEqual(settled["axes"]["怕失去"]["week_value"], model.OFFSET_WEEK_MAX)

    def test_week_settle_rejects_unknown_axis(self):
        with self.assertRaises(ValueError):
            offset.settle_week({"瞎眼": 1.0}, root=self.root, now=T0)
        with self.assertRaises(ValueError):
            offset.apply_daily("瞎眼", 1.0, root=self.root, now=T0)

    def test_daily_one_survives_until_three_days_then_goes_to_zero(self):
        """天线判据：+1 在 0 / 2.9 天还是 1.0，满 3 天变 0.0。"""
        offset.apply_daily("想黏人", 1.0, root=self.root, now=T0)
        self.assertAlmostEqual(offset.read(root=self.root, now=T0)["axes"]["想黏人"]["value"], 1.0)
        self.assertAlmostEqual(offset.read(root=self.root, now=T_2_9D)["axes"]["想黏人"]["value"], 1.0,
                               msg="差 2.4 小时才满 3 天，不许提前掉")
        self.assertEqual(offset.read(root=self.root, now=T_3D)["axes"]["想黏人"]["value"], 0.0,
                         msg="连续 3 天无事件 → 向 0 回落 1，正好落到 0")

    def test_week_two_decays_one_per_three_days(self):
        """周结算 +2：3 天回落 1 → 1.0；6 天回落 2 → 0.0。"""
        offset.settle_week({"想黏人": 2.0}, root=self.root, now=T0)
        self.assertAlmostEqual(offset.read(root=self.root, now=T0)["axes"]["想黏人"]["value"], 2.0)
        self.assertAlmostEqual(offset.read(root=self.root, now=T_3D)["axes"]["想黏人"]["value"], 1.0,
                               msg="3 天只回落一格，2 → 1")
        self.assertEqual(offset.read(root=self.root, now=T_6D)["axes"]["想黏人"]["value"], 0.0,
                         msg="6 天回落两格，2 → 0")

    def test_decay_goes_towards_zero_from_the_negative_side_too(self):
        """负偏移同样朝 0 走，不会越过 0 走到正数去。"""
        offset.settle_week({"服气": -2.0}, root=self.root, now=T0)
        self.assertAlmostEqual(offset.read(root=self.root, now=T_3D)["axes"]["服气"]["value"], -1.0)
        self.assertEqual(offset.read(root=self.root, now=T_6D)["axes"]["服气"]["value"], 0.0)

    def test_reading_does_not_write_or_advance_state(self):
        offset.read(root=self.root, now=T0)
        self.assertFalse((self.root / offset.OFFSET_FILE).exists(), "纯读，一个字节都不写")
        offset.apply_daily("心疼", 1.0, root=self.root, now=T0)
        first = offset.read(root=self.root, now=T_DAY)["axes"]["心疼"]["value"]
        second = offset.read(root=self.root, now=T_DAY)["axes"]["心疼"]["value"]
        self.assertAlmostEqual(first, second, "读不推进状态：同一时刻读两次结果一样")
        self.assertAlmostEqual(store.read_json(self.root / offset.OFFSET_FILE, {})["axes"]["心疼"]["value"], 1.0,
                               "盘上留的是写入时的值，衰减只在读的那一刻现算")

    def test_event_lands_with_an_idle_anchor_and_restarts_the_count(self):
        """有事件就重新起算：昨天的事件不会把今天的闲置天数算进去。"""
        offset.apply_daily("想黏人", 1.0, root=self.root, now=T0)
        row = store.read_json(self.root / offset.OFFSET_FILE, {})["axes"]["想黏人"]
        self.assertEqual(row["idle_anchor_at"], T0)
        # 再过 2 天来一个事件：先按天尺回落（不足 3 天，还是 1），再叠加到 1.0 上限
        again = offset.apply_daily("想黏人", 1.0, root=self.root, now="2026-11-05T12:00:00+08:00")
        self.assertAlmostEqual(again["decayed_value"], 1.0)
        self.assertAlmostEqual(again["value"], 2.0)
        row = store.read_json(self.root / offset.OFFSET_FILE, {})["axes"]["想黏人"]
        self.assertEqual(row["idle_anchor_at"], "2026-11-05T12:00:00+08:00", "事件把起算点刷成事件时刻")
        self.assertAlmostEqual(offset.read(root=self.root, now=T_3D)["axes"]["想黏人"]["value"], 2.0,
                               msg="新起算点之后才 2 天，一格都没落")

    def test_another_axis_event_does_not_reset_this_axis_idle_clock(self):
        """别的轴来事件，不许把这一轴的闲置天数清零（否则回落永远攒不满）。"""
        offset.apply_daily("想黏人", 1.0, root=self.root, now=T0)
        offset.apply_daily("心疼", 1.0, root=self.root, now=T_DAY)
        snap = offset.read(root=self.root, now=T_3D)
        self.assertEqual(snap["axes"]["想黏人"]["idle_anchor_at"], T0)
        self.assertEqual(snap["axes"]["想黏人"]["value"], 0.0, "自己三天没事件就照样回落")
        self.assertAlmostEqual(snap["axes"]["心疼"]["value"], 1.0, "心疼才过两天，一格没落")

    def test_state_without_idle_anchor_falls_back_to_updated_at(self):
        """旧状态没有 idle_anchor_at：按 updated_at 兜底，不炸、也不补偏移。"""
        store.write_json(self.root / offset.OFFSET_FILE, {
            "version": offset.STATE_VERSION, "week": "",
            "axes": {"想黏人": {"value": 1.0, "updated_at": T0,
                              "day": day_model.day_key(T0, cutover_hour=0), "day_delta": 1.0}},
        })
        snap = offset.read(root=self.root, now=T_2_9D)
        self.assertAlmostEqual(snap["axes"]["想黏人"]["value"], 1.0)
        self.assertEqual(snap["axes"]["想黏人"]["idle_anchor_at"], T0)
        self.assertEqual(offset.read(root=self.root, now=T_3D)["axes"]["想黏人"]["value"], 0.0)

    def test_total_offset_is_clamped_with_ledger(self):
        offset.settle_week({"想黏人": model.OFFSET_WEEK_MAX}, root=self.root, now=T0)
        push = offset.apply_daily("想黏人", model.OFFSET_DAILY_PER_AXIS, root=self.root, now=T0)
        self.assertTrue(push["clamped"])
        self.assertAlmostEqual(push["value"], model.OFFSET_MAX)
        self.assertIn(offset.KIND_CLAMP, ledger_kinds(self.root))

    def test_overview_shape(self):
        offset.apply_daily("服气", -1.0, root=self.root, now=T0)
        view = offset.overview(root=self.root, now=T_DAY)
        self.assertIn("week", view)
        for axis in model.AXES:
            row = view["axes"][axis]
            self.assertEqual(set(row), {"value", "decayed_value", "day_delta", "clamped"})
        self.assertLess(view["axes"]["服气"]["value"], 0.0)

    def test_state_file_shape(self):
        offset.apply_daily("心疼", 1.0, root=self.root, now=T0, source="event")
        state = store.read_json(self.root / offset.OFFSET_FILE, {})
        self.assertEqual(state["version"], offset.STATE_VERSION)
        self.assertEqual(state["week"], "", "周结算之前不编造周号")
        row = state["axes"]["心疼"]
        self.assertEqual(set(row), {"value", "updated_at", "day", "day_delta", "idle_anchor_at"})
        self.assertEqual(row["updated_at"], T0)
        self.assertEqual(row["idle_anchor_at"], T0)
        self.assertEqual(row["day"], day_model.day_key(T0, cutover_hour=0))
        self.assertAlmostEqual(row["day_delta"], 1.0)

    def test_card_file_hash_is_untouched_by_every_write_path(self):
        before_hash = sha256_of(card.DEFAULT_CARD_PATH)
        before_bytes = Path(card.DEFAULT_CARD_PATH).read_bytes()
        offset.apply_daily("想黏人", 1.0, root=self.root, now=T0)
        offset.apply_daily("想黏人", 1.0, root=self.root, now=T0)
        offset.settle_week({"想黏人": 2.0, "心疼": -1.0}, root=self.root, now=T0)
        offset.read(root=self.root, now=T_6D)
        offset.overview(root=self.root, now=T_6D)
        self.assertEqual(sha256_of(card.DEFAULT_CARD_PATH), before_hash, "卡哈希不许变")
        self.assertEqual(Path(card.DEFAULT_CARD_PATH).read_bytes(), before_bytes)
        self.assertFalse((self.root / card.CARD_FILE).exists(), "偏移不许往自己根目录落卡")

    def test_no_baseline_in_any_offset_signature(self):
        public = (offset.read, offset.overview, offset.apply_daily, offset.settle_week)
        for func in public:
            for name in inspect.signature(func).parameters:
                self.assertNotIn("baseline", name.lower(), "%s 的签名里不许出现基线" % func.__name__)
        for func in (offset._decay, offset._decayed_value, offset._anchor_of):
            for name in inspect.signature(func).parameters:
                self.assertNotIn("baseline", name.lower(), "%s 的签名里不许出现基线" % func.__name__)

    def test_offset_source_never_touches_the_card(self):
        source = Path(offset.__file__).read_text(encoding="utf-8").lower()
        for banned in ("import card", "baseline", "baseline-card"):
            self.assertNotIn(banned, source, "offset 拿不到卡：%s" % banned)
        self.assertNotIn("organs.heart.card", source)

    def test_offset_source_has_no_half_life_scale_left(self):
        """尺换成了天：源代码里不许再出现半衰那把尺，也不许再搜得到那两个常数。"""
        source = Path(offset.__file__).read_text(encoding="utf-8")
        self.assertNotIn("HALF_LIFE", source)
        self.assertNotIn("0.5 **", source)
