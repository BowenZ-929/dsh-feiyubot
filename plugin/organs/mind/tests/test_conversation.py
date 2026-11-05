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

全部注入假 transport：一个字节都不发真网络。心 / 嘴 / 装配的临时根都经环境变量切走，
仓库里的真数据一个字节不写。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from organs.brain import BrainStore
from organs.heart import model as heart_model
from organs.heart import mood
from organs.mind import conversation
from organs.soul import load as load_soul

PLUGIN = Path(__file__).resolve().parents[3]
SOUL_FIXTURES = PLUGIN / "organs" / "soul" / "fixtures"
MOUTH_DATA = PLUGIN / "organs" / "mouth" / "data"

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
        self.assertEqual("偏热", warm["heart"]["voice"]["tone"])
        self.assertEqual("冰点", chilly["heart"]["voice"]["tone"])
        self.assertNotEqual(warm["assembly"]["reply_budget"]["tier"],
                            chilly["assembly"]["reply_budget"]["tier"])

    def _heart_root(self, name: str) -> Path:
        root = self.tmp / ("heart_" + name)
        root.mkdir()
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


if __name__ == "__main__":
    unittest.main()
