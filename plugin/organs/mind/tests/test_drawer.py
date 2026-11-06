"""抽屉那一节：装配前判一次意图，命中才向家取几篇旧日记（只进提示词，不进长期记忆）。

判据（每条都对应交付里的一句话）：
1. **命中才翻**：问句里出现 drawer.keywords 里任一个词 → 提示词多一节，标题是数据里的
   drawer.label，条数 ≤ max_entries，整节总字数 ≤ cap，节尾有那句 testimony_note。
2. **没命中一次 IO 都不做**：家那两个读口（cards.list_room / cards.read_item）一次都调不到，
   提示词里也没有这一节（不留一个只有标题的空节）。
3. **翻不翻只看意图**：关键词命中 + enabled 就翻，**不看场合**——群聊 / unknown 同样真去读
   （list_room 计数 ≥ 1），不再是一次 IO 都不做。
4. **场合只决定能取哪几格**：drawer.shelves 里 private 含普通 + 恋爱两格，group / unknown 只有
   普通那格；unknown 那档在节内第一行插一句 drawer.unknown_note（「来源未定」），private / group
   都不插。**unknown 不许被推成 group 或 private**：不传 scene 就走这一档。
5. **挑法**：相关性（那个月的说法整词命中，或 name / at / note 与问句连续重合若干字）优先，
   再按日期从新到旧补足。
6. **超长不硬塞**：一篇 2000 字日记压进 cap 以内，且收在句末（绝不截半个词）。
7. **数据驱动**：改 keywords / max_entries 结果跟着变；缺 drawer 块 = 不翻（不是每轮翻）。
8. **日记不进脑**：这一节只进提示词，落脑的条目里没有任何一段日记正文。

全部注入假 transport：一个字节都不发真网络。家（HOME_ROOT）指到临时根，实盘数据一个字都不读。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.brain import BrainStore
from organs.brain import model as brain_model
from organs.home import cards
from organs.mind import conversation
from organs.soul import load as load_soul

WHO = "3425635473"
PLUGIN = Path(__file__).resolve().parents[3]
SOUL_FIXTURES = PLUGIN / "organs" / "soul" / "fixtures"
CONFIG = {"provider": "fake", "model": "fake-model", "base_url": "http://example.invalid/v1",
          "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 1000, "retries": 0,
          "temperature": 0.0, "max_tokens": 200}
ENV = {"QQAGENT_LLM_KEY": "test-key"}
NOW = "2025-01-15T14:05:00+09:00"
ANSWER = "嗯，我想想。"

#: 命中关键词的那一问（「翻翻」「日记」「十月」都在 keywords 里）。
HIT = "翻翻你以前的日记，十月你在忙什么"
#: 不命中的那一问：一个关键词都没有。
MISS = "今天天气怎么样"

#: 「不传 scene」那一种：answer 的缺省（归一成 unknown），不是 private 也不是 group。
OMIT = object()

PLAIN = "日记-普通"
LOVE = "日记-恋爱"


def _transport(captured: list = None):
    """假 transport：回一句固定的话，顺手把请求体记下来（读提示词用）。"""
    def send(url, headers, payload, timeout):
        if captured is not None:
            captured.append(payload)
        body = {"choices": [{"message": {"content": ANSWER}}]}
        return 200, json.dumps(body, ensure_ascii=False).encode("utf-8")
    return send


def _section(text: str, label: str) -> str:
    """提示词里某一节的正文：从「[标题]」到下一个空行分节为止（段与段之间是空行）。"""
    head = text.index("[%s]" % label) + len(label) + 3      # 「[标题]」后面的那个换行也要跳过
    tail = text.find("\n\n[", head)
    return text[head:tail] if tail >= 0 else text[head:]


def _row(card_id: str, date: str, text: str, note: str = "", kind: str = "日记") -> dict:
    """一行日记卡（与实盘抽屉同形：正文就在 params.text 里，testimony=false）。"""
    return {"card_id": card_id, "room": "抽屉", "kind": kind,
            "name": "%s 普通日记（旧家 doc %s）" % (date, card_id),
            "path": None, "sha256": None, "bytes": 0, "chars": len(text),
            "note": note or "旧家日叙事摘要：她当时写下的经过，不是事实，也不是他的话。",
            "by": "迁移", "role": "program", "at": "%sT13:27:04+09:00" % date,
            "testimony": False,
            "params": {"date": date, "text": text, "from_old_home": True}}


class DrawerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "house"
        self.home.mkdir(parents=True)
        patches = mock.patch.dict(os.environ, {
            "QQAGENT_HOME": str(self.tmp / "home"),      # 装配说明书的实盘根：空着，走出厂
            "HOME_ROOT": str(self.home),                  # 家的根：临时根，实盘一个字不读
            "HEART_ROOT": str(self.tmp / "heart"),
            "MOUTH_TRACE_DIR": str(self.tmp / "trace"),
        })
        patches.start()
        self.addCleanup(patches.stop)
        self.store = self._soul()
        self.brain = BrainStore(self.tmp / "brain")

    def _soul(self):
        target = self.tmp / "soul"
        shutil.copytree(str(SOUL_FIXTURES / "valid"), str(target))
        return load_soul(target)

    # —— 造家 ————————————————————————————————

    def _house(self, shelves: dict) -> None:
        """往临时家里写抽屉：{格子: [卡片行]}，一格一份 index.jsonl（只追加的流水）。"""
        for shelf, rows in shelves.items():
            folder = self.home / "抽屉" / shelf
            folder.mkdir(parents=True, exist_ok=True)
            lines = [json.dumps(row, ensure_ascii=False) for row in rows]
            (folder / "index.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _five(self) -> None:
        self._house({
            PLAIN: [
                _row("a1", "2026-10-22", "十月二十二号，去医院陪他复查，走廊里冷得像水里。"),
                _row("a2", "2026-10-31", "十月最后一天，凌晨两点还在群里说话。"),
                _row("a3", "2026-09-15", "九月十五号，把鱼缸擦了一遍。"),
                _row("a4", "2026-11-02", "十一月二号，第一次梦见旧家。"),
            ],
            LOVE: [
                _row("b1", "2026-10-20", "十月二十号，喜欢他这件事写在纸上。"),
            ],
        })

    def _count_reads(self) -> dict:
        """把家那两个读口换成计数的壳：不命中时它们必须一次都没被调到。"""
        calls = {"list_room": 0, "read_item": 0}
        real_list, real_read = cards.list_room, cards.read_item

        def list_room(*args, **kwargs):
            calls["list_room"] += 1
            return real_list(*args, **kwargs)

        def read_item(*args, **kwargs):
            calls["read_item"] += 1
            return real_read(*args, **kwargs)

        for name, stand_in in (("list_room", list_room), ("read_item", read_item)):
            patch = mock.patch.object(cards, name, stand_in)
            patch.start()
            self.addCleanup(patch.stop)
        return calls

    def _answer(self, text: str = HIT, *, scene=OMIT, captured: list = None, **extra):
        cap = captured if captured is not None else []
        args = {} if scene is OMIT else {"scene": scene}      # 不给 = 走 answer 的缺省（unknown）
        return conversation.answer(text, WHO, store=self.store, brain=self.brain,
                                   now=NOW, transport=_transport(cap), config=CONFIG,
                                   env=ENV, **args, **extra), cap

    def _body(self, cap: list, label: str = None) -> str:
        """这一轮抽屉那一节的正文（标题之后、下一个空行分节之前）；没翻就是空串。"""
        label = label if label is not None else conversation.load_assembly()["drawer"]["label"]
        system = cap[0]["messages"][0]["content"]
        return _section(system, label) if ("[%s]" % label) in system else ""

    def _drawer_row(self, got):
        rows = [row for row in got["assembly"]["sections"] if row["key"] == "drawer"]
        self.assertEqual(1, len(rows), "抽屉那一节的回包格不对：%r" % (rows,))
        return rows[0]

    # —— 1) 命中：节 / 条数 / 上限 / 那句说明 ————————————

    def test_a_hit_adds_one_section_with_label_entries_and_the_note(self):
        self._five()
        got, cap = self._answer()
        conf = conversation.load_assembly()["drawer"]
        system = cap[0]["messages"][0]["content"]
        self.assertIn("[%s]" % conf["label"], system, "这一节的标题没进提示词")
        body = _section(system, conf["label"])
        row = self._drawer_row(got)
        self.assertLessEqual(row["count"], conf["max_entries"], "条数超过 max_entries")
        self.assertGreaterEqual(row["count"], 2, "命中了一次都没放进来")
        self.assertLessEqual(len(body), conf["cap"], "整节超过 cap")
        self.assertEqual(len(body), row["chars"], "回包 chars 与实际字数不一致")
        self.assertTrue(body.endswith(conf["testimony_note"]),
                        "节尾少了那句「这些是我自己写下的」：%r" % body[-40:])
        for line in [row for row in body.split("\n") if row.startswith("- ")]:
            self.assertRegex(line, r"^- \d{4}-\d{2}-\d{2}：", "行首没有日期：%r" % line[:20])

    # —— 2) 没命中：三档都是一节都不加，一次 IO 都不做 ————————

    def test_no_keyword_means_no_section_and_no_read_in_any_scene(self):
        """翻不翻只看意图：三档（含不传 scene 的缺省）没命中都是一次 IO 都不做。"""
        self._five()
        label = conversation.load_assembly()["drawer"]["label"]
        for scene in (brain_model.SCENE_PRIVATE, brain_model.SCENE_GROUP,
                      brain_model.SCENE_UNKNOWN, OMIT):
            calls = self._count_reads()
            got, cap = self._answer(MISS, scene=scene)
            system = cap[0]["messages"][0]["content"]
            self.assertNotIn("[%s]" % label, system, "没命中却加了那一节（%r 档）" % (scene,))
            self.assertEqual({"list_room": 0, "read_item": 0}, calls,
                             "没命中还去翻了抽屉（%r 档）" % (scene,))
            self.assertEqual([],
                             [row for row in got["assembly"]["sections"] if row["key"] == "drawer"],
                             "回包里还留着那一格（%r 档）" % (scene,))

    # —— 3) 场合只决定能取哪几格（翻不翻不看它） ——————————

    def _two_shelves(self) -> tuple:
        """普通 + 恋爱各一篇：三档各跑一次，看哪篇进得来、有没有那句「来源未定」。"""
        self._house({
            PLAIN: [_row("p1", "2026-10-22", "十月二十二号，去医院陪他复查，走廊里冷得像水里。")],
            LOVE: [_row("b1", "2026-10-20", "十月二十号，喜欢他这件事写在纸上。")],
        })
        return "十月二十二号", "喜欢他这件事写在纸上"

    def test_private_pulls_both_shelves_and_has_no_source_note(self):
        plain, love = self._two_shelves()
        conf = conversation.load_assembly()["drawer"]
        got, cap = self._answer(scene=brain_model.SCENE_PRIVATE)
        body = self._body(cap)
        self.assertIn(plain, body, "私聊里取不到普通日记")
        self.assertIn(love, body, "私聊里取不到恋爱日记——这条改窄了就是把私聊也削了")
        self.assertNotIn(conf["unknown_note"], body, "私聊里多了一句「来源未定」")
        self.assertEqual(2, self._drawer_row(got)["count"])

    def test_group_flips_but_takes_only_the_plain_shelf(self):
        plain, love = self._two_shelves()
        conf = conversation.load_assembly()["drawer"]
        calls = self._count_reads()
        got, cap = self._answer(scene=brain_model.SCENE_GROUP)
        body = self._body(cap)
        self.assertIn(plain, body, "群聊里关键词命中却没翻（翻不翻不该看场合）")
        self.assertNotIn(love, body, "私密内容进了群聊上下文")
        self.assertNotIn(conf["unknown_note"], body, "群聊里多了一句「来源未定」")
        self.assertGreaterEqual(calls["list_room"], 1, "群聊里一次 IO 都没做")
        self.assertGreaterEqual(calls["read_item"], 1, "群聊里一篇正文都没读")
        self.assertEqual(1, self._drawer_row(got)["count"])

    def test_unknown_flips_plain_only_and_says_the_source_is_undecided(self):
        plain, love = self._two_shelves()
        conf = conversation.load_assembly()["drawer"]
        calls = self._count_reads()
        got, cap = self._answer(scene=brain_model.SCENE_UNKNOWN)
        body = self._body(cap)
        self.assertIn(plain, body, "unknown 里关键词命中却没翻")
        self.assertNotIn(love, body, "unknown 里取了私密那格（保守那一条没做到）")
        self.assertIn(conf["unknown_note"], body, "unknown 里没标「来源未定」")
        self.assertTrue(body.startswith(conf["unknown_note"]),
                        "那句「来源未定」该在节内第一行（节标题之后、条目之前）：%r" % body[:40])
        self.assertGreaterEqual(calls["list_room"], 1, "unknown 里一次 IO 都没做")
        self.assertGreaterEqual(calls["read_item"], 1, "unknown 里一篇正文都没读")
        self.assertEqual(1, self._drawer_row(got)["count"])

    def test_a_missing_scene_is_unknown_and_is_not_guessed(self):
        """不传 scene：走 unknown 那一档——不许被推成 group（少标一句）也不许推成 private（多说私话）。"""
        plain, love = self._two_shelves()
        conf = conversation.load_assembly()["drawer"]
        calls = self._count_reads()
        _got, cap = self._answer()
        body = self._body(cap)
        self.assertTrue(body, "不传 scene 就整节不翻了")
        self.assertIn(plain, body, "缺省场景里取不到普通日记")
        self.assertNotIn(love, body, "缺省场景被推成了 private：私密日记进了上下文")
        self.assertIn(conf["unknown_note"], body,
                      "缺省场景被推成了 group：少了那句「来源未定」（unknown 才插）")
        self.assertGreaterEqual(calls["list_room"], 1)

    def test_the_unknown_shelf_list_is_data_driven(self):
        """把 shelves.unknown 改成含恋爱那格，unknown 里就能取到它——证明不是代码写死的。"""
        _plain, love = self._two_shelves()
        conf = conversation.load_assembly()
        self.assertNotIn(love, self._body(self._answer(scene=brain_model.SCENE_UNKNOWN)[1]))
        conf["drawer"]["shelves"][brain_model.SCENE_UNKNOWN] = [PLAIN, LOVE]
        _got, cap = self._answer(scene=brain_model.SCENE_UNKNOWN, assembly=conf)
        self.assertIn(love, self._body(cap), "shelves.unknown 改了，取格没跟着改")

    # —— 3.5) 主动开口那一轮用同一把尺子 ————————————————

    def test_a_proactive_round_reads_the_same_judgement(self):
        """她自己开口那一轮的「问」是由头：由头提到旧事，同样翻。"""
        self._five()
        got, cap = self._answer(seed="翻翻以前的日记，十月那阵子",
                                scene=brain_model.SCENE_PRIVATE)
        label = conversation.load_assembly()["drawer"]["label"]
        self.assertIn("[%s]" % label, cap[0]["messages"][0]["content"],
                      "主动那一轮没按同一份判据翻")
        self.assertEqual(3, self._drawer_row(got)["count"])

    # —— 4) 恋爱日记只在私聊 ————————————————————————

    def test_shelves_say_love_diary_is_private_only(self):
        conf = conversation.load_assembly()["drawer"]
        self.assertIn(LOVE, conf["shelves"][brain_model.SCENE_PRIVATE])
        self.assertIn(PLAIN, conf["shelves"][brain_model.SCENE_PRIVATE])
        for name in (brain_model.SCENE_GROUP, brain_model.SCENE_UNKNOWN):
            self.assertNotIn(LOVE, conf["shelves"][name], "%s 那格不该有恋爱日记" % name)
            self.assertEqual([PLAIN], conf["shelves"][name])

    def test_a_love_diary_is_reachable_in_private(self):
        self._house({LOVE: [_row("b1", "2026-10-20", "十月二十号，喜欢他这件事写在纸上。")]})
        got, cap = self._answer(scene=brain_model.SCENE_PRIVATE)
        body = _section(cap[0]["messages"][0]["content"],
                        conversation.load_assembly()["drawer"]["label"])
        self.assertIn("喜欢他这件事写在纸上", body, "私聊里选不到恋爱日记")
        self.assertEqual(1, self._drawer_row(got)["count"])

    # —— 5) 挑法：相关性优先于日期 ————————————————————

    def test_a_topic_hit_wins_over_a_newer_diary(self):
        self._house({PLAIN: [
            _row("old", "2026-03-01", "三月一号，那天在医院复查，走廊很长。",
                 note="旧家日叙事摘要：医院复查那一段"),
            _row("new", "2026-10-30", "十月三十号，晚上看了一会儿书。"),
        ]})
        conf = conversation.load_assembly()
        conf["drawer"]["max_entries"] = 1
        got, cap = self._answer("翻翻以前的日记，医院复查那次", assembly=conf)
        body = _section(cap[0]["messages"][0]["content"], conf["drawer"]["label"])
        self.assertIn("医院复查", body, "题材对得上的那篇没先选上")
        self.assertEqual(1, self._drawer_row(got)["count"])

    def test_a_month_word_wins_too(self):
        self._house({PLAIN: [
            _row("sep", "2026-09-20", "九月二十号，晚上把鱼缸擦了。"),
            _row("oct", "2026-10-02", "十月二号，早起看见光斜进来。"),
        ]})
        conf = conversation.load_assembly()
        conf["drawer"]["max_entries"] = 1
        _got, cap = self._answer("翻翻日记，十月那阵子", assembly=conf)
        body = _section(cap[0]["messages"][0]["content"], conf["drawer"]["label"])
        self.assertIn("十月二号", body, "问句里的月份词没当相关性")

    # —— 6) 超长：按 cap 收，收在句末 ————————————————

    def test_a_two_thousand_char_diary_is_cut_at_a_sentence_end(self):
        long_text = "".join("第%d天，她在水里想起旧家的事，那句话没说出口。" % index
                            for index in range(1, 101))
        self.assertGreater(len(long_text), 2000)
        self._house({PLAIN: [_row("long", "2026-10-22", long_text)]})
        conf = conversation.load_assembly()["drawer"]
        got, cap = self._answer()
        body = _section(cap[0]["messages"][0]["content"], conf["label"])
        self.assertLessEqual(len(body), conf["cap"], "整节超过 cap")
        self.assertEqual(len(body), self._drawer_row(got)["chars"])
        ends = conversation.load_assembly()["tone_rules"]["sentence_end_chars"]
        lines = [row for row in body.split("\n") if row.startswith("- ")]
        self.assertEqual(1, len(lines), "一篇日记应该只占一行")
        text = lines[0][len("- 2026-10-22："):]
        self.assertTrue(text.endswith(conf["more"]), "超长那篇没接省略号：%r" % text[-10:])
        self.assertIn(text[-len(conf["more"]) - 1], ends, "截断没落在句末：%r" % text[-10:])
        self.assertLessEqual(len(text) - len(conf["more"]), conf["line_chars"])

    # —— 7) 数据驱动 ————————————————————————————

    def test_editing_the_keywords_and_the_entry_count_changes_the_result(self):
        self._five()
        conf = conversation.load_assembly()
        got, cap = self._answer(MISS, assembly=conf)
        self.assertNotIn(conf["drawer"]["label"], cap[0]["messages"][0]["content"])
        conf["drawer"]["keywords"] = ["天气"]
        got, cap = self._answer(MISS, assembly=conf)
        self.assertIn(conf["drawer"]["label"], cap[0]["messages"][0]["content"],
                      "keywords 改了，判据没跟着改")
        conf["drawer"]["max_entries"] = 1
        got, cap = self._answer(MISS, assembly=conf)
        self.assertEqual(1, self._drawer_row(got)["count"], "max_entries 改了，条数没跟着改")

    def test_missing_drawer_block_means_no_flip(self):
        self._five()
        calls = self._count_reads()
        conf = conversation.load_assembly()
        label = conf["sections"]["drawer"]["label"]
        del conf["drawer"]
        got, cap = self._answer(HIT, assembly=conf)
        self.assertNotIn(label, cap[0]["messages"][0]["content"])
        self.assertEqual({"list_room": 0, "read_item": 0}, calls, "缺 drawer 块还去翻了")
        self.assertEqual([], [row for row in got["assembly"]["sections"] if row["key"] == "drawer"])

    def test_a_disabled_drawer_does_not_flip(self):
        self._five()
        calls = self._count_reads()
        conf = conversation.load_assembly()
        conf["drawer"]["enabled"] = False
        self._answer(HIT, assembly=conf)
        self.assertEqual({"list_room": 0, "read_item": 0}, calls)

    # —— 8) 日记不进脑 ————————————————————————————

    def test_the_diary_text_never_lands_in_the_brain(self):
        body = "十月二十二号，去医院陪他复查，走廊里冷得像水里。"
        self._house({PLAIN: [_row("a1", "2026-10-22", body)]})
        got, cap = self._answer()
        self.assertIn(body[:12], cap[0]["messages"][0]["content"], "没进提示词，这条断言就没意义")
        wrote = got["memory"]["wrote"]
        self.assertTrue(wrote, "这一轮人那句与她那句都没落脑？")
        dump = ""
        for path in sorted((self.tmp / "brain").rglob("*")):
            if path.is_file():
                dump += path.read_text(encoding="utf-8", errors="ignore")
        for size in (6, 8, 12):
            for start in range(0, len(body) - size + 1):
                piece = body[start:start + size]
                self.assertNotIn(piece, dump,
                                 "日记正文的连续 %d 字落进了脑：%r" % (size, piece))
        for row in got["recalled"]:
            self.assertNotIn(body[:8], json.dumps(row, ensure_ascii=False),
                             "日记正文进了回想的条目")

    # —— 9) 数据坏：当场报，不兜底 ——————————————————

    def test_a_drawer_block_that_disagrees_with_its_section_is_reported(self):
        conf = conversation.load_assembly()
        conf["drawer"]["cap"] = 120
        with self.assertRaises(ValueError) as caught:
            self._answer(HIT, assembly=conf)
        self.assertIn("drawer.cap", str(caught.exception), "两处不一致没当场报")

    def test_a_broken_drawer_block_is_reported_not_defaulted(self):
        conf = conversation.load_assembly()
        del conf["drawer"]["keywords"]
        with self.assertRaises(ValueError) as caught:
            self._answer(HIT, assembly=conf)
        self.assertIn("drawer.keywords", str(caught.exception), "缺关键词表被悄悄补了个默认值")

    def test_a_broken_month_table_is_reported(self):
        conf = conversation.load_assembly()
        conf["drawer"]["month_words"] = {}
        with self.assertRaises(ValueError) as caught:
            self._answer(HIT, assembly=conf)
        self.assertIn("drawer.month_words", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
