"""动作配额这一关只管判：这一轮装配没给写动作的资格，动作行就不许出现在回复里。

判据与说法一个字都不写在这份文件里：配额的写法、两个名字、人话模板从
organs/mouth/data/quota.json 读（走 warehouse），括号对与身体部位表从 action_note.json 借，
人称代词表从 policy.json 借。要改判据就改数据、不改测试、更不改 quota.py。

「数据改一条，行为跟着变」是这一关的全部意义，所以末尾有一组测试把 quota.json 拷到临时目录、
加一个别名再按同样的读法读回来——验的是「读数据」，不是验内存里那个 dict。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from organs.mouth import quota
from organs.mouth.quota import QuotaBroken, QuotaHit, check, strip
from organs.mouth.warehouse import load

BOOK = load("quota")
ACTION_NOTE = load("action_note")

# 一句真实的超配额：行首一段括注动作，后面还跟着正文（正文是她的分寸，一个字都不许动）
LINE = "（尾巴在水里晃了一下）可爱归可爱，那也不是胖。"
BODY = "可爱归可爱，那也不是胖。"


def _without_quota(book: dict) -> dict:
    """拷一份判据表但去掉 hit 一节：用来验「缺键当场响」，不动真数据。"""
    one = json.loads(json.dumps(book, ensure_ascii=False))
    one.pop("hit")
    return one


class UnmarkedIsNotAllowed(unittest.TestCase):
    """装配没标（None）→ 这一关没跑：返回 None，而且是 not-checked，不是 allow。"""

    def test_none_returns_nothing(self):
        self.assertIsNone(check(LINE, None))

    def test_the_two_statuses_are_different_words(self):
        self.assertNotEqual(quota.STATUS_OK, quota.STATUS_NOT_CHECKED)

    def test_allowed_never_hits_either(self):
        """标了 allow 同样返回 None——所以「返回 None」本身分不开这两件事，得看状态。"""
        self.assertIsNone(check(LINE, "allow"))


class ForbiddenHits(unittest.TestCase):
    """标了 forbid：出现动作行就命中，而且返回的是一条证据，不是布尔。"""

    def test_action_line_is_evidence_not_a_boolean(self):
        hit = check(LINE, "forbid")
        self.assertIsInstance(hit, QuotaHit)
        self.assertEqual("（尾巴在水里晃了一下）", hit.matched)
        self.assertEqual(("（尾巴在水里晃了一下）",), hit.lines)
        self.assertEqual(BOOK["hit"]["clause_id"], hit.clause_id)   # 名字取自数据
        self.assertEqual(BOOK["hit"]["kind"], hit.kind)
        self.assertIn(hit.matched, hit.reason)
        self.assertGreater(len(hit.reason), 12)                     # 一句话，不是一个词

    def test_every_action_piece_is_counted(self):
        """一行只有一段（行首那一段括注），多段就按出现顺序都记下来。"""
        hit = check("尾巴晃了\n（耳朵垂了）。\n他说（笑）就走了", "forbid")
        self.assertEqual(("尾巴晃了", "（耳朵垂了）"), hit.lines)
        self.assertIn("2", hit.reason)                              # 一共两处

    def test_body_part_line_without_pronoun_is_an_action(self):
        """身体部位开头、没有自称：那是动作行（与 split.py 的 _is_action 同一口径）。"""
        self.assertIsNotNone(check("尾巴垂下去了。", "forbid"))

    def test_pronoun_line_is_speech_not_action(self):
        """带人称代词的是台词，不是动作行——这条不许误伤。"""
        self.assertIsNone(check("我这是正常体型。", "forbid"))

    def test_clean_speech_is_not_an_action(self):
        self.assertIsNone(check(BODY, "forbid"))
        self.assertIsNone(check("订单号发我，我看看。", "forbid"))


class AliasesComeFromData(unittest.TestCase):
    """认哪些写法全在数据里：forbid / allow 与各自的别名。"""

    def test_alias_of_forbid_hits(self):
        for word in BOOK["quota_values"]["forbid"]:
            with self.subTest(word=word):
                self.assertIsNotNone(check(LINE, word))

    def test_alias_of_allow_never_hits(self):
        for word in BOOK["quota_values"]["allow"]:
            with self.subTest(word=word):
                self.assertIsNone(check(LINE, word))

    def test_case_and_padding_do_not_matter(self):
        self.assertIsNotNone(check(LINE, "  FORBID "))

    def test_unknown_word_is_loud(self):
        """认不出的写法当场响：猜成放行等于这一关白设。"""
        with self.assertRaises(QuotaBroken) as caught:
            check(LINE, "随便写的")
        self.assertIn("quota", str(caught.exception))

    def test_new_alias_in_the_data_is_honoured(self):
        """把抄本里的别名换掉，再用仓库的读法读回来——认的就是数据里那份。"""
        book = json.loads(json.dumps(BOOK, ensure_ascii=False))
        book["quota_values"]["forbid"].append("本轮闭嘴")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quota.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(book, handle, ensure_ascii=False)
            reread = load("quota", root=tmp)
            self.assertIsNotNone(check(LINE, "本轮闭嘴", table=reread))
            with self.assertRaises(QuotaBroken):
                check(LINE, "本轮闭嘴")                  # 真数据里没这个词，照样不认


class StripKeepsTheBody(unittest.TestCase):
    """剥行只删动作片段：正文一个字都不许动，行内括号更不许碰。"""

    def test_exactly_the_action_is_removed(self):
        bodies, removed = strip(LINE)
        self.assertEqual(BODY, bodies)                       # 一个字不多、不少
        self.assertEqual(["（尾巴在水里晃了一下）"], removed)

    def test_action_only_line_leaves_nothing(self):
        self.assertEqual(("", ["（尾巴晃了一下）"]), strip("（尾巴晃了一下）"))

    def test_trailing_sentence_mark_is_not_a_body(self):
        """「（尾巴晃了一下）。」删掉括注只剩一个句号：那不是正文。"""
        self.assertEqual("", strip("（尾巴晃了一下）。")[0])

    def test_inline_bracket_is_speech_and_stays(self):
        """行内括号长在句子中间，是台词里夹的一个字：整行原样留着。"""
        bodies, removed = strip("（尾巴晃了一下）他说（笑）就走了")
        self.assertEqual("他说（笑）就走了", bodies)
        self.assertEqual(["（尾巴晃了一下）"], removed)

    def test_inline_bracket_alone_is_untouched(self):
        self.assertEqual(("他说（笑）就走了", []), strip("他说（笑）就走了"))

    def test_unpaired_bracket_is_not_an_action(self):
        """括号没配对：写歪了不等于整行是动作，剥掉半段比不剥更糟。"""
        self.assertEqual(("（尾巴晃了一下", []), strip("（尾巴晃了一下"))

    def test_other_lines_are_kept_in_order(self):
        bodies, removed = strip("第一句。\n（尾巴晃了一下）\n第三句。")
        self.assertEqual("第一句。\n第三句。", bodies)
        self.assertEqual(["（尾巴晃了一下）"], removed)

    def test_pronoun_line_is_kept(self):
        self.assertEqual(("我这是正常体型。", []), strip("我这是正常体型。"))


class BrokenTableIsLoud(unittest.TestCase):
    """坏表当场响并点名，不许静默当成「这一关没有判据」。"""

    def test_missing_section(self):
        with self.assertRaises(QuotaBroken) as caught:
            check(LINE, "forbid", table=_without_quota(BOOK))
        self.assertIn("hit", str(caught.exception))

    def test_empty_word_list(self):
        book = json.loads(json.dumps(BOOK, ensure_ascii=False))
        book["quota_values"]["forbid"] = []
        with self.assertRaises(QuotaBroken) as caught:
            check(LINE, "forbid", table=book)
        self.assertIn("forbid", str(caught.exception))

    def test_forbid_and_allow_may_not_share_a_spelling(self):
        book = json.loads(json.dumps(BOOK, ensure_ascii=False))
        book["quota_values"]["allow"].append(book["quota_values"]["forbid"][0])
        with self.assertRaises(QuotaBroken) as caught:
            check(LINE, "forbid", table=book)
        self.assertIn("quota_values", str(caught.exception))


class WordsComeFromData(unittest.TestCase):
    """四句人话也取自数据：判废、剥行救回、剥完只剩空白、剥完仍被拦下。"""

    def test_hit_reason_carries_the_marker(self):
        hit = check(LINE, "forbid")
        self.assertIn(hit.matched, hit.reason)
        self.assertIn("action-over-quota", hit.reason)        # 账本靠这个记号认门

    def test_strip_reason_names_what_was_removed(self):
        reason = quota.strip_reason(["（尾巴晃了一下）"])
        self.assertIn("（尾巴晃了一下）", reason)
        self.assertIn("action-over-quota", reason)

    def test_blank_reason_is_about_having_nothing_left(self):
        reason = quota.blank_reason(["（尾巴晃了一下）"])
        self.assertIn("action-over-quota", reason)
        self.assertNotEqual(reason, quota.strip_reason(["（尾巴晃了一下）"]))

    def test_leftover_reason_names_the_clause_that_stopped_it(self):
        reason = quota.leftover_reason(BOOK["hit"]["clause_id"], "可爱")
        self.assertIn(BOOK["hit"]["clause_id"], reason)
        self.assertIn("可爱", reason)

    def test_clear_reason_says_the_gate_was_checked(self):
        self.assertTrue(quota.clear_reason())


class QuotaStaysPure(unittest.TestCase):
    """硬约束：不取时钟、不碰随机数、不 import 宿主、不写盘。"""

    def test_no_time_or_random(self):
        source = Path(quota.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?m)^\s*(?:import|from)\s+(?:time|datetime|random)\b")

    def test_no_criteria_literals(self):
        """判据字面量不许进代码：环境词表、闸用的名字、人话模板都在数据里。"""
        source = Path(quota.__file__).read_text(encoding="utf-8")
        for word in ACTION_NOTE["env_words"]:
            self.assertNotIn(word, source)
        self.assertNotIn(BOOK["hit"]["clause_id"], source)
        for line in BOOK["reason"].values():
            self.assertNotIn(line.split("{")[0], source)

    def test_no_host_and_no_writing(self):
        source = Path(quota.__file__).read_text(encoding="utf-8")
        for word in ("astrbot", "deepseek-ai", "cordis", "OneBot", "NapCat", "aiocqhttp"):
            self.assertNotIn(word, source)
        for word in ("open(", "write_text", "mkdir", "os.environ"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
