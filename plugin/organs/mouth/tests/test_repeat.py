"""内容去重只管判：同一动作行、同一开场，在最近几轮里又回来一次就判废。

判据一个字都不写在这份文件里：两个数（开场取几个字、看最近几轮）、两张字符表、两条判据的名字、
闸用的两个名字全部从 organs/mouth/data/repeat.json 读（走 warehouse）——要改判据就改数据，
不改测试、更不改 repeat.py。

「改数据即改行为」是这一关的全部意义，所以下面有一组测试把 repeat.json 拷到临时目录、改一个数
再按同样的读法读回来：验的是「读数据」，不是验内存里那个 dict。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from organs.mouth import repeat as repeats
from organs.mouth.repeat import RepeatBroken, RepeatHit, RepeatSkip
from organs.mouth.warehouse import load

REPEAT = load("repeat")
KIND_OPENING = REPEAT["kinds"]["opening"]      # 数据里的「哪一路」名字，测试不自己造
KIND_ACTION = REPEAT["kinds"]["action_line"]
TAG = REPEAT["tag"]
SKIP_CLAUSE_ID = REPEAT["hit"]["skip_clause_id"]
SKIP_MARK = "skipped-by-request"               # reason.skip 里那个记号，账本靠它认门

# 三条回放用的素材：第 1 条是原句；第 2 条换了开场、动作行只差标点；第 3 条换了动作行、开场同第 1 条。
FIRST = "主人回来啦，我把碗放桌上了。\n（尾巴尖蜷了一下）"
SECOND = "天有点凉了，你多穿点。\n（尾巴尖蜷了一下。）"
THIRD = "主人回来啦，灯我给你留着呢。"
CLEAN = "明天风大，我把窗关好了。"


def _write_and_load(tmp: str, changes: dict) -> dict:
    """把改过的判据表落进临时目录，再按仓库的读法读回来（读法与线上一致，只换根目录）。"""
    book = dict(REPEAT)
    book.update(changes)
    path = os.path.join(tmp, "repeat.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(book, handle, ensure_ascii=False)
    return load("repeat", root=tmp)


class SameLineAndSameOpening(unittest.TestCase):
    """两条判据本身：动作行逐字相等才算，开场只看前几个字。"""

    def test_empty_history_hits_nothing(self):
        self.assertIsNone(repeats.check(FIRST, []))          # 还没有历史就没得重复
        self.assertIsNone(repeats.check(FIRST, [] , None))

    def test_second_hits_the_action_line(self):
        hit = repeats.check(SECOND, [FIRST])
        self.assertIsInstance(hit, RepeatHit)
        self.assertEqual(KIND_ACTION, hit.kind)              # 命中的是动作行那一路（名字取自数据）
        self.assertEqual("尾巴尖蜷了一下", hit.matched)       # 去空白与标点之后的整行
        self.assertEqual(0, hit.prev_index)
        self.assertIn(TAG, hit.reason)                       # 弃言账靠这个记号认判据
        self.assertIn("尾巴尖蜷了一下", hit.reason)           # 人话里点名撞上的那一行

    def test_third_hits_the_opening(self):
        hit = repeats.check(THIRD, [FIRST, SECOND])
        self.assertIsInstance(hit, RepeatHit)
        self.assertEqual(KIND_OPENING, hit.kind)
        self.assertEqual("主人回来啦，", hit.matched)          # 去空白之后的前几个字
        self.assertEqual(0, hit.prev_index)
        self.assertIn(TAG, hit.reason)

    def test_clean_line_hits_nothing(self):
        for text in (CLEAN, "今天不一定回得来。", ""):
            with self.subTest(text=text):
                self.assertIsNone(repeats.check(text, [FIRST, SECOND, THIRD]))

    def test_nearest_occurrence_is_reported(self):
        """同一句在窗口里出现两次：报最近的那一次（第几条要看得出是近的）。"""
        hit = repeats.check(THIRD, [FIRST, CLEAN, FIRST])
        self.assertEqual(2, hit.prev_index)

    def test_action_line_ignores_punctuation_only(self):
        """动作行只差标点 → 命中。"""
        hit = repeats.check("天有点凉了，你多穿点。\n（尾巴尖蜷了一下！）", [FIRST])
        self.assertEqual(KIND_ACTION, hit.kind)

    def test_action_line_differing_in_wording_does_not_hit(self):
        """动作行差一个字 → 不命中：逐字相等才叫同一行，相似度会抹掉她的分寸。"""
        self.assertIsNone(repeats.check("天有点凉了，你多穿点。\n（尾巴尖蜷了两下）", [FIRST]))

    def test_too_short_to_count_as_an_opening(self):
        """字数不够 opening_chars 的不算开场：一句「好的」不该把「好的呀」判成同一个开场。"""
        self.assertIsNone(repeats.check("好的", ["好的呀"]))

    def test_names_come_from_data(self):
        """闸用的两个名字与放行那句人话都取自数据，repeat.py 一个都不写死。"""
        self.assertEqual((REPEAT["hit"]["clause_id"], REPEAT["hit"]["kind"]), repeats.names())
        self.assertIn(str(REPEAT["rounds"]), repeats.clear_reason())


class ARequestToRepeatIsSkipped(unittest.TestCase):
    """对方明说要她再说一遍：撞上重复也只记账、不判废（判据全在数据里）。

    两层缺一不可：**先真的撞上了重复**（同一动作行或同一开场），**再**看对方这句是不是在要她重复。
    只认「他说了再说一遍」就跳过，等于这一关每次都白设；没撞上的时候也没有「判废」可放。
    """

    ASKED = "你刚才说的那句再说一遍"

    def test_the_same_line_is_skipped_not_hit(self):
        skip = repeats.check(THIRD, [FIRST, SECOND], asked=self.ASKED)
        self.assertIsInstance(skip, RepeatSkip)
        self.assertNotIsInstance(skip, RepeatHit)          # 跳过不是命中：闸要分得开
        self.assertEqual(SKIP_MARK, skip.status)
        self.assertEqual(repeats.STATUS_SKIPPED, skip.status)
        self.assertEqual(SKIP_CLAUSE_ID, skip.clause_id)    # 记账用的名字取自数据
        self.assertIn(SKIP_MARK, skip.reason)
        self.assertIsInstance(skip.hit, RepeatHit)          # 本来会判废的那一条还在里面
        self.assertIn(skip.hit.matched, skip.reason)

    def test_without_the_request_it_is_still_a_hit(self):
        self.assertIsInstance(repeats.check(THIRD, [FIRST], asked="今天吃啥"), RepeatHit)
        self.assertIsInstance(repeats.check(THIRD, [FIRST]), RepeatHit)

    def test_an_empty_ask_never_exempts(self):
        """验收：asked 为空一律不豁免——不许默认豁免。"""
        for empty in ("", None, "   ", "\n"):
            with self.subTest(empty=empty):
                self.assertIsInstance(repeats.check(THIRD, [FIRST], asked=empty), RepeatHit)

    def test_every_variant_the_data_lists_counts(self):
        for asked in ("再说一遍", "再说一次", "再来一遍", "再念一遍", "再讲一遍", "再重复一次",
                      "重说一遍", "重复一遍", "重复一次", "你再说一遍", "你刚才说那句",
                      "刚才那句再说一遍", "刚刚说的那句", "上一句再说一下"):
            with self.subTest(asked=asked):
                self.assertIsInstance(repeats.check(THIRD, [FIRST], asked=asked), RepeatSkip)

    def test_the_markers_come_from_the_data(self):
        """把标记表换成一句别的话：原来的请求不再豁免，新标记才开始生效。"""
        with tempfile.TemporaryDirectory() as tmp:
            other = _write_and_load(tmp, {"request_markers": ["闭嘴"]})
            self.assertIsInstance(repeats.check(THIRD, [FIRST], other, self.ASKED), RepeatHit)
            self.assertIsInstance(repeats.check(THIRD, [FIRST], other, "闭嘴"), RepeatSkip)

    def test_a_request_without_a_repeat_is_not_a_skip(self):
        """没撞上重复就没有「判废」可放：不给跳过，也就不记那一笔账。"""
        self.assertIsNone(repeats.check(CLEAN, [FIRST], asked=self.ASKED))
        self.assertIsNone(repeats.check(FIRST, [], asked=self.ASKED))     # 还没有历史

    def test_an_action_line_is_skipped_too(self):
        """动作行那一路同样豁免：同一行原样回来，对方要她重复时也只记账。"""
        skip = repeats.check(SECOND, [FIRST], asked=self.ASKED)
        self.assertIsInstance(skip, RepeatSkip)
        self.assertEqual(KIND_ACTION, skip.hit.kind)


class TheRequestDecisionHasOneHome(unittest.TestCase):
    """请求标记只有一份真源：判定公开成 requested()，闸算一次、两关（去重与复述审计）共用。

    两关各判一次就会各有一份词表，迟早分岔成「这一关当他要重复、那一关不认」——同一句话在账本
    上两种说法。所以这里既验 requested() 本身，也验 check 认的是**传进来的那个结论**，而不是
    自己偷偷重算一遍。
    """

    ASKED = "你刚才说的那句再说一遍"

    def test_it_is_a_public_entry(self):
        self.assertIn("requested", repeats.__all__)

    def test_it_reads_the_data_markers(self):
        self.assertTrue(repeats.requested(self.ASKED))
        for asked in ("今天吃啥", "", None, "   ", "\n"):
            with self.subTest(asked=asked):
                self.assertFalse(repeats.requested(asked))

    def test_the_markers_come_from_the_table_it_is_given(self):
        """换一张表就换一套说法：判据只有数据那一份，函数里一条标记都不列。"""
        with tempfile.TemporaryDirectory() as tmp:
            other = _write_and_load(tmp, {"request_markers": ["闭嘴"]})
            self.assertFalse(repeats.requested(self.ASKED, other))
            self.assertTrue(repeats.requested("闭嘴", other))

    def test_check_obeys_the_conclusion_it_is_handed(self):
        """传进来的结论说了算：同一句 asked、两种结论，结局跟着变——闸就是这么用的。"""
        self.assertIsInstance(repeats.check(THIRD, [FIRST], None, self.ASKED, True), RepeatSkip)
        self.assertIsInstance(repeats.check(THIRD, [FIRST], None, self.ASKED, False), RepeatHit)
        self.assertIsInstance(repeats.check(THIRD, [FIRST], None, "今天吃啥", True), RepeatSkip)

    def test_none_means_the_caller_has_not_decided_yet(self):
        """默认 None：调用方没算过就现算一次，算的还是 requested() 那一处。"""
        self.assertIsInstance(repeats.check(THIRD, [FIRST], None, self.ASKED, None), RepeatSkip)
        self.assertIsInstance(repeats.check(THIRD, [FIRST], None, "今天吃啥", None), RepeatHit)


class NotCheckedIsNotClean(unittest.TestCase):
    """recent 为 None 是「没判」，空清单是「判过、没重复」：两件事同一个返回值，状态必须分得开。"""

    def test_none_returns_no_evidence(self):
        self.assertIsNone(repeats.check(FIRST, None))         # 没有证据 ≠ 判过没重复

    def test_status_vocabulary_is_three_words(self):
        self.assertEqual("ok", repeats.STATUS_OK)
        self.assertEqual("not-checked", repeats.STATUS_NOT_CHECKED)
        self.assertEqual(SKIP_MARK, repeats.STATUS_SKIPPED)
        self.assertEqual(3, len({repeats.STATUS_OK, repeats.STATUS_NOT_CHECKED,
                                 repeats.STATUS_SKIPPED}))


class WindowComesFromData(unittest.TestCase):
    """第 3 条验收：rounds 从 8 调到 1，只看最后一条，结论跟着变。"""

    def test_default_window_sees_the_first_line(self):
        hit = repeats.check(THIRD, [FIRST, SECOND])           # 默认窗口里两条都看得到
        self.assertIsNotNone(hit)
        self.assertEqual(0, hit.prev_index)

    def test_narrow_window_misses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            narrow = _write_and_load(tmp, {"rounds": 1})
            self.assertIsNone(repeats.check(THIRD, [FIRST, SECOND], narrow))   # 只看最后 1 条：判不了重复
            self.assertIsNotNone(repeats.check(THIRD, [SECOND, FIRST], narrow))  # 最后 1 条就是原句：照样命中


class OpeningLengthComesFromData(unittest.TestCase):
    """第 5 条验收：opening_chars 从 6 调到 12，原本「前 6 字相同」的两句不再命中。"""

    def test_six_chars_see_it(self):
        self.assertEqual(KIND_OPENING, repeats.check(THIRD, [FIRST]).kind)

    def test_twelve_chars_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_head = _write_and_load(tmp, {"opening_chars": 12})
            self.assertIsNone(repeats.check(THIRD, [FIRST], long_head))


class BrokenTableIsLoud(unittest.TestCase):
    """坏表当场响并点名，不许静默当成「没有判据」放行。"""

    def test_missing_section_is_named(self):
        with self.assertRaises(RepeatBroken) as caught:
            repeats.check(FIRST, [], {})
        self.assertIn("hit", str(caught.exception))

    def test_bad_number_is_named(self):
        for changes in ({"rounds": 0}, {"opening_chars": 0}, {"rounds": "8"}):
            with self.subTest(changes=changes):
                with self.assertRaises(RepeatBroken) as caught:
                    repeats.check(FIRST, [SECOND], dict(REPEAT, **changes))
                self.assertIn(list(changes)[0], str(caught.exception))

    def test_empty_char_table_is_named(self):
        with self.assertRaises(RepeatBroken) as caught:
            repeats.check(FIRST, [SECOND], dict(REPEAT, strip=""))
        self.assertIn("strip", str(caught.exception))

    def test_same_name_on_both_paths_is_named(self):
        same = dict(REPEAT, kinds={"opening": KIND_OPENING, "action_line": KIND_OPENING})
        with self.assertRaises(RepeatBroken) as caught:
            repeats.check(FIRST, [SECOND], same)
        self.assertIn("kinds", str(caught.exception))

    def test_broken_request_markers_are_loud(self):
        """标记表空着、写成整串字、或某一条正则编译不过，都当场响并点名——不许静默少一种豁免。"""
        for changes, spot in (({"request_markers": []}, "request_markers"),
                              ({"request_markers": "再说一遍"}, "request_markers"),
                              ({"request_markers": ["(("]}, "第 1 条"),
                              ({"hit": {"clause_id": "反复读", "kind": "内容去重"}}, "skip_clause_id")):
            with self.subTest(changes=changes):
                with self.assertRaises(RepeatBroken) as caught:
                    repeats.check(THIRD, [FIRST], dict(REPEAT, **changes), "再说一遍")
                self.assertIn(spot, str(caught.exception))

    def test_bad_recent_is_named(self):
        """recent 只能是「一串原文」，不是一整段话：按字切会把历史读成一个字一条。"""
        with self.assertRaises(RepeatBroken) as caught:
            repeats.check(FIRST, FIRST)
        self.assertIn("recent", str(caught.exception))
        with self.assertRaises(RepeatBroken) as caught:
            repeats.check(FIRST, [FIRST, 3])
        self.assertIn("第 1 条", str(caught.exception))


class RepeatStaysClean(unittest.TestCase):
    """硬约束：判据不进代码，也没有时钟、随机数与写盘。"""

    def setUp(self):
        self.source = Path(repeats.__file__).read_text(encoding="utf-8")

    def test_source_has_no_reason_template(self):
        for line in REPEAT["reason"].values():
            self.assertNotIn(line, self.source)
        self.assertNotIn(REPEAT["skip_reason"], self.source)     # 跳过那条说法也不许进代码

    def test_source_has_no_data_name(self):
        for word in (REPEAT["hit"]["clause_id"], REPEAT["hit"]["kind"],
                     SKIP_CLAUSE_ID, KIND_OPENING, KIND_ACTION):
            self.assertNotIn(word, self.source)

    def test_source_has_no_request_marker(self):
        """请求标记的正则也一条都不许进代码：要改「什么算要求重复」，改数据。"""
        for marker in REPEAT["request_markers"]:
            self.assertNotIn(marker, self.source)

    def test_source_has_no_clock_or_random(self):
        self.assertNotRegex(self.source,
                            r"(?m)^\s*(?:import|from)\s+(?:time|datetime|random)\b")

    def test_source_writes_nothing(self):
        for banned in ("open(", "write_text", ".mkdir", "json.dump", "os.remove", "shutil"):
            self.assertNotIn(banned, self.source)

    def test_source_imports_no_host(self):
        for word in ("astrbot", "deepseek-ai", "cordis", "OneBot", "NapCat", "aiocqhttp"):
            self.assertNotIn(word, self.source)


if __name__ == "__main__":
    unittest.main()
