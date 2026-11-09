"""认亲这一关只管判：谁是主人由号定，不由话定。

判据与说法一个字都不写在这份文件里：熟人称呼表、记忆复述标记、中性替换词、稳妥话术、命中与放行的
人话、闸用的两个名字、以及「够不够熟」那一档阈值（known_by）全从 organs/mouth/data/kin.json 读
（走 warehouse）。要改判据就改数据、不改测试、更不改 kin.py。

「数据改一条，行为跟着变」是这一关的全部意义，所以末尾有一组测试把 kin.json 拷到临时目录、
改一张词表 / 改一档阈值再按同样的读法读回来——验的是「读数据」，不是验内存里那个 dict。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from organs.mouth import kin
from organs.mouth.kin import KinBroken, KinHit, check, neutralize
from organs.mouth.warehouse import load

BOOK = load("kin")
CLAUSE_ID = BOOK["hit"]["clause_id"]
KIND = BOOK["hit"]["kind"]
TERM = BOOK["kin_terms"][0]                 # 熟人称呼的第一个（数据里排在前面的那个）
MEMORY = BOOK["memory_markers"][0]          # 记忆复述的第一个标记
TERM_WORD = BOOK["neutral"]["term_word"]
MEMORY_LINE = BOOK["neutral"]["memory_sentence"]
SAY_ONCE = BOOK["neutral"]["say_once"]

#: 「够不够熟」那一档：字段名与阈值都从数据里读，代码不写死（现在是 relation ≥ 3＝熟人档）。
MIN = BOOK["known_by"]["min"]
FIELD = BOOK["known_by"]["field"]
#: 三种读数（不是三句结论）：不在册 / 刚建档还没爬到那一档 / 够得着。
STRANGER = {"in_book": False}
NEW_FACE = {"in_book": True, FIELD: MIN - 1}
FAMILIAR = {"in_book": True, FIELD: MIN}

#: 素材（不是判据）：一句带着熟人称呼的话，与它的邻居。数据里的原句收在 kin.json 的 note 里。
CLAIM_LINE = TERM + "，我这就来"
OTHER_LINE = "今天风挺大的。"


def _write_and_load(tmp: str, book: dict) -> dict:
    """把改过的判据落进临时目录，再按仓库的读法读回来（读法与线上一致，只换根目录）。"""
    path = os.path.join(tmp, "kin.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(book, handle, ensure_ascii=False)
    return load("kin", root=tmp)


class InBookIsNotFamiliar(unittest.TestCase):
    """「在册」不是「熟」：两段都要过，缺一段就照实判；判不了就不判。"""

    def test_confirmed_names_do_not_require_familiarity_or_permit_other_claims(self):
        known = dict(NEW_FACE, confirmed_terms=['主人', '老大', '亲爱的'])
        self.assertIsNone(check('老大，这是给你的。主人和亲爱的都是你的叫法。', known))
        self.assertEqual('老婆', check('老大，老婆。', known).term)
        self.assertEqual('亲', check('亲爱的。亲。', known).term)
        self.assertIsNotNone(check('老大，你上次说过这个。', known))
        self.assertIsNotNone(check('老大。', dict(STRANGER, confirmed_terms=['老大'])))

    def test_confirmed_names_must_be_nonempty_text_list(self):
        for value in ('老大', [None], ['']):
            with self.subTest(value=value), self.assertRaises(KinBroken):
                check('普通聊天', dict(NEW_FACE, confirmed_terms=value))

    def test_neutralize_changes_the_unconfirmed_occurrence_only(self):
        body = '亲爱的。亲。'
        hit = check(body, dict(NEW_FACE, confirmed_terms=['亲爱的']))
        result, changed = neutralize(body, hit)
        self.assertEqual('亲', changed)
        self.assertTrue(result.startswith('亲爱的。你。'), result)

    def test_a_stranger_is_judged(self):
        self.assertIsInstance(check(CLAIM_LINE, STRANGER), KinHit)

    def test_just_registered_is_still_judged(self):
        """刚建了档、读数还没爬到那一档（answer() 一 ingest 就有卡）：**仍然判**。

        这一条是这一关的命门：拿「查得到他」当「熟」，他只要多说一句话就能认亲。
        """
        self.assertIsInstance(check(CLAIM_LINE, NEW_FACE), KinHit)

    def test_enough_reading_passes(self):
        """够得着数据里那一档：他爱怎么叫就怎么叫，一个判据词都不许判。"""
        self.assertIsNone(check(CLAIM_LINE, FAMILIAR))

    def test_above_the_threshold_passes(self):
        self.assertIsNone(check(CLAIM_LINE, {"in_book": True, FIELD: MIN + 1}))

    def test_a_stranger_with_a_low_reading_is_still_judged(self):
        """不在册时那一格读数是垃圾也不该翻案：不在册就是不在册。"""
        self.assertIsInstance(check(CLAIM_LINE, {"in_book": False, FIELD: 99}), KinHit)

    def test_not_checked_is_not_the_same_as_ok(self):
        """known=None（没拿到人名录读数）：判不了——不判，也不声称判过。"""
        self.assertIsNone(check(CLAIM_LINE, None))
        self.assertNotEqual(kin.STATUS_OK, kin.STATUS_NOT_CHECKED)

    def test_the_table_is_still_validated_without_a_reading(self):
        """没拿到读数也要先把表校验一遍：坏表不许因为这一轮判不了就悄悄跳过。"""
        with self.assertRaises(KinBroken):
            check(CLAIM_LINE, None, {})


class TheReadingShapeIsGuarded(unittest.TestCase):
    """读数只收 None 或一份键值表；布尔与数字一律抛——一个 True 说不出「够不够熟」。"""

    def test_booleans_are_rejected(self):
        for bad in (True, False):
            with self.assertRaises(KinBroken) as caught:
                check(CLAIM_LINE, bad)
            self.assertIn("known", str(caught.exception))

    def test_numbers_and_strings_are_rejected(self):
        for bad in (1, 0, 1.0, "true", "True", "1", ""):
            with self.assertRaises(KinBroken):
                check(CLAIM_LINE, bad)

    def test_a_reading_without_in_book_is_rejected(self):
        with self.assertRaises(KinBroken) as caught:
            check(CLAIM_LINE, {FIELD: MIN})
        self.assertIn("in_book", str(caught.exception))

    def test_in_book_must_be_a_real_boolean(self):
        with self.assertRaises(KinBroken):
            check(CLAIM_LINE, {"in_book": 1, FIELD: MIN})

    def test_a_registered_reading_without_the_tier_is_loud(self):
        """在册、档位那一格读歪了：不许猜成「熟」也不许猜成「不熟」，当场响。"""
        with self.assertRaises(KinBroken) as caught:
            check(CLAIM_LINE, {"in_book": True})
        self.assertIn(FIELD, str(caught.exception))
        with self.assertRaises(KinBroken):
            check(CLAIM_LINE, {"in_book": True, FIELD: "3"})


class HittingWordsAreEvidence(unittest.TestCase):
    """命中返回的是一条证据（哪个词、哪一段、哪一句、为什么），不是布尔。"""

    def test_the_term_and_the_sentence_come_back(self):
        hit = check(CLAIM_LINE, STRANGER)
        self.assertEqual(TERM, hit.term)
        self.assertIn(hit.term, hit.matched)
        self.assertEqual(CLAIM_LINE, hit.sentence)          # 句子里不含句末标点
        self.assertEqual(CLAUSE_ID, hit.clause_id)          # 名字取自数据，代码不造
        self.assertEqual(KIND, hit.kind)

    def test_the_reason_is_a_human_sentence(self):
        hit = check(CLAIM_LINE, STRANGER)
        self.assertIn(hit.matched, hit.reason)
        self.assertGreater(len(hit.reason), 20)

    def test_a_memory_marker_hits_too(self):
        hit = check(MEMORY + "要去医院复查", STRANGER)
        self.assertEqual(MEMORY, hit.term)
        self.assertEqual(CLAUSE_ID, hit.clause_id)

    def test_the_longest_word_wins_on_the_same_spot(self):
        """同一起点上取长的那一条：数据里短词是长词的前半截时，报的必须是长词。"""
        words = BOOK["memory_markers"] + BOOK["kin_terms"]
        pair = [(short, long) for short in words for long in words
                if len(long) > len(short) and long.startswith(short)]
        if not pair:
            self.skipTest("数据里没有「短词是长词前缀」的那一对")
        short, long = pair[0]
        hit = check(long + "吧", STRANGER)
        self.assertEqual(long, hit.term)


class PolitenessPasses(unittest.TestCase):
    """对陌生人客气、问一句「你是？」，或者回那句稳妥话术，都放行。"""

    def test_a_plain_question_passes(self):
        self.assertIsNone(check("你是？", STRANGER))
        self.assertIsNone(check("你好，请问找哪位？", STRANGER))

    def test_the_steady_line_passes(self):
        """陌生人自称主人时她可以这么回：这句是数据里的稳妥话术，不许判成认亲。"""
        self.assertIsNone(check(SAY_ONCE, STRANGER))

    def test_plain_small_talk_passes(self):
        self.assertIsNone(check("今天天气不错。", STRANGER))


class NeutralizeTouchesOnlyThatSpot(unittest.TestCase):
    """改不动时换成中性称呼（不是整条丢），别的句子一个字不动，并补上那句稳妥话术。"""

    def test_the_term_becomes_the_neutral_word(self):
        hit = check(CLAIM_LINE, STRANGER)
        rest, changed = neutralize(CLAIM_LINE, hit)
        self.assertEqual(TERM, changed)
        self.assertNotIn(TERM, rest)
        self.assertIn(TERM_WORD, rest)
        self.assertIn(SAY_ONCE, rest)                       # 带上稳妥话术

    def test_the_other_sentence_is_kept(self):
        line = CLAIM_LINE + "。" + OTHER_LINE
        hit = check(line, STRANGER)
        rest, _changed = neutralize(line, hit)
        self.assertIn(OTHER_LINE, rest)
        self.assertNotIn(TERM, rest)

    def test_a_memory_sentence_is_replaced_by_the_truth(self):
        line = MEMORY + "要去医院复查。" + OTHER_LINE
        hit = check(line, STRANGER)
        rest, changed = neutralize(line, hit)
        self.assertIn(MEMORY_LINE, rest)
        self.assertNotIn(MEMORY, rest)
        self.assertIn(OTHER_LINE, rest)
        self.assertEqual(MEMORY + "要去医院复查", changed)

    def test_the_steady_line_is_not_added_twice(self):
        line = CLAIM_LINE + "。" + SAY_ONCE
        hit = check(line, STRANGER)
        rest, _changed = neutralize(line, hit)
        self.assertEqual(1, rest.count(SAY_ONCE))

    def test_an_unknown_spot_changes_nothing(self):
        hit = check(CLAIM_LINE, STRANGER)
        rest, changed = neutralize("别的句子", hit)
        self.assertEqual("别的句子", rest)
        self.assertEqual("", changed)

    def test_a_foreign_hit_is_rejected(self):
        with self.assertRaises(KinBroken):
            neutralize(CLAIM_LINE, {"term": TERM})


class NeutralizedTextIsClean(unittest.TestCase):
    """中性化之后的正文不再命中：不然闸会一直换下去，最后把整条吞掉。"""

    def test_the_term_case_is_clean(self):
        hit = check(CLAIM_LINE, STRANGER)
        rest, _changed = neutralize(CLAIM_LINE, hit)
        self.assertIsNone(check(rest, STRANGER))

    def test_the_memory_case_is_clean(self):
        line = MEMORY + "要去医院复查。"
        hit = check(line, STRANGER)
        rest, _changed = neutralize(line, hit)
        self.assertIsNone(check(rest, STRANGER))


class ReasonsAreData(unittest.TestCase):
    """人话全在数据里：代码只填占位符。"""

    def test_clear_and_neutralize_reasons_fill(self):
        self.assertEqual(BOOK["reason"]["clear"], kin.clear_reason())
        self.assertIn(TERM, kin.neutralize_reason(TERM))
        self.assertIn(TERM, kin.blank_reason(TERM))
        self.assertIn(CLAUSE_ID, kin.leftover_reason(CLAUSE_ID, "x"))

    def test_a_template_that_cannot_fill_is_loud(self):
        book = json.loads(json.dumps(BOOK))
        book["reason"]["neutralize"] = "只写了一句人话，却带着填不出来的 {nope}"
        with self.assertRaises(KinBroken):
            kin.neutralize_reason(TERM, book)


class BrokenTableIsLoud(unittest.TestCase):
    """坏一处就当场响：空表、撞名、中性词自己带着判据词、词表写成一整串字、阈值写成布尔。"""

    def _book(self) -> dict:
        return json.loads(json.dumps(BOOK))

    def test_missing_section(self):
        with self.assertRaises(KinBroken) as caught:
            check(CLAIM_LINE, STRANGER, {})
        self.assertIn("hit", str(caught.exception))

    def test_a_word_list_written_as_one_string(self):
        book = self._book()
        book["kin_terms"] = "、".join(book["kin_terms"])
        with self.assertRaises(KinBroken) as caught:
            check(CLAIM_LINE, STRANGER, book)
        self.assertIn("kin_terms", str(caught.exception))

    def test_the_two_tables_must_not_share_a_word(self):
        book = self._book()
        book["memory_markers"] = [TERM]
        with self.assertRaises(KinBroken):
            check(CLAIM_LINE, STRANGER, book)

    def test_a_neutral_word_may_not_carry_a_judging_word(self):
        book = self._book()
        book["neutral"]["say_once"] = SAY_ONCE + TERM
        with self.assertRaises(KinBroken) as caught:
            check(CLAIM_LINE, STRANGER, book)
        self.assertIn("neutral", str(caught.exception))

    def test_a_boolean_threshold_is_loud(self):
        """阈值写成 true 等于 1（最低那一档）：那不是判据，是巧合，当场响。"""
        book = self._book()
        book["known_by"]["min"] = True
        with self.assertRaises(KinBroken) as caught:
            check(CLAIM_LINE, STRANGER, book)
        self.assertIn("min", str(caught.exception))

    def test_a_missing_threshold_is_loud(self):
        book = self._book()
        book["known_by"] = {"field": FIELD}
        with self.assertRaises(KinBroken):
            check(CLAIM_LINE, STRANGER, book)


class JudgingWordsComeFromData(unittest.TestCase):
    """改数据就改行为——词表与「够熟」那一档阈值都一样。"""

    def test_the_term_table_is_read_from_disk(self):
        book = json.loads(json.dumps(BOOK))
        book["kin_terms"] = ["老张"]
        with tempfile.TemporaryDirectory() as tmp:
            fresh = _write_and_load(tmp, book)
        self.assertIsNone(check(CLAIM_LINE, STRANGER, fresh))       # 老大不在新表里
        self.assertIsInstance(check("老张，我这就来", STRANGER, fresh), KinHit)

    def test_the_steady_line_is_read_from_disk(self):
        book = json.loads(json.dumps(BOOK))
        book["neutral"]["say_once"] = "我这边只认号。"
        hit = check(CLAIM_LINE, STRANGER, book)
        rest, _changed = neutralize(CLAIM_LINE, hit, book)
        self.assertIn("我这边只认号。", rest)
        self.assertNotIn(SAY_ONCE, rest)

    def test_the_threshold_is_read_from_disk(self):
        """把 min 提一档：本来放行的读数就变成要判——阈值真的在数据里，不是一个恰好。"""
        book = json.loads(json.dumps(BOOK))
        book["known_by"]["min"] = MIN + 1
        with tempfile.TemporaryDirectory() as tmp:
            fresh = _write_and_load(tmp, book)
        self.assertIsInstance(check(CLAIM_LINE, FAMILIAR, fresh), KinHit)
        self.assertIsNone(check(CLAIM_LINE, {"in_book": True, FIELD: MIN + 1}, fresh))


class KinSourceHoldsNoWords(unittest.TestCase):
    """判据的词与说法一个都不许进代码：kin.py 只接判据，不持判据。"""

    def test_the_source_has_no_kin_words(self):
        with open(kin.__file__, encoding="utf-8") as handle:
            source = handle.read()
        # 单字词不比：这一关自己就叫「认亲」，一个字里带着它也是正常中文（数据里那一项仍由行为测试看住）。
        for word in BOOK["kin_terms"] + BOOK["memory_markers"]:
            if len(word) < 2:
                continue
            self.assertNotIn(word, source, "认亲判据 %r 混进了 kin.py：要改就改数据。" % word)
        for line in BOOK["reason"].values():
            self.assertNotIn(line.split("{")[0], source)
        self.assertNotIn(SAY_ONCE, source)


if __name__ == "__main__":
    unittest.main()
