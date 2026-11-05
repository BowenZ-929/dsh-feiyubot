"""动作括注判据只管一件事：她自己的身体 + 我们没给过的场景，不许写在一起。

词表与说法一个字都不写在这份文件里：环境词表、身体部位词表、括号对、两条路的名字、命中与放行
两句人话模板全从 organs/mouth/data/action_note.json 读（走 warehouse）。这里出现的素材是她真写过的
那句与几条例行放行的话——**素材不是判据**：判据改了，下面的期望值跟着数据走，不跟着这份文件走。

「改一条数据，行为跟着变」是这一关的全部意义，所以有一组测试把表拷到临时目录、改一条再按同样的
读法读回来——验的是「读数据」，不是验内存里那个 dict。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from organs.mouth import action_note
from organs.mouth.action_note import ActionBroken, ActionHit, check, clear_reason
from organs.mouth.warehouse import load

TABLE = load("action_note")
WHERE_BRACKET = TABLE["where"]["bracket"]
WHERE_SENTENCE = TABLE["where"]["sentence"]
OPEN, CLOSE = TABLE["brackets"][0]                 # 中文圆括号那一对：素材用它

LINE = "尾巴尖在水里轻轻蜷了一下"                    # 她真写过的那一句（括注里那段）
BODY_ONLY = "尾巴垂下去。"                           # 只有动作
SCENE_ONLY = "外面下雨了。"                          # 只有场景
CHATTER = "主人夸我了嘿嘿"                           # 括号里有话，但没有场景词


def _bracketed(inner: str) -> str:
    """套上数据里的那一对括号（不在这里写死括号本身）。"""
    return OPEN + inner + CLOSE


def _write_and_load(tmp: str, data: dict) -> dict:
    """把改过的表落进临时目录，再按仓库的读法读回来（读法与线上一致，只换根目录）。"""
    path = os.path.join(tmp, "action_note.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    return load("action_note", root=tmp)


class BracketedActionIsStopped(unittest.TestCase):
    """验收 1：她写过的那一句，括号里带着我们没给过的场景。"""

    def test_she_wrote_a_scene_we_never_gave(self):
        hit = check(_bracketed(LINE))
        self.assertIsInstance(hit, ActionHit)          # 不是布尔，是一条命中的证据
        self.assertEqual(WHERE_BRACKET, hit.where)      # 从括号那一路看出来的
        self.assertTrue(hit.env_word)
        self.assertEqual(LINE, hit.matched)             # 括号本身不进命中片段
        self.assertNotIn(OPEN, hit.matched)
        self.assertNotIn(CLOSE, hit.matched)

    def test_reason_is_a_sentence_a_human_can_read(self):
        hit = check(_bracketed(LINE))
        self.assertGreater(len(hit.reason), 12)                 # 一句话，不是一个词
        self.assertIn("「%s」" % hit.matched, hit.reason)        # 点名命中的那一段
        self.assertIn("「%s」" % hit.env_word, hit.reason)       # 点名那个场景词
        self.assertIn("没给过", hit.reason)                      # 说清毛病在哪
        self.assertNotIn("{", hit.reason)                       # 模板得填完，不许漏占位符

    def test_the_env_word_comes_from_the_table(self):
        self.assertIn(check(_bracketed(LINE)).env_word, TABLE["env_words"])


class BareSentenceIsStillStopped(unittest.TestCase):
    """验收 2：不带括号，同一句照样命中——这一关不只认括号。"""

    def test_without_brackets_it_still_hits(self):
        hit = check(LINE)
        self.assertIsInstance(hit, ActionHit)
        self.assertEqual(WHERE_SENTENCE, hit.where)
        self.assertIn(hit.env_word, TABLE["env_words"])
        self.assertEqual(LINE, hit.matched)             # 那一句原样回来，连句末标点都不带


class PassesWithGrounds(unittest.TestCase):
    """验收 3：放行必须有据——只有动作、只有场景、括号里但没有场景，三条都放。"""

    def test_body_only_passes(self):
        self.assertIsNone(check(BODY_ONLY))

    def test_scene_only_passes(self):
        self.assertIsNone(check(SCENE_ONLY))

    def test_bracket_without_scene_passes(self):
        self.assertIsNone(check(_bracketed(CHATTER)))

    def test_nothing_is_judged_on_nothing(self):
        for text in ("", "   ", None, 0):
            with self.subTest(text=text):
                self.assertIsNone(check(text))

    def test_two_scenes_in_two_sentences_pass(self):
        """跨句凑出来的「身体 + 场景」不是动作括注：一句话说事、一句话说环境，放行。"""
        self.assertIsNone(check(BODY_ONLY + SCENE_ONLY))

    def test_clear_reason_is_data(self):
        said = clear_reason()
        self.assertEqual(TABLE["reason"]["clear"], said)
        self.assertNotIn("{", said)


class TwoRoutesAreDifferent(unittest.TestCase):
    """两路同时在的时候报括号那一路（更严），括号里的字也不再算进句子那一路上。"""

    def test_bracket_wins_when_both_hit(self):
        text = _bracketed("外面风大") + LINE + "。"
        self.assertEqual(WHERE_BRACKET, check(text).where)

    def test_bracketed_text_does_not_count_as_a_sentence(self):
        """括号里的身体部位不算句子里的：挖掉括号片段之后，这句就只剩动作，没有场景。"""
        self.assertIsNone(check(_bracketed(CHATTER) + BODY_ONLY))


class DataDrivesTheCriterion(unittest.TestCase):
    """验收 6：判据改数据即改行为——删一个词、加一个词、换两个名字。"""

    def test_env_word_removed_no_longer_hits(self):
        word = check(_bracketed(LINE)).env_word
        fewer = dict(TABLE, env_words=[w for w in TABLE["env_words"] if w != word])
        hit_table = dict(fewer, reason=TABLE["reason"])
        with tempfile.TemporaryDirectory() as tmp:
            reloaded = _write_and_load(tmp, hit_table)
        self.assertIsNone(check(_bracketed(LINE), reloaded))     # 删掉那个词，同一句立刻不拦
        self.assertIsNone(check(LINE, reloaded))

    def test_env_word_added_blocks_previously_passing(self):
        self.assertIsNone(check(_bracketed(CHATTER)))
        word = "夸我"                                  # 临时加的一个「场景词」，只在这份临时表里存在
        more = dict(TABLE, env_words=list(TABLE["env_words"]) + [word])
        with tempfile.TemporaryDirectory() as tmp:
            reloaded = _write_and_load(tmp, more)
        hit = check(_bracketed(CHATTER), reloaded)
        self.assertIsNotNone(hit)                      # 加一个词，原本通的那句被拦下
        self.assertEqual(word, hit.env_word)

    def test_where_labels_come_from_data(self):
        two = dict(TABLE["where"], bracket="临时叫法")
        changed = dict(TABLE, where=two)
        with tempfile.TemporaryDirectory() as tmp:
            reloaded = _write_and_load(tmp, changed)
        self.assertEqual("临时叫法", check(_bracketed(LINE), reloaded).where)

    def test_gate_names_come_from_data(self):
        names = dict(TABLE["hit"], clause_id="临时条款", kind="临时标签")
        changed = dict(TABLE, hit=names)
        with tempfile.TemporaryDirectory() as tmp:
            reloaded = _write_and_load(tmp, changed)
        hit = check(_bracketed(LINE), reloaded)
        self.assertEqual("临时条款", hit.clause_id)      # 闸用的名字也取自数据
        self.assertEqual("临时标签", hit.kind)


class BrokenTableIsLoud(unittest.TestCase):
    """坏表当场响，而且报错要点名是哪一项；不许拿空词表冒充「没有判据」。"""

    def _broken(self, data: dict) -> str:
        with self.assertRaises(ActionBroken) as caught:
            check(_bracketed(LINE), data)
        return str(caught.exception)

    def test_missing_env_words_names_the_key(self):
        data = {k: v for k, v in TABLE.items() if k != "env_words"}
        self.assertIn("env_words", self._broken(data))

    def test_words_must_be_a_list_not_a_string(self):
        data = dict(TABLE, body_parts="".join(TABLE["body_parts"]))
        self.assertIn("清单", self._broken(data))

    def test_empty_word_list_is_not_a_judgement(self):
        self.assertIn("env_words", self._broken(dict(TABLE, env_words=[])))

    def test_bracket_pair_must_be_a_pair(self):
        data = dict(TABLE, brackets=[[OPEN]])
        self.assertIn("对", self._broken(data))

    def test_reason_templates_must_be_there(self):
        data = dict(TABLE, reason={"clear": TABLE["reason"]["clear"]})
        self.assertIn("hit", self._broken(data))

    def test_two_routes_must_be_named_differently(self):
        data = dict(TABLE, where={"bracket": "一样", "sentence": "一样"})
        self.assertIn("where", self._broken(data))

    def test_placeholder_must_be_fillable(self):
        data = dict(TABLE, reason=dict(TABLE["reason"], hit="缺占位符的模板 {not_a_field}"))
        self.assertIn("填不出来", self._broken(data))


class StaysCleanAndPure(unittest.TestCase):
    """硬约束：不取时钟、不写盘、不碰宿主；判据字面量不许进代码。"""

    def setUp(self):
        self.source = Path(action_note.__file__).read_text(encoding="utf-8")

    def test_no_clock_no_random(self):
        self.assertNotRegex(self.source,
                            r"(?m)^\s*(?:import|from)\s+(?:time|datetime|random)\b")

    def test_writes_nothing(self):
        for banned in ("open(", "write_text", ".mkdir", "json.dump", "os.remove", "shutil"):
            self.assertNotIn(banned, self.source)

    def test_imports_no_host(self):
        for word in ("astrbot", "deepseek-ai", "cordis", "OneBot", "NapCat", "aiocqhttp"):
            self.assertNotIn(word, self.source)

    def test_no_judgement_literals_in_code(self):
        """环境词、闸用的 clause_id、两句人话模板，一个都不许出现在代码里。

        身体部位词表不在这条断言里：里面全是「手」「头」这种单字，中文散文里到处都是，grep 定不到
        它——那一条靠上面的「改数据就改行为」来保（where 的说法、kind、词表全由行为测试盯着）。
        """
        for word in TABLE["env_words"]:
            self.assertNotIn(word, self.source)
        self.assertNotIn(TABLE["hit"]["clause_id"], self.source)
        for line in TABLE["reason"].values():
            self.assertNotIn(line, self.source)


if __name__ == "__main__":
    unittest.main()
