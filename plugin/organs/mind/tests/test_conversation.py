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
12. **口癖护栏**：最近 50 轮老用锚句开场（命中率 > 30% 且轮数够 min_rounds）→ 降档只注入
    2 条半句，其余在 anchors.skipped 里点名；命中率低就五条全给。guard 的八/九键对
    页面冻结、值全来自 assembly.json 的 anchors.guard（改数据 → 读数跟着变）。
    这一轮说出口之后账本多一行（hit 与回话开头一致）；被闸弃掉的轮账本不增行。
13. **近轮禁用**：另起一节，只抽她那一侧最近 N 轮的动作行与开场；重复三次只列一条并标
    「重复 3 次」；人的原话一个字都不进这一节；最近几轮的原文段一个字都不删；
    窗口外的更早轮不算禁令；返回体 banned{rounds, action_lines, openings} 与历史对得上。
14. **动作给池子**：action 段的规矩与部位池逐项来自 assembly.json（改数据 → 提示词跟着变），
    代码里一个部位词都没有；banned / action 两块缺键或类型不对一律 ValueError。
15. **对账（补数据也补声明）**：脑里今日「他说」记了、最近几轮这份账上却没落的轮，装配前
    按 msg_id 回填成 fidelity="reconciled"，提示词里那两行带「（补记）」、她那句写
    「（没有记到她那句）」（不替她编）；返回体 continuity.reconciled 说清补了几条、
    rounds 逐轮给 {ts, fidelity, msg_id}；对不上打一行「缺 N 轮」的 warning。
    四条都在账上就一条都不标、一行日志都不打；回填幂等（同一个 msg_id 只补一次）。

