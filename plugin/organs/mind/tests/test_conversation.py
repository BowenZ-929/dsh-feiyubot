"""装配入口的验收：顺序与上限来自数据、提示词装对了、心只给词、冷热两跑、退档不回想、八股闸。

判据（每条都对应交付里的一句话）：
1. **改数据就改装配**：把 assembly.json 换成另一份（顺序倒过来、把魂那一段的上限压到 10 字），
   装配与提示词跟着变——顺序、cap、段落标题都不是代码里的常数。
2. **提示词真的装进去了**：魂六件的六个标题、记忆正文与它的 evidence_key、嘴的口径行都在。
3. **心只给词**：返回体 heart 里没有任何一个数、也没有任何一位数字。
4. **冷热两跑**：同一句话，在「热」与「冷」两份心数据下，回复预算不同、心的语气档（voice.tone）不同。
   心数据全部用器官自己的写入函数造（mood.apply_event）；本轮能源那一格动不了档，故 tone 取 voice.tone。
5. **退档不回想**：脑里那条先命中，retire 之后同一句话不再被引用。
6. **八股闸**：换一份含命中条款的嘴数据，模型回一句命中话——决策 dropped、reply 空、chunks 空。
7. **顺序来自数据**：默认 order 里 recent 在 memory 之前；给一份内存里的 assembly，
   装配立刻跟着变（评测脚本靠这一条关掉某几段做空基线）。
8. **连续性**：15 轮历史 → 提示词里出现「更早还有 3 轮没有加载」、continuity.omitted == 3；
   5 轮 → 没这句、notice 是空串；一条历史都没有 → recent 段写「还没有更早的对话」。
9. **温度两档**：recalled 非空且预算 ≤ 阈值 → 0.5/factual，否则 0.7/default；返回体
   llm.temperature 与假 transport 收到的 payload 里那个温度逐字相同（两个数都从数据读）。
10. **锚句**：没配 / 卡不在 → 提示词含「未配锚句」且 anchors.configured is False；
    写了一张家·仓库的卡 → 提示词含锚句正文、configured True、chars 是真字数。
11. **落账**：一轮之后 recent_turns.jsonl 多一条，said / reply 与返回体一致；
    被闸弃掉的那一轮 reply 记空串、人那句仍然存。

全部注入假 transport：一个字节都不发真网络。心 / 嘴 / 装配的临时根都经环境变量切走，
仓库里的真数据一个字节不写。
"""
from __future__ import annotations

import hashlib
import math
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from api import api
from organs.brain import BrainStore
from organs.heart import energy
from organs.heart import model as heart_model
from organs.heart import mood, sleep
from organs.home import anchors as home_anchors
from organs.home import cards
from organs.mind import conversation, llm, recent
from organs.soul import load as load_soul

PLUGIN = Path(__file__).resolve().parents[3]
SOUL_FIXTURES = PLUGIN / "organs" / "soul" / "fixtures"
MOUTH_DATA = PLUGIN / "organs" / "mouth" / "data"
HEART_DATA = PLUGIN / "organs" / "heart" / "data"
HEART_FACTORY = ("baseline-card.json", "engage_weights.json")   # 出厂件：卡 + 意图权重表

CONFIG = {"provider": "fake", "model": "fake-model", "base_url": "http://example.invalid/v1",
          "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 1000, "retries": 0,
          "temperature": 0.0, "max_tokens": 200}
ENV = {"QQAGENT_LLM_KEY": "test-key"}

SOUL_HEADINGS = ("【自称】", "【形象】", "【性格底色】", "【怕什么】", "【待人姿态】", "【红线】")
QUESTION = "你还记得医院那回事吗？"
MEMORY_TEXT = "我上次去医院是陪我妈复查，我对花生过敏"
SENTENCE = "今天挺好的。你问医院那回事，我记得的，是陪你妈去复查对吧。"

BASE = datetime(2025, 1, 15, 14, 0, 0, tzinfo=datetime.now().astimezone().tzinfo)


def _at(minutes: float = 0.0, base: datetime = BASE) -> str:
    return heart_model.iso(base + timedelta(minutes=minutes))


