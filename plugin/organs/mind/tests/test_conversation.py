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
10. **锚句只装 phrase**：没配 / 卡不在 → 提示词含「未配锚句」且 anchors.configured is False；
    写了一张家·仓库的卡 → 提示词含 **phrase 的半句**，**example 连着的 6 个字一个都不出现**
    （它是「口吻长什么样」的证据，不是给模型看的），when 也不进提示词（它是语境提示，不是用法说明）。
    返回体 anchors.items 只带 id / phrase / injected / when。
11. **落账**：一轮之后 recent_turns.jsonl 多一条，said / reply 与返回体一致；
    被闸弃掉的那一轮 reply 记空串、人那句仍然存。
12. **口癖护栏**：最近 50 轮老用锚句开场（命中率 > 30% 且轮数够 min_rounds）→ 降档只注入
    2 条半句，其余在 anchors.skipped 里点名；命中率低就五条全给。guard 的八/九键对
    页面冻结、值全来自 assembly.json 的 anchors.guard（改数据 → 读数跟着变）。
    这一轮说出口之后账本多一行（hit 与回话开头一致）；被闸弃掉的轮账本不增行。
16. **近轮禁用扩到锚句**：最近 anchors.repeat.window 轮里同一张卡（按 id）命中到 max 次，
    这一轮就不注入那张、并在 anchors.banned_by_repeat 里点名；只命中 1 次不影响；窗口外的旧账
    不算；次序是「先按 guard 定总条数、再按 repeat 剔除」。
17. **照抄审计（两级）**：回话与某张卡的 example 连续重合 ≥ anchors.audit.example_overlap 字
    → 判「报台词」，**发一次重写请求**改一次，改好就发新稿（decision=rewritten）；改完仍重合
    → 这一句弃掉（decision=dropped，reply 空）；只命中 phrase → 放行、只记账（kind=phrase）。
    返回体 audit{decision, kind, card_id, overlap}；数据缺键 / 类型不对一律 ValueError。
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

18. **主动开口（她自己起头那一轮）**：answer(seed=...) 才是主动，seed 是 None 一律被动。
    主动那一轮——提示词换成 assembly.json 的 prompt.proactive（由头与理由进提示词、user 那一问
    是空串）、「这一小段」写 NO_SEGMENT（段给了也忽略）、心判决的 intent 是 proactive（夜里因此
    判 SILENT / reason=night，被动只判 LESS）、落脑只有她那句（side=SIDE_SELF，**没有假的人话**）、
    recent 照记一行但 said 空；回包多 mode 与 seed{text, reason}。数据缺 prompt.proactive → ValueError。

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
from organs.heart import engage as engage_module
from organs.heart import events as heart_events
from organs.heart import model as heart_model
from organs.heart import mood, sleep
from organs.home import anchors as home_anchors
from organs.home import cards
from organs.mind import conversation, events, llm, recent, repeat, usage
from organs.soul import load as load_soul

WHO = "3425635473"   # 脑里的 who 现在是数字号（WHO_RE），昵称只作 display
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
# 她自己开口那一轮的由头（素材，不是判据）：装配拿它当「一问」，回想也用它。
SEED = "今天想问问他吃饭了没有"
SEED_REASON = "night"                     # 由头那一侧的理由码：装配不解释它，原样进提示词与回包

BASE = datetime(2025, 1, 15, 14, 0, 0, tzinfo=datetime.now().astimezone().tzinfo)
# 夜里那一轮：夜间窗口在 organs/heart/model.py 的 NIGHT_START_H / NIGHT_END_H 之间。
NIGHT = datetime(2025, 1, 15, 1, 0, 0, tzinfo=datetime.now().astimezone().tzinfo)


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


