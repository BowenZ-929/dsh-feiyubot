"""重写的判据只管判：怎么让模型改、改成什么样才算改对了、改不动怎么说。

判据一个字都不写在这份文件里：时限、次数、长度区间、三条正则（数字 / 英文 / 引号）、三条铁律、
六条判据的说法、四种「为什么改不动」全部从 organs/mouth/data/rewrite.json 读（走 warehouse）——
要改判据就改数据，不改测试、更不改 rewrite.py。

本文件是纯函数：不调模型、不联网、不取时钟、不写盘。发请求那一步在 api/chat.py，它的测试在
api/tests/test_chat_api.py 里（一律假 transport，一个字节都不发真网络）。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from organs.mouth import rewrite as rewriting
from organs.mouth.rewrite import RewriteBroken, Unrewritable
from organs.mouth.warehouse import load

BOOK = load("rewrite")

# 三句素材：一句普通台词、一句带数字与英文与引号的台词、一句短的
PLAIN = "主人回来啦，我把碗放桌上了。"
KEEPS = "记得3点看 README 里的「三秒」那句。"


def _write_and_load(tmp: str, changes: dict) -> dict:
    """把改过的判据表落进临时目录，再按仓库的读法读回来（读法与线上一致，只换根目录）。"""
    book = dict(BOOK)
    book.update(changes)
    path = os.path.join(tmp, "rewrite.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(book, handle, ensure_ascii=False)
    return load("rewrite", root=tmp)


class PromptCarriesTheThreeRules(unittest.TestCase):
    """prompt() 拼出来的那一段：抬头 + 三条铁律 + 这一次命中的那一处 + 原文。"""

    HIT = {"clause_id": "某条条款", "kind": "某一类", "reason": "某一句人话", "matched": "某几个字"}

    def test_it_carries_the_rules_the_original_and_the_hit(self):
        text = rewriting.prompt(PLAIN, self.HIT)
        self.assertIn(BOOK["prompt"]["head"], text.split("\n")[0])       # 第一行是抬头
        for rule in BOOK["prompt"]["rules"]:
            self.assertIn(rule, text)                                     # 三条铁律一条不少
        self.assertIn(PLAIN, text)                                        # 待改原文原样在里面
        self.assertIn(self.HIT["clause_id"], text)                        # 命中的那一条点得出名字
        self.assertIn(self.HIT["matched"], text)                          # 撞上的那几个字也在

    def test_the_hit_can_be_an_object_as_well_as_a_mapping(self):
        """闸给的是自己的 Hit（dataclass），也能直接交进来：两处都不许只认一种形状。"""
        from organs.mouth.gate import Hit
        one = Hit(clause_id="某条条款", kind="某一类", reason="某一句人话", matched="某几个字")
        self.assertIn("某条条款", rewriting.prompt(PLAIN, one))

    def test_no_hit_does_not_leave_an_empty_pair_of_quotes(self):
        text = rewriting.prompt(PLAIN, None)
        self.assertIn(BOOK["prompt"]["hit_none"], text)
        self.assertNotIn("（）", text)

    def test_the_same_prompt_twice_is_the_same_text(self):
        """纯：同一个输入必须复算出同一段字（不许掺时钟、随机数）。"""
        self.assertEqual(rewriting.prompt(PLAIN, self.HIT), rewriting.prompt(PLAIN, self.HIT))


class AcceptOnlyASurgicalChange(unittest.TestCase):
    """accept 的六条判据：不合格的六条说法全部取自数据。"""

    def test_a_rephrasing_passes(self):
        ok, why = rewriting.accept(PLAIN, "碗我放桌上了，主人回来啦。")
        self.assertTrue(ok)
        self.assertEqual(BOOK["rule"]["passed"], why)

    def test_empty_and_non_text_are_rejected(self):
        for bad in ("", "   ", None, 3):
            with self.subTest(bad=bad):
                ok, why = rewriting.accept(PLAIN, bad)
                self.assertFalse(ok)
                self.assertIn(BOOK["rule"]["empty"].split("（")[0], why)   # 说法取自数据

    def test_a_verbatim_copy_is_rejected(self):
        ok, why = rewriting.accept(PLAIN, PLAIN)
        self.assertFalse(ok)
        self.assertIn("rule=same", why)

    def test_too_short_or_too_long_is_rejected(self):
        short = rewriting.accept(KEEPS, "记得。")
        self.assertFalse(short[0])
        self.assertIn("rule=length", short[1])
        long = rewriting.accept(PLAIN, PLAIN * 3)
        self.assertFalse(long[0])
        self.assertIn("rule=length", long[1])

    def test_a_missing_number_is_judged_a_whole_rewrite(self):
        ok, why = rewriting.accept(KEEPS, "记得看 README 里的「三秒」那句。")
        self.assertFalse(ok)
        self.assertIn("rule=digits", why)
        self.assertIn("3", why)                     # 哪一段数字没了，要说出来

    def test_a_missing_english_word_is_judged_a_whole_rewrite(self):
        ok, why = rewriting.accept(KEEPS, "记得3点看说明书里的「三秒」那句。")
        self.assertFalse(ok)
        self.assertIn("rule=latin", why)
        self.assertIn("README", why)

    def test_a_missing_quoted_line_is_judged_a_whole_rewrite(self):
        ok, why = rewriting.accept(KEEPS, "记得3点看 README 里的三秒那句。")
        self.assertFalse(ok)
        self.assertIn("rule=quotes", why)
        self.assertIn("「三秒」", why)

    def test_keeping_all_three_passes(self):
        ok, why = rewriting.accept(KEEPS, "记得3点看 README 里那句「三秒」，别错过。")
        self.assertTrue(ok, why)

    def test_the_length_range_comes_from_the_data(self):
        """长度区间从 0.4–2.0 改成 0.9–1.1：原本合格的一句就不合格了。"""
        with tempfile.TemporaryDirectory() as tmp:
            narrow = _write_and_load(tmp, {"length": {"min": 0.9, "max": 1.1}})
            self.assertTrue(rewriting.accept(PLAIN, "碗我放桌上了，主人回来啦。")[0])
            self.assertFalse(rewriting.accept(PLAIN, "碗我放桌上了。", narrow)[0])

    def test_the_keep_patterns_come_from_the_data(self):
        """数字那条正则改成「只认字母 x」：数字丢了也不再算整段重编。"""
        with tempfile.TemporaryDirectory() as tmp:
            loose = _write_and_load(tmp, dict(BOOK, keep=dict(BOOK["keep"], digits="x+")))
            ok, why = rewriting.accept(KEEPS, "记得看 README 里的「三秒」那句。", loose)
            self.assertTrue(ok, why)


class TheWordingComesFromTheData(unittest.TestCase):
    """四种「为什么改不动」与那句「留原话」全在数据里，代码只负责填。"""

    def test_every_why_comes_from_the_book(self):
        self.assertEqual(BOOK["why"]["timeout"].format(budget_ms=BOOK["budget_ms"]),
                         rewriting.timeout_why())
        self.assertEqual(BOOK["why"]["no_rewriter"], rewriting.no_rewriter_why())
        self.assertEqual(BOOK["why"]["not_text"], rewriting.not_text_why())
        self.assertIn("TypeError", rewriting.error_why(TypeError("坏了")))

    def test_the_kept_line_says_rewrite_failed(self):
        one = rewriting.failed_reason(rewriting.timeout_why())
        self.assertIn("rewrite-failed", one)        # 账本靠这个记号认门
        self.assertIn(str(BOOK["budget_ms"]), one)  # 时限也在里面
        self.assertIn("rewrite-failed", rewriting.clear_reason() + " rewrite-failed"
                      if False else one)

    def test_budget_and_attempts_come_from_the_data(self):
        self.assertEqual(BOOK["budget_ms"], rewriting.budget_ms())
        self.assertEqual(BOOK["attempts"], rewriting.attempts())

    def test_the_excerpt_is_short_and_says_so(self):
        cut = rewriting.excerpt(PLAIN)
        self.assertLessEqual(len(cut), BOOK["excerpt_chars"] + 1)     # 加一个省略号
        self.assertTrue(cut.endswith("…"))
        self.assertEqual("短句。", rewriting.excerpt("短句。"))

    def test_the_unrewritable_exception_carries_the_why(self):
        one = Unrewritable(rewriting.timeout_why())
        self.assertIn(str(BOOK["budget_ms"]), str(one))


class BrokenTableIsLoud(unittest.TestCase):
    """坏表当场响并点名，不许静默当成「没有判据」放行。"""

    def test_missing_section_is_named(self):
        with self.assertRaises(RewriteBroken) as caught:
            rewriting.accept(PLAIN, "碗我放桌上了。", {})
        self.assertIn("length", str(caught.exception))

    def test_bad_budget_is_named(self):
        for changes in ({"budget_ms": 0}, {"attempts": 0}, {"budget_ms": "8000"}):
            with self.subTest(changes=changes):
                with self.assertRaises(RewriteBroken) as caught:
                    rewriting.budget_ms(dict(BOOK, **changes))
                self.assertIn(list(changes)[0], str(caught.exception))

    def test_reversed_length_range_is_named(self):
        with self.assertRaises(RewriteBroken) as caught:
            rewriting.accept(PLAIN, "碗我放桌上了。", dict(BOOK, length={"min": 2.0, "max": 0.5}))
        self.assertIn("length", str(caught.exception))

    def test_a_bad_pattern_is_named(self):
        with self.assertRaises(RewriteBroken) as caught:
            rewriting.accept(PLAIN, "碗我放桌上了。",
                             dict(BOOK, keep=dict(BOOK["keep"], digits="([0-9")))
        self.assertIn("digits", str(caught.exception))

    def test_a_bad_quote_pair_is_named(self):
        with self.assertRaises(RewriteBroken) as caught:
            rewriting.accept(PLAIN, "碗我放桌上了。",
                             dict(BOOK, keep=dict(BOOK["keep"], quotes=[["「"]])))
        self.assertIn("quotes", str(caught.exception))

    def test_a_missing_rewrite_failed_marker_is_named(self):
        """reason.failed 里没有 rewrite-failed：账本就认不出这一笔，当场响。"""
        with self.assertRaises(RewriteBroken) as caught:
            rewriting.failed_reason("x", dict(BOOK, reason=dict(BOOK["reason"], failed="改了没成")))
        self.assertIn("rewrite-failed", str(caught.exception))


class RewriteStaysClean(unittest.TestCase):
    """硬约束：判据不进代码，也没有时钟、随机数与写盘。"""

    def setUp(self):
        self.source = Path(rewriting.__file__).read_text(encoding="utf-8")

    def test_source_has_no_reason_template(self):
        for line in BOOK["reason"].values():
            self.assertNotIn(line, self.source)
        for line in BOOK["why"].values():
            self.assertNotIn(line.split("{")[0], self.source)
        for line in BOOK["rule"].values():
            self.assertNotIn(line.split("{")[0], self.source)

    def test_source_has_no_prompt(self):
        for rule in BOOK["prompt"]["rules"]:
            self.assertNotIn(rule, self.source)
        self.assertNotIn(BOOK["prompt"]["head"], self.source)

    def test_source_has_no_keep_pattern(self):
        for key in ("digits", "latin"):
            self.assertNotIn(BOOK["keep"][key], self.source)

    def test_source_has_no_clock_or_random(self):
        self.assertNotRegex(self.source,
                            r"(?m)^\s*(?:import|from)\s+(?:time|datetime|random)\b")

    def test_source_writes_nothing_and_calls_no_model(self):
        for banned in ("open(", "write_text", ".mkdir", "json.dump", "os.remove", "shutil",
                       "urllib", "requests", "socket", "llm"):
            self.assertNotIn(banned, self.source)

    def test_source_imports_no_host(self):
        for word in ("astrbot", "deepseek-ai", "cordis", "OneBot", "NapCat", "aiocqhttp"):
            self.assertNotIn(word, self.source)


if __name__ == "__main__":
    unittest.main()
