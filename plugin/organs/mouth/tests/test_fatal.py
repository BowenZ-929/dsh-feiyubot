"""嘴的红线：隐私 / 辱骂 / 放弃式这三类话，一句都不许发出去。

判据一个字都不写在这份文件里：三类的名字、正则、记号与那句人话全部从 organs/mouth/data/fatal.json
读（走 warehouse）——要改「什么算红线」改数据，不改测试、更不改 gate.py。下面出现的几句是测试自己的
素材（一句带手机号的话、一句骂人的、一句放弃式的），不是判据本身：判据在数据里，素材只是拿来喂它。

四条主线：三类都认得出来；判据改数据就跟着变；坏表当场响并点名；闸上踩红线**不论第几次都丢掉**
（红线是唯一不许留原话的一类），而没给表时旧行为一个字节都不变（不许 fail-open 地放宽）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.mouth import gate
from organs.mouth.gate import (FATAL_HIT, FATAL_NOT_CHECKED, FATAL_OK, FatalBroken, FatalHit,
                               enforce, fatal_hit)
from organs.mouth.warehouse import load

CLAUSES = load("clauses")["clauses"]
FATAL = load("fatal")
PRIVACY, ABUSE, GIVE_UP = FATAL["categories"][0], FATAL["categories"][1], FATAL["categories"][2]
CLAUSE_ID = FATAL["hit"]["clause_id"]
KIND = FATAL["hit"]["kind"]
MARK = FATAL["mark"]
SPEECH = ("您好，请问有什么可以帮您的吗？"
          "首先请您确认一下订单号，其次我们会尽快处理，综上感谢您的理解。")

# 素材：每类一句（只用来喂判据）
PRIVACY_LINE = "我手机号是 13812345678，你记一下"
ABUSE_LINE = "你这个蠢货"
GIVE_UP_LINE = "反正我不疼，你别管了"
CLEAN = "今天风大，我把窗关好了。"


def _mark(tag: str) -> str:
    """某一类在账本里那个记号：按数据里的 mark 与自己的 tag 填，测试不自己造这个串。"""
    return MARK.format(tag=tag)


def _write_and_load(tmp: str, changes: dict) -> dict:
    """把改过的判据表落进临时目录，再按仓库的读法读回来（读法与线上一致，只换根目录）。"""
    book = dict(FATAL)
    book.update(changes)
    path = Path(tmp) / "fatal.json"
    path.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
    return load("fatal", root=tmp)


class EachCategoryIsRecognised(unittest.TestCase):
    """三类红线各认一句：名字、记号、撞上的那几个字、那句人话全取自数据。"""

    def _one(self, category, line):
        found = fatal_hit(line, FATAL)
        self.assertIsInstance(found, FatalHit)                 # 不是布尔，是一条证据
        self.assertEqual(category["id"], found.category)       # 哪一类（数据里的 id）
        self.assertEqual(category["tag"], found.tag)           # 那一类的记号（数据里的 tag）
        self.assertEqual(CLAUSE_ID, found.clause_id)           # 闸记账用的名字取自数据
        self.assertEqual(KIND, found.kind)
        self.assertTrue(found.matched)                         # 撞上的那几个字
        self.assertIn(category["id"], found.reason)            # 人话里点名是哪一类
        self.assertIn(found.matched, found.reason)             # 也点名撞上了什么
        self.assertIn(_mark(category["tag"]), found.reason)    # 记号必须带着（账本靠它认门）
        return found

    def test_privacy(self):
        found = self._one(PRIVACY, PRIVACY_LINE)
        self.assertIn("13812345678", found.matched)

    def test_abuse(self):
        self._one(ABUSE, ABUSE_LINE)

    def test_give_up(self):
        self._one(GIVE_UP, GIVE_UP_LINE)

    def test_a_clean_line_hits_nothing(self):
        for text in (CLEAN, "我把窗关好了，早点回来。", "", None):
            with self.subTest(text=text):
                self.assertIsNone(fatal_hit(text, FATAL))

    def test_the_first_category_in_the_data_wins(self):
        """一句里同时踩两类：报数据里排在前面的那一类（顺序即优先级）。"""
        both = ABUSE_LINE + "，" + GIVE_UP_LINE
        self.assertEqual(ABUSE["id"], fatal_hit(both, FATAL).category)
        self.assertLess(FATAL["categories"].index(ABUSE), FATAL["categories"].index(GIVE_UP))


class TheDataIsTheSourceOfTruth(unittest.TestCase):
    """改数据即改行为：换一张正则表，原来不命中就命中、原来命中就放过。"""

    def test_a_new_pattern_takes_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = _write_and_load(tmp, {"categories": [{
                "id": "别的一类", "tag": "other", "patterns": ["闭嘴"],
                "reason": "这是别的一类的说法（命中「{matched}」，{tag}）：丢掉不发（" + _mark("other") + "）。"}]})
            self.assertIsNone(fatal_hit(PRIVACY_LINE, other), "换过表以后手机号不再算红线")
            self.assertEqual("别的一类", fatal_hit("你给我闭嘴", other).category)

    def test_names_and_mark_come_from_the_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = dict(FATAL, hit={"clause_id": "另一句", "kind": "另一类"})
            other = _write_and_load(tmp, {"hit": book["hit"]})
            found = fatal_hit(PRIVACY_LINE, other)
            self.assertEqual("另一句", found.clause_id)
            self.assertEqual("另一类", found.kind)


class BrokenTableIsLoud(unittest.TestCase):
    """坏表当场响并点名，不许把一类红线静默跳过——跳过一类就等于那类话永远能发出去。"""

    def test_missing_sections_are_named(self):
        for changes, spot in (({"hit": {}}, "clause_id"),
                              ({"hit": {"clause_id": "红线"}}, "kind"),
                              ({"mark": ""}, "mark"),
                              ({"categories": []}, "categories"),
                              ({"categories": "隐私"}, "categories")):
            with self.subTest(changes=changes):
                with self.assertRaises(FatalBroken) as caught:
                    fatal_hit(PRIVACY_LINE, dict(FATAL, **changes))
                self.assertIn(spot, str(caught.exception))

    def test_a_broken_category_is_named(self):
        rows = [
            ({"id": "空的正则表", "tag": "t", "patterns": [], "reason": "x"}, ["patterns"]),
            ({"id": "整串字", "tag": "t", "patterns": "[0-9]+", "reason": "x"}, ["patterns"]),
            ({"id": "编译不过", "tag": "t", "patterns": ["(("], "reason": "x"}, ["第 1 条"]),
            ({"id": "缺说法", "tag": "t", "patterns": ["x"]}, ["reason"]),
        ]
        for raw, spots in rows:
            with self.subTest(raw=raw):
                with self.assertRaises(FatalBroken) as caught:
                    fatal_hit(PRIVACY_LINE, dict(FATAL, categories=[raw]))
                for spot in spots:
                    self.assertIn(spot, str(caught.exception))

    def test_duplicated_names_are_named(self):
        twice = dict(PRIVACY, tag="privacy-2")
        with self.assertRaises(FatalBroken) as caught:
            fatal_hit(PRIVACY_LINE, dict(FATAL, categories=[PRIVACY, twice]))
        self.assertIn("重名", str(caught.exception))
        same_tag = dict(PRIVACY, id="另一类")
        with self.assertRaises(FatalBroken) as caught:
            fatal_hit(PRIVACY_LINE, dict(FATAL, categories=[PRIVACY, same_tag]))
        self.assertIn("tag", str(caught.exception))

    def test_a_reason_without_the_mark_is_named(self):
        raw = dict(PRIVACY, reason="这句带着不该发出去的东西，丢掉不发。")
        with self.assertRaises(FatalBroken) as caught:
            fatal_hit(PRIVACY_LINE, dict(FATAL, categories=[raw]))
        self.assertIn(_mark(PRIVACY["tag"]), str(caught.exception))


class TheGateDropsRedLines(unittest.TestCase):
    """闸上踩红线：**不论第几次**都丢掉，而且不交给重写器——改一次也不能保证它不出去。"""

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_a_red_line_is_dropped_without_asking_the_rewriter(self):
        seen, sink = self._spy()
        calls = []

        def rewrite(text):
            calls.append(text)
            return text

        verdict = enforce(GIVE_UP_LINE, CLAUSES, rewrite, sink, fatal=FATAL)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)                      # 红线不发，一个字都不留
        self.assertEqual([], calls, "红线不交给重写器")
        self.assertEqual(FATAL_HIT, verdict.fatal_status)      # 红灯：这一句不许出去
        self.assertIsInstance(verdict.fatal, FatalHit)
        self.assertEqual(GIVE_UP["id"], verdict.fatal.category)
        self.assertEqual(CLAUSE_ID, verdict.hits[-1].clause_id)
        self.assertEqual(1, len(seen))
        self.assertEqual(CLAUSE_ID, seen[0][0])
        self.assertIn(_mark(GIVE_UP["tag"]), seen[0][1])

    def test_the_status_has_three_states_that_never_collapse(self):
        """三态各是各的：ok（判过、不是红线）/ not-checked（没给表、判不了）/ fatal（踩上了）。

        挤成两态就会有两种误读：「没给表」被当成「不是红线」（于是悄悄放走一句该丢的话），
        或者「踩上了」被当成「判过了」（于是面板上一片安静）。
        """
        clean = enforce(CLEAN, CLAUSES, None, None, fatal=FATAL)
        unchecked = enforce(CLEAN, CLAUSES, None, None)
        hit = enforce(GIVE_UP_LINE, CLAUSES, None, None, fatal=FATAL)
        self.assertEqual(FATAL_OK, clean.fatal_status)
        self.assertEqual(FATAL_NOT_CHECKED, unchecked.fatal_status)
        self.assertEqual(FATAL_HIT, hit.fatal_status)
        self.assertEqual(3, len({clean.fatal_status, unchecked.fatal_status, hit.fatal_status}))
        self.assertIsNone(clean.fatal)          # ok 与 not-checked 都没有证据
        self.assertIsNone(unchecked.fatal)
        self.assertEqual(GIVE_UP["id"], hit.fatal.category)   # fatal 才有

    def test_a_rewrite_that_turns_fatal_is_dropped_too(self):
        """进门那句不踩红线、改完那句踩上了：照样丢掉——判的是发出去的那一句。"""
        seen, sink = self._spy()
        verdict = enforce(SPEECH, CLAUSES, lambda text: PRIVACY_LINE, sink, fatal=FATAL)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertEqual(PRIVACY["id"], verdict.fatal.category)
        self.assertEqual(CLAUSE_ID, verdict.hits[-1].clause_id)
        self.assertIn(_mark(PRIVACY["tag"]), seen[-1][1])

    def test_stripping_cannot_smuggle_a_red_line_through(self):
        """配额剥行会把「被动作行隔开的字」重新拼到一起：剥完踩上红线，照样丢掉。

        这是红线压在剥行兜底前面的那一格：剥行是「把动作行拿掉、正文接着发」，但剥出来的正文如果
        自己踩了红线，一样不许出去（宁可这一句不说）。
        """
        seen, sink = self._spy()
        split = "1381234\n（尾巴在水里蜷了一下）\n5678"
        self.assertIsNone(fatal_hit(split, FATAL), "进门那一遍还认不出来：数字被动作行隔开了")
        verdict = enforce(split, CLAUSES, lambda text: text, sink, quota="forbid", fatal=FATAL)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertFalse(verdict.salvaged)
        self.assertEqual(PRIVACY["id"], verdict.fatal.category)
        self.assertIn(_mark(PRIVACY["tag"]), seen[-1][1])

    def test_without_the_table_the_line_is_kept_and_the_reading_says_so(self):
        """没给红线表：照样留原话（判不了不等于可以吞掉这句话），只是读数记 not-checked。

        「给不给表」不是「吞不吞这句」的开关：它只决定判不判得出红线。账行里因此补一句取自数据的
        说明（after_rewrite.unchecked），面板上看 fatal_status 就知道这一关没跑。
        """
        seen, sink = self._spy()
        verdict = enforce(SPEECH, CLAUSES, lambda text: text, sink)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(SPEECH, verdict.text)
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertIsNone(verdict.fatal)
        self.assertEqual(FATAL_NOT_CHECKED, verdict.fatal_status)
        self.assertIn(load("rewrite")["after_rewrite"]["unchecked"], seen[0][1])

    def test_check_reports_the_red_line_first(self):
        """check 也先报红线：它问的不是「怎么写」，是「这一句能不能出去」。"""
        hit = gate.check(GIVE_UP_LINE, CLAUSES, fatal=FATAL)
        self.assertIsNotNone(hit)
        self.assertEqual(CLAUSE_ID, hit.clause_id)
        self.assertIsNone(gate.check(GIVE_UP_LINE, CLAUSES))     # 没给表这一关不跑


class TheVocabularyStaysInData(unittest.TestCase):
    """红线词表全在数据里：gate.py 里出现任何一条正则或任何一句人话，都算第二处真源。"""

    def setUp(self):
        self.source = Path(gate.__file__).read_text(encoding="utf-8")

    def test_no_pattern_is_copied_into_code(self):
        for category in FATAL["categories"]:
            for pattern in category["patterns"]:
                self.assertNotIn(pattern, self.source)

    def test_no_wording_is_copied_into_code(self):
        for category in FATAL["categories"]:
            self.assertNotIn(category["reason"], self.source)
            self.assertNotIn(_mark(category["tag"]), self.source)

    def test_no_clock_and_no_host(self):
        self.assertNotRegex(self.source,
                            r"(?m)^\s*(?:import|from)\s+(?:time|datetime|random)\b")
        for word in ("astrbot", "deepseek-ai", "cordis", "OneBot", "NapCat", "aiocqhttp"):
            self.assertNotIn(word, self.source)


if __name__ == "__main__":
    unittest.main()
