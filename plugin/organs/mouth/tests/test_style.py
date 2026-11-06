"""目标长度与句末标点只管「这一轮该多短」「句末别句句带句号」，不管「最多多长」。

区间、场景、标签、阈值、每一句判词全从 organs/mouth/data/style_target.json 读（走 warehouse）：
这份测试里一个区间数字、一个比例都不写死，要改数就改数据。验六件事——

1. **按场景分表**：私聊与群聊不是同一套区间，群聊更短；同一场景里档位该不一样的地方也不一样；
   不认识场景当场响（不回落）；
2. 落在区间里 → pass，短于下限也 pass（目标是目标，不是配额）；
3. 超目标但没超硬上限 → **不截断**，改一次；改不成就明说「改不了」，放行并记账；
4. 超硬上限 → over_hard_cap=true、text 原样、嘴不截断（交核）；
5. **句末「。」比例**：少于最少句数不判（judged=false）；超上限改一次；二次仍超放行并记账；
6. 记账只走注入的 sink，本层自己不写盘；账本行由调用方给时刻；新稿要过 accept 那一关。
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
SCENE = BOOK["default_scene"]              # 没给场景时用哪一个：数据里定的
SCENES = BOOK["scenes"]
RANGES = SCENES[SCENE]["ranges"]           # 默认场景那套区间
LINES = BOOK["reasons"]
STOPPED = BOOK["stopped"]
MARK = STOPPED["mark"]
CEILING = STOPPED["ceiling"]
FEWEST = STOPPED["min_sentences"]
COLD, HOT = "1", "4"                       # 数据里的两端档
PRIVATE, GROUP = "private", "group"       # 两套实盘场景：/chat 的 scope 就给这两个值
OTHER = GROUP if SCENE != GROUP else PRIVATE   # 与「没给场景」那套不同的另一个场景
NOW = "2026-01-02T03:04:05"                # 时刻由调用方给，这里给一个固定的


def _text(chars: int) -> str:
    """造一句恰好这么多字的素材（只验长度，不验内容）。"""
    return "字" * chars


def _scene_label(scene: str) -> str:
    return SCENES[scene]["label"]


def _fields(tier: str, lo: int, hi: int, cap: int, before: int, after: int | None = None,
            scene: str = SCENE) -> dict:
    """拼模板要的那一套字段：判词里的数都必须来自数据，测试也照着同一套填。"""
    final = before if after is None else after
    return {"tier": tier, "label": RANGES[tier]["label"], "lo": lo, "hi": hi, "cap": cap,
            "chars": final, "before": before, "after": final,
            "scene": _scene_label(scene)}


def _copy_book() -> dict:
    """把数据整份拷一份再改：验的是「读数据」，不是改内存里那个模块级 dict。"""
    return json.loads(json.dumps(BOOK, ensure_ascii=False))


def _passage(stopped: int, plain: int) -> str:
    """造一段话：stopped 句以「。」收尾，plain 句以别的句末符号收尾（断句照旧成立）。"""
    return ("".join("这是第%d句。" % index for index in range(stopped))
            + "".join("那是第%d句！" % index for index in range(plain)))


class ScenesHaveTheirOwnRanges(unittest.TestCase):
    """第 1 条：场景各一套区间，档位在场景里照走，认不得就响。"""

    def test_default_scene_comes_from_data(self):
        self.assertEqual(style.target_range(COLD, BOOK), style.target_range(COLD, BOOK, scene=SCENE))
        self.assertEqual(style.describe(COLD, BOOK)["scene"], SCENE)

    def test_private_and_group_are_different_and_group_is_shorter(self):
        here = style.target_range(COLD, BOOK, scene=PRIVATE)
        there = style.target_range(COLD, BOOK, scene=GROUP)
        self.assertNotEqual(here, there, "两套场景的目标区间不该是同一套数")
        self.assertLess(there[0], here[0], "群聊的下限该比私聊小")
        self.assertLess(there[1], here[1],
                        "群聊的上限该比私聊短（数据里那条理由：群里有别人在等）")
        self.assertEqual(there, (SCENES[GROUP]["ranges"][COLD]["lo"],
                                 SCENES[GROUP]["ranges"][COLD]["hi"]))

    def test_the_no_scene_table_is_the_old_contract(self):
        """没给场景的调用方（/mouth/gate 干跑）读的是 tier_only：那两个数就是它的对外契约。

        api/tests/test_mouth_api.py 把 (8, 24) 与 (20, 72) 硬写在断言里，而那份文件不归这条线改——
        所以这里替它钉住：**改 /chat 的场景表，不许顺手把别人的端点一起改了**。
        """
        self.assertEqual(SCENE, "tier_only", "默认场景该是「只看档位」那套")
        self.assertEqual(style.target_range(COLD, BOOK), (8, 24))
        self.assertEqual(style.target_range(HOT, BOOK), (20, 72))
        self.assertNotEqual(SCENES[SCENE]["ranges"], SCENES[PRIVATE]["ranges"])
        self.assertNotEqual(SCENES[SCENE]["ranges"], SCENES[GROUP]["ranges"])

    def test_int_and_string_tier_are_the_same(self):
        self.assertEqual(style.target_range(1, BOOK), style.target_range("1", BOOK))

    def test_tiers_inside_a_scene_can_differ(self):
        """档位这一层还活着：群聊第 1 档与第 4 档不是同一个区间（私聊四档同宽）。"""
        cold = style.target_range(COLD, BOOK, scene=OTHER)
        hot = style.target_range(HOT, BOOK, scene=OTHER)
        self.assertNotEqual(cold, hot)
        self.assertLess(cold[1], hot[1])

    def test_unknown_tier_names_it(self):
        for bad in (5, "9", None, True):
            with self.assertRaises(StyleBroken) as caught:
                style.target_range(bad, BOOK)
            self.assertIn(repr(bad), str(caught.exception))
        self.assertIn(repr(COLD), str(caught.exception))   # 报错里列着数据里现有的档（键是字符串）

    def test_unknown_scene_names_it_and_never_falls_back(self):
        """场景不认识就响，**不回落默认场景**：回落一次，账本就分不清拿哪一套判的。"""
        with self.assertRaises(StyleBroken) as caught:
            style.target_range(COLD, BOOK, scene="party")
        self.assertIn("party", str(caught.exception))
        self.assertIn(repr(SCENE), str(caught.exception))

    def test_describe_carries_the_whole_reading(self):
        one = style.describe(COLD, BOOK, scene=OTHER)
        self.assertEqual(one["scene"], OTHER)
        self.assertEqual(one["label"], _scene_label(OTHER))
        self.assertEqual(one["lo"], SCENES[OTHER]["ranges"][COLD]["lo"])
        self.assertEqual(one["hi"], SCENES[OTHER]["ranges"][COLD]["hi"])
        self.assertEqual(one["p90"], SCENES[OTHER]["p90"])
        self.assertTrue(one["tier_label"])

    def test_retired_bounds_are_kept_but_not_used(self):
        """旧区间没删：留在 retired 里（带日期与理由）标作废，读区间只认 scenes 那一节。"""
        retired = BOOK["retired"]
        self.assertTrue(retired["reason"].strip())
        self.assertTrue(retired["date"].strip())
        self.assertEqual(set(RANGES), set(retired["ranges"]), "四档一个都不许丢")
        self.assertNotEqual(retired["ranges"], RANGES, "这一版换过区间：旧值不许和现值一样")
        for scene in SCENES:
            self.assertEqual(set(SCENES[scene]["ranges"]), set(retired["ranges"]))
            self.assertNotEqual(SCENES[scene]["ranges"], retired["ranges"],
                                "哪一套场景都不许把作废的那套又当现值用")

    def test_ranges_come_from_data(self):
        """改数据就改行为：把冷档区间改成 5..5，同一句 30 字的话立刻算「在区间外」。"""
        book = _copy_book()
        book["scenes"][SCENE]["ranges"][COLD] = dict(RANGES[COLD], lo=5, hi=5)
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
        self.assertEqual(verdict.scene, SCENE)
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

    def test_a_line_group_rewrites_is_fine_in_private(self):
        """同一句：群聊嫌长、私聊合格——目标区间真的按场景走。"""
        here_lo, here_hi = style.target_range(COLD, BOOK, scene=PRIVATE)
        there_lo, there_hi = style.target_range(COLD, BOOK, scene=GROUP)
        chars = there_hi + 1
        self.assertLessEqual(chars, here_hi, "这一句得落在私聊区间里")
        self.assertGreater(chars, there_hi, "而在群聊区间外")
        self.assertEqual(style.apply(_text(chars), COLD, 300, table=BOOK,
                                     scene=PRIVATE).decision, "pass")
        self.assertEqual(style.apply(_text(chars), COLD, 300, table=BOOK,
                                     scene=GROUP).decision, "over_target")


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


class StoppedMarks(unittest.TestCase):
    """第 5 条：句末「。」比例——句数不够不判，超上限改一次，二次仍超放行并记账。"""

    def test_all_full_stops_is_over_and_recorded(self):
        text = _passage(0, 0) + "这是第一句。" + "这是第二句。" + "这是第三句。"
        noted = []
        verdict = style.stops(text, table=BOOK,
                              sink=lambda decision, reason: noted.append((decision, reason)))
        self.assertTrue(verdict.judged, "三句以上这一关就该判")
        self.assertEqual(verdict.sentences, 3)
        self.assertEqual(verdict.stopped, 3)
        self.assertEqual(verdict.decision, "over_mark")
        self.assertEqual(verdict.rewrites, 0)
        self.assertEqual(verdict.text, text, "没给重写器就原样放行，不吞话")
        self.assertEqual(verdict.ceiling, CEILING)
        self.assertIn("改不了", verdict.reason)
        self.assertEqual([one[0] for one in noted], ["over_mark"])

    def test_fewer_sentences_than_the_minimum_is_not_judged(self):
        """少于最少句数不判——judged=false 不是「判过了没问题」。

        门槛是数据里的数（现在是 1：每一泡都判），所以这里按数据造：够不够 FEWEST 句，测试不写死。
        FEWEST 为 1 时唯一「不够」的情况就是空段，那也照样是「不判」，不是「合格」。
        """
        text = _passage(FEWEST - 1, 0) if FEWEST > 1 else ""
        verdict = style.stops(text, table=BOOK)
        self.assertFalse(verdict.judged)
        self.assertEqual(verdict.decision, "pass")
        self.assertLess(verdict.sentences, FEWEST)
        self.assertEqual(verdict.text, text)
        self.assertIn("不判", verdict.reason)

    def test_one_sentence_with_a_full_stop_is_judged(self):
        """门槛定成 1 之后：一泡一句、句末带句号，正是「像在写稿」的那种收尾，要判。"""
        if FEWEST > 1:
            self.skipTest("数据里的最少句数不是 1：这一条按当前门槛不成立")
        verdict = style.stops("这是第一句。", table=BOOK)
        self.assertTrue(verdict.judged)
        self.assertEqual((verdict.sentences, verdict.stopped), (1, 1))
        self.assertEqual(verdict.decision, "over_mark")

    def test_under_the_ceiling_passes(self):
        total = max(FEWEST, 4)
        stopped = int(CEILING * total)                 # 恰好落在上限以内（向下取整）
        text = _passage(stopped, total - stopped)
        verdict = style.stops(text, table=BOOK)
        self.assertTrue(verdict.judged)
        self.assertLessEqual(verdict.ratio, verdict.ceiling)
        self.assertEqual(verdict.decision, "pass")
        self.assertEqual(verdict.rewrites, 0)
        self.assertEqual(verdict.text, text)
        self.assertIn("没超过", verdict.reason)

    def test_rewrite_brings_it_under_the_ceiling(self):
        total = max(FEWEST, 4)
        text = _passage(total, 0)                      # 句句都带。
        better = _passage(int(CEILING * total), total - int(CEILING * total))
        verdict = style.stops(text, table=BOOK, rewrite=lambda _one: better)
        self.assertEqual(verdict.decision, "rewritten")
        self.assertEqual(verdict.text, better)
        self.assertEqual(verdict.rewrites, 1)
        self.assertLessEqual(verdict.ratio, verdict.ceiling)
        self.assertIn("落到上限里", verdict.reason)

    def test_second_time_still_over_releases_and_records(self):
        """二次仍超 → 放行并记账：标点是毛病，不是红线，不许为它把话吞掉。"""
        text = _passage(4, 0)
        noted = []
        verdict = style.stops(text, table=BOOK, rewrite=lambda _one: _passage(4, 0),
                              sink=lambda decision, reason: noted.append((decision, reason)))
        self.assertEqual(verdict.decision, "over_mark")
        self.assertEqual(verdict.rewrites, 1)
        self.assertEqual(verdict.text, _passage(4, 0))   # 改回来还是那句话，照发
        self.assertIn("仍超过", verdict.reason)
        self.assertEqual([one[0] for one in noted], ["over_mark"])

    def test_rewrite_failure_releases_the_original(self):
        text = _passage(4, 0)

        def boom(_one: str) -> str:
            raise ValueError("模型客户端没接上")

        verdict = style.stops(text, table=BOOK, rewrite=boom)
        self.assertEqual(verdict.text, text)
        self.assertEqual(verdict.decision, "over_mark")
        self.assertIn("ValueError", verdict.reason)

    def test_rewrite_returning_empty_releases_the_original(self):
        text = _passage(4, 0)
        verdict = style.stops(text, table=BOOK, rewrite=lambda _one: "   ")
        self.assertEqual(verdict.text, text)
        self.assertEqual(verdict.decision, "over_mark")
        self.assertEqual(verdict.rewrites, 1)

    def test_thresholds_come_from_data(self):
        """改数据就改行为：把上限抬到 1、最少句数抬到 99，同一段话立刻变成「没超」「不判」。"""
        text = _passage(4, 0)
        book = _copy_book()
        book["stopped"]["ceiling"] = 1.0
        self.assertEqual(style.stops(text, table=book).decision, "pass")
        book = _copy_book()
        book["stopped"]["min_sentences"] = 99
        relaxed = style.stops(text, table=book)
        self.assertFalse(relaxed.judged)
        self.assertEqual(relaxed.decision, "pass")

    def test_stops_never_writes_on_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {style.ENV_VAR: tmp}):
                style.stops(_passage(4, 0), table=BOOK)
                self.assertEqual(os.listdir(tmp), [])


class AcceptTheRewrite(unittest.TestCase):
    """第 6 条：新稿要过 accept（非空 / 与原文不同 / 数字不许丢），长度与句号率由上一层重算。"""

    def test_empty_is_refused(self):
        ok, why = style.accept("你等我三分钟", "   ", table=BOOK)
        self.assertFalse(ok)
        self.assertIn("空的", why)

    def test_identical_is_refused(self):
        ok, why = style.accept("你等我三分钟", "你等我三分钟", table=BOOK)
        self.assertFalse(ok)
        self.assertIn("相同", why)

    def test_losing_a_number_is_refused(self):
        ok, why = style.accept("你等我3分钟", "你等我三分钟", table=BOOK)
        self.assertFalse(ok)
        self.assertIn("3", why)

    def test_changed_and_kept_is_accepted(self):
        self.assertEqual(style.accept("你等我3分钟", "等我3分钟，马上", table=BOOK), (True, ""))


class PromptComesFromData(unittest.TestCase):
    """重写指令在数据里：填不出来就响，代码里不许再写一句「说短点」。"""

    def test_length_and_stops_prompts_fill(self):
        one = style.prompt("length", _text(40), table=BOOK, lo=12, hi=32, chars=40,
                           scene="私聊", tier="3")
        self.assertIn(_text(40), one)
        self.assertIn("12", one)
        two = style.prompt("stops", "甲。乙。丙。", table=BOOK, mark=MARK, stopped=3,
                           sentences=3, ratio=100.0)
        self.assertIn("甲。乙。丙。", two)
        self.assertIn(MARK, two)

    def test_missing_prompt_template_raises(self):
        book = _copy_book()
        del book["prompt"]["stops"]
        with self.assertRaises(StyleBroken) as caught:
            style.prompt("stops", "甲。", table=book, mark=MARK, stopped=1, sentences=1, ratio=100.0)
        self.assertIn("stops", str(caught.exception))

    def test_missing_placeholder_raises(self):
        book = _copy_book()
        with self.assertRaises(StyleBroken):
            style.prompt("length", "甲。", table=book)      # 少 lo / hi / chars / scene / tier


class Ledger(unittest.TestCase):
    """记账只走注入的 sink；账本行由调用方给时刻，路径口径与 drops 一致。"""

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

    def test_record_carries_the_target_range(self):
        """账本行带一列 target（"lo-hi"）：区间按场景分表，没有这一列就分不清拿哪一套判的。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "style.jsonl")
            lo, hi = style.target_range(COLD, BOOK)
            style.record(path, "over_target", "改不了，放行", NOW, target=(lo, hi))
            style.record(path, "over_target", "改不了，放行", NOW)      # 不给就不带这一列
            rows = [json.loads(line) for line in
                    Path(path).read_text(encoding="utf-8").splitlines()]
            self.assertEqual("%d-%d" % (lo, hi), rows[0]["target"])
            self.assertNotIn("target", rows[1], "不给 target 时旧行格式一个字都没变")

    def test_record_refuses_a_bad_target(self):
        """target 不是一对整数、也不是非空字符串：当场抛，不许写一行读不懂的账。"""
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ((1, 2, 3), "", "   ", 24, (True, 24), (8.0, 24), [8]):
                with self.assertRaises(ValueError):
                    style.record(os.path.join(tmp, "style.jsonl"), "over_target", "改不了", NOW,
                                 target=bad)

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
        del book["scenes"][SCENE]["ranges"][COLD]["hi"]
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
        book["scenes"][SCENE]["ranges"][COLD]["lo"] = RANGES[COLD]["hi"] + 1
        with self.assertRaises(StyleBroken):
            style.target_range(COLD, book)

    def test_missing_p90_raises(self):
        book = _copy_book()
        del book["scenes"][SCENE]["p90"]
        with self.assertRaises(StyleBroken) as caught:
            style.describe(COLD, book)
        self.assertIn("p90", str(caught.exception))

    def test_missing_stopped_section_raises(self):
        book = _copy_book()
        del book["stopped"]
        with self.assertRaises(StyleBroken) as caught:
            style.stops("甲。乙。丙。", table=book)
        self.assertIn("stopped", str(caught.exception))

    def test_bad_ceiling_raises(self):
        book = _copy_book()
        book["stopped"]["ceiling"] = "0.3"
        with self.assertRaises(StyleBroken):
            style.stops("甲。乙。丙。", table=book)

    def test_missing_scenes_raise(self):
        book = _copy_book()
        del book["scenes"]
        with self.assertRaises(StyleBroken) as caught:
            style.target_range(COLD, book)
        self.assertIn("scenes", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
