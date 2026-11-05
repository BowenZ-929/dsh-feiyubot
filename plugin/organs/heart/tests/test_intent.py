"""intent 的验收：五个意图现算、权重 × 清醒度排序、过阈值才开口、坏表不炸。

时间一律用带显式偏移的固定串（+08:00），跑在哪个时区都同一个结论；
数据一律落 helpers.make_root 的临时目录，不碰仓库里的真 data（只读它出厂那份权重表）。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

from organs.heart import events, intent, model, pending, store
from organs.heart.tests import helpers

# 固定时刻：BASE 是白天（14 点），NIGHT 在夜间窗口里（23:30）。
BASE = "2024-05-06T14:00:00+08:00"
NIGHT = "2024-05-06T23:30:00+08:00"


def minutes_ago(minutes: float, base: str = BASE) -> str:
    """base 往前 minutes 分钟的时刻串。"""
    return model.iso(model.parse_ts(base) - timedelta(minutes=minutes))


def feed(root: Any, kinds: List[str], gaps: List[float], base: str = BASE) -> None:
    """按 gaps（距 base 几分钟）往临时账本投递一串事件。"""
    for kind, gap in zip(kinds, gaps):
        events.deliver(kind, root=root, now=minutes_ago(gap, base))


def open_thought(root: Any, kind: str = pending.KIND_PRAISE, attempts: int = 0,
                 state: str = pending.STATE_OPEN, base: str = BASE) -> str:
    """直接摆一条念头进临时根（不跑 derive，好把 attempts / state 写死）。"""
    row = {
        "id": "p-test",
        "kind": kind,
        "axis": None,
        "created_at": minutes_ago(10.0, base),
        "expires_at": model.iso(model.parse_ts(base) + timedelta(minutes=120)),
        "attempts": attempts,
        "state": state,
    }
    store.write_json(Path(root) / pending.PENDING_FILE,
                     {"version": 1, "config": {}, "thoughts": [row]})
    return row["id"]


ALL_HIT_CONTEXT: Dict[str, Any] = {"energy_value": 80.0, "today_count": 3,
                                   "familiarity": 3.0, "hour": 14}


class WeightsTableTest(unittest.TestCase):
    """验收 3：参数表读入永不抛错——坏 JSON / 缺键 / 越界一律回默认并 WARN 一行。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = helpers.make_root(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_root_is_factory_data_dir(self) -> None:
        path = intent._weights_path(None)
        self.assertEqual(path, store.DATA_DIR / intent.WEIGHTS_FILE)

    def test_no_file_gives_factory_defaults_and_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = intent.load_weights(self.root)
        self.assertEqual(table["threshold"], intent.DEFAULT_THRESHOLD)
        self.assertEqual(table["weights"], intent.DEFAULT_WEIGHTS)
        self.assertEqual(table["clarity"], intent.DEFAULT_CLARITY)
        self.assertEqual(len(caught), 1, "读不到只 WARN 一行")
        self.assertIn(intent.WEIGHTS_FILE, str(caught[0].message))

    def test_file_values_win(self) -> None:
        helpers.write_raw(self.root / intent.WEIGHTS_FILE, json.dumps({
            "version": 1, "threshold": 0.7,
            "weights": {"unfinished-chat": 0.8, "unresolved": 0.5,
                        "unsaid": 0.4, "bored": 0.2, "lonely": 0.1},
            "clarity": {"tired": 0.5, "normal": 0.7, "fresh": 1.0, "night": 0.6},
        }))
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            table = intent.load_weights(self.root)
        self.assertEqual(table["threshold"], 0.7)
        self.assertEqual(table["weights"]["unfinished-chat"], 0.8)
        self.assertEqual(table["clarity"]["night"], 0.6)

    def test_bad_json_falls_back_to_defaults(self) -> None:
        helpers.write_raw(self.root / intent.WEIGHTS_FILE, "{not json, half a file")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = intent.load_weights(self.root)
        self.assertEqual(table["weights"], intent.DEFAULT_WEIGHTS)
        self.assertEqual(len(caught), 1)
        self.assertIn("读不到", str(caught[0].message))

    def test_top_level_not_object_falls_back(self) -> None:
        helpers.write_raw(self.root / intent.WEIGHTS_FILE, "[1, 2, 3]")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = intent.load_weights(self.root)
        self.assertEqual(table["threshold"], intent.DEFAULT_THRESHOLD)
        self.assertEqual(len(caught), 1)
        self.assertIn("不是 JSON 对象", str(caught[0].message))

    def test_missing_keys_filled_and_warned(self) -> None:
        helpers.write_raw(self.root / intent.WEIGHTS_FILE,
                          json.dumps({"weights": {"unsaid": 0.5}}))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = intent.load_weights(self.root)
        self.assertEqual(table["weights"]["unsaid"], 0.5)
        self.assertEqual(table["weights"]["lonely"], intent.DEFAULT_WEIGHTS["lonely"])
        self.assertEqual(table["clarity"], intent.DEFAULT_CLARITY)
        self.assertEqual(len(caught), 1, "缺键也只 WARN 一行")
        self.assertIn("缺", str(caught[0].message))

    def test_out_of_range_clamped_and_warned(self) -> None:
        helpers.write_raw(self.root / intent.WEIGHTS_FILE, json.dumps({
            "threshold": 3.0,
            "weights": {"unfinished-chat": -0.5, "unresolved": 9.0},
            "clarity": {"night": 9.0, "fresh": -1.0},
        }))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = intent.load_weights(self.root)
        self.assertEqual(table["threshold"], 1.0)
        self.assertEqual(table["weights"]["unfinished-chat"], 0.0)
        self.assertEqual(table["weights"]["unresolved"], 1.0)
        self.assertEqual(table["clarity"]["night"], 2.0)
        self.assertEqual(table["clarity"]["fresh"], 0.1)
        self.assertEqual(len(caught), 1)
        self.assertIn("越界", str(caught[0].message))

    def test_junk_values_never_raise(self) -> None:
        for junk in ('{"threshold": "很困", "weights": 7, "clarity": null}',
                     '{"weights": {"bored": true}}',
                     '{}', 'null', '{"version": "x"}'):
            helpers.write_raw(self.root / intent.WEIGHTS_FILE, junk)
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                table = intent.load_weights(self.root)
            self.assertEqual(sorted(table["weights"]),
                             sorted(intent.INTENT_ORDER))

    def test_table_shape_is_always_complete(self) -> None:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            table = intent.load_weights(self.root)
        self.assertEqual(sorted(table), ["clarity", "threshold", "version", "weights"])
        self.assertEqual(sorted(table["weights"]), sorted(intent.INTENT_ORDER))
        self.assertEqual(sorted(table["clarity"]), ["fresh", "night", "normal", "tired"])

    def test_loading_never_writes_a_weights_file(self) -> None:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            intent.load_weights(self.root)
            intent.rank(root=self.root, now=BASE, context=dict(ALL_HIT_CONTEXT))
        self.assertFalse((self.root / intent.WEIGHTS_FILE).exists())

    def test_factory_file_ships_and_reads_without_warning(self) -> None:
        path = helpers.heart_data() / intent.WEIGHTS_FILE
        self.assertTrue(path.exists(), "出厂要带一份权重表：%s" % path)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = intent.load_weights(helpers.heart_data())
        self.assertEqual([str(w.message) for w in caught], [],
                         "出厂表读入不该有任何 WARN")
        raw = helpers.read_json_file(path)
        self.assertEqual(table["threshold"], raw["threshold"])
        self.assertEqual(table["weights"], raw["weights"])
        self.assertEqual(table["clarity"], raw["clarity"])
        self.assertEqual(table["threshold"], intent.DEFAULT_THRESHOLD)
        self.assertEqual(table["weights"], intent.DEFAULT_WEIGHTS)
        self.assertEqual(table["clarity"], intent.DEFAULT_CLARITY)

    def test_factory_file_says_it_is_not_a_pass_line(self) -> None:
        raw = helpers.read_json_file(helpers.heart_data() / intent.WEIGHTS_FILE)
        self.assertIn("note", raw, "要写明这是标定默认值、不是验收线")
        self.assertIn("不是验收线", raw["note"])
        self.assertIn("改这个文件即生效", raw["note"])

    def test_data_dir_only_holds_dotfiles_card_and_weights(self) -> None:
        names = sorted(p.name for p in helpers.heart_data().iterdir())
        self.assertEqual(names, [".gitignore", ".gitkeep", "baseline-card.json",
                                 intent.WEIGHTS_FILE])
        self.assertNotEqual(Path(self.root), helpers.heart_data())

    def test_copy_of_factory_file_reads_clean_from_a_root(self) -> None:
        shutil.copy(helpers.heart_data() / intent.WEIGHTS_FILE,
                    Path(self.root) / intent.WEIGHTS_FILE)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = intent.load_weights(self.root)
        self.assertEqual([str(w.message) for w in caught], [])
        self.assertEqual(table["weights"], intent.DEFAULT_WEIGHTS)


class ClarityTest(unittest.TestCase):
    """验收 4：清醒度修正——三档 + 夜间那一乘，可见可解释。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = helpers.make_root(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def clarity(self, energy: float, hour: int) -> Dict[str, Any]:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            return intent.rank(root=self.root, now=BASE, context={
                "energy_value": energy, "today_count": 5,
                "familiarity": 3.0, "hour": hour})["clarity"]

    def test_three_bands(self) -> None:
        self.assertEqual(self.clarity(80.0, 14)["factor"], 1.0)
        self.assertEqual(self.clarity(50.0, 14)["factor"], 0.85)
        self.assertEqual(self.clarity(20.0, 14)["factor"], 0.7)

    def test_band_edges(self) -> None:
        self.assertEqual(self.clarity(75.0, 14)["factor"], 1.0)
        self.assertEqual(self.clarity(30.0, 14)["factor"], 0.85)
        self.assertEqual(self.clarity(29.9, 14)["factor"], 0.7)

    def test_night_multiplies(self) -> None:
        self.assertAlmostEqual(self.clarity(80.0, 23)["factor"], 0.8, places=4)
        self.assertAlmostEqual(self.clarity(20.0, 3)["factor"], 0.56, places=4)

    def test_why_states_band_and_night(self) -> None:
        day = self.clarity(80.0, 14)
        self.assertIn("清醒", day["why"])
        self.assertIn("不在夜间", day["why"])
        night = self.clarity(80.0, 23)
        self.assertIn("夜间", night["why"])
        self.assertIn("0.80", night["why"])

    def test_night_window_follows_model(self) -> None:
        # 窗口按 model 的 23 → 8 算，跨午夜也对；8 点整已经出窗。
        self.assertAlmostEqual(self.clarity(80.0, model.NIGHT_END_H)["factor"], 1.0,
                               places=4)
        self.assertAlmostEqual(self.clarity(80.0, model.NIGHT_START_H)["factor"], 0.8,
                               places=4)


class RankTest(unittest.TestCase):
    """验收 1 / 2：多个念头同时命中时的排序、top 与阈值。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = helpers.make_root(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def triple_scene(self) -> None:
        """造出 unfinished-chat / unsaid / bored 同时命中（unresolved / lonely 不命中）。"""
        feed(self.root, [events.KIND_REPLIED, events.KIND_IGNORED,
                         events.KIND_IGNORED], [20.0, 8.0, 5.0])

    def rank(self, now: str = BASE, **ctx: Any) -> Dict[str, Any]:
        context = dict(ALL_HIT_CONTEXT)
        context.update(ctx)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            return intent.rank(root=self.root, now=now, context=context)

    def test_triple_scene_ranks_unfinished_chat_first(self) -> None:
        self.triple_scene()
        res = self.rank(energy_value=60.0, today_count=2)
        hits = [row["intent"] for row in res["order"] if row["hit"]]
        self.assertEqual(hits, [intent.INTENT_UNFINISHED_CHAT, intent.INTENT_UNSAID,
                                intent.INTENT_BORED])
        self.assertEqual(res["top"]["intent"], intent.INTENT_UNFINISHED_CHAT)
        self.assertEqual(res["order"][0]["intent"], intent.INTENT_UNFINISHED_CHAT)
        self.assertEqual(res["score"], res["top"]["weight"])
        self.assertTrue(res["pass"])

    def test_every_row_explains_itself(self) -> None:
        self.triple_scene()
        res = self.rank(energy_value=60.0, today_count=2)
        self.assertEqual(len(res["order"]), len(intent.INTENT_ORDER))
        for row in res["order"]:
            self.assertTrue(row["why"], "每条都要有 why：%r" % (row,))
            self.assertIsInstance(row["hit"], bool)
            self.assertAlmostEqual(
                row["weight"],
                round(intent.DEFAULT_WEIGHTS[row["intent"]] * res["clarity"]["factor"], 4),
                places=4)
        self.assertFalse(res["order"][-1]["hit"] and res["order"][-1]["weight"] >= 0.55)

    def test_order_and_weights_scale_with_clarity(self) -> None:
        self.triple_scene()
        fresh = self.rank(energy_value=80.0, today_count=2)
        tired = self.rank(energy_value=20.0, today_count=2)
        self.assertEqual([r["intent"] for r in fresh["order"]],
                         [r["intent"] for r in tired["order"]])
        for fresh_row, tired_row in zip(fresh["order"], tired["order"]):
            self.assertAlmostEqual(tired_row["weight"],
                                   round(fresh_row["weight"] * 0.7, 4), places=4)
        self.assertGreater(fresh["score"], tired["score"])
        self.assertEqual(fresh["top"]["intent"], tired["top"]["intent"])

    def test_nothing_hit_gives_no_top(self) -> None:
        res = self.rank(energy_value=20.0, today_count=9, familiarity=3.0)
        self.assertIsNone(res["top"])
        self.assertEqual(res["score"], 0.0)
        self.assertFalse(res["pass"])

    def test_unresolved_alone_passes_at_default_threshold(self) -> None:
        open_thought(self.root, kind=pending.KIND_AXIS, attempts=2)
        res = self.rank(energy_value=60.0, today_count=9)
        self.assertEqual([r["intent"] for r in res["order"] if r["hit"]],
                         [intent.INTENT_UNRESOLVED])
        self.assertAlmostEqual(res["score"], 0.75 * 0.85, places=4)
        self.assertTrue(res["pass"])

    def test_raising_threshold_silences_the_same_scene(self) -> None:
        open_thought(self.root, kind=pending.KIND_AXIS, attempts=2)
        before = self.rank(energy_value=60.0, today_count=9)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            table = intent.load_weights(self.root)
        table["threshold"] = round(table["threshold"] + 0.15, 2)
        store.write_json(Path(self.root) / intent.WEIGHTS_FILE, table)
        after = self.rank(energy_value=60.0, today_count=9)
        self.assertTrue(before["pass"])
        self.assertFalse(after["pass"])
        self.assertEqual(before["score"], after["score"])
        self.assertGreater(after["threshold"], after["score"])

    def test_bored_and_lonely_alone_never_cross_the_line(self) -> None:
        lonely = self.rank(energy_value=20.0, today_count=1, familiarity=0.5)
        self.assertEqual([r["intent"] for r in lonely["order"] if r["hit"]],
                         [intent.INTENT_LONELY])
        self.assertFalse(lonely["pass"])
        bored = self.rank(energy_value=80.0, today_count=1, familiarity=5.0)
        self.assertEqual([r["intent"] for r in bored["order"] if r["hit"]],
                         [intent.INTENT_BORED])
        self.assertFalse(bored["pass"])

    def test_missed_intents_stay_listed_but_out_of_top(self) -> None:
        feed(self.root, [events.KIND_IGNORED, events.KIND_IGNORED], [8.0, 5.0])
        res = self.rank(energy_value=20.0, today_count=9)
        self.assertEqual(res["top"]["intent"], intent.INTENT_UNSAID)
        misses = [r["intent"] for r in res["order"] if not r["hit"]]
        self.assertIn(intent.INTENT_UNFINISHED_CHAT, misses)
        self.assertIn(intent.INTENT_LONELY, misses)

    def test_ties_follow_fixed_intent_order(self) -> None:
        store.write_json(Path(self.root) / intent.WEIGHTS_FILE, {
            "threshold": 0.5,
            "weights": {name: 0.5 for name in intent.INTENT_ORDER},
        })
        res = self.rank(energy_value=80.0, today_count=2)
        self.assertEqual([r["intent"] for r in res["order"]],
                         list(intent.INTENT_ORDER))
        self.assertEqual(res["order"][0]["weight"], 0.5)

    def test_context_energy_tier_decides_bored(self) -> None:
        lazy = self.rank(energy_value=60.0, energy_tier="累", today_count=1)
        self.assertFalse([r for r in lazy["order"]
                          if r["intent"] == intent.INTENT_BORED][0]["hit"])
        busy = self.rank(energy_value=60.0, today_count=4)
        self.assertFalse([r for r in busy["order"]
                          if r["intent"] == intent.INTENT_BORED][0]["hit"])

    def test_context_injection_never_touches_brain(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            intent.rank(root=self.root, now=BASE, context=dict(ALL_HIT_CONTEXT))
        self.assertEqual([str(w.message) for w in caught if "ports" in str(w.message)],
                         [], "注入了读数就不该再去问脑")

    def test_missing_brain_degrades_and_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            res = intent.rank(root=self.root, now=BASE)
        self.assertIsNotNone(res["top"])
        self.assertTrue(any("ports" in str(w.message) for w in caught))

    def test_rank_does_not_write_a_weights_file(self) -> None:
        self.rank()
        self.assertFalse((Path(self.root) / intent.WEIGHTS_FILE).exists())


class IntentHitsTest(unittest.TestCase):
    """五个判据逐条验收：命中与不命中的边界。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = helpers.make_root(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def hit(self, name: str, now: str = BASE, **ctx: Any) -> bool:
        context = dict(ALL_HIT_CONTEXT)
        context.update(ctx)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            res = intent.rank(root=self.root, now=now, context=context)
        return [r["hit"] for r in res["order"] if r["intent"] == name][0]

    # —— unfinished-chat ——

    def test_unfinished_chat_hits_with_recent_reply(self) -> None:
        feed(self.root, [events.KIND_REPLIED], [5.0])
        self.assertTrue(self.hit(intent.INTENT_UNFINISHED_CHAT))

    def test_unfinished_chat_needs_a_reply_inside_thirty_minutes(self) -> None:
        feed(self.root, [events.KIND_REPLIED], [40.0])
        self.assertFalse(self.hit(intent.INTENT_UNFINISHED_CHAT))

    def test_normal_after_reply_closes_the_chat(self) -> None:
        feed(self.root, [events.KIND_REPLIED, events.KIND_NORMAL], [20.0, 3.0])
        self.assertFalse(self.hit(intent.INTENT_UNFINISHED_CHAT))

    def test_normal_then_hanging_ignored_is_not_closed(self) -> None:
        feed(self.root, [events.KIND_REPLIED, events.KIND_NORMAL,
                         events.KIND_IGNORED], [20.0, 10.0, 4.0])
        self.assertTrue(self.hit(intent.INTENT_UNFINISHED_CHAT))

    def test_empty_ledger_has_nothing_unfinished(self) -> None:
        self.assertFalse(self.hit(intent.INTENT_UNFINISHED_CHAT))

    # —— unresolved ——

    def test_unresolved_needs_an_attempt(self) -> None:
        open_thought(self.root, attempts=0)
        self.assertFalse(self.hit(intent.INTENT_UNRESOLVED))
        open_thought(self.root, attempts=1)
        self.assertTrue(self.hit(intent.INTENT_UNRESOLVED))

    def test_settled_thought_is_not_unresolved(self) -> None:
        open_thought(self.root, attempts=3, state=pending.STATE_SETTLED)
        self.assertFalse(self.hit(intent.INTENT_UNRESOLVED))

    # —— unsaid ——

    def test_unsaid_needs_two_trailing_ignored(self) -> None:
        feed(self.root, [events.KIND_IGNORED], [5.0])
        self.assertFalse(self.hit(intent.INTENT_UNSAID))
        feed(self.root, [events.KIND_IGNORED], [3.0])
        self.assertTrue(self.hit(intent.INTENT_UNSAID))

    def test_unsaid_tail_breaks_on_a_reply(self) -> None:
        feed(self.root, [events.KIND_IGNORED, events.KIND_IGNORED,
                         events.KIND_REPLIED], [20.0, 15.0, 5.0])
        self.assertFalse(self.hit(intent.INTENT_UNSAID))

    def test_unsaid_from_wanted_praise(self) -> None:
        open_thought(self.root)
        self.assertTrue(self.hit(intent.INTENT_UNSAID))

    # —— bored ——

    def test_bored_needs_mid_energy_and_a_quiet_day(self) -> None:
        self.assertTrue(self.hit(intent.INTENT_BORED, energy_value=60.0,
                                 today_count=3))
        self.assertFalse(self.hit(intent.INTENT_BORED, energy_value=60.0,
                                  today_count=4))
        self.assertFalse(self.hit(intent.INTENT_BORED, energy_value=20.0,
                                  today_count=1))

    # —— lonely ——

    def test_lonely_needs_six_quiet_hours_and_low_familiarity(self) -> None:
        self.assertTrue(self.hit(intent.INTENT_LONELY, familiarity=0.9))
        self.assertFalse(self.hit(intent.INTENT_LONELY, familiarity=1.0))
        feed(self.root, [events.KIND_NORMAL], [300.0])
        self.assertFalse(self.hit(intent.INTENT_LONELY, familiarity=0.9))

    def test_six_hour_window_edges(self) -> None:
        feed(self.root, [events.KIND_NORMAL], [359.0])
        self.assertFalse(self.hit(intent.INTENT_LONELY, familiarity=0.0))
        with tempfile.TemporaryDirectory() as temp:
            older = helpers.make_root(temp)
            feed(older, [events.KIND_NORMAL], [361.0])
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                res = intent.rank(root=older, now=BASE,
                                  context=dict(ALL_HIT_CONTEXT, familiarity=0.0))
        self.assertTrue([r["hit"] for r in res["order"]
                         if r["intent"] == intent.INTENT_LONELY][0])


class NoClockTest(unittest.TestCase):
    """铁律：不挂定时器/线程/cron，时间一律读时现算。"""

    def source(self) -> str:
        return Path(intent.__file__).read_text(encoding="utf-8")

    def test_no_scheduler_vocabulary(self) -> None:
        text = self.source()
        for word in ("threading", "Thread(", "Timer", "cron", "schedule",
                     "sleep(", "setInterval", "time.time()"):
            self.assertNotIn(word, text, "intent.py 里不该出现 %s" % word)

    def test_public_functions_take_now(self) -> None:
        import inspect
        for fn in (intent.rank, intent.load_weights):
            params = inspect.signature(fn).parameters
            self.assertIn("root", params)
        self.assertIn("now", inspect.signature(intent.rank).parameters)


if __name__ == "__main__":
    unittest.main()