全部注入假 transport：一个字节都不发真网络。心 / 嘴 / 装配的临时根都经环境变量切走，
仓库里的真数据一个字节不写。
"""
from __future__ import annotations

import hashlib
import logging
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
from organs.brain import model as brain_model
from organs.heart import energy
from organs.heart import model as heart_model
from organs.heart import mood, sleep
from organs.home import anchors as home_anchors
from organs.home import cards
from organs.mind import conversation, llm, recent, repeat, usage
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


def _section(text: str, label: str) -> str:
    """提示词里某一节的正文：从「[标题]」到下一个空行分节为止（段与段之间是空行）。"""
    head = text.index("[%s]" % label) + len(label) + 2
    tail = text.find("\n\n[", head)
    return text[head:tail] if tail >= 0 else text[head:]


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

        # 一段不超预算、但超过嘴的切分线的回复：真链路里该切成多泡（每泡 1–2 句；
        # max_parts=12 只是防炸上限）。旧的「2–3 泡」口径已退役。
        sentence = "你问医院那回事，我记得的，是陪你妈去复查对吧。花生过敏我也记着，别再吃带花生的点心了。"
        reply_text = sentence
        while len(reply_text) <= max_chars:
            reply_text += sentence
        self.assertLessEqual(len(reply_text), normal, "这段假回复超了平常档预算")
        got = self._answer(reply=reply_text)
        tier = got["assembly"]["reply_budget"]["tier"]
        self.assertGreater(reply_conf["budget_by_tier"][tier], max_chars)
        max_parts = int(mouth_policy["split"]["max_parts"])
        self.assertTrue(2 <= len(got["chunks"]) <= max_parts,
                        "平常档的一段回复切出 %d 泡（嘴现在的上限是 %d）：%r"
                        % (len(got["chunks"]), max_parts, got["chunks"]))
        for chunk in got["chunks"]:
            self.assertLessEqual(len(chunk["text"]), max_chars,
                                 "有一泡超过嘴的 split.max_chars：%r" % (chunk,))
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

    # —— 15) 对账：脑里记了、这份账上没落 ————————————

    SPOKEN = ["我今早去医院拿片子", "回来的路上碰到老陈", "他问我妈复查得怎么样",
              "我说下周三再去一趟"]

    def _rig(self, name: str, missed: bool, legacy: bool = False):
        """造一个脑（今日「他说」四条）与最近几轮那份账（前三条亲历落好，msg_id 指回脑）。

        missed=True：第四条账上没有（进程重启 / 写盘失败那一态）——装配该把它补回来；
        missed=False：四条都在账上——一条都不许标。
        legacy=True：账上的行按**升级前**的形状写（没有 msg_id 这一栏），
        它们只是没有凭据、不是丢了的轮——装配该认领回 id，而不是标成补记。
        """
        root = self.tmp / name
        ids = []
        for index, text in enumerate(self.SPOKEN):
            got = self.brain.ingest(who="主人", text=text, ts=_at(index * 12))
            ids.append(got["entry"]["id"])
        for index in range(3 if missed else 4):
            row = {"ts": _at(index * 12), "who": "主人",
                   "said": self.SPOKEN[index], "reply": "第%d答" % index}
            if not legacy:
                row["msg_id"] = ids[index]
            recent.append(row, root=root)
        return root, ids

    def _ledger(self, root: Path) -> list:
        return recent.load(root=root, turns=99)["turns"]

    def _warned(self, run) -> list:
        """把 qqagent.mind 的日志抓下来（一条都不许漏，所以按 handler 收，不按 level 过滤）。"""
        records: list = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        logger = logging.getLogger("qqagent.mind")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        run()
        return records

    def test_a_round_the_ledger_missed_is_backfilled_marked_and_named(self):
        root, ids = self._rig("reconcile_missed", missed=True)
        capture = []
        with self.assertLogs("qqagent.mind", level="WARNING") as logs:
            got = self._answer(captured=capture, recent_root=root, now=_at(60))

        rows = self._ledger(root)
        filled = [row for row in rows if row["fidelity"] == recent.RECONCILED]
        self.assertEqual(1, len(filled), "脑里多出来的那一轮没回填")
        self.assertEqual(ids[3], filled[0]["msg_id"], "回填那条的 msg_id 不是脑里那条的 id")
        self.assertEqual(self.SPOKEN[3], filled[0]["said"], "回填的不是脑里那句原话")
        self.assertEqual("", filled[0]["reply"], "回填时替她编了一句她说的话")
        self.assertEqual(3, rows.index(filled[0]),
                         "缺的是最新那一轮，补记该在末尾（塞到最前面就把「刚才」写成「最早」）")
        stamps = [row["ts"] for row in rows]
        self.assertEqual(sorted(stamps), stamps, "回填后这份账的 ts 不是单调不减")

        system = capture[0]["messages"][0]["content"]
        block = _section(system, conversation.load_assembly()["sections"]["recent"]["label"])
        marked = [line for line in block.splitlines() if recent.BACKFILL_MARK in line]
        self.assertEqual(2, len(marked), "补记那一轮的两行没都带上「（补记）」")
        self.assertIn(self.SPOKEN[3], marked[0])
        self.assertIn(recent.MISSING_REPLY, marked[1], "她那句没写「没有记到」，像是编了一句")
        live = [line for line in block.splitlines() if self.SPOKEN[0] in line]
        self.assertEqual(1, len(live))
        self.assertNotIn(recent.BACKFILL_MARK, live[0], "亲历那一轮也被标成了补记")
        tail = [line for line in block.splitlines() if line.strip()][-2:]
        self.assertEqual(marked, tail, "（补记）那两行没落在 recent 段末尾（顺序把「刚才」变成了「最早」）")

        continuity = got["continuity"]
        self.assertEqual(1, continuity["reconciled"], "返回体没说清补了几条")
        self.assertEqual({"turns_loaded", "omitted", "notice", "reconciled", "rounds"},
                         set(continuity), "既有四键一个都不许动，只许多出这两栏")
        hit = [row for row in continuity["rounds"] if row["msg_id"] == ids[3]]
        self.assertEqual(1, len(hit))
        self.assertEqual({"ts", "fidelity", "msg_id"}, set(hit[0]))
        self.assertEqual(recent.RECONCILED, hit[0]["fidelity"], "rounds 里那一轮没标 reconciled")
        self.assertEqual(_at(36), hit[0]["ts"])
        stamps = [row["ts"] for row in continuity["rounds"]]
        self.assertEqual(sorted(stamps), stamps, "rounds 的 ts 不是单调不减")

        said = [line for line in logs.output if "缺 1 轮" in line]
        self.assertEqual(1, len(said), "对不上却没留痕（或留了不止一行）：%r" % (logs.output,))

    def test_when_the_ledger_has_every_round_nothing_is_marked(self):
        root, ids = self._rig("reconcile_whole", missed=False)
        capture = []
        box: list = []
        lines = self._warned(
            lambda: box.append(self._answer(captured=capture, recent_root=root, now=_at(60))))
        got = box[0]

        rows = self._ledger(root)
        self.assertEqual([], [row for row in rows if row["fidelity"] == recent.RECONCILED],
                         "四条都在账上，却还是标了补记")
        self.assertEqual(5, len(rows), "四条亲历 + 这一轮亲历那一行：不该再多")
        self.assertEqual([], [line for line in lines if "缺" in line], "没缺东西却刷了一行对账")

        system = capture[0]["messages"][0]["content"]
        self.assertNotIn(recent.BACKFILL_MARK, system, "提示词里出现了「（补记）」")
        self.assertNotIn(recent.MISSING_REPLY, system, "提示词里出现了「没有记到」")
        self.assertEqual(0, got["continuity"]["reconciled"])
        self.assertEqual([], [row for row in got["continuity"]["rounds"]
                              if row["fidelity"] == recent.RECONCILED])
        self.assertEqual(4, got["continuity"]["turns_loaded"])

    def test_backfill_happens_only_once(self):
        """幂等：同一个 msg_id 只补一次——第二次装配多出来的只有它自己亲历的那一行。"""
        root, ids = self._rig("reconcile_twice", missed=True)
        with self.assertLogs("qqagent.mind", level="WARNING"):
            self._answer(recent_root=root, now=_at(60))
        first = self._ledger(root)
        self.assertEqual(1, len([row for row in first if row["fidelity"] == recent.RECONCILED]))

        box: list = []
        lines = self._warned(lambda: box.append(self._answer(recent_root=root, now=_at(120))))
        got = box[0]
        second = self._ledger(root)
        self.assertEqual(len(first) + 1, len(second),
                         "第二次装配多出来的只该是它自己亲历的那一行")
        self.assertEqual(1, len([row for row in second if row["fidelity"] == recent.RECONCILED]),
                         "补记被写了第二遍")
        self.assertEqual(1, len([row for row in second if row["msg_id"] == ids[3]]),
                         "同一条补记在盘上出现了两次")
        self.assertEqual([], [line for line in lines if "缺" in line], "第二次装配又刷了一行对账")
        self.assertEqual(1, got["continuity"]["reconciled"], "补记那一轮从提示词里掉了")

    # —— 17) 另外两种「其实记过 / 不是刚才」 ————————————

    def test_brain_rows_sharing_a_ledger_ts_are_not_backfilled_again(self):
        """她早年那句回话被错标成 side=他说，ts 与账上人那一轮完全相同：同一轮，别补。"""
        root = self.tmp / "reconcile_same_ts"
        stamp = _at(0)
        human = self.brain.ingest(who="主人", text="我今早去医院拿片子", ts=stamp)["entry"]["id"]
        self.brain.ingest(who="江迟", text="江迟。\n（尾巴尖轻轻摆了一下）", ts=stamp,
                          side=brain_model.SIDE_OTHER)
        recent.append({"ts": stamp, "who": "主人", "said": "我今早去医院拿片子",
                       "reply": "江迟。"}, root=root)
        capture = []
        box: list = []
        lines = self._warned(
            lambda: box.append(self._answer(captured=capture, recent_root=root, now=_at(60))))
        got = box[0]

        rows = self._ledger(root)
        self.assertEqual([], [row for row in rows if row["fidelity"] == recent.RECONCILED],
                         "同 ts 的那一轮被当成缺的补了进来")
        self.assertEqual(human, rows[0]["msg_id"], "人那一句没认领回 id")
        self.assertEqual(2, len(rows), "只该有账上那一轮 + 这一轮亲历那一行")
        self.assertEqual([], [line for line in lines if "缺" in line], "同一轮却吼了「缺 N 轮」")
        self.assertNotIn(recent.BACKFILL_MARK, capture[0]["messages"][0]["content"])
        self.assertEqual(0, got["continuity"]["reconciled"])

    def test_a_future_entry_is_not_treated_as_just_now(self):
        """ts 晚于本轮装配时刻的条（明天才发生的事）不是「刚才那一轮」，不回填。"""
        root = self.tmp / "reconcile_future"
        self.brain.ingest(who="主人", text="他要求过叫他什么", ts=_at(90))
        capture = []
        box: list = []
        lines = self._warned(
            lambda: box.append(self._answer(captured=capture, recent_root=root, now=_at(60))))
        got = box[0]

        rows = self._ledger(root)
        self.assertEqual(1, len(rows), "未来那一条被补进了最近几轮")
        self.assertEqual(recent.LIVE, rows[0]["fidelity"])
        self.assertEqual([], [line for line in lines if "缺" in line], "未来那一条吼了「缺 N 轮」")
        self.assertNotIn(recent.BACKFILL_MARK, capture[0]["messages"][0]["content"])
        self.assertEqual(0, got["continuity"]["reconciled"])

    def _her_display(self) -> str:
        return self.store.snapshot().identity.display

    def test_her_own_line_mis_sided_as_his_is_never_backfilled(self):
        """她当年那句回话被记成 side=他说：那是她的话，不许当「他说的一轮」补回来。"""
        root = self.tmp / "reconcile_her_line"
        display = self._her_display()
        stamp = _at(0)
        human = self.brain.ingest(who="主人", text="我今早去医院拿片子", ts=stamp)["entry"]["id"]
        recent.append({"ts": stamp, "who": "主人", "said": "我今早去医院拿片子",
                       "reply": "江迟。"}, root=root, cap=99999)
        # 账上少了这一行（ts=T）：脑里同一时刻有两条——真·对方（QQ 号）与她自己的回话
        self.brain.ingest(who="3425635473", text="大肥鱼是你喜欢的名字吗", ts=_at(24))
        self.brain.ingest(who=display, text="不喜欢，听着就沉。", ts=_at(24),
                          side=brain_model.SIDE_OTHER)
        capture = []
        box: list = []
        with self.assertLogs("qqagent.mind", level="WARNING") as logs:
            box.append(self._answer(captured=capture, recent_root=root, now=_at(60)))
        got = box[0]

        rows = self._ledger(root)
        filled = [row for row in rows if row["fidelity"] == recent.RECONCILED]
        self.assertEqual(1, len(filled), "她自己那句被当成「他说」补了回来（或对方那条没补）")
        self.assertEqual("3425635473", filled[0]["who"], "补记的发言者不是对方")
        self.assertEqual("大肥鱼是你喜欢的名字吗", filled[0]["said"])
        self.assertNotIn(display, [row["who"] for row in filled], "补记的发言者写成了她自己")
        self.assertIn(human, [row["msg_id"] for row in rows])

        block = _section(capture[0]["messages"][0]["content"],
                         conversation.load_assembly()["sections"]["recent"]["label"])
        self.assertEqual(2, block.count(recent.BACKFILL_MARK), "（补记）只该落在对方那一条上")
        marked = [line for line in block.splitlines() if recent.BACKFILL_MARK in line]
        self.assertIn("3425635473", marked[0])
        self.assertNotIn(display, "\n".join(marked), "补记那一行的发言者是她自己")
        self.assertEqual(1, got["continuity"]["reconciled"])
        self.assertEqual(1, len([line for line in logs.output if "缺 1 轮" in line]))

    def test_her_own_line_is_skipped_even_when_its_ts_is_free(self):
        """她自己的回话即使 ts 与账上任何一行都不同，也不回填。"""
        root = self.tmp / "reconcile_her_only"
        self.brain.ingest(who=self._her_display(), text="江迟。\n（尾巴尖轻轻摆了一下）",
                          ts=_at(0), side=brain_model.SIDE_OTHER)
        capture = []
        box: list = []
        lines = self._warned(
            lambda: box.append(self._answer(captured=capture, recent_root=root, now=_at(60))))
        got = box[0]

        rows = self._ledger(root)
        self.assertEqual(1, len(rows), "她自己的那句被补成了一轮")
        self.assertEqual(recent.LIVE, rows[0]["fidelity"])
        self.assertEqual([], [line for line in lines if "缺" in line], "她自己的话却吼了「缺 N 轮」")
        self.assertNotIn(recent.BACKFILL_MARK, capture[0]["messages"][0]["content"])
        self.assertEqual(0, got["continuity"]["reconciled"])

    # —— 16) 旧行认领：升级前的行不是补的 ————————————

    def test_legacy_rows_are_claimed_back_to_live_not_marked_as_backfilled(self):
        """账上的旧行没有 msg_id，但脑里那条 (ts, said) 对得上：认领回 id，不许标补记。"""
        root, ids = self._rig("reconcile_legacy", missed=False, legacy=True)
        capture = []
        box: list = []
        lines = self._warned(
            lambda: box.append(self._answer(captured=capture, recent_root=root, now=_at(60))))
        got = box[0]

        rows = self._ledger(root)
        self.assertEqual([], [row for row in rows if row["fidelity"] == recent.RECONCILED],
                         "旧行被误标成补记（它们只是没有凭据，不是丢了的轮）")
        self.assertEqual(ids[:4], [row["msg_id"] for row in rows[:4]], "旧行没认领回 msg_id")
        self.assertEqual({recent.LIVE}, {row["fidelity"] for row in rows},
                         "认领把 fidelity 改了")
        self.assertEqual([], [line for line in lines if "缺" in line], "认领不该出声")
        self.assertNotIn(recent.BACKFILL_MARK, capture[0]["messages"][0]["content"],
                         "提示词里出现了「（补记）」")
        self.assertEqual(0, got["continuity"]["reconciled"])

    def test_claiming_happens_once_and_really_missing_rows_are_still_backfilled(self):
        """认领之后还缺的那条照补：旧行认领 + 真缺回填，两件事各归各。"""
        root, ids = self._rig("reconcile_legacy_lost", missed=True, legacy=True)
        with self.assertLogs("qqagent.mind", level="WARNING") as logs:
            got = self._answer(recent_root=root, now=_at(60))
        rows = self._ledger(root)
        filled = [row for row in rows if row["fidelity"] == recent.RECONCILED]
        self.assertEqual(1, len(filled), "真缺的那一条没补（或把旧行也补了）")
        self.assertEqual(ids[3], filled[0]["msg_id"])
        self.assertEqual(3, rows.index(filled[0]), "缺的最新那一轮没排在末尾")
        self.assertEqual(ids[:3], [row["msg_id"] for row in rows[:3]], "旧行没认领回 msg_id")
        self.assertEqual(1, len([line for line in logs.output if "缺 1 轮" in line]))

    def test_a_legacy_row_whose_ts_and_said_match_nothing_is_a_real_backfill(self):
        """旧行的 (ts, said) 在脑里对不上（ts 被改过 / 真丢了）：仍按缺的补，标补记。"""
        root, ids = self._rig("reconcile_altered", missed=True, legacy=True)
        path = recent.path_of(root)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[0]["ts"] = _at(-6)         # ts 被改过：脑里那条 ts 与账上任何一行都对不上了
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")
        with self.assertLogs("qqagent.mind", level="WARNING") as logs:
            got = self._answer(recent_root=root, now=_at(60))
        filled = [row for row in self._ledger(root) if row["fidelity"] == recent.RECONCILED]
        self.assertEqual(2, len(filled), "改过的旧行与真缺的那条都该补")
        self.assertEqual([ids[0], ids[3]], [row["msg_id"] for row in filled])
        self.assertEqual(1, len([line for line in logs.output if "缺 2 轮" in line]))
        self.assertEqual(2, got["continuity"]["reconciled"])

    def test_claiming_does_not_rewrite_the_ledger_a_second_time(self):
        root, ids = self._rig("reconcile_legacy_once", missed=False, legacy=True)
        self._answer(recent_root=root, now=_at(60))
        once = recent.path_of(root).read_text(encoding="utf-8")
        rows = self._ledger(root)
        self.assertEqual(rows, conversation._reconcile(
            self.brain, {"turns": rows, "omitted": 0, "total": len(rows)},
            root, conversation.load_assembly(), _at(60))["turns"])
        self.assertEqual(once, recent.path_of(root).read_text(encoding="utf-8"),
                         "第二次装配又把文件改写了一遍（认领不幂等）")

    def test_reconciling_the_same_ledger_twice_adds_no_line(self):
        """幂等的硬证据：同一份账对两次，盘上一个字节都不多（行数不增）。"""
        root, ids = self._rig("reconcile_again", missed=True)
        conf = conversation.load_assembly()
        rows = recent.load(root=root, turns=99)["turns"]
        with self.assertLogs("qqagent.mind", level="WARNING"):
            conversation._reconcile(self.brain, {"turns": rows, "omitted": 0,
                                                 "total": len(rows)}, root, conf, _at(60))
        once = recent.path_of(root).read_text(encoding="utf-8")
        self.assertEqual(4, len(once.splitlines()), "第一次对账没把缺的那一轮补进来")

        again = recent.load(root=root, turns=99)["turns"]
        self.assertEqual(again, conversation._reconcile(
            self.brain, {"turns": again, "omitted": 0, "total": len(again)},
            root, conf, _at(60))["turns"], "第二次对账动了盘上的轮次")
        self.assertEqual(once, recent.path_of(root).read_text(encoding="utf-8"),
                         "第二次对账又写了一遍文件（不幂等）")
        self.assertEqual(4, len(once.splitlines()), "行数变了")

    # —— 13) 近轮禁用与动作池 ————————————————————————

    HER_ACTION = "（尾巴尖在水里慢慢蜷了一下）"
    HUMAN_WORDS = "我昨天在小卖部看见一只猫"

    def _her_history(self, replies, name: str) -> Path:
        """造一份历史：每轮的 reply 按给定顺序落盘（said 是人的原话，用来查泄漏）。"""
        root = self.tmp / name
        for index, reply in enumerate(replies):
            recent.append({"ts": _at(index), "who": "主人",
                           "said": "%s（第%d轮）" % (self.HUMAN_WORDS, index),
                           "reply": reply}, root=root)
        return root

    def _banned_block(self, got_capture, conf=None):
        """提示词里近轮禁用那一节的正文（标题从数据读，不写死中文名）。"""
        label = (conf or conversation.load_assembly())["sections"]["banned"]["label"]
        return _section(got_capture[0]["messages"][0]["content"], label)

    def test_banned_section_bans_her_action_line_and_never_the_human_words(self):
        """三轮同一个动作行 → 只列一条 + 重复 3 次；人的原话一个字都不进来。"""
        replies = ["%s\n\n……这个啊。" % self.HER_ACTION,
                   "%s\n\n……是。有过。" % self.HER_ACTION,
                   "%s\n\n……是。有过。" % self.HER_ACTION]
        root = self._her_history(replies, "banned3")
        capture = []
        got = self._answer(captured=capture, recent_root=root)
        system = capture[0]["messages"][0]["content"]
        conf = conversation.load_assembly()
        block = self._banned_block(capture, conf)

        self.assertIn(conf["banned"]["note"], block, "那句禁令没进提示词")
        self.assertIn(repeat.REPEAT % 3, block, "三条一样的动作行没有标「重复 3 次」")
        self.assertEqual(1, block.count(self.HER_ACTION),
                         "同一个动作行在禁令里出现了不止一次：%r" % (block,))
        self.assertNotIn(self.HUMAN_WORDS, block, "人的原话进了禁令：%r" % (block,))
        self.assertNotIn(self.HUMAN_WORDS, str(got["banned"]),
                         "人的原话进了返回体的禁令：%r" % (got["banned"],))
        self.assertIn("……这个啊。", system, "最近几轮的原文段被动了（原文一个字都不许删）")
        label_banned = conf["sections"]["banned"]["label"]
        self.assertLess(system.index("[%s]" % conf["sections"]["recent"]["label"]),
                        system.index("[%s]" % label_banned), "禁令该在最近几轮之后")
        self.assertLess(system.index("[%s]" % label_banned),
                        system.index("[%s]" % conf["sections"]["action"]["label"]),
                        "动作池该在禁令之后")
        self.assertLess(system.index("[%s]" % conf["sections"]["action"]["label"]),
                        system.index("[%s]" % conf["sections"]["memory"]["label"]),
                        "动作池该在脑装包之前")

    def test_banned_read_matches_the_history(self):
        """返回体 banned 的三个键与盘上那几轮对得上（页面与验收照它核对）。"""
        replies = ["%s\n\n……这个啊。" % self.HER_ACTION,
                   "%s\n\n……是。有过。" % self.HER_ACTION,
                   "%s\n\n……是。有过。" % self.HER_ACTION]
        root = self._her_history(replies, "banned_read")
        got = self._answer(recent_root=root)
        conf = conversation.load_assembly()["banned"]
        read = got["banned"]
        self.assertEqual({"rounds", "action_lines", "openings"}, set(read), "banned 的键对外冻结")
        self.assertEqual(conf["rounds"], read["rounds"])
        self.assertEqual([{"text": self.HER_ACTION, "count": 3}], read["action_lines"])
        heads = [usage.opening(repeat.without_actions(reply), conf["opening_chars"])
                 for reply in replies]
        self.assertEqual([head for head, _count in repeat.tally(heads)], read["openings"],
                         "开场不是 usage.opening 的那个口径（去标点 + 前 N 字，再各自去重）")

    def test_ban_window_is_exactly_one_longer_than_the_quota_window(self):
        """配额放行的那一轮，禁令里还得看得见它：banned.rounds == quota.window + 1。

        窗口取相等时（历史事故）：Q01 的动作在 Q08 同时滑出两个窗口 → 配额重置、
        禁令也空 → 同一条动作重生，实测相似度只降到 0.357。多一轮才盖得住。
        """
        conf = conversation.load_assembly()
        self.assertEqual(conf["action"]["quota"]["window"] + 1, conf["banned"]["rounds"],
                         "禁令窗口与配额窗口的关系断了（要么缝里重生，要么白读旧动作）")
        ok = conversation.load_assembly()
        ok["action"]["quota"]["window"] = 4
        ok["banned"]["rounds"] = 5
        self.assertEqual(5, conversation._check_assembly(ok, "test")["banned"]["rounds"])
        bad = conversation.load_assembly()
        bad["action"]["quota"]["window"] = 4
        bad["banned"]["rounds"] = 6
        with self.assertRaises(ValueError) as ctx:
            conversation._check_assembly(bad, "test")
        self.assertIn("action.quota.window + 1", str(ctx.exception))

    def test_banned_window_keeps_only_the_latest_rounds(self):
        """窗口外的更早轮不该出现在禁令里：轮数从数据现读，造 N+1 轮。"""
        rounds = conversation.load_assembly()["banned"]["rounds"]
        oldest = "(最早那一轮的动作)"
        replies = [oldest] + ["%s\n\n第%d轮。" % (self.HER_ACTION, index)
                             for index in range(1, rounds + 1)]
        root = self._her_history(replies, "banned_window")
        capture = []
        got = self._answer(captured=capture, recent_root=root)
        block = self._banned_block(capture)
        self.assertNotIn(oldest, block, "窗口外的更早轮进了禁令：%r" % (block,))
        self.assertIn(repeat.REPEAT % rounds, block,
                      "最近 %d 轮的那条动作行没标重复 %d 次" % (rounds, rounds))
        self.assertEqual([{"text": self.HER_ACTION, "count": rounds}],
                         got["banned"]["action_lines"])

    def test_banned_sees_nothing_when_there_is_no_history(self):
        """一条历史都没有：这一节写一句实话，不留白（空白会被她读成「随便用」）。"""
        capture = []
        got = self._answer(captured=capture)
        block = self._banned_block(capture)
        self.assertIn(conversation.load_assembly()["banned"]["note"], block)
        self.assertIn(conversation.BANNED_EMPTY, block)
        self.assertEqual({"rounds": conversation.load_assembly()["banned"]["rounds"],
                          "action_lines": [], "openings": []}, got["banned"])

    def test_action_pool_and_rule_come_from_the_data_file(self):
        """规矩与池子逐项来自 assembly.json：改数据 → 提示词跟着变，代码里没有部位词。"""
        conf = conversation.load_assembly()
        label = conf["sections"]["action"]["label"]
        capture = []
        self._answer(assembly=conf, captured=capture)
        block = _section(capture[0]["messages"][0]["content"], label)
        self.assertIn(conf["action"]["rule"], block, "换部位的规矩没进提示词")
        for part in conf["action"]["pool"]:
            self.assertIn(part, block, "池子里的 %r 没进提示词" % (part,))

        quota = dict(conf["action"]["quota"])
        conf["action"] = {"pool": ["左耳", "右耳"], "rule": "这一轮只用耳边那些声响。",
                          "quota": quota}
        changed = []
        got = self._answer(assembly=conf, captured=changed)
        block2 = _section(changed[0]["messages"][0]["content"], label)
        self.assertIn("这一轮只用耳边那些声响。", block2)
        self.assertIn("左耳", block2)
        self.assertNotIn("尾巴", block2, "换了池子还带着旧部位：%r" % (block2,))
        self.assertEqual({"pool": ["左耳", "右耳"], "rule": "这一轮只用耳边那些声响。",
                          "quota": {"window": quota["window"], "max": quota["max"],
                                    "used": 0, "blocked": False,
                                    "stripped": 0, "kept_because_empty": False}},
                         got["action"])

    def test_broken_banned_and_action_data_is_reported_not_defaulted(self):
        """banned / action 缺键、类型不对 → ValueError，且指名道姓（代码里没有兜底默认值）。"""
        cases = ((lambda conf: conf["banned"].pop("rounds"), "banned.rounds"),
                 (lambda conf: conf["banned"].__setitem__("opening_chars", "六"),
                  "banned.opening_chars"),
                 (lambda conf: conf["banned"].__setitem__("note", ""), "banned.note"),
                 (lambda conf: conf.__setitem__("action", None), "action"),
                 (lambda conf: conf["action"].__setitem__("pool", []), "action.pool"),
                 (lambda conf: conf["action"].__setitem__("pool", ["尾巴", ""]), "action.pool"),
                 (lambda conf: conf["action"].pop("rule"), "action.rule"),
                 (lambda conf: conf["action"].pop("quota"), "action.quota"),
                 (lambda conf: conf["action"]["quota"].__setitem__("window", 0),
                  "action.quota.window"),
                 (lambda conf: conf["action"]["quota"].__setitem__("max", 9),
                  "action.quota.max"),
                 (lambda conf: conf["action"]["quota"].__setitem__("max", True),
                  "action.quota.max"),
                 (lambda conf: conf["action"]["quota"].__setitem__("blocked", " "),
                  "action.quota.blocked"),
                 (lambda conf: conf["action"]["quota"].pop("sparing"), "action.quota.sparing"))
        for mutate, needle in cases:
            conf = conversation.load_assembly()
            mutate(conf)
            with self.assertRaises(ValueError) as caught:
                self._answer(assembly=conf)
            self.assertIn(needle, str(caught.exception),
                          "数据坏在 %s 时没有指名报错：%s" % (needle, caught.exception))

    # —— 14) 动作配额 ————————————————————————

    QUIET = "……嗯。"

    def _action_block(self, capture, conf=None):
        """提示词里动作池那一节的正文（标题从数据读，不写死中文名）。

        去掉标题后那个换行：这一节被判成「只有一句话」时，要能逐字对着数据里的那一句比。
        """
        label = (conf or conversation.load_assembly())["sections"]["action"]["label"]
        return _section(capture[0]["messages"][0]["content"], label).strip()

    def test_zero_actions_in_the_window_give_the_pool_and_the_sparing_line(self):
        """最近 6 轮一条动作都没有：规矩 + sparing + 池子全给，配额报 used 0 / blocked False。"""
        conf = conversation.load_assembly()
        quota = conf["action"]["quota"]
        root = self._her_history([self.QUIET] * quota["window"], "quota_none")
        capture = []
        got = self._answer(captured=capture, recent_root=root)
        block = self._action_block(capture, conf)
        self.assertIn(conf["action"]["rule"], block, "新口径的规矩没进提示词")
        self.assertIn(quota["sparing"], block, "没顶到配额时该补那句 sparing")
        self.assertNotIn(quota["blocked"], block, "还没顶到配额就写了「不许写动作」")
        for part in conf["action"]["pool"]:
            self.assertIn(part, block, "池子里的 %r 没进提示词" % (part,))
        self.assertEqual({"window": quota["window"], "max": quota["max"],
                          "used": 0, "blocked": False,
                          "stripped": 0, "kept_because_empty": False}, got["action"]["quota"])
        self.assertEqual(conf["action"]["pool"], got["action"]["pool"])

    def test_one_action_in_the_window_blocks_the_whole_pool(self):
        """最近 6 轮里已经有一轮带动作：这一节只剩「本轮不许写动作」，池子与规矩一个字不给。"""
        conf = conversation.load_assembly()
        quota = conf["action"]["quota"]
        replies = ["%s\n\n……这个啊。" % self.HER_ACTION] + [self.QUIET] * (quota["window"] - 1)
        root = self._her_history(replies, "quota_one")
        capture = []
        got = self._answer(captured=capture, recent_root=root)
        block = self._action_block(capture, conf)
        self.assertEqual(quota["blocked"], block,
                         "顶到配额时这一节该只有那一句：%r" % (block,))
        # 只查这一节：别处的锚句 instruction 里也有部位词，那不是这一段给的
        for part in conf["action"]["pool"]:
            self.assertNotIn(part, block, "顶到配额还给着部位 %r：%r" % (part, block))
        self.assertNotIn(conf["action"]["rule"], block, "顶到配额还给了写法规矩")
        self.assertNotIn(quota["sparing"], block, "顶到配额还给了 sparing 那一句")
        self.assertEqual({"window": quota["window"], "max": quota["max"],
                          "used": 1, "blocked": True,
                          "stripped": 0, "kept_because_empty": False}, got["action"]["quota"])
        self.assertEqual([], got["action"]["pool"], "blocked 时 pool 要给空列表，不是 None")
        self.assertEqual(conf["action"]["rule"], got["action"]["rule"])

    def test_an_action_outside_the_window_does_not_block(self):
        """动作落在第 7 轮（窗口就是 6）：不算，照旧给池子与 sparing。"""
        conf = conversation.load_assembly()
        quota = conf["action"]["quota"]
        replies = ["%s\n\n……这个啊。" % self.HER_ACTION] + [self.QUIET] * quota["window"]
        root = self._her_history(replies, "quota_outside")
        capture = []
        got = self._answer(captured=capture, recent_root=root)
        block = self._action_block(capture, conf)
        self.assertFalse(got["action"]["quota"]["blocked"], "窗口外那一条动作把这一轮封了")
        self.assertEqual(0, got["action"]["quota"]["used"])
        self.assertIn(quota["sparing"], block)
        self.assertIn(conf["action"]["pool"][0], block)

    def test_quota_numbers_come_from_the_data_file(self):
        """配额从数据现读：max 改成 2，同样 1 轮带动作就不再 blocked。"""
        conf = conversation.load_assembly()
        quota = conf["action"]["quota"]
        conf["action"]["quota"] = dict(quota, max=2)
        replies = ["%s\n\n……这个啊。" % self.HER_ACTION] + [self.QUIET] * (quota["window"] - 1)
        root = self._her_history(replies, "quota_data")
        capture = []
        got = self._answer(assembly=conf, captured=capture, recent_root=root)
        block = self._action_block(capture, conf)
        self.assertEqual({"window": quota["window"], "max": 2, "used": 1, "blocked": False,
                          "stripped": 0, "kept_because_empty": False},
                         got["action"]["quota"], "配额数没有跟着数据走")
        self.assertIn(conf["action"]["pool"][0], block, "max 改成 2 了还给不出池子")
        self.assertIn(conf["action"]["quota"]["sparing"], block)

    # —— 15) blocked 的硬兜底（不靠模型自觉） ————————————

    STRIPPED_ACTION = "（尾巴轻轻摆了一下）"
    KEPT_ACTION = "（尾巴轻轻蜷了一下）"
    BODY = "今天水里不冷。"

    def _blocked_root(self, name: str) -> Path:
        """一份配额已经满了的历史：最近 window 轮里有一轮带动作。"""
        quota = conversation.load_assembly()["action"]["quota"]
        replies = ["%s\n\n……这个啊。" % self.HER_ACTION] + [self.QUIET] * (quota["window"] - 1)
        return self._her_history(replies, name)

    def test_blocked_round_strips_the_action_line_out_of_the_reply(self):
        """配额满了模型还写动作行：过嘴之前就删掉，chunks / 落账 / 落脑都不含它。"""
        root = self._blocked_root("quota_strip")
        got = self._answer(reply="%s\n\n%s" % (self.BODY, self.STRIPPED_ACTION),
                           recent_root=root)
        self.assertEqual(self.BODY, got["reply"], "删掉动作行之后正文该原样留着")
        self.assertEqual(1, got["action"]["quota"]["stripped"], "删了几行要记账")
        self.assertFalse(got["action"]["quota"]["kept_because_empty"])
        self.assertTrue(got["chunks"], "删掉一行动作之后 chunks 不该是空的")
        for chunk in got["chunks"]:
            self.assertNotIn("尾巴", chunk["text"], "chunks 里还带着动作行：%r" % (chunk,))
        row = recent.load(root=root, turns=12)["turns"][-1]
        self.assertEqual(self.BODY, row["reply"], "落进最近几轮的那句还带着动作行：%r" % (row,))
        texts = [entry.get("text") for entry in self.brain.today_entries(_at(30))]
        self.assertNotIn(self.STRIPPED_ACTION, texts, "落脑的那句还带着动作行")

    def test_blocked_round_that_is_only_an_action_line_keeps_the_original(self):
        """整轮只有一个动作行：不删（删空比带一行动作更糟），stripped 记 0 并标 kept_because_empty。"""
        root = self._blocked_root("quota_kept")
        got = self._answer(reply=self.KEPT_ACTION, recent_root=root)
        self.assertEqual(self.KEPT_ACTION, got["reply"], "把一轮回复删空了")
        self.assertEqual(0, got["action"]["quota"]["stripped"])
        self.assertTrue(got["action"]["quota"]["kept_because_empty"])

    def test_action_line_survives_when_the_quota_is_not_reached(self):
        """没顶到配额：动作行是这一轮该有的那一个，一个字都不动。"""
        quota = conversation.load_assembly()["action"]["quota"]
        root = self._her_history([self.QUIET] * quota["window"], "quota_keep")
        reply = "%s\n\n%s" % (self.BODY, self.KEPT_ACTION)
        got = self._answer(reply=reply, recent_root=root)
        self.assertEqual(reply, got["reply"], "没顶到配额却动了动作行")
        self.assertEqual(0, got["action"]["quota"]["stripped"])
        self.assertFalse(got["action"]["quota"]["kept_because_empty"])

    def test_blocked_swaps_the_whole_set_of_speaking_rules(self):
        """blocked 时提示词用 format_blocked：动作那条从「怎么写」换成「不许写」。"""
        conf = conversation.load_assembly()
        root = self._blocked_root("quota_rules")
        capture = []
        self._answer(captured=capture, recent_root=root)
        system = capture[0]["messages"][0]["content"]
        blocked_rules = conf["reply"]["format_blocked"]
        self.assertTrue(blocked_rules, "format_blocked 是空的：那一态就没有规矩了")
        for rule in blocked_rules:
            self.assertIn(rule, system, "blocked 的规矩没进提示词：%s" % rule)
        for rule in conf["reply"]["format"]:
            if "圆括号" in rule:
                self.assertNotIn(rule, system, "blocked 时还在教她怎么写动作行：%s" % rule)

    def test_broken_blocked_rules_are_reported_not_defaulted(self):
        """format_blocked 缺 / 动作那条为空 → ValueError（代码里不补默认规矩）。"""
        for mutate in (lambda conf: conf["reply"].pop("format_blocked"),
                       lambda conf: conf["reply"]["format_blocked"].__setitem__(1, ""),
                       lambda conf: conf["reply"]["format_blocked"].append(" ")):
            conf = conversation.load_assembly()
            mutate(conf)
            with self.assertRaises(ValueError) as caught:
                self._answer(assembly=conf)
            self.assertIn("reply.format_blocked", str(caught.exception),
                          "format_blocked 坏了却没有指名报错：%s" % caught.exception)

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

    # —— 9.5) 说话格式（生成侧就短，不靠出口切） ————————

    def test_speaking_format_rules_reach_the_prompt(self):
        """三条规矩写进 assembly.json 的 reply.format，逐条出现在提示词里。"""
        fmt = conversation.load_assembly()["reply"]["format"]
        self.assertGreaterEqual(len(fmt), 3, "三条说话格式规矩要在数据文件里")
        capture = []
        self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        for rule in fmt:
            self.assertIn(rule, system, "这条说话格式没进提示词：%s" % rule)
        self.assertIn("一次说一到两句", system)
        self.assertIn("圆括号", system)
        self.assertIn("最多一个动作", system)

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

    # —— 12) 口癖护栏 ————————————————————————————

    GUARD_KEYS = {"window", "threshold", "min_rounds", "rounds", "hits", "rate",
                  "over", "injected", "available"}

    def _usage_seed(self, count: int, hits: int) -> None:
        """造 count 轮口癖账，最后 hits 轮命中。

        默认落点就是 answer 会读的那一份（<QQAGENT_HOME>/mind/anchor_usage.jsonl），
        所以这里不传 root——读的和写的必须是同一本账，否则护栏永远看不见历史。
        """
        for index in range(count):
            hit = index >= count - hits
            usage.append(hit, "这个啊又来了", "不想聊" if hit else "")

    def _halves(self):
        """家那几条半句，**按台词库的现行顺序**（read_all 给的顺序，不是 FIVE 的书写顺序）。

        现行顺序是 latest_by_scene 排出来的（ts → scene），所以降档时注入的是它开头那两条——
        从 read_all 现读，测试才不会替台词库另定一个顺序。
        """
        conf = conversation.load_assembly()["anchors"]
        out = []
        for row in home_anchors.read_all()["anchors"]:
            say = str(row.get("say") or "")
            cut = int(math.ceil(len(say) * conf["inject"]["fraction"]))
            out.append((str(row.get("scene") or ""), say[:cut] + conf["inject"]["suffix"]))
        return out

    def test_frequent_opening_downgrades_to_the_first_two_halves(self):
        """60 轮全命中 → over：只注入现行顺序的前 2 条，其余 3 条进 anchors.skipped。"""
        self._anchors_home(list(self.FIVE))
        self._usage_seed(60, 60)
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]

        guard = got["guard"]
        self.assertEqual(self.GUARD_KEYS, set(guard), "guard 的键是对外冻结的那一份")
        self.assertTrue(guard["over"], "60 轮全命中却没降档")
        self.assertEqual(50, guard["window"])
        self.assertEqual(50, guard["rounds"], "读数只算最近 50 轮")
        self.assertEqual(50, guard["hits"])
        self.assertEqual(1.0, guard["rate"])
        self.assertEqual(2, guard["injected"])
        self.assertEqual(5, guard["available"])

        halves = self._halves()
        for scene, half in halves[:2]:
            self.assertIn(half, system, "前两条该注入：%s" % scene)
        for scene, half in halves[2:]:
            self.assertNotIn(half, system, "降档了还在注入：%s" % scene)
        self.assertEqual([scene for scene, _half in halves[2:]], got["anchors"]["skipped"],
                         "被跳过的 3 条要逐个点名，不许静默丢")
        self.assertEqual(2, got["anchors"]["injected"])
        self.assertEqual(5, got["anchors"]["available"])
        self.assertEqual(2, len(got["anchors"]["items"]), "items 只列实际注入的那几条")

    def test_low_hit_rate_keeps_all_five_halves(self):
        """60 轮里只有 6 轮命中（窗口内 6/50 = 12%）→ 不降档，五条半句全给。"""
        self._anchors_home(list(self.FIVE))
        self._usage_seed(60, 6)
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]

        guard = got["guard"]
        self.assertFalse(guard["over"], "12% 的命中率被判成超阈值了")
        self.assertEqual(50, guard["rounds"])
        self.assertEqual(6, guard["hits"])
        self.assertAlmostEqual(0.12, guard["rate"])
        self.assertEqual(5, guard["injected"])
        self.assertEqual(5, got["anchors"]["injected"])
        self.assertEqual([], got["anchors"]["skipped"])
        for scene, half in self._halves():
            self.assertIn(half, system, "没降档却不给这半句：%s" % scene)

    def test_this_round_lands_in_the_usage_ledger(self):
        """说出口之后账本多一行：opening 是回话开头，hit / scene 与家那五条对得上。"""
        self._anchors_home(list(self.FIVE))
        reply = "这个啊，又来了。"
        got = self._answer(reply=reply)
        self.assertEqual("pass", got["gate"]["decision"])

        ledger = usage.read()
        self.assertEqual(1, ledger["rounds"], "这一轮没进账")
        row = ledger["rows"][-1]
        conf = conversation.load_assembly()["anchors"]["guard"]
        self.assertEqual(usage.opening(reply, conf["opening_chars"]), row["opening"],
                         "opening 不是这一轮回话的开头")
        self.assertTrue(row["hit"], "回话开头就是「这个啊」，却判成没命中")
        self.assertEqual("不想聊", row["scene"], "命中要记那条台词的 scene")
        self.assertTrue(row["ts"], "账本缺时刻")

    def test_dropped_round_is_not_written_to_the_usage_ledger(self):
        """被闸弃掉的那一轮没说出口：没有开头，就不许记一行。"""
        root = self.tmp / "mouth_usage"
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
        self._anchors_home(list(self.FIVE))
        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(root)}):
            got = self._answer(reply="作为一个 AI 语言模型，我不能这样说。")
        self.assertEqual("dropped", got["gate"]["decision"])
        self.assertEqual("", got["reply"])
        self.assertEqual(0, usage.read()["rounds"], "被闸弃掉的轮没说出口，不该记开头")

    def test_guard_readings_come_from_the_assembly_file(self):
        """换一份 guard 数据 → 读数跟着变（八/九个数一个都不在代码里）。"""
        self._anchors_home(list(self.FIVE))
        self._usage_seed(3, 2)
        conf = conversation.load_assembly()
        conf["anchors"]["guard"] = {"window": 7, "threshold": 0.5, "opening_chars": 4,
                                    "prefix_chars": 2, "reduced_count": 1, "min_rounds": 1}
        got = self._answer(assembly=conf)
        guard = got["guard"]
        self.assertEqual(self.GUARD_KEYS, set(guard))
        self.assertEqual(7, guard["window"])
        self.assertEqual(0.5, guard["threshold"])
        self.assertEqual(1, guard["min_rounds"])
        self.assertEqual(3, guard["rounds"])
        self.assertEqual(2, guard["hits"])
        self.assertAlmostEqual(2.0 / 3.0, guard["rate"])
        self.assertTrue(guard["over"])
        self.assertEqual(1, guard["injected"], "reduced_count 改了，注入条数没跟着改")
        self.assertEqual(1, got["anchors"]["injected"])

    def test_broken_guard_data_is_reported_not_defaulted(self):
        """guard 缺键、类型不对 → ValueError（代码里不许有这几个数的兜底默认值）。"""
        for breakage, needle in ((("drop", "prefix_chars"), "anchors.guard.prefix_chars"),
                                 (("bad", "window"), "anchors.guard.window"),
                                 (("bad", "threshold"), "anchors.guard.threshold")):
            conf = conversation.load_assembly()
            guard = conf["anchors"]["guard"]
            if breakage[0] == "drop":
                guard.pop(breakage[1])
            else:
                guard[breakage[1]] = "五十" if breakage[1] == "window" else 3
            with self.assertRaises(ValueError) as caught:
                self._answer(assembly=conf)
            self.assertIn(needle, str(caught.exception),
                          "缺 %s 时没有指名报错：%s" % (breakage[1], caught.exception))

    def test_no_anchors_section_still_carries_the_guard_keys(self):
        """没配锚句就没有护栏：八个键照样交出去，值一律 null（页面写「没给」），over 是 False。"""
        conf = conversation.load_assembly()
        conf["anchors"] = None
        got = self._answer(assembly=conf)
        self.assertEqual(self.GUARD_KEYS, set(got["guard"]))
        self.assertFalse(got["guard"]["over"])
        for key in ("window", "threshold", "min_rounds", "rounds", "hits", "rate",
                    "injected", "available"):
            self.assertIsNone(got["guard"][key], "没配护栏却给出一个数：%s" % key)

    def test_guard_is_read_before_this_round_is_written(self):
        """先是读数、后是落账：19 轮全命中时还没到 min_rounds（20），这一轮不该自己把自己顶上去。"""
        self._anchors_home(list(self.FIVE))
        self._usage_seed(19, 19)
        got = self._answer()
        self.assertEqual(19, got["guard"]["rounds"], "护栏把这一轮的账也算进读数了")
        self.assertFalse(got["guard"]["over"])
        self.assertEqual(20, got["guard"]["min_rounds"])
        self.assertEqual(20, usage.read()["rounds"], "这一轮该在读数之后才落账")


if __name__ == "__main__":
    unittest.main()
