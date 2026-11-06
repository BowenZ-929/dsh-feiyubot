"""冒充做过这一关只管判：说了这一轮没做过的动作就该拦，说话类的话要放行。

判据与说法一个字都不写在这份文件里：动作类动词、说话类动词、前缀与后缀、分句标点、命中与放行的
人话、闸用的两个名字全从 organs/mouth/data/claim.json 读（走 warehouse）。要改判据就改数据、
不改测试、更不改 claim.py。

「数据改一条，行为跟着变」是这一关的全部意义，所以末尾有一组测试把 claim.json 拷到临时目录、
改一张词表再按同样的读法读回来——验的是「读数据」，不是验内存里那个 dict。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from organs.mouth import claim
from organs.mouth.claim import ClaimBroken, ClaimHit, check, strip_sentence
from organs.mouth.warehouse import load

BOOK = load("claim")
CLAUSE_ID = BOOK["hit"]["clause_id"]
KIND = BOOK["hit"]["kind"]

#: 素材（不是判据）：这一轮手头什么都没做时她会说的那一句。数据里的原句收在 claim.json 的 note 里。
CLAIM_LINE = "我这就去画"
#: 同一句话的邻居：删句只删前一句，后一句原样留下。
OTHER_LINE = "今天风挺大的。"


def _sentence_of(verb: str, prefix: str = "", suffix: str = "") -> str:
    """按数据里的前缀与后缀拼一句「说成做过」的话——测试不自己猜语序，语序从数据来。"""
    return "%s%s%s%s" % (BOOK["person"], prefix, verb, suffix)


def _write_and_load(tmp: str, book: dict) -> dict:
    """把改过的判据落进临时目录，再按仓库的读法读回来（读法与线上一致，只换根目录）。"""
    path = os.path.join(tmp, "claim.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(book, handle, ensure_ascii=False)
    return load("claim", root=tmp)


class NotCheckedIsNotTheSameAsJudged(unittest.TestCase):
    """验收 4 / 5：动作账没给（None）判不了；给了空账（[]）才是判过——两种形状必须分得开。"""

    def test_none_means_this_gate_did_not_run(self):
        self.assertIsNone(check(CLAIM_LINE, None))

    def test_an_empty_ledger_is_a_real_judgement(self):
        """空清单是「给了账、账上空着」：这一轮确实什么都没做，该判冒充。"""
        hit = check(CLAIM_LINE, [])
        self.assertIsInstance(hit, ClaimHit)

    def test_the_two_statuses_are_different_words(self):
        self.assertNotEqual(claim.STATUS_OK, claim.STATUS_NOT_CHECKED)

    def test_the_table_is_still_validated_without_a_ledger(self):
        """没给账也要先把表校验一遍：坏表不许因为这一轮没账就悄悄跳过。"""
        with self.assertRaises(ClaimBroken):
            check(CLAIM_LINE, None, {})


class HittingWordsAreEvidence(unittest.TestCase):
    """命中返回的是一条证据（哪个动词、哪一段、哪一句、为什么），不是布尔。"""

    def test_the_verb_and_the_sentence_come_back(self):
        hit = check(CLAIM_LINE, [])
        self.assertEqual(BOOK["action_verbs"][0], hit.verb)
        self.assertIn(hit.verb, hit.matched)
        self.assertEqual(CLAIM_LINE, hit.sentence)          # 句子里不含句末标点
        self.assertEqual(CLAUSE_ID, hit.clause_id)          # 名字取自数据，代码不造
        self.assertEqual(KIND, hit.kind)

    def test_the_reason_is_a_human_sentence(self):
        hit = check(CLAIM_LINE, [])
        self.assertIn(hit.matched, hit.reason)
        self.assertIn(hit.verb, hit.reason)
        self.assertGreater(len(hit.reason), 20)

    def test_every_action_verb_in_the_table_can_hit(self):
        """词表里的每一个动作类动词都能被判出来：判据是数据给的，不是代码里挑着认的几个。"""
        for verb in BOOK["action_verbs"]:
            with self.subTest(verb=verb):
                self.assertIsInstance(check(_sentence_of(verb, suffix=BOOK["suffixes"][0]), []),
                                      ClaimHit)

    def test_every_speech_verb_in_the_table_is_exempt(self):
        """说话类动词一律豁免：说话不是动作，把它们记成冒充做过等于逼她不许开口。"""
        for verb in BOOK["speech_verbs"]:
            with self.subTest(verb=verb):
                self.assertIsNone(check(_sentence_of(verb, suffix=BOOK["suffixes"][0]), []))

    def test_a_bare_prefix_needs_no_suffix(self):
        """数据里的裸前缀（正在去做那一类）后面不带了也算：正要去做不等于不要判。"""
        prefix = BOOK["bare_prefixes"][0]
        self.assertIsInstance(check(_sentence_of(BOOK["action_verbs"][0], prefix=prefix), []),
                              ClaimHit)

    def test_a_plain_prefix_needs_the_suffix(self):
        """非裸前缀后面不带了就不算：光说「我…动作」没有「了」或「过」，还没说成做过。"""
        prefix = [one for one in BOOK["prefixes"] if one not in BOOK["bare_prefixes"]][0]
        self.assertIsNone(check(_sentence_of(BOOK["action_verbs"][0], prefix=prefix), []))


class TheLedgerDecides(unittest.TestCase):
    """真做过（账上有这个动词）放行；账上对不上就命中。"""

    def test_a_matching_verb_passes(self):
        hit = check(CLAIM_LINE, [])
        self.assertIsNone(check(CLAIM_LINE, [{"verb": hit.verb}]))

    def test_another_action_on_the_ledger_is_not_this_one(self):
        hit = check(CLAIM_LINE, [])
        other = [one for one in BOOK["action_verbs"] if one != hit.verb][0]
        self.assertIsNotNone(check(CLAIM_LINE, [{"verb": other}]))

    def test_a_broken_ledger_is_loud(self):
        """账本形状坏了当场响：猜它的意思等于替她记一笔没发生的动作。"""
        for bad in ("发过了", {"verb": "发"}, [{"kind": "发"}], [{"verb": "  "}]):
            with self.subTest(bad=bad):
                with self.assertRaises(ClaimBroken):
                    check(CLAIM_LINE, bad)


class StripOnlyThatSentence(unittest.TestCase):
    """改不动时删的是那一句，不碰别的句子——谎只长在那一句里。"""

    def test_the_other_sentence_survives(self):
        hit = check(CLAIM_LINE, [])
        rest, removed = strip_sentence(CLAIM_LINE + "。" + OTHER_LINE, hit)
        self.assertEqual(CLAIM_LINE, removed)
        self.assertEqual(OTHER_LINE, rest)

    def test_the_sentence_in_the_middle(self):
        hit = check(CLAIM_LINE, [])
        rest, removed = strip_sentence(OTHER_LINE + CLAIM_LINE + "。" + OTHER_LINE, hit)
        self.assertEqual(CLAIM_LINE, removed)
        self.assertEqual(OTHER_LINE + OTHER_LINE, rest)

    def test_only_that_sentence_leaves_nothing(self):
        """整条只有那一句：删完是空串，闸那边据此弃掉整条。"""
        hit = check(CLAIM_LINE, [])
        self.assertEqual(("", CLAIM_LINE), strip_sentence(CLAIM_LINE, hit))

    def test_a_comma_is_not_a_sentence_end(self):
        """逗号不算句末：一句话里夹着逗号，删的仍是整句，不切成两半。"""
        line = CLAIM_LINE + "，你别催。"
        hit = check(line, [])
        self.assertEqual("", strip_sentence(line, hit)[0])

    def test_nothing_found_deletes_nothing(self):
        """那一句取不出来就一个字都不动、removed 是空串：拿不准删没删干净，闸那边会弃整条。"""
        hit = ClaimHit(verb="", matched="这一句不在文里", sentence="这一句不在文里",
                       reason="", clause_id=CLAUSE_ID, kind=KIND)
        self.assertEqual((OTHER_LINE, ""), strip_sentence(OTHER_LINE, hit))


class BrokenTableIsLoud(unittest.TestCase):
    """坏表当场响并点名，不许静默当成「这一关没有判据」放行。"""

    def test_missing_sections_name_the_item(self):
        for book, spot in (({}, "hit"),
                           (dict(BOOK, action_verbs=[]), "action_verbs"),
                           (dict(BOOK, action_verbs="画发"), "action_verbs"),
                           (dict(BOOK, bare_prefixes=["不在前缀表里"]), "bare_prefixes"),
                           (dict(BOOK, speech_verbs=BOOK["action_verbs"]), "说话类动词")):
            with self.subTest(spot=spot):
                with self.assertRaises(ClaimBroken) as caught:
                    check(CLAIM_LINE, [], book)
                self.assertIn(spot, str(caught.exception))

    def test_a_reason_template_that_cannot_be_filled_is_loud(self):
        book = json.loads(json.dumps(BOOK, ensure_ascii=False))
        book["reason"]["hit"] = "这句填不出来：{没有这一格}"
        with self.assertRaises(ClaimBroken):
            check(CLAIM_LINE, [], book)


class TheDataIsTheOnlySource(unittest.TestCase):
    """改数据即改行为：判据不在代码里，拷一份改掉就换一套判法。"""

    def test_removing_a_verb_from_the_table_stops_it_hitting(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = json.loads(json.dumps(BOOK, ensure_ascii=False))
            hit = check(CLAIM_LINE, [])
            book["action_verbs"] = [one for one in book["action_verbs"] if one != hit.verb]
            self.assertIsNone(check(CLAIM_LINE, [], _write_and_load(tmp, book)))

    def test_adding_a_verb_to_the_table_starts_it_hitting(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = json.loads(json.dumps(BOOK, ensure_ascii=False))
            book["action_verbs"].append("浇水")
            table = _write_and_load(tmp, book)
            self.assertIsInstance(check("我浇水了", [], table), ClaimHit)

    def test_the_two_names_come_from_the_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = json.loads(json.dumps(BOOK, ensure_ascii=False))
            book["hit"] = {"clause_id": "改名了", "kind": "换标签了"}
            hit = check(CLAIM_LINE, [], _write_and_load(tmp, book))
            self.assertEqual("改名了", hit.clause_id)
            self.assertEqual("换标签了", hit.kind)

    def test_no_word_list_and_no_wording_lives_in_the_code(self):
        """词表与人话只在数据里：代码里出现一条就是第二处真源。

        多字条目（前缀、填充词、多字动词）与四句人话模板一条都不许进代码——单字动词是常用汉字，
        写人话时躲不开，这一段只卡能被机械认出来的那些。
        """
        source = Path(claim.__file__).read_text(encoding="utf-8")
        words = [word for key in ("prefixes", "bare_prefixes", "fillers", "action_verbs",
                                  "speech_verbs", "suffixes") for word in BOOK[key]]
        for word in dict.fromkeys(words):
            if len(word) >= 2:
                self.assertNotIn(word, source)
        for template in BOOK["reason"].values():
            self.assertNotIn(template, source)


class ClaimStaysClean(unittest.TestCase):
    """硬约束：本文件不取时钟、不碰随机数、不写盘、不 import 宿主。"""

    def setUp(self):
        self.source = Path(claim.__file__).read_text(encoding="utf-8")

    def test_no_clock_and_no_random(self):
        self.assertNotRegex(self.source,
                            r"(?m)^\s*(?:import|from)\s+(?:time|datetime|random)\b")

    def test_writes_nothing(self):
        for banned in ("open(", "write_text", ".mkdir", "json.dump", "os.remove", "shutil"):
            self.assertNotIn(banned, self.source)

    def test_imports_no_host(self):
        for word in ("astrbot", "deepseek-ai", "cordis", "OneBot", "NapCat", "aiocqhttp"):
            self.assertNotIn(word, self.source)


if __name__ == "__main__":
    unittest.main()
