"""抽屉那一节：装配前判一次意图，命中才向家取几篇旧日记（只进提示词，不进长期记忆）。

判据（每条都对应交付里的一句话）：
1. **恒定一篇**：每轮都带**一篇**与这一句最贴近的旧日记（drawer_always 那一节，标题
   `sections.drawer_always.label`「（你写过的一篇日记）」），一行旧日记 + 一句 always.note；
   整块（含段标题前缀）≤ always.max_chars。三档场合都有，字数单独可断言，**与关键词无关**。
   排在最前的那一篇压不进上限（第一句就比余地长、截不出整句）就换下一篇——实盘 343 篇里
   排第一的那几篇第一句常常 100+ 字，老代码在这一篇上就地放弃，于是 10 轮里恒定段一次都没出现。
2. **注入里不许出现实现词**：两节标题与几句说明都是她视角，常带 / 恒定 / 回流 / 背景 / 缓存
   一个都不许出现（判据由 `test_the_injected_prompt_has_no_implementation_words` 直接 grep）。
3. **同一轮不许重复同一篇**：恒定那篇用过的那一篇从"被问时候选"里剔掉；剔完没了被问那一段
   就空着（不为凑数把同一篇放回来）。
4. **同一篇不连着带**：name 在 dedupe_days 天内带过就换下一篇（账本
   <器官目录>/mind/drawer_usage.jsonl，一轮一行 {at,name,scene,score,why}）；8 天前的
   那一行出了窗口，可以用回来。
5. **命中才翻那 2–3 篇**：问句里出现 drawer.keywords 里任一个词 → 再多一节，标题是数据里的
   drawer.label，条数 ≤ max_entries，整节总字数 ≤ cap，节尾有那句 testimony_note。
6. **翻不翻只看意图**：关键词命中 + enabled 就翻，**不看场合**——群聊 / unknown 同样真去读
   （list_room 计数 ≥ 1）；**没命中就只留恒定那篇**，多篇那一节一节都不加。抽屉整块关着 /
   缺这一块才是「一次 IO 都不做」。
7. **场合只决定能取哪几格**：drawer.shelves 里 private 含普通 + 恋爱两格，group / unknown 只有
   普通那格（两段都照这一份）；unknown 那档在多篇那一节的节内第一行插一句
   drawer.unknown_note（「来源未定」），private / group 都不插。**unknown 不许被推成 group 或
   private**：不传 scene 就走这一档。
8. **挑法**：相关性（那个月的说法整词命中，或 name / at / note 与问句连续重合若干字）优先，
   再按日期从新到旧补足；恒定那篇按同一份分取最高的。
9. **超长不硬塞**：一篇 2000 字日记压进 cap 以内，且收在句末（绝不截半个词）。
10. **数据驱动**：改 keywords / max_entries / max_chars 结果跟着变；缺 drawer 块 = 不翻；
   缺 always、max_chars / dedupe_days 非正 → ValueError 并指名。
11. **日记不进脑**：这一节只进提示词，落脑的条目里没有任何一段日记正文。

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
        system = "\n\n".join(row["content"] for row in cap[0]["messages"])
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
        system = "\n\n".join(row["content"] for row in cap[0]["messages"])
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

    # —— 2) 没命中：多篇那一节一节都不加（恒定那篇照旧） ——————————

    def test_no_keyword_drops_the_flipped_part_and_keeps_the_background_one(self):
        """翻不翻只看意图：三档（含不传 scene 的缺省）没命中就只留恒定那篇。

        恒定那篇**每轮都读**（一次 list_room + 一次 read_item 就是它的成本）——这与
        「抽屉整块关着 / 缺这一块一次 IO 都不做」是两件事，那两条另测（见第 7 节）。
        """
        self._five()
        conf = conversation.load_assembly()
        label = conf["sections"]["drawer"]["label"]
        for scene in (brain_model.SCENE_PRIVATE, brain_model.SCENE_GROUP,
                      brain_model.SCENE_UNKNOWN, OMIT):
            self._ledger([])          # 每一档都从头带，别让上一档的去重把它挡掉
            calls = self._count_reads()
            got, cap = self._answer(MISS, scene=scene)
            system = "\n\n".join(row["content"] for row in cap[0]["messages"])
            self.assertNotIn("[%s]" % label, system, "没命中却加了那一节（%r 档）" % (scene,))
            self.assertTrue(self._always_body(cap), "没命中就连恒定那篇也没了（%r 档）" % (scene,))
            self.assertGreaterEqual(calls["list_room"], 1, "恒定那篇没去读抽屉（%r 档）" % (scene,))
            self.assertEqual(1, calls["read_item"], "恒定那篇该正好读一篇正文（%r 档）" % (scene,))
            self.assertEqual([],
                             [row for row in got["assembly"]["sections"] if row["key"] == "drawer"],
                             "回包里还留着那一格（%r 档）" % (scene,))

    # —— 3) 场合只决定能取哪几格（翻不翻不看它） ——————————

    def _two_shelves(self) -> tuple:
        """普通 + 恋爱**各两篇**：三档各跑一次，看哪篇进得来、有没有那句「来源未定」。

        每格两篇是互斥规则要求的：恒定那篇先占掉每格里排名第一的那一篇，被问那一段还得有
        同格的下一篇可挑——一格一篇的话剔完就空，测不出「取得到这一格」。
        同一格两篇共用一个标记句，断言才不认「具体是哪一篇」。
        """
        self._house({
            PLAIN: [_row("p1", "2026-10-22", "十月二十二号，去医院陪他复查，走廊里冷得像水里。"),
                    _row("p2", "2026-10-11", "十月十一号，又去医院陪他复查，走廊里还是冷。")],
            LOVE: [_row("b1", "2026-10-20", "十月二十号，喜欢他这件事写在纸上。"),
                   _row("b2", "2026-10-02", "十月二号，喜欢他这件事写在纸上，一直没改。")],
        })
        return "去医院陪他复查", "喜欢他这件事写在纸上"

    def test_private_pulls_both_shelves_and_has_no_source_note(self):
        plain, love = self._two_shelves()
        conf = conversation.load_assembly()["drawer"]
        got, cap = self._answer(scene=brain_model.SCENE_PRIVATE)
        body = self._body(cap)
        self.assertIn(plain, body, "私聊里取不到普通日记")
        self.assertIn(love, body, "私聊里取不到恋爱日记——这条改窄了就是把私聊也削了")
        self.assertNotIn(conf["unknown_note"], body, "私聊里多了一句「来源未定」")
        # 四篇里恒定那篇先占一篇，剩下三篇正好是被问那一段的上限（max_entries = 3）。
        self.assertEqual(3, self._drawer_row(got)["count"])

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
        self.assertIn("[%s]" % label, "\n\n".join(row["content"] for row in cap[0]["messages"]),
                      "主动那一轮没按同一份判据翻")
        self.assertEqual(3, self._drawer_row(got)["count"])

    # —— 4) 恋爱日记只在私聊 ————————————————————————

    def test_newly_written_diary_reaches_private_chat_but_not_group_or_unknown(self):
        from organs.day import night
        from organs.day.tests.helpers import fake_mind
        from organs.home import rooms
        for name in rooms.ROOM_NAMES:
            (self.home/name).mkdir(parents=True,exist_ok=True)
        (self.home/'access.json').write_text(json.dumps({'version':1,'writable':[]}))
        text = '刷到纸箱里的小狐狸，探头又缩了回去。'
        stamp = '2025-01-14T21:00:00+09:00'
        self.brain.ingest(who=WHO, text='聊到纸箱里的小狐狸。', ts=stamp)
        result = night.run(stamp, root=self.tmp/'day', brain=self.brain,
                           heart_root=self.tmp/'heart', home_root=self.home,
                           mind=fake_mind(text), only=['home_diary'])
        self.assertEqual('ok',result['steps']['home_diary']['status'],result)
        entries=self.brain.today_view(stamp)['entries']
        self.assertTrue(any(row.get('source_type')=='日记' for row in entries))
        self.assertFalse(any(text in row.get('text','') for row in entries))
        path = self.home/'抽屉'/'日记'/'index.jsonl'
        self.assertTrue(path.is_file())
        before = path.read_bytes()
        for scene in (brain_model.SCENE_GROUP,brain_model.SCENE_UNKNOWN):
            _, captured = self._answer('还记得纸箱里的小狐狸吗', scene=scene)
            self.assertNotIn(text,"\n\n".join(row["content"] for row in captured[0]['messages']))
        _, captured = self._answer('还记得纸箱里的小狐狸吗',scene=brain_model.SCENE_PRIVATE)
        self.assertIn(text,"\n\n".join(row["content"] for row in captured[0]['messages']))
        self.assertEqual(before,path.read_bytes())

    def test_shelves_say_love_diary_is_private_only(self):
        conf = conversation.load_assembly()["drawer"]
        self.assertIn(LOVE, conf["shelves"][brain_model.SCENE_PRIVATE])
        self.assertIn(PLAIN, conf["shelves"][brain_model.SCENE_PRIVATE])
        for name in (brain_model.SCENE_GROUP, brain_model.SCENE_UNKNOWN):
            self.assertNotIn(LOVE, conf["shelves"][name], "%s 那格不该有恋爱日记" % name)
            self.assertEqual([PLAIN], conf["shelves"][name])

    def test_a_love_diary_is_reachable_in_private(self):
        self._house({LOVE: [
            _row("b1", "2026-10-20", "十月二十号，喜欢他这件事写在纸上。"),
            _row("b2", "2026-10-02", "十月二号，喜欢他这件事写在纸上，一直没改。"),
        ]})
        got, cap = self._answer(scene=brain_model.SCENE_PRIVATE)
        body = _section("\n\n".join(row["content"] for row in cap[0]["messages"]),
                        conversation.load_assembly()["drawer"]["label"])
        self.assertIn("喜欢他这件事写在纸上", body, "私聊里选不到恋爱日记")
        self.assertEqual(1, self._drawer_row(got)["count"])

    # —— 5) 挑法：相关性优先于日期 ————————————————————

    def test_body_topic_past_list_excerpt_reaches_prompt_with_whole_sentence(self):
        topic = '旧渡口那只纸船漂到了桥洞下面。'
        body = '那天只是坐着发呆。' * 24 + topic + '后来就回去了。' * 10
        self._house({PLAIN:[_row('old-topic','2024-01-01',body)] + [
            _row('new-'+str(i),'2024-12-%02d' % i,'那天整理了桌面。') for i in range(1,6)],
            LOVE:[_row('private-topic','2024-12-20','旧渡口那只纸船旁有私密约定。')]})
        before={p:p.read_bytes() for p in self.home.rglob('index.jsonl')}
        for scene in (brain_model.SCENE_GROUP,brain_model.SCENE_UNKNOWN):
            _, captured=self._answer('还记得旧渡口那只纸船吗',scene=scene)
            system="\n\n".join(row["content"] for row in captured[0]['messages'])
            self.assertIn(topic,system)
            self.assertNotIn('私密约定',system)
        conf=conversation.load_assembly()
        text=self._body(captured)
        self.assertLessEqual(len(text),conf['drawer']['cap'])
        self.assertIn('…'+topic,system, '从正文中间取出时应明确前文省略')
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_diary_topic_excerpt_preserves_boundaries_and_marks_both_omissions(self):
        spec={'more':'…'}
        body='前面是无关闲话。旧渡口那只纸船漂走了。后面还是无关闲话。'
        piece,cut=conversation._drawer_excerpt(body,18,spec,'。！？',said='旧渡口那只纸船',topic_min_chars=3)
        self.assertEqual('…旧渡口那只纸船漂走了。…',piece)
        self.assertTrue(cut)
        self.assertLessEqual(len(piece),18)
        self.assertEqual(('',True),conversation._drawer_excerpt(body,8,spec,'。！？',said='旧渡口那只纸船',topic_min_chars=3))

    def test_longest_topic_match_keeps_repeated_text_and_unicode(self):
        for query,body,expected in [('', '旧日记',0),('纸船','纸和船',1),
                                    ('旧渡口纸船','桥洞旧渡口纸船漂走',5),
                                    ('哈'*210,'哈'*300,210),('鲸鱼🐋','蓝色鲸鱼🐋',3)]:
            with self.subTest(query=query[:10]):
                self.assertEqual(expected,conversation._drawer_overlap(query,body))

    def test_a_topic_hit_wins_over_a_newer_diary(self):
        self._house({PLAIN: [
            _row("old", "2026-03-01", "三月一号，那天在医院复查，走廊很长。",
                 note="旧家日叙事摘要：医院复查那一段"),
            _row("old2", "2026-03-02", "三月二号，还在说医院复查的事。",
                 note="旧家日叙事摘要：医院复查那一段"),
            _row("new", "2026-10-30", "十月三十号，晚上看了一会儿书。"),
        ]})
        conf = conversation.load_assembly()
        conf["drawer"]["max_entries"] = 1
        got, cap = self._answer("翻翻以前的日记，医院复查那次", assembly=conf)
        body = _section("\n\n".join(row["content"] for row in cap[0]["messages"]), conf["drawer"]["label"])
        self.assertIn("医院复查", body, "题材对得上的那篇没先选上")
        self.assertEqual(1, self._drawer_row(got)["count"])

    def test_a_month_word_wins_too(self):
        self._house({PLAIN: [
            _row("sep", "2026-09-20", "九月二十号，晚上把鱼缸擦了。"),
            _row("oct", "2026-10-02", "十月二号，早起看见光斜进来。"),
            _row("oct2", "2026-10-03", "十月三号，光又斜进来了。"),
        ]})
        conf = conversation.load_assembly()
        conf["drawer"]["max_entries"] = 1
        _got, cap = self._answer("翻翻日记，十月那阵子", assembly=conf)
        body = _section("\n\n".join(row["content"] for row in cap[0]["messages"]), conf["drawer"]["label"])
        self.assertIn("十月二号", body, "问句里的月份词没当相关性")

    # —— 6) 超长：按 cap 收，收在句末 ————————————————

    def test_a_two_thousand_char_diary_is_cut_at_a_sentence_end(self):
        long_text = "".join("第%d天，她在水里想起旧家的事，那句话没说出口。" % index
                            for index in range(1, 101))
        self.assertGreater(len(long_text), 2000)
        # 恒定那篇要落在另一篇上（互斥）：超长这一篇必须留在被问那一段里才测得到截断。
        self._house({PLAIN: [
            _row("long", "2026-03-01", long_text),
            _row("short", "2026-10-28", "十月二十八号，晚上看了一会儿书。"),
        ]})
        conf = conversation.load_assembly()["drawer"]
        got, cap = self._answer()
        body = _section("\n\n".join(row["content"] for row in cap[0]["messages"]), conf["label"])
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
        self.assertNotIn(conf["drawer"]["label"], "\n\n".join(row["content"] for row in cap[0]["messages"]))
        conf["drawer"]["keywords"] = ["天气"]
        got, cap = self._answer(MISS, assembly=conf)
        self.assertIn(conf["drawer"]["label"], "\n\n".join(row["content"] for row in cap[0]["messages"]),
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
        self.assertNotIn(label, "\n\n".join(row["content"] for row in cap[0]["messages"]))
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
        self.assertIn(body[:12], "\n\n".join(row["content"] for row in cap[0]["messages"]), "没进提示词，这条断言就没意义")
        wrote = got["memory"]["wrote"]
        self.assertTrue(wrote, "这一轮人那句与她那句都没落脑？")
        dump = ""
        # 三处都查：脑（长期记忆）、recent（更早那几轮）、恒定那篇的账本（只该有 name，没有正文）。
        for folder in (self.tmp / "brain", self.tmp / "home"):
            for path in sorted(folder.rglob("*")):
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

    # —— 10) 恒定那篇：每轮一篇、按话题挑、7 天不重复 ——————————

    def _always_label(self, conf: dict = None) -> str:
        conf = conf if conf is not None else conversation.load_assembly()
        return conf["sections"]["drawer_always"]["label"]

    def _always_body(self, cap: list, conf: dict = None) -> str:
        """提示词里恒定那一段的正文（标题之后、下一节之前）；没有这一段就是空串。"""
        system = "\n\n".join(row["content"] for row in cap[0]["messages"])
        label = self._always_label(conf)
        return _section(system, label) if ("[%s]" % label) in system else ""

    def _always_block(self, cap: list, conf: dict = None) -> str:
        """那一整块：段标题前缀也要算进 always.max_chars（交付里那句「含前缀」）。"""
        body = self._always_body(cap, conf)
        return "" if not body else "[%s]\n%s" % (self._always_label(conf), body)

    def _ledger(self, rows: list) -> None:
        """把恒定那篇的账本写成这几种时刻的历史（去重读的就是这一本）。"""
        path = conversation.drawer_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")

    def test_every_round_carries_one_background_diary_in_every_scene(self):
        """三档场合每轮都有一篇，且那一块（含标题前缀）≤ always.max_chars。"""
        self._house({PLAIN: [
            _row("a2", "2026-10-31", "十月最后一天，凌晨两点还在群里说话。"),
            _row("a4", "2026-11-02", "十一月二号，第一次梦见旧家。"),
        ]})
        conf = conversation.load_assembly()
        always = conf["drawer"]["always"]
        self.assertEqual("（你写过的一篇日记）", self._always_label(conf),
                         "恒定那一段的标题要是她视角（不许出现实现词）：现在是 %r"
                         % self._always_label(conf))
        for scene in (brain_model.SCENE_PRIVATE, brain_model.SCENE_GROUP,
                      brain_model.SCENE_UNKNOWN):
            self._ledger([])
            got, cap = self._answer(MISS, scene=scene)      # 没关键词：只有恒定那篇
            block = self._always_block(cap)
            self.assertTrue(block, "%r 档这一轮没有恒定那篇" % (scene,))
            body = self._always_body(cap)
            lines = [row for row in body.split("\n") if row.startswith("- ")]
            self.assertEqual(1, len(lines), "恒定那篇该正好一篇：%r" % (body,))
            self.assertLessEqual(len(block), always["max_chars"],
                                 "恒定那一段（含「[标题]」前缀）超过 max_chars：%d" % len(block))
            self.assertIn(always["note"], body, "恒定那一段少了那句「这是背景，不是话头」")
            self.assertNotIn(always["note"], lines[0], "说明该另起一行，不是压在日记行尾")
            rows = [row for row in got["assembly"]["sections"] if row["key"] == "drawer_always"]
            self.assertEqual(1, len(rows), "回包里没有恒定那一格（%r 档）" % (scene,))
            self.assertEqual(1, rows[0]["count"])
            self.assertEqual(len(body), rows[0]["chars"], "回包 chars 与实际字数不一致")

    def test_the_background_piece_is_the_one_closest_to_the_question(self):
        """两篇：一篇与问句重合、一篇不重合——恒定那篇挑重合的那一篇（不是随机、不是最新）。"""
        self._house({PLAIN: [
            _row("near", "2026-03-01", "医院复查那天，走廊很长。",
                 note="旧家日叙事摘要：医院复查那一段"),
            _row("far", "2026-10-30", "晚上看了一会儿书，窗外的风很大。"),
        ]})
        _got, cap = self._answer("翻翻以前的日记，医院复查那次")
        body = self._always_body(cap)
        self.assertIn("医院复查", body, "恒定那篇没挑最贴近这一句的那一篇")
        self.assertNotIn("窗外的风", body, "恒定那篇挑成了不相干的那一篇")

    def test_the_same_diary_is_not_carried_again_within_the_window(self):
        """窗口内带过的那一篇换下一篇；8 天前那一行出了窗口，可以用回来。"""
        near = _row("near", "2026-03-01", "医院复查那天，走廊很长。",
                    note="旧家日叙事摘要：医院复查那一段")["name"]
        self._house({PLAIN: [
            _row("near", "2026-03-01", "医院复查那天，走廊很长。",
                 note="旧家日叙事摘要：医院复查那一段"),
            _row("far", "2026-10-30", "晚上看了一会儿书，窗外的风很大。"),
        ]})
        question = "翻翻以前的日记，医院复查那次"
        # 3 天前带过最贴近的那篇（NOW 是 2025-01-15T14:05:00+09:00）→ 这一轮换下一篇
        self._ledger([{"at": "2025-01-12T14:05:00+09:00", "name": near, "scene": "unknown",
                       "score": 4, "why": "topic"}])
        _got, cap = self._answer(question)
        body = self._always_body(cap)
        self.assertNotIn("医院复查", body, "窗口内带过的那一篇又被带了一遍")
        self.assertIn("窗外的风", body, "命中窗口后没有换下一篇")
        # 8 天前 → 出了窗口，可以再用
        self._ledger([{"at": "2025-01-07T14:05:00+09:00", "name": near, "scene": "unknown",
                       "score": 4, "why": "topic"}])
        _got, cap = self._answer(question)
        self.assertIn("医院复查", self._always_body(cap), "8 天前那一行还在窗口里？")

    def test_each_round_logs_one_usage_row(self):
        """一轮一行 {at,name,scene,score,why}：去重靠的就是这一本账。"""
        self._house({PLAIN: [_row("a1", "2026-10-22", "十月二十二号，去医院陪他复查。")]})
        self._answer(MISS, scene=brain_model.SCENE_PRIVATE)
        rows = conversation.read_drawer_log()
        self.assertEqual(1, len(rows), "一轮该正好一行账")
        self.assertEqual({"at", "name", "scene", "score", "why"}, set(rows[0]))
        self.assertEqual("drawer_usage.jsonl", conversation.drawer_log_path().name)
        self.assertEqual(brain_model.SCENE_PRIVATE, rows[0]["scene"])
        self.assertTrue(str(rows[0]["name"]).startswith("2026-10-22"), rows[0])
        self.assertIn(rows[0]["why"], (conversation.DRAWER_WHY_TOPIC,
                                       conversation.DRAWER_WHY_DATE))

    def test_the_two_parts_are_counted_separately(self):
        """命中关键词时两段都在，各一个上限、字数各数各的。"""
        self._five()
        conf = conversation.load_assembly()
        got, cap = self._answer(HIT)
        system = "\n\n".join(row["content"] for row in cap[0]["messages"])
        asked = _section(system, conf["sections"]["drawer"]["label"])
        always = self._always_body(cap)
        self.assertTrue(asked, "命中关键词却没有那一节")
        self.assertTrue(always, "命中关键词那一轮没有恒定那篇")
        self.assertLessEqual(len(asked), conf["drawer"]["cap"], "多篇那一节超过 cap")
        self.assertLessEqual(len(self._always_block(cap)), conf["drawer"]["always"]["max_chars"],
                             "恒定那一段超过 max_chars")
        rows = {row["key"]: row for row in got["assembly"]["sections"]}
        self.assertEqual(len(asked), rows["drawer"]["chars"])
        self.assertEqual(len(always), rows["drawer_always"]["chars"])
        self.assertGreaterEqual(rows["drawer"]["count"], 2, "命中了一次都没放进来")
        self.assertLessEqual(rows["drawer"]["count"], conf["drawer"]["max_entries"])

    def test_the_background_piece_obeys_the_same_shelves(self):
        """group / unknown 的恒定那篇也只取普通那格（私密日记不进群聊上下文）。"""
        plain, love = self._two_shelves()
        for scene in (brain_model.SCENE_GROUP, brain_model.SCENE_UNKNOWN):
            self._ledger([])
            _got, cap = self._answer(MISS, scene=scene)
            body = self._always_body(cap)
            self.assertIn(plain, body, "%r 档的恒定那篇没取到普通日记" % (scene,))
            self.assertNotIn(love, body, "%r 档的恒定那篇取了私密那格" % (scene,))

    def test_a_broken_always_block_is_reported_not_defaulted(self):
        """缺 always / max_chars 非正 / dedupe_days 非正 → ValueError 并指名。"""
        def drop(conf):
            del conf["drawer"]["always"]

        def bad_chars(conf):
            conf["drawer"]["always"]["max_chars"] = 0

        def bad_days(conf):
            conf["drawer"]["always"]["dedupe_days"] = 0

        for name, mutate, want in (("缺 always", drop, "drawer.always"),
                                   ("max_chars 非正", bad_chars, "drawer.always.max_chars"),
                                   ("dedupe_days 非正", bad_days, "drawer.always.dedupe_days")):
            conf = conversation.load_assembly()
            mutate(conf)
            with self.assertRaises(ValueError) as caught:
                self._answer(MISS, assembly=conf)
            self.assertIn(want, str(caught.exception), "%s 没当场报" % name)

    # —— 11) 两节标题是她视角、同一轮互斥、恒定篇与关键词无关 ————————

    def test_the_two_labels_say_what_she_wrote_not_how_it_is_carried(self):
        """两节标题的精确值：都是她视角，一个实现词都没有。"""
        conf = conversation.load_assembly()
        self.assertEqual("（你写过的旧日记）", conf["sections"]["drawer"]["label"])
        self.assertEqual("（你写过的一篇日记）", conf["sections"]["drawer_always"]["label"])
        self.assertEqual(conf["sections"]["drawer"]["label"], conf["drawer"]["label"],
                         "drawer.label 与 sections.drawer.label 是同一节，两处不一致")

    def test_the_injected_prompt_has_no_implementation_words(self):
        """注入里不许出现实现词：常带 / 恒定 / 回流 / 背景 / 缓存。两节标题与说明都算。"""
        self._five()
        banned = ("常带", "恒定", "回流", "背景", "缓存")
        for scene in (brain_model.SCENE_PRIVATE, brain_model.SCENE_UNKNOWN):
            self._ledger([])
            _got, cap = self._answer(HIT, scene=scene)
            system = "\n\n".join(row["content"] for row in cap[0]["messages"])
            for word in banned:
                self.assertNotIn(word, system,
                                 "注入里出现了实现词「%s」（%r 档）" % (word, scene))

    def test_the_two_sections_never_use_the_same_diary_in_one_round(self):
        """互斥：恒定那篇选走之后，被问那一段的候选里就不许再有它。"""
        self._five()
        _got, cap = self._answer(HIT)
        always, asked = self._always_body(cap), self._body(cap)
        self.assertTrue(always, "这一轮没有恒定那篇，这条断言就没意义")
        self.assertTrue(asked, "命中关键词却没有被问那一段")
        line = [row for row in always.split("\n") if row.startswith("- ")]
        self.assertEqual(1, len(line), "恒定那一段该正好一篇：%r" % (always,))
        self.assertNotIn(line[0], asked, "同一轮两节用了同一篇")

    def test_five_diaries_still_leave_two_or_three_for_the_asked_part(self):
        """五篇夹具：剔掉恒定那篇还剩四篇，被问那一段照旧 2–3 篇。"""
        self._five()
        got, cap = self._answer(HIT)
        row = self._drawer_row(got)
        self.assertGreaterEqual(row["count"], 2, "剔掉恒定那篇之后被问段不够 2 篇")
        self.assertLessEqual(row["count"], conversation.load_assembly()["drawer"]["max_entries"])
        self.assertTrue(self._always_body(cap), "恒定那篇没了")

    def test_one_diary_only_leaves_the_asked_part_empty(self):
        """只有 1 篇日记：恒定段照旧 1 篇，被问那一段**空着**——不为凑数把同一篇放回来。"""
        self._house({PLAIN: [_row("only", "2026-10-22", "十月二十二号，去医院陪他复查。")]})
        conf = conversation.load_assembly()
        got, cap = self._answer(HIT)
        system = "\n\n".join(row["content"] for row in cap[0]["messages"])
        self.assertNotIn("[%s]" % conf["sections"]["drawer"]["label"], system,
                         "同一篇在两节里都放了")
        self.assertEqual([], [row for row in got["assembly"]["sections"] if row["key"] == "drawer"],
                         "剔完没有候选，被问那一段该空着（回包里也不留这一格）")
        rows = [row for row in got["assembly"]["sections"] if row["key"] == "drawer_always"]
        self.assertEqual(1, len(rows), "恒定那一段没了")
        self.assertEqual(1, rows[0]["count"])
        self.assertTrue(self._always_body(cap), "恒定那一段该照旧有 1 篇")

    def test_the_background_piece_steps_past_a_diary_it_cannot_shorten(self):
        """回归（实盘 0/10 的病根）：排名第一那篇的第一句就比余地长，整段不许因此变空。

        实盘上没命中关键词时排名只看日期，最前面那几篇的第一句常常 100+ 字，压不进
        always.max_chars 就截不出整句——老代码在那一篇上就地 return，于是每轮都空。
        """
        self._house({PLAIN: [
            _row("long", "2026-11-02", "那一天" + "水" * 120 + "。后面还有话。"),
            _row("short", "2026-10-30", "十月三十号，晚上看了一会儿书。"),
        ]})
        got, cap = self._answer(MISS)          # 没关键词：排名第一就是最新的那篇（long）
        body = self._always_body(cap)
        self.assertTrue(body, "排名第一那篇截不出整句，整段就空了（0/10 的老病）")
        self.assertIn("晚上看了一会儿书", body, "没有换到下一篇")
        rows = [row for row in got["assembly"]["sections"] if row["key"] == "drawer_always"]
        self.assertEqual(1, rows[0]["count"])

    def test_the_background_piece_is_computed_even_when_no_keyword_hits(self):
        """恒定篇与关键词无关：一个关键词都不命中的轮次，恒定那一段照旧算、照旧写一行账。"""
        self._five()
        self._ledger([])
        got, cap = self._answer(MISS, scene=brain_model.SCENE_PRIVATE)
        self.assertTrue(self._always_body(cap), "没命中关键词那一轮恒定那篇没了")
        self.assertEqual(1, [row for row in got["assembly"]["sections"]
                             if row["key"] == "drawer_always"][0]["count"])
        self.assertEqual(1, len(conversation.read_drawer_log()), "恒定那篇这一轮没落账")


if __name__ == "__main__":
    unittest.main()
