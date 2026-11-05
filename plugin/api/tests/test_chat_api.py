"""POST /chat 的验收：走 api.handle 真链路（路由表 → 派发 → 装配 → 假 transport → 回包）。

判据：
- 成功体是契约那一份（ok / reply / chunks / recalled / heart / gate / assembly / memory）；
  分泡 2–3 条、gap_before 是数、send_at 非空；recalled 每条带 source / source_type /
  evidence_key，且与脑里那条对得上；心只有词；gate 的 pass 与 dropped 两条路都走到。
- 失败一律非 200：没配 key 503、超时 504、限流 429、鉴权 502、空内容 502；
  kind 与 message 原样交出去，**绝不把空串当成功**。
- GET / PUT / DELETE /chat 全 405（这一条是写）。
- 落脑：一轮之后脑的今日档里有人那句与她那句，source_type 都是亲历、evidence_key 非空。
- 只有一份装配：api.chat.dispatch 走的就是 conversation.answer（数一遍调用次数）。
- 拒绝的**唯一投递点**在这一层（本轮加的）：判成软转向或那三句标准拒绝之一就投一次心
  （refusals.json 多一行）+ 记一行我们自己的 refuse.jsonl；正面答了不添 refusal 一节、一行都不写。
  同一句在 /mouth/gate 上是 delivered=false（干跑只判不投），两处的判法是同一份。

模型一律换成本文件里的假 transport：一个字节都不发真网络。心 / 嘴 / 装配的临时根都经
环境变量切走，仓库里的真数据一个字节不写。
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hashlib

from api import api
from api.tests.helpers import copy_fixture
from organs.brain import BrainStore
from organs.home import anchors as home_anchors
from organs.home import cards
from organs.mind import conversation, llm, recent, usage
from organs.mouth import refusal as mouth_refusal
from organs.soul import load

CONFIG = {"provider": "fake", "model": "fake-model", "base_url": "http://example.invalid/v1",
          "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 1000, "retries": 0,
          "temperature": 0.0, "max_tokens": 200}
ENV = {"QQAGENT_LLM_KEY": "test-key"}
PLUGIN = Path(__file__).resolve().parents[2]
MOUTH_DATA = PLUGIN / "organs" / "mouth" / "data"

MEMORY_TEXT = "我上次去医院是陪我妈复查，我对花生过敏"
QUESTION = "你还记得医院那回事吗？"
# 一份≈平常档预算长度、且超过嘴的 split.max_chars 的回复：真链路里该切成多泡——
# 现行口径是「每泡 1–2 句，max_parts=12 只是防炸上限」，旧的「2–3 泡」已退役。
LONG_REPLY = ("你问医院那回事，我记得的，是陪你妈去复查对吧。"
              "花生过敏我也记着，别再吃带花生的点心了，上次你嘴肿了半天。"
              "中午吃了面，汤有点咸，晚上想喝粥。"
              "周末要是不下雨，我陪你去一趟，顺便把上次落下的检查补上。") * 2

CONTRACT_KEYS = {"ok", "reply", "chunks", "recalled", "heart", "gate",
                 "assembly", "continuity", "banned", "action", "llm", "anchors",
                 "guard", "memory"}
BANNED_KEYS = {"rounds", "action_lines", "openings"}
ACTION_KEYS = {"pool", "rule"}
GUARD_KEYS = {"window", "threshold", "min_rounds", "rounds", "hits", "rate",
              "over", "injected", "available"}


def _body(reply: str, captured: list = None, status: int = 200):
    """假 transport：把一句话当作模型应答；captured 收请求体。"""
    def send(url, headers, payload, timeout):
        if captured is not None:
            captured.append(payload)
        data = {"choices": [{"message": {"content": reply}}]}
        return status, json.dumps(data, ensure_ascii=False).encode("utf-8")
    return send


def _failing(exc: Exception):
    def send(url, headers, payload, timeout):
        raise exc
    return send


def _status(status: int, body: bytes = b""):
    def send(url, headers, payload, timeout):
        return status, body
    return send


class ChatApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patches = mock.patch.dict(os.environ, {
            "QQAGENT_HOME": str(self.tmp / "home"),
            "HEART_ROOT": str(self.tmp / "heart"),
            "MOUTH_TRACE_DIR": str(self.tmp / "trace"),
            "HOME_ROOT": str(self.tmp / "house"),
        })
        (self.tmp / "house" / "仓库").mkdir(parents=True, exist_ok=True)
        patches.start()
        self.addCleanup(patches.stop)
        # 本机 <仓库>/home/.env 里可能真有 key：把 llm 认的那个 home 钉到临时目录，
        # 否则「没配 key 该是 503」那条会被本机密钥悄悄变成「请求发出去了」（同 test_llm 的做法）。
        home_patch = mock.patch.object(llm.paths, "HOME", self.tmp / "repo-home")
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.store = load(copy_fixture("valid", str(self.tmp)))
        self.brain = BrainStore(self.tmp / "brain")

    # —— 小工具 ————————————————————————————————

    def _answer(self, transport, *, config=CONFIG, env=ENV, counter: list = None):
        """把假 transport 塞进真装配：api.handle → api.chat.dispatch → conversation.answer。

        包的只是 answer 的三个注入点（transport / config / env），装配本身一次都没有替身。
        """
        real = conversation.answer

        def wrapper(text, who="", **kw):
            kw.setdefault("transport", transport)
            kw.setdefault("config", config)
            kw.setdefault("env", env)
            if counter is not None:
                counter.append(text)
            return real(text, who, **kw)

        return mock.patch.object(conversation, "answer", wrapper)

    def _post(self, body, transport=None, *, store=..., brain=..., **extra):
        transport = body if transport is None else transport
        with self._answer(transport, **extra):
            return api.handle("POST", "/chat", body,
                             self.store if store is ... else store,
                             brain=self.brain if brain is ... else brain)

    def _send(self, text: str = QUESTION, reply: str = LONG_REPLY, **extra):
        return self._post({"text": text}, _body(reply), **extra)

    # —— 成功体 ————————————————————————————————

    def test_success_body_is_the_contract(self):
        self.brain.ingest(who="主人", text=MEMORY_TEXT)
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(CONTRACT_KEYS, set(payload),
                         "成功体的键不是契约那一份：%s" % sorted(payload))
        self.assertTrue(payload["ok"])
        self.assertEqual(LONG_REPLY, payload["reply"])
        chunks = payload["chunks"]
        mouth_policy = json.loads((MOUTH_DATA / "policy.json").read_text(encoding="utf-8"))
        max_parts = int(mouth_policy["split"]["max_parts"])
        max_chars = int(mouth_policy["split"]["max_chars"])
        self.assertTrue(2 <= len(chunks) <= max_parts,
                        "分泡该在 2–%d 条之间（现读嘴的 policy）：%r" % (max_parts, chunks))
        for chunk in chunks:
            self.assertTrue(chunk["text"])
            self.assertLessEqual(len(chunk["text"]), max_chars,
                                 "有一泡超过嘴的 split.max_chars：%r" % (chunk,))
            self.assertIsInstance(chunk["gap_before"], (int, float))
            self.assertIsInstance(chunk["send_at"], (int, float))
            self.assertIsNotNone(chunk["send_at"])
        self.assertEqual(CONFIG["model"], capture[0]["model"])
        self.assertEqual(["system", "user"],
                         [item["role"] for item in capture[0]["messages"]])

    def test_banned_and_action_ride_along_in_the_success_body(self):
        """防抄自己那两块也要随成功体出去：禁令读三键、动作池读两键，形状对外冻结。"""
        conf = conversation.load_assembly()
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        banned = payload["banned"]
        action = payload["action"]
        self.assertEqual(BANNED_KEYS, set(banned), "banned 的键不是契约那一份：%s" % sorted(banned))
        self.assertEqual(ACTION_KEYS, set(action), "action 的键不是契约那一份：%s" % sorted(action))
        self.assertEqual(conf["banned"]["rounds"], banned["rounds"])
        self.assertIsInstance(banned["rounds"], int)
        self.assertIsInstance(banned["action_lines"], list)
        self.assertIsInstance(banned["openings"], list)
        for row in banned["action_lines"]:
            self.assertEqual({"text", "count"}, set(row))
            self.assertTrue(row["text"])
            self.assertGreaterEqual(row["count"], 1)
        for head in banned["openings"]:
            self.assertIsInstance(head, str)
            self.assertTrue(head)
        self.assertEqual(conf["action"]["pool"], action["pool"], "池子该照抄 assembly.json")
        self.assertEqual(conf["action"]["rule"], action["rule"])
        # 两段真的进了提示词（不是只在回包里挂个空壳）
        system = capture[0]["messages"][0]["content"]
        self.assertIn("[%s]" % conf["sections"]["banned"]["label"], system,
                      "近轮禁用那一节没进提示词")
        self.assertIn("[%s]" % conf["sections"]["action"]["label"], system,
                      "动作池那一节没进提示词")

    def test_recalled_carries_real_evidence_from_brain(self):
        entry = self.brain.ingest(who="主人", text=MEMORY_TEXT)
        status, payload = self._send()
        self.assertEqual(200, status)
        self.assertTrue(payload["recalled"], "一句带「医院」的话该想起那条记忆")
        row = payload["recalled"][0]
        self.assertEqual({"id", "text", "source", "source_type", "evidence_key", "score", "who"},
                         set(row))
        self.assertEqual(MEMORY_TEXT, row["text"])
        self.assertEqual(entry["entry"]["evidence_key"], row["evidence_key"])
        self.assertEqual(entry["entry"]["id"], row["id"])
        self.assertEqual("亲历", row["source_type"])
        self.assertTrue(row["source"], "来源档没给")
        stored = [item for item in self.brain.today_entries()
                  if item.get("id") == entry["entry"]["id"]]
        self.assertEqual(1, len(stored), "脑里找不到那条被想起的记忆")
        self.assertEqual(stored[0]["evidence_key"], row["evidence_key"])

    def test_heart_is_words_only(self):
        status, payload = self._send()
        self.assertEqual(200, status)
        heart = payload["heart"]
        self.assertEqual({"tier", "direction"}, set(heart["mood"]))
        self.assertEqual({"tone", "direction", "bans"}, set(heart["voice"]))
        self.assertEqual({"state", "reason"}, set(heart["engage"]))
        self.assertTrue(heart["axes"])
        text = json.dumps(heart, ensure_ascii=False)
        self.assertEqual([], [ch for ch in text if ch.isdigit()],
                         "心的回包里有数字：%s" % text)

    def test_gate_pass_and_dropped_are_both_reachable(self):
        status, passed = self._send()
        self.assertEqual(200, status)
        self.assertEqual("pass", passed["gate"]["decision"])
        self.assertIsNone(passed["gate"]["hit"])
        self.assertTrue(passed["chunks"])

        # 命中路径要一份含命中条款的嘴数据：出厂那份不认这句话，所以整份拷到临时根再改条款。
        root = self.tmp / "mouth"
        if not root.is_dir():
            import shutil
            shutil.copytree(str(MOUTH_DATA), str(root))
            clauses = json.loads((root / "clauses.json").read_text(encoding="utf-8"))
            clauses["clauses"].insert(0, {
                "id": "ai-self-reference",
                "kind": "反例",
                "pattern": "作为一个\\s*(AI|人工智能)\\s*语言模型",
                "reason": "这是标准模型免责声明：她不是那个东西",
            })
            (root / "clauses.json").write_text(json.dumps(clauses, ensure_ascii=False),
                                               encoding="utf-8")
        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(root)}):
            dropped = self._send(reply="作为一个 AI 语言模型，我不能这样说。")
        self.assertEqual(200, dropped[0])
        self.assertEqual("dropped", dropped[1]["gate"]["decision"])
        self.assertEqual("", dropped[1]["reply"])
        self.assertEqual([], dropped[1]["chunks"])
        self.assertTrue(dropped[1]["gate"]["hit"]["clause_id"])
        self.assertTrue(dropped[1]["gate"]["hit"]["reason"])
        self.assertGreaterEqual(dropped[1]["gate"]["drops"]["today"], 1)

    # —— 拒绝：全仓唯一的投递点（本轮加的） ————————————————

    SOFT_ANSWER = ("……这个啊。鱼在海里游着游着，有些同伴就游远了……行了行了，"
                   "我又不是不能自己待着。倒是你，怎么突然问这个")
    ASKED_CAT = "猫今天怎么没来？"                    # 话题词表里带「猫」，投心才认得出来
    ANSWERED = "明天上午九点，我在医院门口等你。"      # 撞上正面回答的信号：她答了

    def _topics(self, *names: str):
        """把心的话题词表指到临时文件：认话题只认心自己那份词表，测试也照这个口径给。"""
        path = self.tmp / "topics.json"
        path.write_text(json.dumps({name: [] for name in names}, ensure_ascii=False),
                        encoding="utf-8")
        return mock.patch.dict(os.environ, {"HEART_TOPICS_FILE": str(path)})

    def _heart_file(self) -> Path:
        return Path(os.environ["HEART_ROOT"]) / "refusals.json"

    def _heart_rows(self):
        """心的冷却账里现在有几行（文件还不存在就是空表——不是「读不到」）。"""
        path = self._heart_file()
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("refusals") or []

    def _refusal_rows(self):
        """我们自己的拒绝账（refuse.jsonl）里现在有几行。"""
        path = Path(os.environ["MOUTH_TRACE_DIR"]) / "refuse.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_soft_redirect_lands_one_row_in_the_heart(self):
        """软转向走真链路：心的 refusals.json 多一行（含 topic 与 until），返回里 delivered=true。

        这就是「投递点只有 /chat」的正身：同一句在 /mouth/gate 上是 delivered=false（干跑），
        在这里才真落盘——两处判的法一模一样，区别只在写不写。
        """
        with self._topics("猫"):
            status, payload = self._send(text=self.ASKED_CAT, reply=self.SOFT_ANSWER)
        self.assertEqual(200, status)
        section = payload["refusal"]
        self.assertEqual({"kind", "shape", "verdict_reason", "delivered"}, set(section))
        self.assertEqual("soft", section["kind"])
        self.assertEqual("soft", section["shape"])
        self.assertIn("软转向", section["verdict_reason"])
        delivered = section["delivered"]
        self.assertEqual({"delivered", "topic", "until", "remaining_min", "reason"},
                         set(delivered))
        self.assertTrue(delivered["delivered"], "软转向也是拒绝，真对话里要投给心")
        self.assertEqual("猫", delivered["topic"])
        self.assertTrue(delivered["until"], "投进去必须带回冷却到什么时候")
        rows = self._heart_rows()
        self.assertEqual(1, len(rows), "一次真实对话就落一行")
        self.assertEqual("猫", rows[0]["topic"])
        self.assertTrue(rows[0]["until"])
        self.assertEqual("refused-topic", rows[0]["reason"])
        ledger = self._refusal_rows()
        self.assertEqual(1, len(ledger), "拒绝的账也在这儿记")
        self.assertEqual("soft", ledger[0]["shape"])
        self.assertTrue(ledger[0]["sent"])

    def test_a_standard_refusal_line_is_delivered_too(self):
        """她的答话就是数据里那三句之一：硬拒绝同样投一次，形状取数据里的键，不写死在代码里。"""
        said = mouth_refusal.say("topic")
        with self._topics("猫"):
            status, payload = self._send(text=self.ASKED_CAT, reply=said)
        self.assertEqual(200, status)
        section = payload["refusal"]
        self.assertEqual("hard", section["kind"])
        self.assertEqual("topic", section["shape"], "形状名取自 data/refusal.json")
        self.assertTrue(section["delivered"]["delivered"])
        rows = self._heart_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("猫", rows[0]["topic"])
        self.assertEqual("refused-topic", rows[0]["reason"])
        self.assertEqual("hard", self._refusal_rows()[0]["shape"])

    def test_answered_reply_leaves_no_section_and_no_row(self):
        """正面答了：成功体里没有 refusal 一节（不加空壳），心与我们自己的账一行都不添。"""
        with self._topics("猫"):
            status, payload = self._send(text=self.ASKED_CAT, reply=self.ANSWERED)
        self.assertEqual(200, status)
        self.assertNotIn("refusal", payload, "没判出拒绝就不添这一节")
        self.assertEqual([], self._heart_rows())
        self.assertEqual([], self._refusal_rows())

    def test_unknown_topic_is_recorded_but_not_pretended(self):
        """asked 里没有词表上的话题名：事件成立、拒绝账照记，但投不进去，delivered=false 说清为什么。"""
        with self._topics("猫"):
            status, payload = self._send(text="今天好热。", reply=self.SOFT_ANSWER)
        self.assertEqual(200, status)
        delivered = payload["refusal"]["delivered"]
        self.assertFalse(delivered["delivered"])
        self.assertIsNone(delivered["topic"])
        self.assertIn("认不出", delivered["reason"])
        self.assertEqual([], self._heart_rows(), "认不出话题就一个字节都不写心")
        ledger = self._refusal_rows()
        self.assertEqual(1, len(ledger), "她拒了这件事照记，不因为投不进心就不记账")
        self.assertFalse(ledger[0]["sent"])

    # —— 失败 ————————————————————————————————

    def test_missing_key_is_503_with_a_readable_message(self):
        status, payload = self._post({"text": QUESTION}, _failing(AssertionError("不该发请求")),
                                     env={})
        self.assertEqual(503, status, "没配 key 该是 503")
        self.assertFalse(payload["ok"])
        self.assertEqual("no-key", payload["kind"])
        self.assertIn("还没配模型 key", payload["message"])
        self.assertIn("QQAGENT_LLM_KEY", payload["message"])
        self.assertNotEqual("", payload["message"], "失败必须带一句人话，不是空串")
        self.assertNotIn("reply", payload)

    def test_timeout_is_504(self):
        status, payload = self._post({"text": QUESTION}, _failing(TimeoutError("慢")))
        self.assertEqual(504, status)
        self.assertEqual("timeout", payload["kind"])
        self.assertTrue(payload["message"])

    def test_rate_limit_is_429(self):
        status, payload = self._post({"text": QUESTION}, _status(429, b'{"error":"slow down"}'))
        self.assertEqual(429, status)
        self.assertEqual("rate_limit", payload["kind"])

    def test_auth_is_502(self):
        status, payload = self._post({"text": QUESTION}, _status(401, b'{"error":"bad key"}'))
        self.assertEqual(502, status)
        self.assertEqual("auth", payload["kind"])

    def test_empty_answer_is_502_not_a_success(self):
        empty = json.dumps({"choices": [{"message": {"content": "   "}}]}).encode("utf-8")
        status, payload = self._post({"text": QUESTION}, _status(200, empty))
        self.assertEqual(502, status, "空内容被当成成功了")
        self.assertEqual("empty", payload["kind"])
        self.assertNotIn("reply", payload)

    def test_missing_text_and_missing_organs(self):
        with self._answer(_failing(AssertionError("不该发请求"))):
            status, payload = api.handle("POST", "/chat", {}, self.store, brain=self.brain)
            self.assertEqual(400, status)
            self.assertIn("POST /chat 要给 text", payload["message"])
            for body in ({"text": "   "}, None):
                status, payload = api.handle("POST", "/chat", body, self.store,
                                             brain=self.brain)
                self.assertEqual(400, status, body)
                self.assertIn("POST /chat 要给 text", payload["message"])
            status, payload = api.handle("POST", "/chat", {"text": QUESTION},
                                         None, brain=self.brain)
            self.assertEqual(503, status)
            self.assertIn("魂未载入", payload["message"])
            status, payload = api.handle("POST", "/chat", {"text": QUESTION},
                                         self.store, brain=None)
            self.assertEqual(503, status)
            self.assertIn("脑未载入", payload["message"])

    # —— 方法 ————————————————————————————————

    def test_other_methods_are_405(self):
        for method in ("GET", "PUT", "DELETE"):
            status, payload = api.handle(method, "/chat", None, self.store, brain=self.brain)
            self.assertEqual(405, status, "%s /chat 该是 405" % method)
            self.assertFalse(payload["ok"])
            self.assertIn("只收 POST", payload["message"])

    # —— 落脑 ————————————————————————————————

    def test_this_round_lands_in_brain(self):
        status, payload = self._send(text=QUESTION)
        self.assertEqual(200, status)
        rows = self.brain.today_entries()
        texts = [row.get("text") for row in rows]
        self.assertIn(QUESTION, texts, "人的原话没进脑")
        self.assertIn(LONG_REPLY, texts, "她那句话没进脑")
        for row in rows[-2:]:
            self.assertEqual("亲历", row.get("source_type"))
            self.assertTrue(row.get("evidence_key"), "证据指针是空的")
        wrote = payload["memory"]["wrote"]
        self.assertEqual(2, len(wrote))
        self.assertEqual([row["evidence_key"] for row in rows[-2:]],
                         [item["id"] for item in wrote])

    # —— 连续性与温度 ————————————————————————

    def test_continuity_llm_and_anchors_are_in_the_body(self):
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)

        self.assertEqual({"turns_loaded", "omitted", "notice"}, set(payload["continuity"]))
        self.assertEqual(0, payload["continuity"]["turns_loaded"])
        self.assertEqual(0, payload["continuity"]["omitted"])
        self.assertEqual("", payload["continuity"]["notice"], "没省任何一轮时 notice 该是空串")

        self.assertEqual({"model", "temperature", "temperature_tier"}, set(payload["llm"]))
        self.assertEqual(CONFIG["model"], payload["llm"]["model"])
        self.assertIn(payload["llm"]["temperature_tier"], ("default", "factual"))
        self.assertEqual(capture[0]["temperature"], payload["llm"]["temperature"],
                         "回包里的温度与真发出去的那个不一致")

        self.assertFalse(payload["anchors"]["configured"], "家·仓库里没有台词库，却报成配了")
        self.assertEqual(conversation.ANCHORS_MISSING, payload["anchors"]["note"])

    def test_fifteen_rounds_show_the_omitted_notice_over_http(self):
        for index in range(15):
            recent.append({"ts": "2025-01-15T14:0%d:00+08:00" % (index % 10), "who": "主人",
                           "said": "第%d句" % index, "reply": "第%d答" % index})
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        continuity = payload["continuity"]
        self.assertEqual(12, continuity["turns_loaded"])
        self.assertEqual(3, continuity["omitted"])
        notice = conversation.load_assembly()["recent"]["notice"].replace(recent.NOTE_SLOT, "3")
        self.assertEqual(notice, continuity["notice"])
        self.assertIn(notice, capture[0]["messages"][0]["content"], "提示词里没有那句「更早还有」")

    def test_five_rounds_carry_no_notice(self):
        for index in range(5):
            recent.append({"ts": "2025-01-15T14:0%d:00+08:00" % index, "who": "主人",
                           "said": "第%d句" % index, "reply": "第%d答" % index})
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual(5, payload["continuity"]["turns_loaded"])
        self.assertEqual(0, payload["continuity"]["omitted"])
        self.assertEqual("", payload["continuity"]["notice"])

    def test_configured_anchor_arrives_at_the_endpoint(self):
        """家·仓库里登记两张台词卡：端点报成配了，全文与 body 都回给人核对，body 不进提示词。"""
        home = Path(os.environ["HOME_ROOT"])
        home_anchors.append_anchors(root=home, role="human", by="人", anchors=[
            {"scene": "被夸", "say": "哼，才知道啊。", "body": "尾巴翘起来晃了两下"},
            {"scene": "认错", "say": "……是我记岔了。", "body": "低头、尾巴垂下去不敢动"},
        ])
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        anchors = payload["anchors"]
        self.assertEqual({"configured", "count", "available", "injected", "instruction",
                          "items", "skipped"}, set(anchors))
        self.assertTrue(anchors["configured"])
        self.assertEqual(2, anchors["count"])
        self.assertEqual(conversation.load_assembly()["anchors"]["instruction"],
                         anchors["instruction"])
        by_scene = {item["scene"]: item for item in anchors["items"]}
        self.assertEqual("尾巴翘起来晃了两下", by_scene["被夸"]["body"])
        system = capture[0]["messages"][0]["content"]
        self.assertIn(by_scene["被夸"]["injected"], system)
        self.assertNotIn("尾巴翘起来晃了两下", system, "body 进了提示词")
        self.assertNotIn("哼，才知道啊。", system, "整句进了提示词")

    # —— 口癖护栏 ————————————————————————————

    FIVE = (
        {"scene": "被夸", "say": "哼，才知道啊。", "body": "尾巴翘起来晃了两下"},
        {"scene": "被关心", "say": "行了行了，我又不是玻璃做的。", "body": "别开头、脸微微发红"},
        {"scene": "不想聊", "say": "这个啊……以后再说吧。", "body": ""},
        {"scene": "认错", "say": "……是我记岔了。", "body": "低头、尾巴垂下去不敢动"},
        {"scene": "你要走", "say": "你忙你的，我在这儿。", "body": "尾巴慢慢摆一下，乖巧的笑了笑"},
    )

    def _five_anchors(self):
        """家·仓库里登记五张台词卡（端点侧只看得到回包，看不到家里的文件）。"""
        home = Path(os.environ["HOME_ROOT"])
        home_anchors.append_anchors(root=home, role="human", by="人", anchors=list(self.FIVE))

    def _usage_seed(self, count: int, hits: int) -> None:
        """造口癖账：与 conversation 读的是同一本（<QQAGENT_HOME>/mind/anchor_usage.jsonl）。"""
        for index in range(count):
            hit = index >= count - hits
            usage.append(hit, "这个啊又来了", "不想聊" if hit else "")

    def test_guard_is_in_the_body_and_values_come_from_the_data(self):
        status, payload = self._send()
        self.assertEqual(200, status)
        self.assertEqual(GUARD_KEYS, set(payload["guard"]),
                         "guard 的键是对外冻结的那一份：%s" % sorted(payload["guard"]))
        conf = conversation.load_assembly()["anchors"]["guard"]
        self.assertEqual(conf["window"], payload["guard"]["window"])
        self.assertEqual(conf["threshold"], payload["guard"]["threshold"])
        self.assertEqual(conf["min_rounds"], payload["guard"]["min_rounds"])
        self.assertFalse(payload["guard"]["over"])

    def test_over_threshold_downgrades_to_two_halves_over_http(self):
        """60 轮全命中 → over：回包报 injected 2、skipped 3，提示词里只有 2 条半句。"""
        self._five_anchors()
        self._usage_seed(60, 60)
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        guard = payload["guard"]
        self.assertTrue(guard["over"], "命中率 100% 却没降档")
        self.assertEqual(50, guard["rounds"])
        self.assertEqual(2, guard["injected"])
        self.assertEqual(5, guard["available"])
        anchors = payload["anchors"]
        self.assertEqual(2, anchors["injected"])
        self.assertEqual(5, anchors["available"])
        self.assertEqual(3, len(anchors["skipped"]), "被跳过的 3 条要逐个点名")
        system = capture[0]["messages"][0]["content"]
        conf = conversation.load_assembly()["anchors"]
        injected = [item["injected"] for item in anchors["items"]]
        self.assertEqual(2, len(injected))
        halves_in_prompt = [half for half in injected if half in system]
        self.assertEqual(2, len(halves_in_prompt), "提示词里该正好 2 条半句")
        skipped = [item for item in self.FIVE if item["scene"] in anchors["skipped"]]
        self.assertEqual(3, len(skipped))
        for item in skipped:
            cut = int(math.ceil(len(item["say"]) * conf["inject"]["fraction"]))
            self.assertNotIn(item["say"][:cut] + conf["inject"]["suffix"], system,
                             "降档了还在注入：%s" % item["scene"])

    # —— 只有一份装配 ————————————————————————

    def test_dispatch_goes_through_the_single_assembly_entry(self):
        counter = []
        status, payload = self._send(counter=counter)
        self.assertEqual(200, status)
        self.assertEqual([QUESTION], counter, "api.chat.dispatch 没走 conversation.answer")
        self.assertTrue(payload["reply"])


if __name__ == "__main__":
    unittest.main()