def _transport(reply: str, captured: list = None, status: int = 200):
    """假 transport：拿一句话当作模型应答，顺手把请求体记下来（读 payload 用）。"""
    def send(url, headers, payload, timeout):
        if captured is not None:
            captured.append(payload)
        body = {"choices": [{"message": {"content": reply}}]}
        return status, json.dumps(body, ensure_ascii=False).encode("utf-8")
    return send


class ConversationAnswerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patches = mock.patch.dict(os.environ, {
            "QQAGENT_HOME": str(self.tmp / "home"),          # 装配说明书的实盘根：空着，走出厂
            "HEART_ROOT": str(self.tmp / "heart"),           # 心的状态文件：临时根
            "MOUTH_TRACE_DIR": str(self.tmp / "trace"),      # 弃言账本：临时根
        })
        patches.start()
        self.addCleanup(patches.stop)
        self.store = self._soul()
        self.brain = BrainStore(self.tmp / "brain")

    def _soul(self):
        target = self.tmp / "soul"
        shutil.copytree(str(SOUL_FIXTURES / "valid"), str(target))
        return load_soul(target)

    def _answer(self, text: str = QUESTION, reply: str = SENTENCE, *, who: str = "主人",
                now: str = _at(5), captured: list = None, transport=None, **extra):
        send = transport if transport is not None else _transport(reply, captured)
        return conversation.answer(text, who, store=self.store, brain=self.brain,
                                   now=now, transport=send, config=CONFIG, env=ENV, **extra)

    # —— 1) 装配来自数据 ————————————————————————————

    def test_order_and_caps_come_from_the_data_file(self):
        """换一份 assembly.json：顺序倒过来、魂那段上限压到 10 字，装配与提示词跟着变。"""
        capture = []
        live = self.tmp / "home" / "mind"
        live.mkdir(parents=True)
        data = conversation.load_assembly()             # 出厂那份，照着改
        data["order"] = ["mouth", "heart", "memory", "soul"]
        data["sections"]["soul"]["cap"] = 10
        data["reply"]["instruction"] = "把话说短。"
        (live / "assembly.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

        got = self._answer(captured=capture)
        self.assertEqual(["mouth", "heart", "memory", "soul"], got["assembly"]["order"])
        self.assertEqual("mouth", got["assembly"]["sections"][0]["key"])
        soul = [row for row in got["assembly"]["sections"] if row["key"] == "soul"][0]
        self.assertEqual(10, soul["cap"])
        self.assertEqual(10, soul["chars"], "上限改了，装配没跟着截断")
        system = capture[0]["messages"][0]["content"]
        self.assertIn("把话说短。", system)
        self.assertLess(system.index("[嘴语气]"), system.index("[魂六件]"), "提示词的顺序没跟着改")

    # —— 2) 四段都装进去了 ————————————————————————

    def test_prompt_carries_soul_headings_memory_evidence_and_tone_line(self):
        entry = self.brain.ingest(who="主人", text=MEMORY_TEXT, ts=_at(0))
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        self.assertEqual(QUESTION, capture[0]["messages"][1]["content"])
        self.assertEqual(CONFIG["model"], capture[0]["model"])
        for heading in SOUL_HEADINGS:
            self.assertIn(heading, system, "魂六件缺标题：%s" % heading)
        self.assertIn(MEMORY_TEXT, system, "记忆正文没进提示词")
        self.assertIn(entry["entry"]["evidence_key"], system, "证据指针没进提示词")
        self.assertIn("｜自称", system, "嘴的口径行没进提示词")
        self.assertIn("档｜对「", system, "语气档那行没进提示词")
        self.assertEqual("魂六件", conversation.load_assembly()["sections"]["soul"]["label"])
        self.assertIn("魂六件", system, "段落标题没进提示词")

    # —— 3) 心只给词 ————————————————————————————

    def test_heart_gives_words_and_no_numbers(self):
        got = self._answer()
        heart = got["heart"]
        self.assertEqual({"tier", "direction"}, set(heart["mood"]))
        self.assertEqual({"tone", "direction", "bans"}, set(heart["voice"]))
        self.assertEqual({"state", "reason"}, set(heart["engage"]))
        self.assertTrue(heart["axes"], "六轴一个词都没有")
        for axis, word in heart["axes"].items():
            self.assertIsInstance(word, str, "%s 的档位不是词：%r" % (axis, word))
        for node in self._walk(heart):
            self.assertNotIsInstance(node, (int, float), "心里混进了数：%r" % (node,))
        text = json.dumps(heart, ensure_ascii=False)
        self.assertEqual([], [ch for ch in text if ch.isdigit()], "心的返回体里有数字：%s" % text)

    def _walk(self, node):
        if isinstance(node, dict):
            for value in node.values():
                yield from self._walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                yield from self._walk(value)
        else:
            yield node

    # —— 4) 冷热两跑 ————————————————————————————

    def test_hot_and_cold_heart_data_change_budget_and_tone(self):
        """同一句话、两份心数据：回复预算不同，心的语气档不同（档位词，不是数）。"""
        hot = self._heart_root("hot")
        self._seed(hot, "praise", 15, gap=10.1)          # 一路夸：心境爬到「偏热」
        cold = self._heart_root("cold")
        last = self._seed(cold, "cold", 4, gap=0.5)      # 一路冷：心境掉到「冰点」

        warm = self._answer(now=_at(160), heart_root=hot)
        chilly = self._answer(now=last, heart_root=cold)

        self.assertNotEqual(warm["assembly"]["reply_budget"]["chars"],
                            chilly["assembly"]["reply_budget"]["chars"],
                            "两份心数据下回复预算一样")
        self.assertNotEqual(warm["heart"]["voice"]["tone"], chilly["heart"]["voice"]["tone"],
                            "两份心数据下心的语气档一样")
        self.assertNotEqual(warm["heart"]["mood"]["tier"], chilly["heart"]["mood"]["tier"])
        self.assertNotEqual(warm["assembly"]["reply_budget"]["tier"],
                            chilly["assembly"]["reply_budget"]["tier"])
        # 预算与档位不许写死在这里：从 assembly.json 现读，档位与心的档位词同源。
        budgets = conversation.load_assembly()["reply"]["budget_by_tier"]
        for got in (warm, chilly):
            tier = got["assembly"]["reply_budget"]["tier"]
            self.assertEqual(got["heart"]["voice"]["tone"], tier, "voice.tone 与预算档不同源")
            self.assertEqual(budgets[tier], got["assembly"]["reply_budget"]["chars"])

    def test_engage_matches_the_heart_endpoint_on_the_same_data(self):
        """同一份心数据 + 同一个 who/now：heart.engage 与 GET /heart/engage 逐字相同。

        GET /heart/engage 的返回就是 engage.decide 的原样输出（api/heart.py：payload = dict(decide(...))），
        所以对拍端点等于拿 decide 对拍——测试里不重写一遍门的接线，免得测试变成第二份判据。

        这份心数据特意造了三夜又晚又短的睡眠账、并先结算过一次精力：睡觉窗口与回血封顶一旦
        没传给 energy，两边精力读数就分叉；意图排序一旦没送进第 5 道软门，理由码就会从
        intent-below-threshold 退回「没有理由」——两种漏法这条都红。

        先对拍、后说话：这一轮落脑会改人名录读数（熟稔 / 今日条数），先问面板才与判决同输入。
        """
        root = self._heart_root("engage")
        for day in range(3):
            sleep.record("asleep", root, now=_at(14 * 60 + day * 24 * 60))
            sleep.record("awake", root, now=_at(16.5 * 60 + day * 24 * 60))
        energy.read(root, _at(0))
        when = _at(25 * 60)                      # 次日 15:00：白天，硬门不掺和，软门自己判
        self.brain.interact("主人", ts=when)     # 熟稔过线：让第 5 道软门成为唯一理由
        query = urlencode({"now": when, "who": "主人", "root": str(root)})
        status, readout = api.handle("GET", "/heart/engage?" + query, None, self.store,
                                     brain=self.brain)
        self.assertEqual(200, status)
        got = self._answer(now=when, heart_root=root)
        self.assertEqual(readout["state"], got["heart"]["engage"]["state"],
                         "同一个人同一刻：说话这边与面板判的态不一样")
        self.assertEqual(readout["reason"], got["heart"]["engage"]["reason"],
                         "态一样理由不一样：门喂的入参分叉了")

    def test_normal_budget_is_wide_enough_to_cut_two_bubbles(self):
        """平常档预算必须大于嘴的 split.max_chars：不然那一档永远只切出一泡（交叉不变式）。"""
        mouth_policy = json.loads((MOUTH_DATA / "policy.json").read_text(encoding="utf-8"))
        max_chars = int(mouth_policy["split"]["max_chars"])
        reply_conf = conversation.load_assembly()["reply"]
        normal = reply_conf["budget_by_tier"]["平常"]
        self.assertGreater(normal, max_chars,
                           "平常档预算 %d 不大于嘴的 split.max_chars %d：这一档永远切不出第二泡"
                           % (normal, max_chars))
        self.assertEqual(normal, reply_conf["default"], "default 要跟着平常走")

        # 一段不超预算、但超过嘴的切分线的回复：真链路里该切成 2–3 泡。
        sentence = "你问医院那回事，我记得的，是陪你妈去复查对吧。花生过敏我也记着，别再吃带花生的点心了。"
        reply_text = sentence
        while len(reply_text) <= max_chars:
            reply_text += sentence
        self.assertLessEqual(len(reply_text), normal, "这段假回复超了平常档预算")
        got = self._answer(reply=reply_text)
        tier = got["assembly"]["reply_budget"]["tier"]
        self.assertGreater(reply_conf["budget_by_tier"][tier], max_chars)
        self.assertTrue(2 <= len(got["chunks"]) <= 3,
                        "平常档的一段回复只切出 %d 泡：%r" % (len(got["chunks"]), got["chunks"]))
        self.assertEqual(0.0, got["chunks"][0]["gap_before"], "首泡该立即发")
        for chunk in got["chunks"]:
            self.assertIsInstance(chunk["send_at"], float)
            self.assertIsInstance(chunk["gap_before"], float)
        self.assertTrue(any(chunk["gap_before"] > 0 for chunk in got["chunks"][1:]),
                        "第二泡之后该有可见的间隔")

    def _heart_root(self, name: str) -> Path:
        """一份心数据目录：出厂件（卡 + 意图权重表）+ 这个用例自己的状态文件。

        卡不在，合成会降级成只要 mood 一块；权重表不在，意图排序回内置默认并 WARN——
        两种都是「测试环境与实盘不一样」，所以照出厂目录补齐。
        """
        root = self.tmp / ("heart_" + name)
        root.mkdir()
        for name_of_file in HEART_FACTORY:
            shutil.copy(str(HEART_DATA / name_of_file), str(root / name_of_file))
        return root

    def _seed(self, root: Path, kind: str, count: int, *, gap: float) -> str:
        """用器官自己的写入函数造心状态（mood.apply_event），不手搓状态文件的 schema。"""
        last = ""
        for index in range(count):
            last = _at(index * gap)
            got = mood.apply_event(kind, root, now=last, who="人%d" % index)
            self.assertTrue(got["ok"], "第 %d 条心事件被挡了：%r" % (index, got))
        return last

    # —— 5) 退档不回想 ————————————————————————————

    def test_retired_memory_is_not_recalled_again(self):
        entry = self.brain.ingest(who="主人", text=MEMORY_TEXT, ts=_at(0))
        entry_id = entry["entry"]["id"]
        first = self._answer()
        self.assertEqual([entry_id], [row["id"] for row in first["recalled"]],
                         "第一次没想起那条记忆")
        self.brain.retire(entry_id, ts=_at(6))
        second = self._answer(now=_at(7))
        self.assertNotIn(entry_id, [row["id"] for row in second["recalled"]],
                         "退了档还在被引用：%r" % (second["recalled"],))

    # —— 6) 八股闸 ————————————————————————————

    def test_formula_clause_drops_the_reply(self):
        root = self.tmp / "mouth"
        shutil.copytree(str(MOUTH_DATA), str(root))
        clauses = json.loads((root / "clauses.json").read_text(encoding="utf-8"))
        clauses["clauses"].insert(0, {
            "id": "ai-self-reference",
            "kind": "反例",
            "pattern": "作为一个\\s*(AI|人工智能)\\s*语言模型",
            "reason": "这是标准模型免责声明：她不是那个东西，不该冒出来",
        })
        (root / "clauses.json").write_text(json.dumps(clauses, ensure_ascii=False),
                                           encoding="utf-8")
        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(root)}):
            got = self._answer(reply="作为一个 AI 语言模型，我不能这样说。")

        self.assertEqual("dropped", got["gate"]["decision"])
        self.assertEqual("", got["reply"], "被弃的话还是被当成功说出去了")
        self.assertEqual([], got["chunks"], "被弃的话还留着分泡")
        self.assertEqual("ai-self-reference", got["gate"]["hit"]["clause_id"])
        self.assertIn("命中", got["gate"]["hit"]["reason"])
        self.assertGreaterEqual(got["gate"]["drops"]["today"], 1, "弃言没进账本")
        self.assertEqual(1, len(got["memory"]["wrote"]), "被弃的话不该落进脑")

    # —— 落脑 ————————————————————————————————————

    def test_this_round_lands_in_memory_with_evidence_keys(self):
        got = self._answer()
        rows = self.brain.today_entries(_at(30))
        texts = [row.get("text") for row in rows]
        self.assertIn(QUESTION, texts, "人的原话没落进脑")
        self.assertIn(SENTENCE, texts, "她那句话没落进脑")
        wrote = got["memory"]["wrote"]
        self.assertEqual(2, len(wrote), "这一轮该落两条：人的原话 + 她那句")
        by_who = dict((row["who"], row["id"]) for row in wrote)
        self.assertEqual(QUESTION and "主人", [row["who"] for row in rows][-2])
        for entry in rows:
            self.assertEqual("亲历", entry.get("source_type"))
            self.assertTrue(entry.get("evidence_key"), "证据指针是空的")
            self.assertEqual(entry["evidence_key"], entry["id"])
        self.assertEqual(rows[-2]["evidence_key"], by_who["主人"])
        self.assertEqual(rows[-1]["evidence_key"],
                         by_who[self.store.snapshot().identity.display])
        self.assertEqual([], [row["id"] for row in got["recalled"] if row["id"] in by_who.values()],
                         "这一轮刚写进去的话不该被这一轮的回想引用（先回想、最后写）")

    # —— 7) 顺序来自数据 ————————————————————————

    def test_recent_sits_before_memory_in_the_default_order(self):
        """默认 order 是魂 → 锚句 → 最近几轮 → 脑装包 → 心 → 嘴；装配结果逐项照抄它。"""
        order = conversation.load_assembly()["order"]
        self.assertIn("recent", order, "默认 order 里没有 recent 这一段")
        self.assertLess(order.index("recent"), order.index("memory"),
                        "最近几轮跑到了脑装包后面：%r" % (order,))
        got = self._answer()
        self.assertEqual(order, got["assembly"]["order"])
        self.assertEqual(order, [row["key"] for row in got["assembly"]["sections"]])

    def test_in_memory_assembly_replaces_the_file(self):
        """给一份内存里的装配说明就用它：顺序与 cap 立刻跟着变，一行盘都不读。"""
        conf = conversation.load_assembly()
        conf["order"] = ["mouth", "recent"]
        conf["sections"]["recent"]["cap"] = 12
        root = self._history(3, "recent_cap")
        capture = []
        got = self._answer(assembly=conf, recent_root=root, captured=capture)
        self.assertEqual(["mouth", "recent"], got["assembly"]["order"])
        recent_row = [row for row in got["assembly"]["sections"] if row["key"] == "recent"][0]
        self.assertEqual(12, recent_row["cap"])
        self.assertEqual(12, recent_row["chars"], "cap 改了，recent 段没跟着截")
        system = capture[0]["messages"][0]["content"]
        self.assertNotIn("[魂六件]", system, "内存里的装配说明没有真的替掉文件")

    # —— 8) 连续性 ————————————————————————————

    def _history(self, count: int, name: str) -> Path:
        """造 count 轮历史：直接写 recent 那份账（一轮一问一答一条）。"""
        root = self.tmp / name
        for index in range(count):
            recent.append({"ts": _at(index), "who": "主人",
                           "said": "第%d句" % index, "reply": "第%d答" % index}, root=root)
        return root

    def test_fifteen_rounds_leave_three_omitted_and_say_so(self):
        root = self._history(15, "recent15")
        capture = []
        got = self._answer(captured=capture, recent_root=root)
        system = capture[0]["messages"][0]["content"]
        template = conversation.load_assembly()["recent"]["notice"]
        said = template.replace(recent.NOTE_SLOT, "3")
        self.assertIn(said, system, "omitted > 0 时提示词里没有那句「更早还有 3 轮」")
        self.assertEqual(3, got["continuity"]["omitted"])
        self.assertEqual(12, got["continuity"]["turns_loaded"])
        self.assertEqual(said, got["continuity"]["notice"])
        self.assertLess(system.index(said), system.index("[脑装包]"), "那句不在 recent 段末尾")
        self.assertNotIn("第0句", system, "被省掉的那几轮还在提示词里")
        self.assertIn("第14句", system, "最近一轮没进提示词")

    def test_five_rounds_leave_no_notice_at_all(self):
        root = self._history(5, "recent5")
        capture = []
        got = self._answer(captured=capture, recent_root=root)
        system = capture[0]["messages"][0]["content"]
        self.assertNotIn("更早还有", system, "一轮都没省，却编了一句「更早还有」")
        self.assertEqual(0, got["continuity"]["omitted"])
        self.assertEqual("", got["continuity"]["notice"], "omitted 为 0 时 notice 该是空串")
        self.assertEqual(5, got["continuity"]["turns_loaded"])
        for index in range(5):
            self.assertIn("第%d句" % index, system)

    def test_notice_survives_the_section_cap(self):
        """段上限 2400 字装不下 12 个长轮：段尾那句必须还在（先给 notice 留位置，再截正文）。"""
        root = self.tmp / "recent_long"
        for index in range(15):
            recent.append({"ts": _at(index), "who": "主人",
                           "said": "前" * 200, "reply": "后" * 200}, root=root)
        capture = []
        got = self._answer(captured=capture, recent_root=root)
        system = capture[0]["messages"][0]["content"]
        said = conversation.load_assembly()["recent"]["notice"].replace(recent.NOTE_SLOT, "3")
        self.assertIn(said, system, "正文顶到 cap 时，段尾那句被一起锯掉了")
        row = [item for item in got["assembly"]["sections"] if item["key"] == "recent"][0]
        self.assertEqual(row["cap"], row["chars"], "12 个长轮的正文本该顶到 cap")
        self.assertLess(system.index(said), system.index("[脑装包]"), "那句跑出了 recent 段")

    def test_no_history_writes_the_empty_line_instead_of_nothing(self):
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        self.assertIn(conversation.NO_TURNS, system, "一条历史都没有时 recent 段是空白")
        self.assertEqual(0, got["continuity"]["turns_loaded"])
        self.assertEqual("", got["continuity"]["notice"])

    # —— 9) 温度两档 ————————————————————————————

    def test_temperature_defaults_when_nothing_is_recalled(self):
        capture = []
        got = self._answer(captured=capture)
        policy = llm.load_config()
        self.assertEqual([], got["recalled"], "这一轮本来没东西可回想")
        self.assertEqual("default", got["llm"]["temperature_tier"])
        self.assertEqual(policy["temperature"], got["llm"]["temperature"])
        self.assertEqual(got["llm"]["temperature"], capture[0]["temperature"],
                         "返回体里的温度与真发出去的那个不一致")
        self.assertEqual(CONFIG["model"], capture[0]["model"])
        self.assertEqual(capture[0]["model"], got["llm"]["model"],
                         "返回体的模型名与真发出去的不是同一个")

    def test_factual_tier_when_recalled_and_the_budget_is_tight(self):
        """回想非空 + 预算 ≤ 阈值（冷心境那一档是 40）→ 降成事实档 0.5。"""
        self.brain.ingest(who="主人", text=MEMORY_TEXT, ts=_at(0))
        cold = self._heart_root("temp_cold")
        last = self._seed(cold, "cold", 4, gap=0.5)
        capture = []
        got = self._answer(now=last, heart_root=cold, captured=capture)
        policy = llm.load_config()
        trigger = policy["temperature_tiers"]["trigger"]
        budget = got["assembly"]["reply_budget"]["chars"]
        self.assertTrue(got["recalled"], "这一轮该想起医院那回事")
        self.assertLessEqual(budget, trigger["reply_budget_at_most"])
        self.assertEqual("factual", got["llm"]["temperature_tier"])
        self.assertEqual(policy["temperature_tiers"]["factual"], got["llm"]["temperature"])
        self.assertEqual(got["llm"]["temperature"], capture[0]["temperature"])

    def test_both_tiers_follow_the_data_trigger(self):
        """两跑对拍：档位名必须与数据里那两条判据算出来的一致，不许另有第二套判据。"""
        self.brain.ingest(who="主人", text=MEMORY_TEXT, ts=_at(0))
        trigger = llm.load_config()["temperature_tiers"]["trigger"]
        cold = self._heart_root("temp_rule_cold")
        last = self._seed(cold, "cold", 4, gap=0.5)
        for got in (self._answer(now=_at(5)), self._answer(now=last, heart_root=cold)):
            budget = got["assembly"]["reply_budget"]["chars"]
            hits = bool(got["recalled"]) and budget <= trigger["reply_budget_at_most"]
            self.assertEqual("factual" if hits else "default", got["llm"]["temperature_tier"],
                             "预算 %d / recalled %r 时的档位与数据里的判据不符"
                             % (budget, bool(got["recalled"])))

    # —— 10) 锚句（家·仓库的五张台词卡） ————————————————

    FIVE = (
        {"scene": "被夸", "say": "哼，才知道啊。", "body": "尾巴翘起来晃了两下"},
        {"scene": "被关心", "say": "行了行了，我又不是玻璃做的。", "body": "别开头、脸微微发红"},
        {"scene": "不想聊", "say": "这个啊……以后再说吧。", "body": ""},
        {"scene": "认错", "say": "……是我记岔了。", "body": "低头、尾巴垂下去不敢动"},
        {"scene": "你要走", "say": "你忙你的，我在这儿。", "body": "尾巴慢慢摆一下，乖巧的笑了笑"},
    )

    def _anchors_home(self, rows=None):
        """造一份只有台词库的家：HOME_ROOT 指到临时根，写卡走家自己的公开写口。"""
        home = self.tmp / ("house-%d" % len(os.listdir(self.tmp)))
        (home / "仓库").mkdir(parents=True, exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME_ROOT": str(home)})
        patch.start()
        self.addCleanup(patch.stop)
        if rows:
            home_anchors.append_anchors(root=home, role="human", by="人", anchors=list(rows))
        return home

    def test_missing_anchor_is_said_out_loud(self):
        self._anchors_home()
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        self.assertIn("[%s]" % conversation.load_assembly()["sections"]["anchors"]["label"], system)
        self.assertIn(conversation.ANCHORS_MISSING, system, "锚句没配却什么都没写")
        self.assertEqual({"configured": False, "note": conversation.ANCHORS_MISSING},
                         got["anchors"])

    def test_empty_anchors_file_is_not_configured(self):
        home = self._anchors_home()
        (home / "仓库" / "anchors.jsonl").write_text("", encoding="utf-8")
        self.assertFalse(self._answer()["anchors"]["configured"], "空文件也算配了？")

    def test_no_anchors_section_is_also_not_silently_skipped(self):
        conf = conversation.load_assembly()
        conf["anchors"] = None
        capture = []
        got = self._answer(assembly=conf, captured=capture)
        self.assertIn(conversation.ANCHORS_MISSING, capture[0]["messages"][0]["content"])
        self.assertFalse(got["anchors"]["configured"])

    def test_five_halves_go_in_and_no_full_line_no_body(self):
        """五条半句全注入、不按场合挑；整句与 body 一个字都不许进提示词。"""
        self._anchors_home(list(self.FIVE))
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        conf = conversation.load_assembly()["anchors"]
        self.assertTrue(got["anchors"]["configured"])
        self.assertEqual(5, got["anchors"]["count"], "五条全给，不按场合挑")
        self.assertIn(conf["instruction"], system, "行为规矩没进提示词")
        for want in self.FIVE:
            cut = int(math.ceil(len(want["say"]) * conf["inject"]["fraction"]))
            half = want["say"][:cut] + conf["inject"]["suffix"]
            self.assertIn(half, system, "半句没装上：%s" % want["scene"])
            self.assertNotIn(want["say"], system, "整句进了提示词：%s" % want["scene"])
            if want["body"]:
                self.assertNotIn(want["body"], system,
                                 "body 进了提示词（那是在等着被照抄）：%s" % want["scene"])
        items = {item["scene"]: item for item in got["anchors"]["items"]}
        self.assertEqual(len(self.FIVE), len(items))
        for want in self.FIVE:
            self.assertEqual(want["say"], items[want["scene"]]["say"], "全文要回给人核对")
            self.assertEqual(want["body"], items[want["scene"]]["body"], "body 要回给人核对")

    def test_half_line_keeps_its_shape(self):
        """人给的样子：「哼，才知道啊。」注入成「哼，才知……」。"""
        self._anchors_home([{"scene": "被夸", "say": "哼，才知道啊。", "body": "尾巴翘起来晃了两下"}])
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        self.assertEqual("哼，才知……", got["anchors"]["items"][0]["injected"])
        self.assertIn("哼，才知……", system)
        self.assertNotIn("哼，才知道啊。", system)
        self.assertNotIn("尾巴翘起来晃了两下", system)

    # —— 11) 落账 ————————————————————————————

    def test_this_round_lands_in_recent_verbatim(self):
        got = self._answer()
        loaded = recent.load(turns=12)          # 默认落点：<QQAGENT_HOME>/mind/recent_turns.jsonl
        self.assertEqual(1, loaded["total"], "这一轮没落进最近几轮那份账")
        row = loaded["turns"][0]
        self.assertEqual("主人", row["who"])
        self.assertEqual(QUESTION, row["said"])
        self.assertEqual(got["reply"], row["reply"], "落下去的不是过嘴之后那一整段")
        self.assertEqual({"said": False, "reply": False}, row["truncated"])
        self.assertEqual(0, got["continuity"]["turns_loaded"],
                         "这一轮自己不算进「加载了几轮」：turns_loaded 说的是装进提示词的历史")

    def test_dropped_round_still_keeps_what_the_human_said(self):
        """被闸弃掉的那一轮：reply 记空串，人那句仍然要存——她得记得人说过什么。"""
        root = self.tmp / "mouth_recent"
        shutil.copytree(str(MOUTH_DATA), str(root))
        clauses = json.loads((root / "clauses.json").read_text(encoding="utf-8"))
        clauses["clauses"].insert(0, {
            "id": "ai-self-reference",
            "kind": "反例",
            "pattern": "作为一个\\s*(AI|人工智能)\\s*语言模型",
            "reason": "这是标准模型免责声明：她不是那个东西，不该冒出来",
        })
        (root / "clauses.json").write_text(json.dumps(clauses, ensure_ascii=False),
                                           encoding="utf-8")
        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(root)}):
            got = self._answer(reply="作为一个 AI 语言模型，我不能这样说。")
        self.assertEqual("dropped", got["gate"]["decision"])
        row = recent.load(turns=12)["turns"][-1]
        self.assertEqual(QUESTION, row["said"], "被弃的那一轮，人那句也没存")
        self.assertEqual("", row["reply"], "被弃的话没说出口，不该当成她说的原文存下来")


if __name__ == "__main__":
    unittest.main()
