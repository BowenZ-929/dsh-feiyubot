"""目标长度只管「这一轮该多短」，不管「最多多长」。

区间、标签、每一句判词全从 organs/mouth/data/style_target.json 读（走 warehouse）：这份测试里
一个区间数字都不写死，要改区间就改数据。验五件事——

1. 档位不同、目标区间不同（偏冷更短、偏热更长）；
2. 落在区间里 → pass，短于下限也 pass（目标是目标，不是配额）；
3. 超目标但没超硬上限 → **不截断**，改一次；改不成就明说「改不了」，放行并记账；
4. 超硬上限 → over_hard_cap=true、text 原样、嘴不截断（交核）；
5. 记账只走注入的 sink，本层自己不写盘；账本行由调用方给时刻。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.mouth import style
from organs.mouth.style import StyleBroken, StyleVerdict
from organs.mouth.warehouse import load

BOOK = load("style_target")
RANGES = BOOK["ranges"]
LINES = BOOK["reasons"]
COLD, HOT = "1", "4"                       # 数据里的两端档：1 偏冷短、4 偏热长
NOW = "2026-01-02T03:04:05"                # 时刻由调用方给，这里给一个固定的


def _text(chars: int) -> str:
    """造一句恰好这么多字的素材（只验长度，不验内容）。"""
    return "字" * chars


def _fields(tier: str, lo: int, hi: int, cap: int, before: int, after: int | None = None) -> dict:
    """拼模板要的那一套字段：判词里的数都必须来自数据，测试也照着同一套填。"""
    final = before if after is None else after
    return {"tier": tier, "label": RANGES[tier]["label"], "lo": lo, "hi": hi, "cap": cap,
            "chars": final, "before": before, "after": final}


def _copy_book() -> dict:
    """把数据整份拷一份再改：验的是「读数据」，不是改内存里那个模块级 dict。"""
    return json.loads(json.dumps(BOOK, ensure_ascii=False))


class RangesDifferByTier(unittest.TestCase):
    """第 1 条：偏冷档与偏热档的目标区间不是同一个，而且冷档更短。"""

    def test_cold_and_hot_have_different_ranges(self):
        cold = style.target_range(COLD, BOOK)
        hot = style.target_range(HOT, BOOK)
        self.assertNotEqual(cold, hot)
        self.assertLess(cold[1], hot[1])            # 冷档的上限比热档短
        self.assertEqual(cold, (RANGES[COLD]["lo"], RANGES[COLD]["hi"]))
        self.assertEqual(hot, (RANGES[HOT]["lo"], RANGES[HOT]["hi"]))

    def test_int_and_string_tier_are_the_same(self):
        self.assertEqual(style.target_range(1, BOOK), style.target_range("1", BOOK))

    def test_unknown_tier_names_it(self):
        for bad in (5, "9", None, True):
            with self.assertRaises(StyleBroken) as caught:
                style.target_range(bad, BOOK)
            self.assertIn(repr(bad), str(caught.exception))
        self.assertIn(repr(COLD), str(caught.exception))   # 报错里列着数据里现有的档（键是字符串）

    def test_ranges_come_from_data(self):
        """改数据就改行为：把冷档区间改成 5..5，同一句 30 字的话立刻算「在区间外」。"""
        book = _copy_book()
        book["ranges"][COLD] = dict(book["ranges"][COLD], lo=5, hi=5)
        verdict = style.apply(_text(30), COLD, 150, table=book)
        self.assertEqual(verdict.decision, "over_target")     # 被数据判成超目标了
        self.assertEqual((verdict.target_lo, verdict.target_hi), (5, 5))


class InsideTheRange(unittest.TestCase):
    """第 2 条：区间内 pass；短于下限也 pass（目标是目标，不是配额）。"""

    def test_inside_range_passes_untouched(self):
        lo, hi = style.target_range(COLD, BOOK)
        chars = (lo + hi) // 2
        verdict = style.apply(_text(chars), COLD, 150, table=BOOK)
        self.assertEqual(verdict.text, _text(chars))
        self.assertEqual(verdict.chars, chars)
        self.assertEqual(verdict.decision, "pass")
        self.assertEqual(verdict.rewrites, 0)
        self.assertFalse(verdict.over_hard_cap)
        self.assertEqual(verdict.reason,
                         LINES["pass"].format(**_fields(COLD, lo, hi, 150, chars)))

    def test_short_below_lo_still_passes(self):
        lo, hi = style.target_range(COLD, BOOK)
        chars = lo - 1
        verdict = style.apply(_text(chars), COLD, 150, table=BOOK)
        self.assertEqual(verdict.text, _text(chars))
        self.assertEqual(verdict.decision, "pass")
        self.assertEqual(verdict.reason,
                         LINES["short"].format(**_fields(COLD, lo, hi, 150, chars)))

    def test_hot_tier_keeps_a_longer_line_that_cold_tier_rewrites(self):
        """同一句：冷档嫌长、热档合格——目标区间真的按档走。"""
        cold_lo, cold_hi = style.target_range(COLD, BOOK)
        hot_lo, hot_hi = style.target_range(HOT, BOOK)
        chars = cold_hi + 5
        self.assertGreaterEqual(chars, hot_lo)
        self.assertLessEqual(chars, hot_hi)
        self.assertEqual(style.apply(_text(chars), HOT, 300, table=BOOK).decision, "pass")
        self.assertEqual(style.apply(_text(chars), COLD, 300, table=BOOK).decision, "over_target")


class OverTargetIsRewrittenOnce(unittest.TestCase):
    """第 3 条：超目标不截断，改一次；改不了就明说「改不了」，放行并记账。"""

    def test_rewritten_into_range(self):
        lo, hi = style.target_range(COLD, BOOK)
        chars = hi + 10
        verdict = style.apply(_text(chars), COLD, 150, table=BOOK, rewrite=lambda text: text[:lo])
        self.assertEqual(verdict.decision, "rewritten")
        self.assertEqual(verdict.text, _text(lo))
        self.assertEqual(verdict.chars, lo)
        self.assertEqual(verdict.rewrites, 1)
        self.assertFalse(verdict.over_hard_cap)
        self.assertEqual(verdict.reason,
                         LINES["rewritten"].format(**_fields(COLD, lo, hi, 150, chars, lo)))

    def test_without_rewrite_it_says_it_cannot(self):
        """没给 rewrite：放行 + 记账，判词说的是「改不了」，不是「合格」。"""
        lo, hi = style.target_range(COLD, BOOK)
        chars = hi + 10
        noted = []
        verdict = style.apply(_text(chars), COLD, 150, table=BOOK,
                              sink=lambda decision, reason: noted.append((decision, reason)))
        expected = LINES["no_rewrite"].format(**_fields(COLD, lo, hi, 150, chars))
        self.assertEqual(verdict.decision, "over_target")
        self.assertEqual(verdict.text, _text(chars))          # 原样放行，没截断
        self.assertEqual(verdict.chars, chars)
        self.assertEqual(verdict.rewrites, 0)
        self.assertFalse(verdict.over_hard_cap)
        self.assertEqual(verdict.reason, expected)
        self.assertIn("改不了", verdict.reason)                # 说的是改不了，不是合格
        self.assertEqual(noted, [("over_target", expected)])

    def test_rewrite_still_over_releases_and_records(self):
        lo, hi = style.target_range(COLD, BOOK)
        chars = hi + 10
        after = hi + 2                                        # 改了，但还在区间外
        noted = []
        verdict = style.apply(_text(chars), COLD, 150, table=BOOK, rewrite=lambda text: _text(after),
                              sink=lambda decision, reason: noted.append((decision, reason)))
        self.assertEqual(verdict.decision, "over_target")
        self.assertEqual(verdict.text, _text(after))          # 发改写后的那句（不截断）
        self.assertEqual(verdict.chars, after)
        self.assertEqual(verdict.rewrites, 1)
        self.assertEqual(verdict.reason,
                         LINES["still_over"].format(**_fields(COLD, lo, hi, 150, chars, after)))
        self.assertEqual(len(noted), 1)

    def test_rewrite_failure_releases_the_original(self):
        lo, hi = style.target_range(COLD, BOOK)
        chars = hi + 10
        noted = []

        def boom(text: str) -> str:
            raise ValueError("模型客户端没接上")

        verdict = style.apply(_text(chars), COLD, 150, table=BOOK, rewrite=boom,
                              sink=lambda decision, reason: noted.append(decision))
        self.assertEqual(verdict.text, _text(chars))          # 原句照发，出口不断
        self.assertEqual(verdict.decision, "over_target")
        self.assertEqual(verdict.rewrites, 1)
        self.assertIn("ValueError", verdict.reason)
        self.assertEqual(noted, ["over_target"])

    def test_rewrite_returning_empty_releases_the_original(self):
        lo, hi = style.target_range(COLD, BOOK)
        verdict = style.apply(_text(hi + 10), COLD, 150, table=BOOK, rewrite=lambda text: "   ")
        self.assertEqual(verdict.text, _text(hi + 10))
        self.assertEqual(verdict.decision, "over_target")
        self.assertEqual(verdict.rewrites, 1)


class OverHardCapIsHandedToTheCore(unittest.TestCase):
    """第 4 条：超硬上限不截断，只举条子交核——截断是核的事。"""

    def test_not_truncated_and_flagged(self):
        lo, hi = style.target_range(COLD, BOOK)
        cap = hi + 100
        chars = cap + 5
        calls = []

        def never(_text_: str) -> str:            # 超上限就不该再找重写器
            calls.append(_text_)
            return _text_[:lo]

        noted = []
        verdict = style.apply(_text(chars), COLD, cap, table=BOOK, rewrite=never,
                              sink=lambda decision, reason: noted.append((decision, reason)))
        self.assertTrue(verdict.over_hard_cap)
        self.assertEqual(verdict.text, _text(chars))         # 原样，一个字节都不删
        self.assertEqual(verdict.chars, chars)
        self.assertEqual(verdict.decision, "over_target")
        self.assertEqual(verdict.rewrites, 0)
        self.assertEqual(calls, [])                          # 没去改
        self.assertEqual((verdict.target_lo, verdict.target_hi), (lo, hi))   # 目标区间照旧报出来
        self.assertEqual(verdict.reason,
                         LINES["over_hard_cap"].format(**_fields(COLD, lo, hi, cap, chars)))
        self.assertEqual(noted, [("over_target", verdict.reason)])

    def test_bad_hard_cap_raises(self):
        for bad in (None, "150", 150.0, True):
            with self.assertRaises(StyleBroken):
                style.apply(_text(10), COLD, bad, table=BOOK)


class Ledger(unittest.TestCase):
    """第 5 条：记账只走注入的 sink；账本行由调用方给时刻，路径口径与 drops 一致。"""

    def test_record_appends_one_row_per_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "style.jsonl")
            style.record(path, "over_target", "改不了，放行", NOW)
            style.record(path, "rewritten", "改一次就进区间了", NOW)
            lines = Path(path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            rows = [json.loads(line) for line in lines]
            self.assertEqual([row["decision"] for row in rows], ["over_target", "rewritten"])
            self.assertEqual([row["ts"] for row in rows], [NOW, NOW])
            self.assertTrue(all(row["reason"] for row in rows))

    def test_record_refuses_row_without_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                style.record(os.path.join(tmp, "style.jsonl"), "pass", "", NOW)

    def test_path_of_three_levels(self):
        self.assertEqual(style.path_of(None).name, style.LEDGER_NAME)
        with mock.patch.dict(os.environ, {style.ENV_VAR: os.path.join(tempfile.gettempdir(), "tracedir")}):
            self.assertEqual(style.path_of(None),
                             Path(tempfile.gettempdir()) / "tracedir" / style.LEDGER_NAME)
        self.assertEqual(style.path_of("/tmp/one.jsonl"), Path("/tmp/one.jsonl"))
        self.assertEqual(style.path_of("/tmp/dir"), Path("/tmp/dir") / style.LEDGER_NAME)

    def test_apply_never_writes_on_its_own(self):
        """不给 sink 就一行都不写：写盘是调用方的决定（本层不取时钟、不写盘）。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {style.ENV_VAR: tmp}):
                style.apply(_text(style.target_range(COLD, BOOK)[1] + 10), COLD, 150, table=BOOK)
                self.assertEqual(os.listdir(tmp), [])


class DataIsBrokenLoudly(unittest.TestCase):
    """数据坏了当场响并指名：缺键不补默认值。"""

    def test_missing_range_bounds_raise(self):
        book = _copy_book()
        del book["ranges"][COLD]["hi"]
        with self.assertRaises(StyleBroken) as caught:
            style.target_range(COLD, book)
        self.assertIn(COLD, str(caught.exception))
        self.assertIn("hi", str(caught.exception))

    def test_missing_reason_template_raises(self):
        book = _copy_book()
        del book["reasons"]["no_rewrite"]
        with self.assertRaises(StyleBroken) as caught:
            style.apply(_text(RANGES[COLD]["hi"] + 10), COLD, 150, table=book)
        self.assertIn("no_rewrite", str(caught.exception))

    def test_lo_above_hi_raises(self):
        book = _copy_book()
        book["ranges"][COLD]["lo"] = book["ranges"][COLD]["hi"] + 1
        with self.assertRaises(StyleBroken):
            style.target_range(COLD, book)


if __name__ == "__main__":
    unittest.main()