def _script(*replies: str, captured: list = None):
    """假 transport：按调用顺序依次回那几句（第 1 次是装配那一问，第 2 次起是重写）。

    最后一句会一直用下去：一泡命中、后面再命中时，重写都是用最后那一句回的。
    """
    queue = list(replies)

    def send(url, headers, payload, timeout):
        if captured is not None:
            captured.append(payload)
        text = queue[0] if len(queue) == 1 else queue.pop(0)
        data = {"choices": [{"message": {"content": text}}]}
        return 200, json.dumps(data, ensure_ascii=False).encode("utf-8")

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

    def _answer(self, text: str = QUESTION, reply: str = SENTENCE, *, who: str = WHO,
                now: str = _at(5), captured: list = None, transport=None, brain=None,
                **extra):
        send = transport if transport is not None else _transport(reply, captured)
        return conversation.answer(text, who, store=self.store,
                                   brain=self.brain if brain is None else brain,
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

    # —— 1.5) 魂那一段的段首说明（这一轮加的） ——————————

    def test_soul_section_starts_with_the_not_to_recite_line(self):
        """魂六件顶上第一行是那句说明（来自 assembly.json），而且读起来是人话、不是系统提示。

        为什么：魂六件是人写给她的底子，整段照抄进提示词读起来与台词一模一样，上一轮她就把
        「喜欢的食物」那半句原样念了出来。这一句说的是「这些是你的底子，不是要背出来的话」。
        判据两条：**在段首**（标签之后、正文之前），**是人话**（以下 / 注意 / 请勿 / 系统 这类
        系统腔一个都不许有）。
        """
        notice = conversation.load_assembly()["sections"]["soul"]["notice"]
        self.assertIn("别照着念", notice, "这句话说的就是「别照着念」")
        for word in ("以下", "注意", "请勿", "系统"):
            self.assertNotIn(word, notice, "段首那句是人话，一个系统腔的词都不该有：%s" % word)
        capture = []
        self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        label = conversation.load_assembly()["sections"]["soul"]["label"]
        block = _section(system, label)
        first = [line for line in block.strip().splitlines() if line.strip()][0]
        self.assertEqual(notice, first, "魂那一段的第一行不是那句说明")
        self.assertLess(system.index(notice), system.index(SOUL_HEADINGS[0]),
                        "说明要排在魂正文之前")

    def test_soul_notice_missing_or_empty_is_a_named_error(self):
        """数据把这句话删掉 / 写成空白：当场 ValueError 并点名 sections.soul.notice（不许悄悄少一句）。

        这句话不是样式，是她念不念设定的那一格：悄悄少了，装配照旧跑、账上也看不出来，
        于是又回到「查两圈才明白她为什么念」。
        """
        live = self.tmp / "home" / "mind"
        live.mkdir(parents=True)
        base = conversation.load_assembly()               # 出厂那份（实盘根还空着），照着改
        for how in ("missing", "empty"):
            data = json.loads(json.dumps(base, ensure_ascii=False))
            if how == "missing":
                data["sections"]["soul"].pop("notice")
            else:
                data["sections"]["soul"]["notice"] = "   "
            (live / "assembly.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                conversation.load_assembly()
            self.assertIn("sections.soul.notice", str(caught.exception),
                          "报错要点名缺的是哪一格（%s）：%s" % (how, caught.exception))

    # —— 2) 四段都装进去了 ————————————————————————

    def test_prompt_carries_soul_headings_memory_evidence_and_tone_line(self):
        entry = self.brain.ingest(who="3425635473", text=MEMORY_TEXT, ts=_at(0))
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
        self.assertTrue({"tier", "direction"} <= set(heart["mood"]), "mood 少了必备键")
        self.assertTrue({"tone", "direction", "bans"} <= set(heart["voice"]),
                        "voice 少了必备键（多出来的键不算错：加字段不该把断言搞红）")
        self.assertTrue({"state", "reason"} <= set(heart["engage"]))
        self.assertTrue(heart["axes"], "六轴一个词都没有")
        for axis, word in heart["axes"].items():
            self.assertIsInstance(word, str, "%s 的档位不是词：%r" % (axis, word))
        for node in self._walk(heart):
            self.assertNotIsInstance(node, (int, float), "心里混进了数：%r" % (node,))
        text = json.dumps(heart, ensure_ascii=False)
        self.assertEqual([], [ch for ch in text if ch.isdigit()], "心的返回体里有数字：%s" % text)

    def test_the_engage_input_itself_goes_out_under_a_private_key(self):
        """装配把这一轮判决用的那一份 EngageInput 原样交出来（私有键 _gate_input）。

        出站闸判「发不发」必须用**同一份输入**：这里钉住它是这一轮那一份——时刻、是谁、
        以及拿它再 decide 一次得到的态与回包 heart.engage 那份一致（不是另一个造出来的）。
        """
        got = self._answer(now=_at(5), who=WHO)
        inp = got["_gate_input"]
        self.assertIsInstance(inp, engage_module.EngageInput, "交出来的不是判决输入本身")
        self.assertEqual(_at(5), inp.now, "不是这一轮那个时刻")
        self.assertEqual(WHO, inp.who)
        self.assertEqual(got["heart"]["engage"]["state"], engage_module.decide(inp)["state"],
                         "交回来的不是这一轮判决用的那一份输入")

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
        self.brain.interact(WHO, ts=when)     # 熟稔过线：让第 5 道软门成为唯一理由
        query = urlencode({"now": when, "who": WHO, "root": str(root)})
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
        entry = self.brain.ingest(who="3425635473", text=MEMORY_TEXT, ts=_at(0))
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
        self.assertEqual(WHO, [row["who"] for row in rows][-2])
        for entry in rows:
            self.assertEqual("亲历", entry.get("source_type"))
            self.assertTrue(entry.get("evidence_key"), "证据指针是空的")
            self.assertEqual(entry["evidence_key"], entry["id"])
        self.assertEqual(rows[-2]["evidence_key"], by_who[WHO])
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
        # 抽屉那一节是**有条件**的：没翻就整节不加（不留一个空标题）。这一轮没给 scene（归一成
        # unknown），但翻不翻不看场合——「还记得」命中了关键词，所以它真去翻了；只是临时家里一颗
        # 日记都没有，读不出 → 整节不加。别的节仍与 order 一一对上。
        rendered = [row["key"] for row in got["assembly"]["sections"]]
        self.assertNotIn(conversation.DRAWER_KEY, rendered, "没命中抽屉却多出那一节")
        self.assertEqual([key for key in order if key in rendered], rendered,
                         "装配多出或漏掉了 order 之外的节")

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
            got = self.brain.ingest(who="3425635473", text=text, ts=_at(index * 12))
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
        self.assertTrue({"ts", "fidelity", "msg_id"} <= set(hit[0]))
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
        human = self.brain.ingest(who="3425635473", text="我今早去医院拿片子", ts=stamp)["entry"]["id"]
        # 她那句当年被错标成 side=他说，但 who 仍是她自己；若这里也用人那个号，
        # 脑的 10 分钟并条窗口会把两句并成一条，测的就不是「同一轮的错标」了。
        self.brain.ingest(who=self.store.snapshot().identity.display,
                          text="江迟。\n（尾巴尖轻轻摆了一下）", ts=stamp,
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
        self.brain.ingest(who="3425635473", text="他要求过叫他什么", ts=_at(90))
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
        human = self.brain.ingest(who="3425635473", text="我今早去医院拿片子", ts=stamp)["entry"]["id"]
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
        self.assertTrue({"rounds", "action_lines", "openings"} <= set(read),
                        "banned 少了必备键（多出来的键不算错）")
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
        self.brain.ingest(who="3425635473", text=MEMORY_TEXT, ts=_at(0))
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
        self.brain.ingest(who="3425635473", text=MEMORY_TEXT, ts=_at(0))
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

    #: 五张现行台词卡（新格式）：phrase ≤4 字是进提示词的那一档，example 是整句例句（不进提示词），
    #: when 是语境提示（只回给人看，不是触发条件），body 只写她自己的身体。
    FIVE = (
        {"id": "被夸", "phrase": "才知道啊", "example": "哼，才知道啊。",
         "when": "被夸，且不想直接承认高兴", "body": "尾巴翘起来晃了两下"},
        {"id": "被关心", "phrase": "行了行了", "example": "行了行了，我又不是玻璃做的。",
         "when": "被关心，且不想被当成弱者", "body": "别开头、脸微微发红"},
        {"id": "不想聊", "phrase": "以后再说", "example": "这个啊……以后再说吧。",
         "when": "不想继续聊，但要留门", "body": ""},
        {"id": "认错", "phrase": "记岔了", "example": "……是我记岔了。",
         "when": "发现自己记错了", "body": "低头、尾巴垂下去不敢动"},
        {"id": "你要走", "phrase": "我在这儿", "example": "你忙你的，我在这儿。",
         "when": "他要走或要忙", "body": "尾巴慢慢摆一下，乖巧的笑了笑"},
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

    def test_five_halves_go_in_and_never_the_example(self):
        """五条 phrase 的半句全注入、不按场合挑；example 与 when 一个字都不许进提示词。"""
        self._anchors_home(list(self.FIVE))
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        conf = conversation.load_assembly()["anchors"]
        self.assertTrue(got["anchors"]["configured"])
        self.assertEqual(5, got["anchors"]["count"], "五条全给，不按场合挑")
        self.assertIn(conf["instruction"], system, "行为规矩没进提示词")
        for want in self.FIVE:
            cut = int(math.ceil(len(want["phrase"]) * conf["inject"]["fraction"]))
            half = want["phrase"][:cut] + conf["inject"]["suffix"]
            self.assertIn(half, system, "半句没装上：%s" % want["id"])
            self.assertNotIn(want["example"], system, "整句例句进了提示词：%s" % want["id"])
            if want["body"]:
                self.assertNotIn(want["body"], system,
                                 "body 进了提示词（那是在等着被照抄）：%s" % want["id"])
            # when 是语境提示（回给人看），**不是触发条件**：不许按它挑卡，也不许当用法说明
            self.assertNotIn(want["when"], system, "when 进了提示词：%s" % want["id"])
        items = {item["id"]: item for item in got["anchors"]["items"]}
        self.assertEqual(len(self.FIVE), len(items))
        for want in self.FIVE:
            self.assertEqual({"id", "phrase", "injected", "when", "example", "body"},
                         set(items[want["id"]]),
                             "items 只带这四栏：example 一个字都不回（审计要用时现读家）")
            self.assertEqual(want["phrase"], items[want["id"]]["phrase"])
            self.assertEqual(want["when"], items[want["id"]]["when"])

    def test_no_six_chars_of_any_example_reach_the_prompt(self):
        """逐张卡取 example 的每一段连续 6 字：提示词里一个都找不到（phrase 那半句不算）。"""
        self._anchors_home(list(self.FIVE))
        capture = []
        self._answer(captured=capture)
        system = usage.normalize(capture[0]["messages"][0]["content"])
        size = int(conversation.load_assembly()["anchors"]["audit"]["example_overlap"])
        for want in self.FIVE:
            example = usage.normalize(want["example"])
            for start in range(0, len(example) - size + 1):
                piece = example[start:start + size]
                self.assertNotIn(piece, system,
                                 "example 的连续 %d 字进了提示词：%r（%s）"
                                 % (size, piece, want["id"]))

    def test_half_line_keeps_its_shape(self):
        """人给的样子：phrase「才知道啊」注入成「才知……」——半句从 phrase 取，不从整句取。"""
        self._anchors_home([{"id": "被夸", "phrase": "才知道啊", "example": "哼，才知道啊。",
                             "when": "被夸，且不想直接承认高兴", "body": "尾巴翘起来晃了两下"}])
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        self.assertEqual("才知……", got["anchors"]["items"][0]["injected"])
        self.assertIn("才知……", system)
        self.assertNotIn("哼，才知……", system, "半句是从整句开头截的：不是 phrase 那一档")
        self.assertNotIn("哼，才知道啊。", system)
        self.assertNotIn("尾巴翘起来晃了两下", system)

    def test_a_card_without_phrase_gives_no_half_line(self):
        """老卡（只有 say）没有 phrase：不拿整句顶上去——example 一个字都不进提示词。"""
        self._anchors_home([{"scene": "被夸", "say": "哼，才知道啊。", "body": "尾巴翘起来晃了两下"}])
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        self.assertFalse(got["anchors"]["configured"], "没有 phrase 却报成配了半句")
        self.assertIn(conversation.ANCHORS_MISSING, system)
        self.assertNotIn("哼，才知道啊。", system)

    # —— 11) 落账 ————————————————————————————

    def test_this_round_lands_in_recent_verbatim(self):
        got = self._answer()
        loaded = recent.load(turns=12)          # 默认落点：<QQAGENT_HOME>/mind/recent_turns.jsonl
        self.assertEqual(1, loaded["total"], "这一轮没落进最近几轮那份账")
        row = loaded["turns"][0]
        self.assertEqual(WHO, row["who"])
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

        现行顺序是 latest_by_scene 排出来的（ts → id），所以降档时注入的是它开头那两条——
        从 read_all 现读，测试才不会替台词库另定一个顺序。半句一律从 **phrase** 取。
        """
        conf = conversation.load_assembly()["anchors"]
        out = []
        for row in home_anchors.read_all()["anchors"]:
            phrase = str(row.get("phrase") or "")
            cut = int(math.ceil(len(phrase) * conf["inject"]["fraction"]))
            out.append((str(row.get("id") or ""), phrase[:cut] + conf["inject"]["suffix"]))
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
        for card, half in halves[:2]:
            self.assertIn(half, system, "前两条该注入：%s" % card)
        for card, half in halves[2:]:
            self.assertNotIn(half, system, "降档了还在注入：%s" % card)
        self.assertEqual([card for card, _half in halves[2:]], got["anchors"]["skipped"],
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
        for card, half in self._halves():
            self.assertIn(half, system, "没降档却不给这半句：%s" % card)

    def test_this_round_lands_in_the_usage_ledger(self):
        """说出口之后账本多一行：opening 是回话开头，hit / id / phrase / scene 与家那五条对得上。"""
        self._anchors_home(list(self.FIVE))
        reply = "以后再说吧，现在不行。"
        got = self._answer(reply=reply)
        self.assertEqual("pass", got["gate"]["decision"])

        ledger = usage.read()
        self.assertEqual(1, ledger["rounds"], "这一轮没进账")
        row = ledger["rows"][-1]
        conf = conversation.load_assembly()["anchors"]["guard"]
        self.assertEqual(usage.opening(reply, conf["opening_chars"]), row["opening"],
                         "opening 不是这一轮回话的开头")
        self.assertTrue(row["hit"], "回话开头就是 phrase「以后再说」，却判成没命中")
        self.assertEqual("不想聊", row["id"], "命中要记是哪一张卡（近轮禁用按 id 数次数）")
        self.assertEqual("以后再说", row["phrase"], "命中要记是哪一句口癖")
        self.assertEqual("不想继续聊，但要留门", row["scene"],
                         "scene 那一栏口径不变：读侧给的场景（等于 when）")
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


    # —— 16) 近轮禁用扩到锚句 ————————————————————

    def _card_seed(self, card_id: str, phrase: str, times: int) -> None:
        """往同一本口癖账里记 times 轮「命中了这一张卡」（带 id 与 phrase）。"""
        for _ in range(times):
            usage.append(True, "才知道什么呀", "被夸", card_id=card_id, phrase=phrase)

    def test_a_card_seen_twice_in_the_window_is_not_injected(self):
        """最近 12 轮里同一张卡出现 2 次（= anchors.repeat.max）→ 这一轮不注入它、点名报出来。"""
        self._anchors_home(list(self.FIVE))
        repeats = conversation.load_assembly()["anchors"]["repeat"]
        self._card_seed("被夸", "才知道啊", int(repeats["max"]))
        capture = []
        got = self._answer(captured=capture)
        system = capture[0]["messages"][0]["content"]
        halves = dict(self._halves())
        self.assertEqual(["被夸"], got["anchors"]["banned_by_repeat"],
                         "被近轮禁用挡住的那张要逐个点名，不许静默丢")
        self.assertNotIn(halves["被夸"], system, "被禁的那张还在注入")
        self.assertIn(halves["被关心"], system, "别的卡跟着一起被禁了")
        self.assertEqual(len(self.FIVE) - 1, got["anchors"]["injected"])
        self.assertEqual(len(self.FIVE), got["anchors"]["available"])

    def test_one_hit_in_the_window_does_not_ban(self):
        self._anchors_home(list(self.FIVE))
        self._card_seed("被夸", "才知道啊", 1)
        capture = []
        got = self._answer(captured=capture)
        self.assertEqual([], got["anchors"]["banned_by_repeat"])
        self.assertIn(dict(self._halves())["被夸"], capture[0]["messages"][0]["content"])

    def test_a_hit_outside_the_window_does_not_ban(self):
        """窗口外的旧账不算：先记 2 次，再空 12 轮，那两次已经滑出去了。"""
        self._anchors_home(list(self.FIVE))
        self._card_seed("被夸", "才知道啊", 2)
        for _ in range(int(conversation.load_assembly()["anchors"]["repeat"]["window"])):
            usage.append(False, "今天挺好的", "")
        capture = []
        got = self._answer(captured=capture)
        self.assertEqual([], got["anchors"]["banned_by_repeat"])
        self.assertIn(dict(self._halves())["被夸"], capture[0]["messages"][0]["content"])

    def test_repeat_window_and_max_come_from_the_assembly_file(self):
        """改数据 → 判定跟着变：window 收到 3、max 1 → 同一张卡只出现过 1 次也禁。"""
        self._anchors_home(list(self.FIVE))
        conf = conversation.load_assembly()
        conf["anchors"]["repeat"] = {"window": 3, "max": 1}
        self._card_seed("被夸", "才知道啊", 1)
        capture = []
        got = self._answer(assembly=conf, captured=capture)
        self.assertEqual(["被夸"], got["anchors"]["banned_by_repeat"])
        self.assertNotIn(dict(self._halves())["被夸"], capture[0]["messages"][0]["content"])

    def test_ledger_rows_without_id_never_ban_a_card(self):
        """老行读不到 id：当它没命中过（猜一个 id 回去，会把从没出现过的卡禁掉）。"""
        self._anchors_home(list(self.FIVE))
        for _ in range(5):
            usage.append(True, "这个啊又来了", "不想聊")        # 老形状：没有 id / phrase
        got = self._answer()
        self.assertEqual([], got["anchors"]["banned_by_repeat"])

    def test_broken_repeat_data_is_reported_not_defaulted(self):
        """anchors.repeat 缺键 / 类型不对 / max 大过 window → ValueError，并指名是哪一项。"""
        for name, value, needle in (("window", None, "anchors.repeat.window"),
                                    ("max", "两次", "anchors.repeat.max"),
                                    ("max", 99, "anchors.repeat.max")):
            conf = conversation.load_assembly()
            repeat_conf = conf["anchors"]["repeat"]
            if value is None:
                repeat_conf.pop(name)
            else:
                repeat_conf[name] = value
            with self.assertRaises(ValueError) as caught:
                self._answer(assembly=conf)
            self.assertIn(needle, str(caught.exception),
                          "缺 %s 时没有指名报错：%s" % (name, caught.exception))

    # —— 17) 照抄审计（两级）————————————————————

    def test_a_copied_example_is_rewritten_once(self):
        """回话与某张卡的 example 连续重合 ≥ 阈值 → 报台词：**发一次重写请求**，改好就发新稿。"""
        self._anchors_home(list(self.FIVE))
        copied = "嗯……行了行了，我又不是玻璃做的。"
        rewrite = "我哪有那么脆，别瞎操心。"
        capture = []
        got = self._answer(reply=copied, transport=_script(copied, rewrite, captured=capture))
        threshold = int(conversation.load_assembly()["anchors"]["audit"]["example_overlap"])
        self.assertEqual("example", got["audit"]["kind"])
        self.assertEqual("rewritten", got["audit"]["decision"])
        self.assertEqual("被关心", got["audit"]["card_id"])
        self.assertGreaterEqual(got["audit"]["overlap"], threshold)
        self.assertEqual(rewrite, got["reply"], "改好了却没发改写后的那句")
        self.assertEqual(2, len(capture), "该有两次模型请求：装配一次、重写一次")
        self.assertIn("【原文】", capture[1]["messages"][0]["content"],
                      "第二次请求不是那套重写指令（organs/mouth/rewrite.py 的 prompt）")
        self.assertIn(copied, capture[1]["messages"][0]["content"], "重写请求里没有那一句原文")
        row = usage.read()["rows"][-1]
        guard = conversation.load_assembly()["anchors"]["guard"]
        self.assertEqual(usage.opening(rewrite, guard["opening_chars"]), row["opening"],
                         "账本记的开头不是她真说出口的那一句（换过稿之后要按新稿数）")
        self.assertFalse(row["hit"], "改稿之后她没念任何口癖，账上却记成命中")

    def test_a_rewrite_that_still_copies_is_dropped(self):
        """改了一次仍与 example 连续重合 ≥ 阈值 → 这一句弃掉（不发出去、不落账）。"""
        self._anchors_home(list(self.FIVE))
        copied = "行了行了，我又不是玻璃做的。"
        again = "行了行了，我又不是铁打的。"
        capture = []
        got = self._answer(reply=copied, transport=_script(copied, again, captured=capture))
        self.assertEqual("example", got["audit"]["kind"])
        self.assertEqual("dropped", got["audit"]["decision"])
        self.assertEqual("", got["reply"], "改了还是照抄，却照样发了出去")
        self.assertEqual([], got["chunks"])
        self.assertEqual(2, len(capture), "重写那一次该发出去过")
        self.assertEqual(0, usage.read()["rounds"], "没说出口的轮不该记账")

    def test_a_rewrite_that_cannot_be_read_is_dropped(self):
        """重写回来原样抄一遍（accept 判「等于没改」）→ 按改不动处置：这一句不发。"""
        self._anchors_home(list(self.FIVE))
        copied = "嗯……行了行了，我又不是玻璃做的。"
        got = self._answer(reply=copied, transport=_script(copied, copied))
        self.assertEqual("dropped", got["audit"]["decision"])
        self.assertEqual("", got["reply"])
        self.assertTrue(got["audit"]["card_id"])

    def test_a_phrase_hit_passes_and_is_only_recorded(self):
        """只命中 phrase（≤4 字那一档）→ 放行、只记账：不判废、不改写、不发第二次请求。"""
        self._anchors_home(list(self.FIVE))
        reply = "才知道什么呀，我才不承认。"
        capture = []
        got = self._answer(reply=reply, captured=capture)
        self.assertEqual(1, len(capture), "只命中 phrase 却发了第二次模型请求")
        self.assertEqual("pass", got["audit"]["decision"])
        self.assertEqual("phrase", got["audit"]["kind"])
        self.assertEqual("被夸", got["audit"]["card_id"])
        self.assertEqual(reply, got["reply"])
        row = usage.read()["rows"][-1]
        self.assertTrue(row["hit"])
        self.assertEqual("被夸", row["id"], "命中要带 id（近轮禁用靠它数次数）")
        self.assertEqual("才知道啊", row["phrase"])

    def test_no_hit_leaves_an_honest_empty_audit(self):
        self._anchors_home(list(self.FIVE))
        got = self._answer(reply="今天风挺大的，窗户响了一夜。")
        self.assertEqual({"decision": "pass", "kind": "none", "card_id": "", "overlap": 0},
                         got["audit"])

    def test_without_anchors_the_audit_is_empty_and_asks_nothing(self):
        """没配锚句：没有例句可比，审计如实写 none，也不多发一次请求。"""
        self._anchors_home()
        capture = []
        got = self._answer(reply="今天风挺大的。", captured=capture)
        self.assertEqual(1, len(capture))
        self.assertEqual({"decision": "pass", "kind": "none", "card_id": "", "overlap": 0},
                         got["audit"])

    def test_broken_audit_data_is_reported_not_defaulted(self):
        """anchors.audit 缺键 / 类型不对 → ValueError，并指名是哪一项。"""
        for name, value, needle in (("example_overlap", None, "anchors.audit.example_overlap"),
                                    ("example_overlap", "六个字", "anchors.audit.example_overlap"),
                                    ("example_overlap", True, "anchors.audit.example_overlap")):
            conf = conversation.load_assembly()
            audit_conf = conf["anchors"]["audit"]
            if value is None:
                audit_conf.pop(name)
            else:
                audit_conf[name] = value
            with self.assertRaises(ValueError) as caught:
                self._answer(assembly=conf)
            self.assertIn(needle, str(caught.exception),
                          "缺 %s 时没有指名报错：%s" % (name, caught.exception))

    def test_a_missing_repeat_or_audit_section_is_reported(self):
        for name in ("repeat", "audit"):
            conf = conversation.load_assembly()
            conf["anchors"].pop(name)
            with self.assertRaises(ValueError) as caught:
                self._answer(assembly=conf)
            self.assertIn("anchors.%s" % name, str(caught.exception))


    # —— 场合 ————————————————————————————————

    def _scene_book(self):
        """场合那一段的标题与三句话：都从装配说明里读，测试里一个字的文案都没有。"""
        conf = conversation.load_assembly()
        return conf["sections"]["scene"]["label"], conf["scene"]["lines"]

    def _scene_block(self, captured):
        label, _ = self._scene_book()
        return _section(captured[0]["messages"][0]["content"], label)

    def test_group_scene_says_the_group_line(self):
        """scene=group：提示词里「场合」段是群里那一句，另两句一个字都不出现。"""
        label, lines = self._scene_book()
        capture = []
        got = self._answer(scene=brain_model.SCENE_GROUP, captured=capture)
        self.assertEqual({"value": brain_model.SCENE_GROUP}, got["scene"])
        self.assertIn(label, capture[0]["messages"][0]["content"], "「场合」那一段没进提示词")
        block = self._scene_block(capture)
        self.assertEqual(lines[brain_model.SCENE_GROUP], block.strip().splitlines()[0])
        for name in (brain_model.SCENE_PRIVATE, brain_model.SCENE_UNKNOWN):
            self.assertNotIn(lines[name], capture[0]["messages"][0]["content"],
                             "别处的场合那句也进来了：%s" % name)

    def test_private_scene_says_the_private_line(self):
        label, lines = self._scene_book()
        capture = []
        got = self._answer(scene=brain_model.SCENE_PRIVATE, captured=capture)
        self.assertEqual({"value": brain_model.SCENE_PRIVATE}, got["scene"])
        self.assertEqual(lines[brain_model.SCENE_PRIVATE],
                         self._scene_block(capture).strip().splitlines()[0])
        self.assertNotIn(lines[brain_model.SCENE_GROUP], capture[0]["messages"][0]["content"])

    def test_a_missing_or_bogus_scene_is_unknown_not_private(self):
        """不传 / 空白 / 非法都落成 unknown（**不猜成私聊**：猜错就是把私事在群里说出去了）。"""
        label, lines = self._scene_book()
        for value in (None, "", "   ", "公屏", 123):
            capture = []
            got = self._answer(scene=value, captured=capture)
            self.assertEqual({"value": brain_model.SCENE_UNKNOWN}, got["scene"],
                             "scene=%r 没落成 unknown" % (value,))
            self.assertEqual(lines[brain_model.SCENE_UNKNOWN],
                             self._scene_block(capture).strip().splitlines()[0],
                             "scene=%r 时「场合」不是「不知道这是哪儿」那一句" % (value,))

    def test_the_three_scenes_reach_the_brain(self):
        """三态各打一次：脑里今日两条（人和她）的 scene 都等于这一轮给的（不传就是 unknown）。"""
        cases = (("group", brain_model.SCENE_GROUP, brain_model.SCENE_GROUP),
                 ("private", brain_model.SCENE_PRIVATE, brain_model.SCENE_PRIVATE),
                 ("none", None, brain_model.SCENE_UNKNOWN))
        for name, scene, expected in cases:
            brain = BrainStore(self.tmp / ("scene-" + name) / "brain")
            self._answer(scene=scene, brain=brain)
            rows = brain.today_entries(_at(5))
            self.assertEqual(2, len(rows), "这一轮该落两条（人一句、她一句）：%r" % (rows,))
            self.assertEqual([expected, expected], [row.get("scene") for row in rows],
                             "scene=%r 没落到脑里（两条还不一致）" % (scene,))

    def test_a_private_memory_is_not_retellable_in_a_group_but_is_in_private(self):
        """group 时脑把 audience=私密 的那条打 retellable=False + retell_note，两栏原样透出。

        那一轮的记忆离这一轮 15 分钟（脑的合并窗口 10 分钟）：同一个人窗口内的两条会被并成
        一条，并的时候 audience 也一起被这一轮自己的原话盖掉——那是脑的规矩，这里不去动它。
        """
        self.brain.ingest(who="3425635473", text=MEMORY_TEXT, ts=_at(0),
                          audience=brain_model.AUDIENCE_PRIVATE)
        got = self._answer(scene=brain_model.SCENE_GROUP, now=_at(15))
        row = next(one for one in got["recalled"] if one["text"] == MEMORY_TEXT)
        self.assertIs(False, row["retellable"], "群里这一条没被标成不可复述")
        self.assertTrue(row["retell_note"], "retell_note 空着：判据没透出来")

        brain = BrainStore(self.tmp / "private" / "brain")
        brain.ingest(who="3425635473", text=MEMORY_TEXT, ts=_at(0),
                     audience=brain_model.AUDIENCE_PRIVATE)
        got = self._answer(scene=brain_model.SCENE_PRIVATE, now=_at(15), brain=brain)
        row = next(one for one in got["recalled"] if one["text"] == MEMORY_TEXT)
        self.assertTrue(row["retellable"], "私聊里这一条该是可复述的")
        self.assertNotIn("retell_note", row, "私聊里不该有「不可复述」那条理由")

    def test_assembly_without_scene_is_reported(self):
        """数据里缺「场合」那一整节就报，不拿默认话兜底（兜底就成第二处真源）。"""
        conf = conversation.load_assembly()
        conf.pop("scene")
        with self.assertRaises(ValueError) as caught:
            self._answer(assembly=conf)
        self.assertIn("scene", str(caught.exception))

    def test_scene_lines_missing_one_scene_is_reported(self):
        """三句话缺一句就报，并点名缺的是哪一句。"""
        conf = conversation.load_assembly()
        conf["scene"]["lines"].pop(brain_model.SCENE_GROUP)
        with self.assertRaises(ValueError) as caught:
            self._answer(assembly=conf)
        self.assertIn(brain_model.SCENE_GROUP, str(caught.exception))

    def test_scene_cap_may_shrink_but_not_grow(self):
        """sections.scene.cap 可以更小，不许超过 200：超了当场报，正好 200 照常装配。"""
        conf = conversation.load_assembly()
        conf["sections"]["scene"]["cap"] = conversation.SCENE_CAP_MAX + 1
        with self.assertRaises(ValueError):
            self._answer(assembly=conf)
        conf["sections"]["scene"]["cap"] = conversation.SCENE_CAP_MAX
        got = self._answer(assembly=conf)
        caps = [row["cap"] for row in got["assembly"]["sections"] if row["key"] == "scene"]
        self.assertEqual([conversation.SCENE_CAP_MAX], caps)

    # —— 18) 这一小段：段是刚刚这一小段，recent 是更早的那 12 轮 ————————————————

    def _segment_body(self, system: str) -> str:
        """提示词里「这一小段」那一节的正文档（标题从数据里现读，不写死）。"""
        label = conversation.load_assembly()["sections"]["segment"]["label"]
        return _section(system, label).strip("\n")

    def test_segment_lines_reach_the_prompt_in_order(self):
        """段三行（同一个人）→ 那节逐行出现、顺序不变；返回体 assembly.sections 里有 segment。"""
        rows = [{"speaker": "阿黄", "text": "我妈住院了"},
                {"speaker": "阿黄", "text": "明天要做手术"},
                {"speaker": "阿黄", "text": "我不太敢一个人去"}]
        capture = []
        got = self._answer(captured=capture, segment=rows)
        system = capture[0]["messages"][0]["content"]
        self.assertEqual(["阿黄：我妈住院了", "阿黄：明天要做手术", "阿黄：我不太敢一个人去"],
                         self._segment_body(system).splitlines(), "段的行没按原顺序逐行进提示词")
        keys = [row["key"] for row in got["assembly"]["sections"]]
        self.assertIn("segment", keys, "返回体的 assembly.sections 里没有 segment 这一节")
        order = conversation.load_assembly()["order"]
        self.assertLess(order.index("recent"), order.index("segment"), "段没排在 recent 之后")

    def test_both_segment_shapes_normalize_the_same(self):
        """两种形状（对象 / 二元组）归一成同一份 [(谁, 说了什么)]；没给就是 None。"""
        pairs = [("阿黄", "我妈住院了"), ("小明", "我陪你去吧")]
        self.assertEqual(pairs, conversation.normalize_segment([["阿黄", "我妈住院了"],
                                                               ["小明", "我陪你去吧"]]))
        self.assertEqual(pairs, conversation.normalize_segment([{"speaker": "阿黄", "text": "我妈住院了"},
                                                               {"speaker": "小明", "text": "我陪你去吧"}]))
        self.assertEqual(pairs, conversation.normalize_segment(pairs), "归一化不幂等")
        self.assertIsNone(conversation.normalize_segment(None), "没给段该是 None")

    def test_empty_segment_writes_an_honest_line(self):
        """段空着 / 没给就写一句实话：这一节不许留白。"""
        for case in (None, []):
            with self.subTest(segment=case):
                capture = []
                self._answer(captured=capture, segment=case)
                body = self._segment_body(capture[0]["messages"][0]["content"])
                self.assertEqual(conversation.NO_SEGMENT, body, "段空着时那一节留白了")

    def test_group_notice_only_for_a_group_segment_with_two_speakers(self):
        """群聊且段里 ≥2 人才接 group 那句（接在段尾）；私聊 / 单人 / unknown 一律不加。"""
        notice = conversation.load_assembly()["segment"]["notice"][brain_model.SCENE_GROUP]
        two = [["阿黄", "我妈住院了"], ["小明", "我陪你去吧"]]
        one = [["阿黄", "我妈住院了"], ["阿黄", "明天要做手术"]]
        cases = [(brain_model.SCENE_GROUP, two, True), (brain_model.SCENE_GROUP, one, False),
                 (brain_model.SCENE_PRIVATE, two, False),
                 (brain_model.SCENE_UNKNOWN, two, False)]
        for scene, rows, wanted in cases:
            with self.subTest(scene=scene, wanted=wanted):
                capture = []
                self._answer(captured=capture, scene=scene, segment=rows)
                body = self._segment_body(capture[0]["messages"][0]["content"])
                if wanted:
                    self.assertIn(notice, body, "群聊双人段没接那句话")
                    self.assertTrue(body.endswith(notice), "那句话没接在段尾")
                else:
                    self.assertNotIn(notice, body, "这一态不该加那句话")

    def test_segment_notice_text_comes_from_the_data(self):
        """那句话的文案在 assembly.json 里：换一份内存里的装配，提示词跟着变。"""
        conf = conversation.load_assembly()
        conf["segment"]["notice"][brain_model.SCENE_GROUP] = "他们在聊，你可以先听着。"
        capture = []
        self._answer(assembly=conf, scene=brain_model.SCENE_GROUP, captured=capture,
                     segment=[["阿黄", "我妈住院了"], ["小明", "我陪你去吧"]])
        system = capture[0]["messages"][0]["content"]
        self.assertIn("他们在聊，你可以先听着。", system, "那句话是代码里写死的，不跟着数据走")
        self.assertNotIn(conversation.load_assembly()["segment"]["notice"][brain_model.SCENE_GROUP],
                         system, "出厂那句还在：两份文案同时进了提示词")

    def test_segment_section_name_and_cap_come_from_the_data(self):
        """标题与上限同样来自数据：改标题、压到 12 字，装配立刻跟着变。"""
        conf = conversation.load_assembly()
        conf["sections"]["segment"]["label"] = "刚才那几个"
        conf["sections"]["segment"]["cap"] = 12
        capture = []
        got = self._answer(assembly=conf, captured=capture,
                           segment=[["阿黄", "我妈住院了"], ["小明", "我陪你去吧"]])
        system = capture[0]["messages"][0]["content"]
        self.assertIn("[刚才那几个]", system, "段那节的标题是代码里写死的")
        row = [item for item in got["assembly"]["sections"] if item["key"] == "segment"][0]
        self.assertEqual(12, row["cap"])
        self.assertEqual(12, row["chars"], "上限改了，段那节没跟着截断")

    def test_segment_notice_survives_the_section_cap(self):
        """段正文顶到 cap 时，段尾那句话必须还在（先给 notice 留位置，再截正文）。"""
        conf = conversation.load_assembly()
        notice = conf["segment"]["notice"][brain_model.SCENE_GROUP]
        conf["sections"]["segment"]["cap"] = len(notice) + 3
        capture = []
        got = self._answer(assembly=conf, scene=brain_model.SCENE_GROUP, captured=capture,
                           segment=[["阿黄", "前" * 60], ["小明", "后" * 60]])
        system = capture[0]["messages"][0]["content"]
        body = self._segment_body(system)
        self.assertTrue(body.endswith(notice), "正文顶到 cap 时那句话被一起锯掉了：%r" % (body,))
        row = [item for item in got["assembly"]["sections"] if item["key"] == "segment"][0]
        self.assertEqual(row["cap"], row["chars"], "这一节本该顶到 cap")

    def test_segment_and_recent_do_not_mix(self):
        """3 轮 recent + 3 行段：recent 那节只有那 3 轮（6 行），段那节只有那 3 行。"""
        root = self._history(3, "recent_segment")
        rows = [["阿黄", "我妈住院了"], ["小明", "我陪你去吧"], ["阿黄", "谢谢你"]]
        capture = []
        self._answer(captured=capture, recent_root=root, segment=rows)
        system = capture[0]["messages"][0]["content"]
        recent_body = _section(system, "最近几轮").strip("\n")
        segment_body = self._segment_body(system)
        self.assertEqual(6, len(recent_body.splitlines()), "3 轮 recent 该是 6 行（一问一答）")
        self.assertEqual(["阿黄：我妈住院了", "小明：我陪你去吧", "阿黄：谢谢你"],
                         segment_body.splitlines(), "段那节的行数或内容不对")
        for index in range(3):
            self.assertIn("第%d句" % index, recent_body, "recent 那少了一轮")
            self.assertNotIn("第%d句" % index, segment_body, "更早的轮混进了段那节")
        for line in segment_body.splitlines():
            self.assertNotIn(line, recent_body, "段里的一行跑进了 recent 那节：%r" % (line,))

    def test_bad_segment_shape_raises(self):
        """形状坏就当场 ValueError（不兜底）：不是 list / 元素不对 / speaker 空 / text 空。"""
        bad = ["阿黄：我妈住院了",
               {"speaker": "阿黄", "text": "我妈住院了"},
               [["阿黄"]],
               [["阿黄", "我妈住院了", "多一条"]],
               [[1, "我妈住院了"]],
               [["", "我妈住院了"]],
               [{"speaker": "阿黄"}],
               [{"speaker": "阿黄", "text": "   "}],
               [["阿黄", 42]]]
        for case in bad:
            with self.subTest(segment=case):
                with self.assertRaises(ValueError):
                    self._answer(segment=case)

    # —— 18) 四个旋钮：心的读数真进嘴 ————————————————

    def _heart_state(self, name: str, *, mood_now: float, energy_value: float,
                     at: str = "") -> Path:
        """一份心状态：两个状态文件都写在这一刻（与 api 侧 _double_low 同一种写法）。

        写文件而不是 apply_event：心情要落在一个精确的数上（33 落在低档里、又高过「双低」的
        30），精力也只在这一处给。updated_at 就是这一轮的时刻，两笔都不结算。
        """
        root = self._heart_root(name)
        when = at or _at(5)
        (root / mood.MOOD_FILE).write_text(
            json.dumps({"now": mood_now, "baseline": 60.0, "updated_at": when}),
            encoding="utf-8")
        (root / energy.ENERGY_FILE).write_text(
            json.dumps({"value": energy_value, "updated_at": when, "tier": "一般"}),
            encoding="utf-8")
        return root

    def test_low_mood_and_low_energy_narrow_the_round_and_switch_both_valves(self):
        """心情 0.33 + 精力 20：上限收紧、玩笑句与问句整句删掉，提示词与回包都写明。

        这份心数据特意高过「双低」的 30（心情 33：低，但不双低）：双低是 SILENT，由出站闸
        整轮拦下，不归装配这一层；判成「少说」才是 no_question 那一条该走的路。
        """
        rules = conversation.load_assembly()["tone_rules"]
        root = self._heart_state("low_both", mood_now=33.0, energy_value=20.0)
        keep_line = "今天有点累。"
        joke_line = "哈哈，这有什么。"
        ask_line = "你还要去吗？"
        reply = "%s\n%s\n%s" % (keep_line, joke_line, ask_line)
        capture = []
        got = self._answer(reply=reply, heart_root=root, captured=capture)
        tone = got["tone"]
        tier_chars = got["assembly"]["reply_budget"]["tier_chars"]
        self.assertEqual(min(tier_chars, rules["max_chars_low"]), tone["max_chars"],
                         "心情凉/精力低没收紧上限")
        self.assertTrue(tone["no_question"], "判成「少说」却没关问句")
        self.assertTrue(tone["ban_joke"], "心情凉却没禁玩笑")
        self.assertIn("已接入", tone["heart_source"], "心读数没真传进嘴：%s" % tone["heart_source"])
        self.assertNotIn("未接入", tone["heart_source"])
        system = capture[0]["messages"][0]["content"]
        self.assertIn("已接入", system, "提示词的口径行还写着「未接入」")
        self.assertIn("这一次回复不超过 %d 个字" % tone["max_chars"], system,
                      "提示词里的字数上限没跟着收紧")
        self.assertIn(rules["ban_joke_note"], system, "禁玩笑那句没进提示词")
        self.assertEqual(keep_line, got["reply"], "该留的那一句被动了，或该删的没删干净")
        row = conversation.read_tone_log()[-1]
        self.assertEqual(1, row["dropped_jokes"], "玩笑句该整句删掉")
        self.assertEqual(1, row["dropped_questions"], "问句该整句删掉")
        self.assertTrue(row["ban_joke"])
        self.assertTrue(row["no_question"])

    def test_a_neutral_round_keeps_every_byte_and_the_tier_budget(self):
        """中性一轮：上限就是档位预算、两个开关都关、回话一个字都不动。

        这一轮得真判成「说」：熟稔过线（数字号那个人、真建档）+ 心那边一条 replied 事件
        （没聊完那个意图过线）。少一样门就判「少说」，no_question 会是 true——那是另一条路，
        与「中性行为一字不改」不是同一条判据。
        """
        self.brain.interact("3425635473", ts=_at(0))
        heart_events.deliver(heart_events.KIND_REPLIED, who="3425635473",
                             root=self.tmp / "heart", now=_at(4))
        rules = conversation.load_assembly()["tone_rules"]
        capture = []
        got = self._answer(who="3425635473", captured=capture)
        tone = got["tone"]
        budget = conversation.load_assembly()["reply"]["budget_by_tier"][
            got["assembly"]["reply_budget"]["tier"]]
        self.assertEqual(budget, tone["max_chars"], "中性档该照旧用档位预算")
        self.assertEqual(budget, got["assembly"]["reply_budget"]["chars"])
        self.assertFalse(tone["no_question"])
        self.assertFalse(tone["ban_joke"])
        self.assertFalse(tone["truncated"])
        self.assertEqual(SENTENCE, got["reply"], "中性一轮把她的话改了")
        system = capture[0]["messages"][0]["content"]
        self.assertNotIn(rules["ban_joke_note"], system, "中性一轮不该加禁玩笑那句")
        self.assertIn("这一次回复不超过 %d 个字" % budget, system)

    def test_a_long_reply_is_cut_at_a_sentence_end_not_in_the_middle(self):
        """低心情那一轮按上限在句末截：留下的最后一个是句末标点，绝不留半句。"""
        rules = conversation.load_assembly()["tone_rules"]
        root = self._heart_state("cold_long", mood_now=20.0, energy_value=80.0)
        reply = ("今天下雨了，路上有点滑。"
                 "你问医院那回事，我记得的，是陪你妈去复查对吧。"
                 "中午吃了面，汤有点咸，晚上想喝粥。"
                 "周末要是不下雨，我陪你去一趟。")
        self.assertGreater(len(reply), rules["max_chars_low"], "这段假回复本来就不长，截不出来")
        got = self._answer(reply=reply, heart_root=root)
        self.assertTrue(got["tone"]["truncated"], "超了上限却没截")
        self.assertLess(len(got["reply"]), len(reply))
        self.assertTrue(reply.startswith(got["reply"]), "留下的不是原话的前缀：%r" % got["reply"])
        self.assertIn(got["reply"][-1], str(rules["sentence_end_chars"]), "断在半句上了")
        self.assertLessEqual(len(got["reply"]), got["tone"]["max_chars"])
        row = conversation.read_tone_log()[-1]
        self.assertTrue(row["truncated"], "账上没写截了")
        self.assertEqual(got["tone"]["max_chars"], row["max_chars"])
        self.assertEqual(0, row["dropped_questions"], "这句里没有问句，不该有删问的记录")
        self.assertEqual(0, row["dropped_jokes"], "这句里没有玩笑词，不该有删玩笑的记录")

    def test_the_two_word_lists_and_thresholds_come_from_the_data_file(self):
        """改 tone_rules 就改结果：阈值抬到 0.99 → 中性这一轮也算心情凉；词表换成别的词 → 那句被删。"""
        live = self.tmp / "home" / "mind"
        live.mkdir(parents=True)
        data = conversation.load_assembly()
        data["tone_rules"]["low_mood_below"] = 0.99
        data["tone_rules"]["max_chars_low"] = 12
        data["tone_rules"]["joke_words"] = ["医院"]
        (live / "assembly.json").write_text(json.dumps(data, ensure_ascii=False),
                                            encoding="utf-8")
        got = self._answer(reply=SENTENCE)
        self.assertTrue(got["tone"]["ban_joke"], "阈值改了，判据还是老那个")
        self.assertEqual(12, got["tone"]["max_chars"], "上限没跟着数据走")
        self.assertEqual("今天挺好的。", got["reply"], "换过的玩笑词表没生效")

    def test_a_round_that_is_only_a_question_keeps_the_line_and_says_why(self):
        """整句都该删就留原句：删光了不是「她没说」，账上记 kept_because_empty。"""
        root = self._heart_state("only_question", mood_now=20.0, energy_value=80.0)
        only = "你还要去吗？"
        got = self._answer(reply=only, heart_root=root)
        self.assertTrue(got["tone"]["no_question"])
        self.assertEqual(only, got["reply"], "删空了该留原句，不许把她的话删成沉默")
        row = conversation.read_tone_log()[-1]
        self.assertEqual(1, row["dropped_questions"])
        self.assertTrue(row["kept_because_empty"])

    def test_the_tone_ledger_gets_one_row_per_round_with_every_field(self):
        """每轮一行、字段齐、与回包对得上：两跑两行，一行不多一行不少。"""
        self.brain.interact(WHO, ts=_at(0))
        low = self._heart_state("ledger_low", mood_now=33.0, energy_value=20.0)
        first = self._answer(now=_at(5))
        second = self._answer(now=_at(6), heart_root=low)
        rows = conversation.read_tone_log()
        self.assertEqual(2, len(rows), "两轮该有两行：%r" % (rows,))
        lines = conversation.tone_log_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, len([line for line in lines if line.strip()]), "一轮不止一行")
        for row, got in zip(rows, (first, second)):
            self.assertEqual(set(conversation.TONE_FIELDS), set(row), "账上字段不齐：%r" % (row,))
            self.assertEqual(got["heart"]["mood"]["tier"], row["mood_tier"])
            self.assertEqual(got["tone"]["label"], row["tone_tier"])
            self.assertEqual(got["tone"]["softness"], row["softness"])
            self.assertEqual(got["tone"]["max_chars"], row["max_chars"])
            self.assertEqual(got["tone"]["no_question"], row["no_question"])
            self.assertEqual(got["tone"]["ban_joke"], row["ban_joke"])
            self.assertEqual(got["tone"]["truncated"], row["truncated"])
            self.assertEqual(got["tone"]["heart_source"], row["heart_source"])
            self.assertNotIn("未接入", row["heart_source"], "账上还写着心未接入")
            self.assertTrue(str(row["energy_tier"] or "").strip(), "精力档位是空的")
            self.assertTrue(str(row["address"] or "").strip(), "称呼是空的")
        self.assertNotEqual(rows[0]["max_chars"], rows[1]["max_chars"], "两跑的上限该不一样")

    def test_broken_tone_rules_are_reported_not_defaulted(self):
        """tone_rules 缺阈值 / 上限写成 0 / 词表空 → ValueError，且指名道姓。"""
        cases = ((lambda conf: conf.pop("tone_rules"), "tone_rules"),
                 (lambda conf: conf["tone_rules"].pop("low_mood_below"),
                  "tone_rules.low_mood_below"),
                 (lambda conf: conf["tone_rules"].__setitem__("low_energy_below", "三十"),
                  "tone_rules.low_energy_below"),
                 (lambda conf: conf["tone_rules"].__setitem__("max_chars_low", 0),
                  "tone_rules.max_chars_low"),
                 (lambda conf: conf["tone_rules"].__setitem__("max_chars_neutral", 0),
                  "tone_rules.max_chars_neutral"),
                 (lambda conf: conf["tone_rules"].__setitem__("joke_words", []),
                  "tone_rules.joke_words"),
                 (lambda conf: conf["tone_rules"].__setitem__("joke_words", ["哈哈", ""]),
                  "tone_rules.joke_words"),
                 (lambda conf: conf["tone_rules"].__setitem__("question_chars", " "),
                  "tone_rules.question_chars"),
                 (lambda conf: conf["tone_rules"].pop("ban_joke_note"), "tone_rules.ban_joke_note"),
                 (lambda conf: conf["tone_rules"].pop("rules_note"), "tone_rules.rules_note"))
        for mutate, needle in cases:
            conf = conversation.load_assembly()
            mutate(conf)
            with self.assertRaises(ValueError) as caught:
                self._answer(assembly=conf)
            self.assertIn(needle, str(caught.exception),
                          "数据坏在 %s 时没有指名报错：%s" % (needle, caught.exception))


    # —— 18) 主动开口：她自己起头那一轮 ————————————————

    def test_a_proactive_round_uses_the_proactive_template_and_an_empty_question(self):
        """主动那一轮：提示词换 prompt.proactive（由头 + 理由在里面），user 那一问是空串。

        判据三件：① 用的是主动那份模板（被动那句一个字都不在）；② 由头与理由码逐字进提示词，
        没给理由时用数据里那句顶上（本文件不写这句中文）；③ user 那一问是空串——没有对方的话。
        """
        conf = conversation.load_assembly()
        proactive = conf["prompt"]["proactive"]
        passive = conf["prompt"]["system"]
        capture = []
        got = self._answer(text=SEED, seed=SEED, reason=SEED_REASON, captured=capture)
        prompt = capture[0]["messages"][0]["content"]
        self.assertEqual("proactive", got["mode"])
        self.assertEqual({"text": SEED, "reason": SEED_REASON}, got["seed"])
        self.assertIn(proactive.split("{sections}")[0].strip(), prompt,
                      "提示词没用 prompt.proactive 那份模板")
        self.assertNotIn(passive.split("{sections}")[0].strip(), prompt,
                         "主动那一轮还在用被动的模板")
        self.assertIn(SEED, prompt, "由头没进提示词")
        self.assertIn(SEED_REASON, prompt, "理由码没进提示词")
        self.assertEqual("", capture[0]["messages"][1]["content"],
                         "主动那一轮没有对方的话：user 那一问该是空串")
        # 没给理由码：用数据里那句顶上，读的仍是同一份数据（代码里没有这句中文）
        capture = []
        got = self._answer(text=SEED, seed=SEED, captured=capture)
        self.assertEqual({"text": SEED, "reason": ""}, got["seed"], "没给理由却记下了一个")
        self.assertIn(conf["prompt"]["proactive_reason_missing"],
                      capture[0]["messages"][0]["content"], "没给理由时该用数据那句顶上")
        # 装配的几段照旧：order 一位不动、这一节是「这一段没有别的发言」
        self.assertEqual(conversation.load_assembly()["order"], got["assembly"]["order"])
        label = conf["sections"]["segment"]["label"]
        self.assertEqual(conversation.NO_SEGMENT, _section(prompt, label).strip("\n"),
                         "主动那一轮的段不是「这一段没有别的发言」")

    def test_the_segment_is_ignored_on_a_proactive_round(self):
        """段给了也忽略：这一轮没有别人说话，段进不了提示词（连形状都不判）。"""
        capture = []
        got = self._answer(text=SEED, seed=SEED, captured=capture,
                           segment=[["阿黄", "我妈住院了"]])
        label = conversation.load_assembly()["sections"]["segment"]["label"]
        self.assertEqual(conversation.NO_SEGMENT,
                         _section(capture[0]["messages"][0]["content"], label).strip("\n"),
                         "段被塞进了主动那一轮")
        self.assertEqual(0, got["continuity"]["turns_loaded"])

    def test_a_proactive_round_carries_the_proactive_intent(self):
        """心判决的意图：主动 → INTENT_PROACTIVE，被动照旧 INTENT_PASSIVE；被骂恒 False。

        intent 是「夜里主动 → SILENT」那条硬门生效的前提：给了它，门才判得出两种起头的差别。
        """
        got = self._answer(text=SEED, seed=SEED)
        inp = got["_gate_input"]
        self.assertEqual(heart_model.INTENT_PROACTIVE, inp.intent, "主动那一轮的心意图不是 proactive")
        self.assertFalse(inp.insulted_today, "没人说话，就没有「被骂」")
        passive = self._answer()
        self.assertEqual(heart_model.INTENT_PASSIVE, passive["_gate_input"].intent,
                         "被动那条路被改了")

    def test_a_night_time_proactive_round_is_silenced_by_the_heart(self):
        """同样是她自己起头：白天判「说」，夜里判 SILENT 且理由码是 night（被动夜里只判少说）。"""
        day = self._answer(text=SEED, seed=SEED)
        self.assertNotEqual(heart_model.STATE_SILENT, day["heart"]["engage"]["state"],
                            "白天的主动这一轮不该被判「不说」：%r" % (day["heart"]["engage"],))
        night = self._answer(text=SEED, seed=SEED, now=_at(0, base=NIGHT))
        self.assertEqual(heart_model.STATE_SILENT, night["heart"]["engage"]["state"],
                         "夜里主动该判「不说」：%r" % (night["heart"]["engage"],))
        self.assertEqual(heart_model.REASON_NIGHT, night["heart"]["engage"]["reason"])
        # 被动那条路一字不改：同样在夜里，它只判「少说」
        passive = self._answer(now=_at(0, base=NIGHT))
        self.assertNotEqual(heart_model.STATE_SILENT, passive["heart"]["engage"]["state"],
                            "被动夜里被改成了「不说」：%r" % (passive["heart"]["engage"],))

    def test_a_proactive_round_lands_only_her_own_line(self):
        """落脑只有一条：她说、side=她说。**不许**为凑形状写一条假的人话（由头不是人的原话）。"""
        got = self._answer(text=SEED, seed=SEED)
        rows = self.brain.today_entries(_at(30))
        self.assertEqual([SENTENCE], [row.get("text") for row in rows],
                         "主动那一轮多落了一条（那是她自己编的「对方说的话」）：%r" % (rows,))
        self.assertEqual(brain_model.SIDE_SELF, rows[0].get("side"), "她那句的 side 不对")
        self.assertEqual(1, len(got["memory"]["wrote"]), "这一轮该只落她那一条")
        self.assertEqual(self.store.snapshot().identity.display, got["memory"]["wrote"][0]["who"])
        # recent 照记一行（她这一轮同样是一轮），但 said 是空串：没有对方原话
        ledger = recent.load(turns=conversation.load_assembly()["recent"]["turns"])["turns"]
        self.assertEqual(1, len(ledger), "主动那一轮没记进最近几轮")
        self.assertEqual("", ledger[0]["said"], "由头被记成了「他说了什么」")
        self.assertEqual(SENTENCE, ledger[0]["reply"])
        self.assertEqual("", ledger[0]["msg_id"], "没有人的那条凭据，就不该有 msg_id")

    def test_a_round_with_no_words_out_lands_nothing(self):
        """模型什么都没说（这一层拿到空串）：reply 空、chunks 空、落脑一条都不落。"""
        with mock.patch.object(conversation.llm, "complete", lambda *a, **k: ""):
            got = self._answer(text=SEED, seed=SEED)
        self.assertEqual("", got["reply"], "空的那一轮还是把话交出去了")
        self.assertEqual([], got["chunks"])
        self.assertEqual(0, len(got["memory"]["wrote"]), "没出口的话不该落脑")

    def test_a_missing_proactive_template_is_an_error_not_a_fallback(self):
        """数据缺 prompt.proactive → ValueError 且指名道姓（不兜底成被动那一份）。"""
        conf = conversation.load_assembly()
        conf["prompt"].pop("proactive")
        with self.assertRaises(ValueError) as caught:
            self._answer(text=SEED, seed=SEED, assembly=conf)
        self.assertIn("prompt.proactive", str(caught.exception),
                      "数据坏在这一格却没有点名：%s" % (caught.exception,))
        # 空白也算缺：不许用一句空话把这一轮糊过去
        conf = conversation.load_assembly()
        conf["prompt"]["proactive"] = "   "
        with self.assertRaises(ValueError):
            self._answer(text=SEED, seed=SEED, assembly=conf)

    def test_an_empty_seed_is_an_error(self):
        """给了 seed 却空着：这一轮不是「她想开口」，是调用方写错了（当场报，不悄悄退回被动）。"""
        with self.assertRaises(ValueError) as caught:
            self._answer(text=SEED, seed="   ")
        self.assertIn("seed", str(caught.exception))


class ConversationEventTest(unittest.TestCase):
    """事件接进装配（本轮加的）：识别 → 投心 → 落账，一路上都不许进提示词。

    判据：
    1. **夸**：「你真厉害」（私聊）→ event.kind=praise、applied=True，而且心那边的心情读数
       （mood.read 这一份公开读数，不读私有文件）比调用前**高**——投递真的推了心。
    2. **冷落**：最近三轮她的回话都是「嗯」（用 recent 那本账造出来）→ 这一轮 event.kind=cold、
       applied=True、心情下行。窗口凑不满就是不判——不拿一轮当三轮。
    3. **中性**：「今天下午三点开会」→ event.kind=""（没识别出）、applied=False、reason 非空；
       账本上**仍然有一行**——"什么都没发生"与"被心拒了"在账上分得开。
    4. **被骂两套口径同结论**：带脏话的一句 → event.kind=insult，且**同一轮**的心判决给出
       insulted-today（engage 的硬门）；交回去的 EngageInput.insulted_today 也是同一个布尔。
    5. **mode**：每轮账本行都带 mode；直接调 answer(mode="proactive") 的行是 proactive，
       不给 / 给个别的值一律回落 passive（口径在 events 那一侧，本层不另判）。
    6. **一个字都不进提示词**：抓 system，账上那句 note 与命中的词一个都不出现，
       装配也不因为多了一件事而多一节（order / sections 一位不动）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patches = mock.patch.dict(os.environ, {
            "QQAGENT_HOME": str(self.tmp / "home"),          # 事件账落 <home>/organs/mind
            "HEART_ROOT": str(self.tmp / "heart"),           # 心：事件真的推进这里
            "MOUTH_TRACE_DIR": str(self.tmp / "trace"),
        })
        patches.start()
        self.addCleanup(patches.stop)
        target = self.tmp / "soul"
        shutil.copytree(str(SOUL_FIXTURES / "valid"), str(target))
        self.store = load_soul(target)
        self.brain = BrainStore(self.tmp / "brain")

    def _answer(self, text: str = QUESTION, reply: str = SENTENCE, *, captured: list = None,
                **extra):
        return conversation.answer(text, WHO, store=self.store, brain=self.brain,
                                   now=_at(5), transport=_transport(reply, captured),
                                   config=CONFIG, env=ENV, **extra)

    def _mood_now(self, at: str = "") -> float:
        """心那边的公开读数（mood.read 的 now）：测试不读 mood.json 这种私有文件。"""
        return mood.read(now=at or _at(5))["now"]

    def _round_reply(self, reply: str, at: str) -> None:
        """往 recent 那本账里造一轮（她的回话就是 reply）：冷落窗口数的是她那侧。"""
        recent.append({"ts": at, "who": "主人", "said": "随便说点什么", "reply": reply,
                       "msg_id": ""}, cap=conversation.load_assembly()["recent"]["cap"])

    # —— 1) 夸 ————————————————————————————————

    def test_praise_is_applied_and_lifts_the_mood(self):
        before = self._mood_now()
        got = self._answer("你真厉害", scene="private")
        self.assertEqual("praise", got["event"]["kind"], "夸没识别出来：%r" % (got["event"],))
        self.assertTrue(got["event"]["applied"], "心没接住：%r" % (got["event"],))
        after = self._mood_now()
        self.assertGreater(after, before, "夸了却没上行：%r → %r" % (before, after))
        row = events.rows()[-1]
        self.assertEqual("praise", row["kind"])
        self.assertTrue(row["applied"])

    # —— 2) 冷落（窗口判据）————————————————————————————

    def test_three_bare_replies_make_the_next_round_cold_and_push_the_mood_down(self):
        for index, at in enumerate((_at(1), _at(2), _at(3)), 1):
            self._round_reply("嗯", at)
        before = self._mood_now()
        got = self._answer("嗯", scene="private")
        self.assertEqual("cold", got["event"]["kind"],
                         "三轮都是「嗯」该判冷落：%r" % (got["event"],))
        self.assertTrue(got["event"]["applied"], "心没接住：%r" % (got["event"],))
        after = self._mood_now()
        self.assertLess(after, before, "冷落却没下行：%r → %r" % (before, after))

    def test_two_bare_replies_are_not_enough_for_the_window(self):
        """窗口凑不满就不判（不拿一轮当 N 轮）：两轮「嗯」之后这一轮还是没事件。"""
        for at in (_at(2), _at(3)):
            self._round_reply("嗯", at)
        got = self._answer("嗯", scene="private")
        self.assertEqual("", got["event"]["kind"], "窗口没满却判了冷落：%r" % (got["event"],))
        self.assertFalse(got["event"]["applied"])

    # —— 3) 中性 ————————————————————————————————

    def test_a_neutral_round_writes_one_row_and_says_why(self):
        got = self._answer("今天下午三点开会", scene="private")
        self.assertEqual("", got["event"]["kind"], "中性闲聊不该识别出事件：%r" % (got["event"],))
        self.assertFalse(got["event"]["applied"])
        self.assertTrue(got["event"]["reason"], "没识别出也要说清为什么（reason 不许空）")
        rows = events.rows()
        self.assertEqual(1, len(rows), "没识别出也要落一行：%r" % (rows,))
        self.assertEqual("", rows[0]["kind"])
        self.assertFalse(rows[0]["applied"])
        self.assertTrue(rows[0]["reason"])

    # —— 4) 被骂：两套口径同结论 ————————————————————————

    def test_a_curse_word_is_insult_and_the_gate_says_insulted(self):
        got = self._answer("你这个废物，给我滚开", scene="private")
        self.assertEqual("insult", got["event"]["kind"],
                         "被骂没识别出来：%r" % (got["event"],))
        self.assertEqual(heart_model.REASON_INSULTED, got["heart"]["engage"]["reason"],
                         "同一轮的心判决没走被骂那道硬门：%r" % (got["heart"]["engage"],))
        self.assertIs(True, got["_gate_input"].insulted_today,
                      "交给出站闸的那份输入没带上同一个布尔")

    # —— 5) mode ————————————————————————————————

    def test_every_row_carries_the_mode_of_its_round(self):
        passive = self._answer()
        self.assertEqual("passive", passive["event"]["mode"], "缺省该是 passive")
        self.assertEqual("passive", events.rows()[-1]["mode"])
        proactive = self._answer(mode="proactive")
        self.assertEqual("proactive", proactive["event"]["mode"])
        self.assertEqual("proactive", events.rows()[-1]["mode"])
        wrong = self._answer(mode="随口写的值")
        self.assertEqual("passive", wrong["event"]["mode"], "非法值要回落 passive")
        self.assertEqual("passive", events.rows()[-1]["mode"])

    # —— 6) 不进提示词 ————————————————————————————————

    def test_the_event_never_reaches_the_prompt_and_adds_no_section(self):
        capture = []
        got = self._answer("你真厉害", scene="private", captured=capture)
        system = capture[0]["messages"][0]["content"]
        row = events.rows()[-1]
        self.assertEqual("praise", row["kind"])
        self.assertNotIn(row["note"], system, "事件账上的依据进了提示词：%r" % (row["note"],))
        self.assertNotIn("词表命中", system, "识别口径漏进了提示词")
        self.assertNotIn("evidence", system, "evidence 那一栏漏进了提示词")
        for key in ("event", "note", "evidence"):
            self.assertNotIn(key, got["assembly"]["order"], "装配多了一节：%r" % (key,))
            self.assertNotIn(key, [one["key"] for one in got["assembly"]["sections"]])

    # —— 别名：id 是身份，昵称只是别名 ————————————————

    def test_aliases_reach_the_person_card_without_writing_memory(self):
        """answer(aliases=[…]) 只走人名录：昵称落显示名、群名片进别名表、一条记忆都不多写。"""
        got = self._answer(aliases=["能毕业就是胜利", "小红花的爹"])
        self.assertTrue(got["ok"])
        person = self.brain.lookup(WHO)
        self.assertIsNotNone(person, "号下没有卡：别名没登记进人名录")
        self.assertEqual(WHO, person.get("id"), "身份被别名顶掉了")
        self.assertEqual("能毕业就是胜利", person.get("display"), "昵称没落成显示名")
        self.assertEqual(["小红花的爹"], person.get("aliases"), "群名片没进别名表")
        # 登记别名不写记忆条：这一轮仍然只有他那句与她那句
        self.assertEqual(2, len(got["memory"]["wrote"]))
        self.assertEqual(2, len(self.brain.today_entries()))

    def test_the_person_section_carries_the_alias(self):
        """名片段取到东西，且昵称在里头（称呼）、群名片在别名那一格。"""
        self._answer(aliases=["能毕业就是胜利", "小红花的爹"])
        text = conversation._person_text(self.brain, WHO)
        self.assertTrue(text.strip(), "「关于他」整段是空的")
        self.assertIn("能毕业就是胜利", text, "昵称没进名片段：%r" % (text,))
        self.assertIn("小红花的爹", text, "别名没进名片段：%r" % (text,))

    def test_broken_aliases_never_break_the_round(self):
        """别名形状坏了（字符串 / 数字 / 一表空白 / None / 一表非串）只是不登记，绝不抛。"""
        for bad in ("小红花", 123, [], ["  ", None, 7], [""], None, {"a": "b"}):
            got = self._answer(aliases=bad)
            self.assertTrue(got["ok"], "别名形状坏了把整轮弄坏了：%r" % (bad,))
        # 没登记过任何别名：卡还在（ingest 建的），只是别名表空着
        person = self.brain.lookup(WHO)
        self.assertIsNotNone(person)
        self.assertEqual([], person.get("aliases") or [], "坏形状的别名被登记了")

    def test_aliases_under_an_unusable_who_are_dropped(self):
        """认不出是谁（who 不是号）就别登记别名：别名跟着身份走，不按昵称建卡。"""
        for bad in ("能毕业就是胜利", "", "  ", "aiocqhttp:12"):
            got = conversation.answer(QUESTION, bad, store=self.store, brain=self.brain,
                                      now=_at(5), transport=_transport(SENTENCE),
                                      config=CONFIG, env=ENV, aliases=["能毕业就是胜利"])
            self.assertTrue(got["ok"])
        people = self.brain._people_data().get("people", {})
        self.assertEqual([], [one for one in people if not one.isdigit()],
                         "按昵称建了卡：%r" % (sorted(people),))


if __name__ == "__main__":
    unittest.main()
