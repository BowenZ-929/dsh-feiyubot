"""「要我记事」那条线的验收：认出来就落卡、认不出时刻就反问、闲聊一个字都不动。

判据（每条都对应交付里的一句话）：
1. **十分钟后叫我** → 工作台多一张卡，due = 现在 + 10 分钟（临时 home + 真 add_task），
   回包 remind.ok 为真、card_id 非空；
2. **明天中午提醒我** → due 是次日 12:00；
3. **回头提醒我**（有意图没时间）→ **不建卡**、回一句反问（断言工作台卡数不变）；
4. **今天天气怎么样** → 不建卡、remind 那一格根本不出现（handle 回 None）；
5. **到点** → 日子线的 pending_task 报得出这张卡（reason_code 与 card_id 对得上）；
6. **词表 / 文案改数据** → 行为跟着变（代码里没有这些词）；
7. **数据坏（缺键）** → ValueError 并点名缺的是哪一格。
8. **装配那一头**：answer() 认作记事的那一轮，嘴里那句就是回执、回包多 remind 一格；
   普通闲聊那一轮没有这一格。
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
from organs.day import proactive
from organs.home import cards as home_cards
from organs.hand import store as hand_store
from organs.mind import conversation, remind
from organs.soul import load as load_soul

PLUGIN = Path(__file__).resolve().parents[3]
SOUL_FIXTURES = PLUGIN / "organs" / "soul" / "fixtures"
CONFIG = {"provider": "fake", "model": "fake-model", "base_url": "http://example.invalid/v1",
          "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 1000, "retries": 0,
          "temperature": 0.0, "max_tokens": 200}
ENV = {"QQAGENT_LLM_KEY": "test-key"}
WHO = "3425635473"
#: 固定的「现在」：用例不看本机时钟（与手那边的 helpers.NOW 同一个时刻）。
NOW = "2026-11-06T09:00:00+09:00"
TEN_MINUTES_LATER = "2026-11-06T09:10:00+09:00"
TOMORROW_NOON = "2026-11-07T12:00:00+09:00"


def _transport(reply: str):
    """假 transport：一个字节都不发真网络。"""
    def send(url, headers, payload, timeout):
        body = {"choices": [{"message": {"content": reply}}]}
        return 200, json.dumps(body, ensure_ascii=False).encode("utf-8")
    return send


class RemindTest(unittest.TestCase):
    """记事这一条线：卡落在哪、嘴说的是什么、日子线认不认。"""

    def test_confirmed_intent_without_task_or_time_asks_without_writing(self):
        with mock.patch.object(remind.hand_actions, "add_task") as add:
            no_task = remind.handle("", intent={"when": ["十分钟后"], "task": ""}, now=NOW)
            no_time = remind.handle("", intent={"when": [], "task": "喝水"}, now=NOW)
        self.assertEqual("no_task", no_task["why"])
        self.assertEqual("no_time", no_time["why"])
        add.assert_not_called()

    def test_semantic_absolute_date_and_task_are_not_mixed_in_time_parser(self):
        with mock.patch.object(remind.hand_actions, "add_task", return_value={"state": "done", "card_id": "test"}) as add:
            result = remind.handle("", who=WHO, now=NOW, intent={"when": ["明天", "下午三点"], "task": "取快递"})
        self.assertTrue(result["ok"])
        self.assertEqual("2026-11-07T15:00:00+09:00", add.call_args.kwargs["due"])
        self.assertEqual("取快递", add.call_args.kwargs["title"])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # 手的实盘根：家按兄弟目录换算（home 与 hand 并排，与手那边同一条口径）
        self.hand_root = self.tmp / "hand"
        self.hand_root.mkdir()
        self.home_root = hand_store.home_root(self.hand_root)
        self.day_root = self.tmp / "day"

    # —— 工具 ————————————————————————————————

    def _handle(self, text: str, *, root=None, config=None):
        return remind.handle(text, who=WHO, now=NOW, config=config,
                             root=self.hand_root if root is None else root)

    def _tasks(self):
        return home_cards.list_tasks(root=self.home_root)

    def _data(self):
        return remind.load_config()

    # —— 1) 十分钟后 ——————————————————————

    def test_ten_minutes_later_lands_one_card_due_in_ten_minutes(self):
        got = self._handle("十分钟后叫我")
        self.assertTrue(got["ok"], "真落成了卡才算记着：%r" % (got,))
        self.assertTrue(got["parsed"])
        self.assertEqual(TEN_MINUTES_LATER, got["due"], "due 要是当前 + 10 分钟")
        self.assertEqual(got["say"], self._data()["say"]["ok"].replace(
            "{name}", got["title"]).replace(
            "{when}", datetime.fromisoformat(TEN_MINUTES_LATER).strftime(
                self._data()["say"]["when_format"])))
        tasks = self._tasks()
        self.assertEqual(1, len(tasks), "工作台该多一张卡")
        self.assertEqual(TEN_MINUTES_LATER, tasks[0]["params"]["due"], "盘上那张卡的 due 不对")
        self.assertEqual(got["card_id"], tasks[0]["card_id"], "回执里的 card_id 就是盘上那张")
        self.assertEqual(remind.PACKET_KEYS, tuple(remind.packet(got)), "回包那一格的键是冻结的")

    # —— 2) 明天中午 ————————————————————

    def test_tomorrow_noon_is_the_next_day_at_twelve(self):
        got = self._handle("明天中午提醒我")
        self.assertTrue(got["ok"], got)
        self.assertEqual(TOMORROW_NOON, got["due"], "明天中午 = 次日 12:00")
        self.assertEqual(TOMORROW_NOON, self._tasks()[0]["params"]["due"])

    # —— 3) 有意图没时间 ——————————————————

    def test_intent_without_time_asks_and_lands_no_card(self):
        before = len(self._tasks())
        got = self._handle("回头提醒我")
        self.assertIsNotNone(got, "有意图就该有这一格（反问也是一种回应）")
        self.assertFalse(got["ok"], "没时间就不许说记着了")
        self.assertFalse(got["parsed"])
        self.assertIsNone(got["due"])
        self.assertIsNone(got["card_id"])
        self.assertEqual(remind.WHY_NO_TIME, got["why"])
        self.assertEqual(self._data()["say"]["ask"], got["say"], "反问那句来自数据")
        self.assertEqual(before, len(self._tasks()), "解析不出时刻就一张卡都不许建")

    # —— 4) 闲聊 ——————————————————————

    def test_chit_chat_is_not_a_reminder_and_leaves_no_trace(self):
        self.assertIsNone(self._handle("今天天气怎么样"), "普通闲聊不许被认成记事")
        self.assertEqual([], self._tasks(), "闲聊不建卡")
        self.assertEqual({}, remind.packet(None), "闲聊那一轮回包里没有 remind 这一格")

    # —— 5) 到点：日子线认这张卡 ————————————————

    def test_the_card_becomes_a_pending_task_when_the_clock_reaches_due(self):
        got = self._handle("十分钟后叫我")
        self.assertTrue(got["ok"], got)
        card_id = got["card_id"]
        read = proactive.pending_task(got["due"], root=self.day_root,
                                      home_root=self.home_root)
        self.assertTrue(read["ok"], read["error"])
        self.assertIsNotNone(read["fired"], "到点了日子线该认出这张卡：%r" % (read["cards"],))
        self.assertEqual(card_id, read["fired"]["card_id"], "报出来的就是刚才那张卡")
        found = proactive.sources(got["due"], root=self.day_root, home_root=self.home_root,
                                  heart_root=self.tmp / "heart", task=read)
        codes = [row["reason_code"] for row in found]
        self.assertIn(proactive.REASON_PENDING_TASK, codes, "由头里没有 pending_task：%r" % codes)
        row = [item for item in found
               if item["reason_code"] == proactive.REASON_PENDING_TASK][0]
        self.assertEqual(card_id, row["task"]["card_id"], "由头上的 card_id 对不上")

    # —— 5.5) 没落成卡：不许说记着了 ——————————————

    def test_a_card_that_could_not_be_written_says_so_and_names_the_code(self):
        # 手的额度归手：把每人每天的额度写成 0（真文件、真判据），这一轮就落不了卡。
        (self.hand_root / "limits.json").write_text(
            json.dumps({"version": 1, "per_person_per_day": 0, "note": "测试用"},
                       ensure_ascii=False), encoding="utf-8")
        got = self._handle("十分钟后叫我")
        self.assertFalse(got["ok"], "卡没落成不许说记着了：%r" % (got,))
        self.assertIsNone(got["card_id"])
        self.assertEqual("QUOTA_EXCEEDED", got["why"], "why 要说清是手那边的哪个码")
        self.assertEqual(self._data()["say"]["fail"], got["say"], "失败那句来自数据")
        self.assertEqual([], self._tasks(), "没落成就是一张卡都没有")

    # —— 6) 改数据就改行为 ——————————————————

    def test_the_words_and_copy_come_from_the_data(self):
        data = json.loads(json.dumps(self._data(), ensure_ascii=False))
        data["intent"]["words"] = ["别忘了我"]
        data["say"]["ok"] = "这份数据说了算：{name}@{when}"
        live = self.tmp / "mind"
        live.mkdir()
        (live / "remind.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        config = remind.load_config(root=live)

        self.assertIsNone(self._handle("十分钟后叫我", config=config),
                          "词表改了：原来那句不再是记事")
        got = self._handle("十分钟后别忘了我", config=config)
        self.assertTrue(got["ok"], got)
        self.assertTrue(got["say"].startswith("这份数据说了算："), "文案没跟着数据走：%r" % got["say"])

    # —— 7) 数据坏：点名 ————————————————————

    def test_broken_data_names_the_key_it_is_missing(self):
        live = self.tmp / "mind"
        live.mkdir()
        data = json.loads(json.dumps(self._data(), ensure_ascii=False))
        data["say"].pop("ok")
        (live / "remind.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            remind.load_config(root=live)
        self.assertIn("say.ok", str(caught.exception), "报错要点名缺的是哪一格")

    def test_a_broken_word_list_is_a_named_error_too(self):
        live = self.tmp / "mind"
        live.mkdir()
        data = json.loads(json.dumps(self._data(), ensure_ascii=False))
        data["intent"]["words"] = []
        (live / "remind.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            remind.load_config(root=live)
        self.assertIn("intent.words", str(caught.exception))


class RemindInAssemblyTest(unittest.TestCase):
    """装配那一头：认作记事的那一轮，嘴里那句就是回执、回包多 remind 一格。"""

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
        self.hand_root = self.tmp / "hand"
        self.hand_root.mkdir()

    def _soul(self):
        target = self.tmp / "soul"
        shutil.copytree(str(SOUL_FIXTURES / "valid"), str(target))
        return load_soul(target)

    def _answer(self, text: str, reply: str = "嗯，我在。"):
        return conversation.answer(text, WHO, store=self.store, brain=self.brain,
                                   now=NOW, transport=_transport(reply), config=CONFIG,
                                   env=ENV, remind_root=self.hand_root)

    def test_the_card_is_landed_and_she_says_the_receipt(self):
        got = self._answer("十分钟后叫我")
        self.assertTrue(got["ok"])
        self.assertEqual(remind.PACKET_KEYS, tuple(got["remind"]))
        self.assertTrue(got["remind"]["ok"], got["remind"])
        self.assertEqual(TEN_MINUTES_LATER, got["remind"]["due"])
        card_id = str(got["remind"]["card_id"] or "")
        self.assertTrue(card_id, "回执里要能看见是哪张卡")
        data = remind.load_config()
        head = data["say"]["ok"].split("{")[0].strip()
        self.assertTrue(got["reply"].startswith(head),
                        "嘴上那句是数据里的回执（%s）：%r" % (head, got["reply"]))
        self.assertIn("叫我", got["reply"], "回执里带着卡名（去掉时间词的那半句）")
        self.assertEqual(1, len(home_cards.list_tasks(
            root=hand_store.home_root(self.hand_root))))

    def test_intent_without_time_asks_in_the_reply_and_lands_no_card(self):
        got = self._answer("回头提醒我")
        data = remind.load_config()
        self.assertEqual(data["say"]["ask"], got["reply"], "反问那句要整句说出来（不许被删半句）")
        self.assertIn("几点", got["reply"])
        self.assertFalse(got["remind"]["ok"])
        self.assertEqual(remind.WHY_NO_TIME, got["remind"]["why"])
        self.assertEqual([], home_cards.list_tasks(
            root=hand_store.home_root(self.hand_root)), "没时间就不许建卡")

    def test_chit_chat_has_no_remind_cell(self):
        got = self._answer("今天天气怎么样")
        self.assertEqual("嗯，我在。", got["reply"], "闲聊的回答一个字都不该变")
        self.assertNotIn("remind", got, "闲聊那一轮回包里不该有 remind 这一格")
        self.assertEqual([], home_cards.list_tasks(
            root=hand_store.home_root(self.hand_root)))
