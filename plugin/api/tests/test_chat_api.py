"""POST /chat 的验收：走 api.handle 真链路（路由表 → 派发 → 装配 → 假 transport → 回包）。

判据：
- 成功体是契约那一份（ok / reply / chunks / recalled / heart / gate / assembly / memory）；
  分泡 2–3 条、gap_before 是数、send_at 非空；recalled 每条带 source / source_type /
  evidence_key，且与脑里那条对得上；心只有词；gate 的 pass 与 dropped 两条路都走到。
- 失败一律非 200：没配 key 503、超时 504、限流 429、鉴权 502、空内容 502；
  kind 与 message 原样交出去，**绝不把空串当成功**。
- GET / PUT / DELETE /chat 全 405（这一条是写）。
- 落脑：一轮之后脑的今日档里有人那句与她那句，source_type 都是亲历、evidence_key 非空。
- **闸在 /chat 生效（本轮加的）**：逐泡过一遍闸，结论真的落到返回体——复读（反复读）、动作括注
  （动作带场景）、复述设定段三条命中的路都走到 dropped；一句正常的回话一处都不改（chunks 的 text
  与不接闸时逐字相同）；全被弃掉时 chunks=[]、reply=""，并各记一行弃言账。
- **动作配额也从装配传进闸（本轮补的）**：action.quota.blocked 为真 → forbid（剥行救回正文，
  quota_status=ok，账本 reason 里带 action-over-quota）；为假 → allow（判过，放行动作括注那一关拦）；
  装配没标 → None（quota_status=not-checked），**没标不是 allow**。
- 只有一份装配：api.chat.dispatch 走的就是 conversation.answer（数一遍调用次数）。
- 拒绝的**唯一投递点**在这一层（本轮加的）：判成软转向或那三句标准拒绝之一就投一次心
  （refusals.json 多一行）+ 记一行我们自己的 refuse.jsonl；正面答了不添 refusal 一节、一行都不写。
  同一句在 /mouth/gate 上是 delivered=false（干跑只判不投），两处的判法是同一份。

模型一律换成本文件里的假 transport：一个字节都不发真网络。心 / 嘴 / 装配的临时根都经
环境变量切走，仓库里的真数据一个字节不写。
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import hashlib

from api import api, chat
from api.tests.helpers import copy_fixture, write_json
from organs.brain import BrainStore
from organs.home import anchors as home_anchors
from organs.home import cards
from organs.home import residents as home_residents
from organs.heart import refuse as heart_refuse
from organs.mind import conversation, llm, recent, usage
from organs.mouth import address as mouth_address
from organs.mouth import audit as mouth_audit
from organs.mouth import refusal as mouth_refusal
from organs.mouth import repeat as mouth_repeat
from organs.mouth import warehouse
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

# 判据里的名字与记号一个都不写死：复读那条 clause_id、跳过记账用的名字、两个记号都从数据里读。
REPEAT_BOOK = json.loads((MOUTH_DATA / "repeat.json").read_text(encoding="utf-8"))
REPEAT_CLAUSE = REPEAT_BOOK["hit"]["clause_id"]
SKIP_CLAUSE = REPEAT_BOOK["hit"]["skip_clause_id"]
SKIP_MARK = "skipped-by-request"        # 跳过那句 reason 里必须出现的记号
AUDIT_BOOK = json.loads((MOUTH_DATA / "audit.json").read_text(encoding="utf-8"))
AUDIT_SKIP = AUDIT_BOOK["skip"]         # 复述审计按他说的放过那一笔：名字、记号、说法全在数据里
AUDIT_SKIP_CLAUSE = AUDIT_SKIP["clause_id"]
AUDIT_SKIP_MARK = AUDIT_SKIP["status"]
FATAL_BOOK = json.loads((MOUTH_DATA / "fatal.json").read_text(encoding="utf-8"))
FATAL_CLAUSE = FATAL_BOOK["hit"]["clause_id"]
GIVE_UP_CATEGORY = FATAL_BOOK["categories"][2]      # 放弃式那一类（顺序写在数据里）
GIVE_UP_LINE = "反正我不疼，你别管了"                 # 一句放弃式的素材，不是判据
AFTER_MARKS = json.loads((MOUTH_DATA / "rewrite.json").read_text(
    encoding="utf-8"))["after_rewrite"]["marks"]


def _mark_of(tag: str) -> str:
    """某一类红线在账本里那个记号（按数据里的 mark 与 tag 填）：测试不自己造这个字符串。"""
    return FATAL_BOOK["mark"].format(tag=tag)
KEPT_MARK = "rewrite-failed"            # 改不动、留原话那句 reason 里必须出现的记号
REWRITE_KEYS = {"status", "why", "from", "to"}
# 借阅读数里「条款用掉几条」= 这一轮过闸真比过的条数（启用条款全表）：条数不写死，读数据。
CLAUSES = json.loads((MOUTH_DATA / "clauses.json").read_text(encoding="utf-8"))["clauses"]
ENABLED_CLAUSES = len([one for one in CLAUSES if one.get("enabled", True)])

CONTRACT_KEYS = {"ok", "reply", "chunks", "recalled", "heart", "gate",
                 "assembly", "continuity", "banned", "action", "llm", "anchors",
                 "guard", "memory", "addressed", "borrow", "refusal"}
BANNED_KEYS = {"rounds", "action_lines", "openings"}
ACTION_KEYS = {"pool", "rule", "quota"}
QUOTA_KEYS = {"window", "max", "used", "blocked", "stripped", "kept_because_empty"}
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


def _script(*replies: str, captured: list = None):
    """假 transport：按调用顺序依次回那几句话（第 1 次是装配那一问，第 2 次起是重写）。

    最后一句会一直用下去：一泡命中、后面还有泡命中时，重写都是用最后那一句回的。
    """
    queue = list(replies)

    def send(url, headers, payload, timeout):
        if captured is not None:
            captured.append(payload)
        text = queue[0] if len(queue) == 1 else queue.pop(0)
        data = {"choices": [{"message": {"content": text}}]}
        return 200, json.dumps(data, ensure_ascii=False).encode("utf-8")

    return send


def _script_then_raise(reply: str, exc: Exception):
    """假 transport：第 1 次（装配那一问）正常回 reply，之后每一次（重写）都抛 exc。"""
    calls = []

    def send(url, headers, payload, timeout):
        calls.append(payload)
        if len(calls) > 1:
            raise exc
        data = {"choices": [{"message": {"content": reply}}]}
        return 200, json.dumps(data, ensure_ascii=False).encode("utf-8")

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
        """把假 transport 塞进真链路：api.handle → api.chat.dispatch → answer / 重写那一问。

        两个入口都只换注入点（transport / config / env），装配与闸本身一次都没有替身：
        answer() 那三个参数走 mock.patch，重写那一次走 api.chat 顶上的 TRANSPORT / CONFIG / ENV。
        同一份假 transport 两边共用——重写走的确实是「answer() 用的那一条出口」。
        """
        real = conversation.answer

        def wrapper(text, who="", **kw):
            kw.setdefault("transport", transport)
            kw.setdefault("config", config)
            kw.setdefault("env", env)
            if counter is not None:
                counter.append(text)
            return real(text, who, **kw)

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(conversation, "answer", wrapper))
        stack.enter_context(mock.patch.object(chat, "TRANSPORT", transport))
        stack.enter_context(mock.patch.object(chat, "CONFIG", config))
        stack.enter_context(mock.patch.object(chat, "ENV", env))
        return stack

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
        """防抄自己那两块也要随成功体出去：禁令读三键、动作池读三键（含配额四键），形状对外冻结。"""
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
        quota = action["quota"]
        self.assertEqual(QUOTA_KEYS, set(quota), "quota 的六个键对外冻结：%s" % sorted(quota))
        self.assertEqual(conf["action"]["quota"]["window"], quota["window"])
        self.assertEqual(conf["action"]["quota"]["max"], quota["max"])
        self.assertIsInstance(quota["used"], int)
        self.assertIsInstance(quota["blocked"], bool)
        self.assertEqual(quota["used"] >= quota["max"], quota["blocked"],
                         "blocked 要跟着 used 与 max 算，不许另写一个值")
        self.assertIsInstance(quota["stripped"], int)
        self.assertIsInstance(quota["kept_because_empty"], bool)
        self.assertEqual(0, quota["stripped"], "没顶到配额时一行都不许删")
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
        self.assertTrue({"id", "text", "source", "source_type", "evidence_key", "score", "who"}
                        .issubset(set(row)),
                        "回包 recalled 至少带这些证据字段，实际：%r" % (sorted(set(row)),))
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
        line = "作为一个 AI 语言模型，我不能这样说。"
        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(root)}):
            dropped = self._post({"text": QUESTION},
                                 _script(line, "作为一个 AI 语言模型，这话我不能说。"))
        self.assertEqual(200, dropped[0])
        self.assertEqual("dropped", dropped[1]["gate"]["decision"])
        self.assertEqual("", dropped[1]["reply"])
        self.assertEqual([], dropped[1]["chunks"])
        self.assertTrue(dropped[1]["gate"]["hit"]["clause_id"])
        self.assertTrue(dropped[1]["gate"]["hit"]["reason"])
        self.assertGreaterEqual(dropped[1]["gate"]["drops"]["today"], 1)

# —— 闸：逐泡过一遍，结论真的落到返回体（本轮加的） ————————————

    HER_LINE = "主人回来啦，我把碗放桌上了。"          # 她自己说过的一句（复读那一关的靶子）
    NOTED_LINE = "（尾巴在水里蜷了一下）"              # 括号里写了我们没给过的场景（action_note 的 env_words）

    def _drops_rows(self):
        """弃言账本（drops.jsonl）现在有几行——每被弃一泡就多一行。"""
        path = Path(os.environ["MOUTH_TRACE_DIR"]) / "drops.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _mouth_root(self) -> Path:
        """把嘴的整份判据拷到临时根：改哪个数就改哪个 JSON，代码里一个数都不动。"""
        root = self.tmp / "mouth"
        if not root.is_dir():
            shutil.copytree(str(MOUTH_DATA), str(root))
        return root

    def test_a_repeated_reply_survives_the_failed_rewrite(self):
        """第二轮她说了一模一样的一句：内容去重命中，改了还是同一个开场 → 留进门原话，不再丢空。

        **这条断言是被新规矩改准的**：以前二次命中即弃（chunks 空 = 她哑了）。现在规矩是「在最该
        回答的那一句上沉默，比说话带点毛病糟得多」——所以 decision 是 pass、chunks 里还是她那句
        原话，账本记一行带 repeat-after-rewrite 的账，parts[*].kept_after_rewrite=true。
        判据照样看得见：hits 里两条命中、repeat_status=ok、rewrite.status=ok（模型确实改了）。
        """
        status, first = self._send(text="我回来了", reply=self.HER_LINE)
        self.assertEqual(200, status)
        self.assertEqual("pass", first["gate"]["decision"])
        self.assertEqual(1, len(first["chunks"]))
        before = len(self._drops_rows())
        status, second = self._post({"text": "我回来了"},
                                    _script(self.HER_LINE, self.HER_LINE + "主人"))
        self.assertEqual(200, status)
        gate = second["gate"]
        part = gate["parts"][0]
        self.assertEqual("pass", gate["decision"], "二次命中不再丢空")
        self.assertNotEqual([], second["chunks"], "二次命中非红线：chunks 不许空")
        self.assertEqual([self.HER_LINE], [one["text"] for one in second["chunks"]])
        self.assertEqual(self.HER_LINE, second["reply"])
        self.assertEqual(1, gate["out_parts"])
        self.assertEqual("pass", part["decision"])
        self.assertTrue(part["kept_after_rewrite"], "留原话那一格要看得出来")
        self.assertEqual(self.HER_LINE, part["after"], "发的是进门那句原话，不是没改成的新稿")
        self.assertEqual(["反复读", "反复读"], [one["clause_id"] for one in part["hits"]])
        self.assertEqual("ok", part["repeat_status"], "这一关真的比过了")
        self.assertEqual("ok", part["rewrite"]["status"], "重写那一次真的跑了")
        self.assertEqual([], gate["blocked"], "这一泡没被弃，blocked 就不该有它")
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "留原话也要留一行账")
        self.assertEqual("反复读", rows[-1]["clause_id"])
        self.assertIn(AFTER_MARKS["repeat"], rows[-1]["reason"])

    def test_a_noted_line_survives_the_failed_rewrite(self):
        """动作括注里出现了我们没给过的场景：命中，改了还带着同一个场景词 → 留进门原话，不再丢空。

        **这条断言是被新规矩改准的**：以前二次命中即弃。判据照旧看得见（hits 里是「动作带场景」，
        人话点名那个场景词、account 上是一行 action-after-rewrite），只是这一泡不再整泡消失。
        """
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你在干嘛"},
                                     _script(self.NOTED_LINE, "（尾巴在水里轻轻地蜷了一下）"))
        self.assertEqual(200, status)
        gate = payload["gate"]
        part = gate["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertTrue(part["kept_after_rewrite"])
        self.assertNotEqual([], payload["chunks"], "二次命中非红线：chunks 不许空")
        self.assertEqual([self.NOTED_LINE], [one["text"] for one in payload["chunks"]])
        self.assertEqual(self.NOTED_LINE, payload["reply"])
        self.assertEqual("ok", part["action_status"], "这一关真的判过了（不是没跑）")
        self.assertEqual("动作带场景", part["hits"][-1]["clause_id"])
        self.assertIn("水里", part["hits"][-1]["reason"], "拦下她要说出是哪个场景词")
        self.assertEqual(before + 1, len(self._drops_rows()))
        self.assertEqual("动作带场景", self._drops_rows()[-1]["clause_id"])
        self.assertIn(AFTER_MARKS["action"], self._drops_rows()[-1]["reason"])

    def test_a_recited_soul_card_survives_the_failed_rewrite(self):
        """验收一：她把魂那张卡上的话原样念了出来，改了一次还在念 → **原话发出**，不再丢空。

        rewrite.status 是 ok（模型确实改了），但新稿仍撞「复述设定段」——这正是以前会丢掉整泡的
        那一格。现在 decision 是 pass、chunks / reply 非空、账上一行含 audit-after-rewrite；
        audit.status 仍是 ok、hit 照旧带着（面板看得见她引了设定）。
        """
        line = "我嘴上不认，行动先到"
        soul = self.tmp / "soul"
        write_json(soul / "manner.json", {"stance": line})
        store = load(soul)
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你是不是又不承认"},
                                     _script(line, "我嘴上不认，行动倒是先到了。"), store=store)
        self.assertEqual(200, status)
        gate = payload["gate"]
        part = gate["parts"][0]
        self.assertEqual("pass", part["decision"], "二次命中非红线：不许丢掉整泡")
        self.assertEqual("ok", part["rewrite"]["status"], "模型确实改了（改写这回事没出问题）")
        self.assertTrue(part["kept_after_rewrite"])
        self.assertNotEqual([], payload["chunks"], "chunks 不许空")
        self.assertTrue(payload["reply"], "reply 不许空")
        self.assertEqual([line], [one["text"] for one in payload["chunks"]], "发的是进门那句原话")
        self.assertEqual(line, payload["reply"])
        self.assertFalse(part["audit"]["skipped"], "没要求重复，就没有跳过")
        self.assertEqual("ok", part["audit"]["status"], "审计真的比过了（这里该是 ok）")
        self.assertEqual("ok", part["fatal_status"], "红线这一关判过了、不是红线（ok 不是 not-checked）")
        self.assertIsNone(part["fatal"])
        self.assertEqual("复述设定段", part["audit"]["hit"]["kind"])
        self.assertEqual("魂", part["audit"]["hit"]["seg_kind"])
        self.assertEqual("复述设定段", part["hits"][-1]["clause_id"])
        self.assertEqual([], gate["blocked"], "这一泡没被弃，blocked 就不该有它")
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "留原话也要留一行账")
        self.assertEqual("复述设定段", rows[-1]["clause_id"])
        self.assertIn(AFTER_MARKS["audit"], rows[-1]["reason"], "账上要看得出是审计这一关没改掉")

    def test_a_normal_reply_passes_and_keeps_every_chunk_verbatim(self):
        """一句正常回话一处都不改：chunks 的 text 与**不接闸**那一遍逐字相同（diff 为空）。"""
        reply = ("今天风大，我把窗关上了。晚上想喝粥，我给你留了一碗。"
                 "你要是回来晚，我就先不睡。")
        # 先跑过闸那一遍（账上还没有历史、脑里也还没有她这一句），再跑不接闸那一遍做对照：
        # 反过来的话，第一遍会把她这句话记进脑与账，第二遍就变成「复述/复读自己」了。
        status, gated = self._send(text="今天怎么样", reply=reply)
        self.assertEqual(200, status)
        with mock.patch.object(chat, "_gate_round", lambda *args, **kw: None):
            status, plain = self._send(text="今天怎么样", reply=reply)
        self.assertEqual(200, status)
        self.assertEqual([one["text"] for one in plain["chunks"]],
                         [one["text"] for one in gated["chunks"]],
                         "一泡都没被改时 chunks 的 text 要与不接闸时逐字相同")
        gate = gated["gate"]
        self.assertEqual("pass", gate["decision"])
        self.assertEqual([], gate["blocked"])
        self.assertEqual(gate["in_parts"], gate["out_parts"])
        self.assertTrue(all(row["decision"] == "pass" for row in gate["parts"]))
        self.assertEqual(reply, gated["reply"])

    def test_all_parts_dropped_leaves_nothing_and_records_one_drop(self):
        """全被弃掉：chunks=[]、reply=""，弃言账本多一行，reason 里带着 clause_id。

        **这一格换了素材**：二次命中留原话之后，非红线已经走不到「整泡消失」了——能全被弃掉的只剩
        红线（隐私 / 辱骂 / 放弃式：唯一不许留原话的一类）。这句里带着手机号，一泡都发不出去。
        """
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你有电话吗"},
                                     _body("我手机号是 13812345678，你记一下"))
        self.assertEqual(200, status)
        self.assertEqual([], payload["chunks"])
        self.assertEqual("", payload["reply"])
        self.assertEqual("dropped", payload["gate"]["decision"])
        self.assertEqual(0, payload["gate"]["out_parts"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "弃掉的这一泡要留一行弃言账")
        self.assertEqual(FATAL_CLAUSE, rows[-1]["clause_id"])
        self.assertIn(FATAL_CLAUSE, rows[-1]["reason"], "弃言账的 reason 里要能看见 clause_id")
        self.assertEqual(FATAL_BOOK["categories"][0]["id"], payload["gate"]["parts"][0]["fatal"]["category"])

    def test_a_red_line_is_dropped_and_the_panel_shows_which_kind(self):
        """验收二：一句放弃式（「反正我不疼，你别管了」）——红线，整泡丢掉，面板说得出是哪一类。

        红线是唯一不许留原话的一类：二次命中留原话那一条规矩到它这里就停（改一次也不能保证它
        发不出去）。parts[*].fatal 里带着类别与撞上的那几个字，fatal_status 是 fatal（红灯）。
        """
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你还好吗"}, _body(GIVE_UP_LINE))
        self.assertEqual(200, status)
        self.assertEqual("dropped", payload["gate"]["decision"])
        self.assertEqual([], payload["chunks"], "红线不许留原话")
        self.assertEqual("", payload["reply"])
        part = payload["gate"]["parts"][0]
        self.assertEqual(GIVE_UP_CATEGORY["id"], part["fatal"]["category"])
        self.assertEqual(GIVE_UP_CATEGORY["tag"], part["fatal"]["tag"])
        self.assertEqual("fatal", part["fatal_status"], "红灯：这一关真的判出了红线")
        self.assertIn("不疼", part["fatal"]["matched"])
        self.assertFalse(part["kept_after_rewrite"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows))
        self.assertEqual(FATAL_CLAUSE, rows[-1]["clause_id"])
        self.assertIn(_mark_of(GIVE_UP_CATEGORY["tag"]), rows[-1]["reason"])

    def test_the_repeat_window_comes_from_the_data(self):
        """复读窗口不写死：repeat.json 的 rounds 改成 1 之后，隔了一轮的那句就不算复读了。"""
        root = self._mouth_root()
        book = json.loads((root / "repeat.json").read_text(encoding="utf-8"))
        book["rounds"] = 1
        (root / "repeat.json").write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(root)}):
            self._send(text="第一句", reply="第一句的回话，我记得的。")
            self._send(text="第二句", reply="第二句的回话，我也记得的。")
            status, payload = self._send(text="第三句", reply="第一句的回话，我记得的。")
        self.assertEqual(200, status)
        self.assertEqual("pass", payload["gate"]["decision"],
                         "窗口只有 1 轮，隔了一轮的同一句不该算复读")

    def test_no_inject_at_all_is_audit_unavailable_not_a_pass(self):
        """一段注入原文都取不到时交给审计的是 None：审计报 audit-unavailable，不许伪造通过。"""
        class _Bare:
            def raw(self, block):
                return {}

        table = warehouse.load("audit")
        with mock.patch.object(chat, "_rule_text", lambda: ""):
            inject = chat._inject("", {"anchors": {}, "recalled": [], "heart": {}},
                                 _Bare(), table)
        self.assertIsNone(inject, "一段都取不到时要交 None，不是空清单")
        seen = mouth_audit.audit("一句正常的话", inject=None)
        self.assertEqual("audit-unavailable", seen.status)
        self.assertIsNone(seen.hit)

# —— 动作配额：从装配传进闸（本轮补的） ————————————————

    QUOTA_LINE = "（尾巴晃了一下）可爱归可爱，那也不是胖。"      # 踩配额那一句（正文还在）
    NOTED_QUOTA_LINE = "（尾巴在水里晃了一下）可爱归可爱，那也不是胖。"

    def _block_actions(self):
        """把装配的动作配额顶满：账上最近一轮她自己写过动作行，blocked 就是真。

        走的是装配那边现成的算法（repeat.count_with_actions 数最近 quota.window 轮），
        这里只负责造一轮历史，不另算一遍配额。
        """
        recent.append({"ts": "2025-01-15T14:00:00+08:00", "who": "主人",
                       "said": "你刚才那个样子", "reply": "（耳尖动了动）"})

    def test_quota_forbid_strips_the_action_line_and_keeps_the_body(self):
        """装配标了不许写动作：这一泡不被整条丢，动作行剥掉、正文留下，账照记。"""
        self._block_actions()
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你是不是胖了"}, _body(self.QUOTA_LINE))
        self.assertEqual(200, status)
        self.assertTrue(payload["action"]["quota"]["blocked"], "这一轮的装配配额该是顶满的")
        gate = payload["gate"]
        self.assertEqual("forbid", gate["quota"], "blocked 为真要翻成 forbid")
        part = gate["parts"][0]
        self.assertEqual("ok", part["quota_status"], "配额这一关真的判过了")
        self.assertEqual("rewritten", part["decision"], "配额命中不能整条丢：剥行动作、正文留下")
        self.assertEqual("动作超配额", part["hits"][-1]["clause_id"])
        self.assertEqual(1, len(payload["chunks"]), "这一泡该留着")
        self.assertEqual("可爱归可爱，那也不是胖。", payload["chunks"][0]["text"],
                         "chunks 里那一泡要恰为正文")
        self.assertEqual("可爱归可爱，那也不是胖。", payload["reply"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "剥行救回来的那一笔也要记账")
        self.assertEqual("动作超配额", rows[-1]["clause_id"])
        self.assertIn("action-over-quota", rows[-1]["reason"])

    def test_quota_allow_leaves_the_action_note_gate_in_charge(self):
        """装配标了放行（sparing → allow）：同一句走动作括注那一关，命中就 dropped。

        重写那一句仍带着同一个场景词：改一次还踩，按老规矩弃掉整泡。
        """
        status, payload = self._post({"text": "你是不是胖了"},
                                     _script(self.NOTED_QUOTA_LINE,
                                             "（尾巴在水里晃了晃）可爱归可爱，那也不是胖。"))
        self.assertEqual(200, status)
        self.assertFalse(payload["action"]["quota"]["blocked"], "这一轮的装配配额该是没顶满")
        gate = payload["gate"]
        self.assertEqual("allow", gate["quota"], "blocked 为假要翻成 allow")
        part = gate["parts"][0]
        self.assertEqual("ok", part["quota_status"], "标了 allow 也算判过")
        self.assertEqual("动作带场景", part["hits"][-1]["clause_id"], "拦下它的是动作括注那一关")
        self.assertEqual("pass", part["decision"], "二次命中非红线：留原话，不再丢空（断言已改准）")
        self.assertTrue(part["kept_after_rewrite"])
        self.assertEqual([self.NOTED_QUOTA_LINE], [one["text"] for one in payload["chunks"]])

    def test_assembly_without_quota_is_not_checked_not_allow(self):
        """装配没标配额：交给闸的是 None（记 not-checked），chunks 与不接闸时逐字相同。"""
        self.assertIsNone(chat._quota({}), "回包里没有 action 时不许猜")
        self.assertIsNone(chat._quota({"action": {"pool": [], "rule": "随便"}}), "没有 quota 一项时不许猜")
        self.assertIsNone(chat._quota({"action": {"quota": {"blocked": "true"}}}),
                          "blocked 不是布尔时不许猜成 allow")

        reply = ("今天风大，我把窗关上了。晚上想喝粥，我给你留了一碗。"
                 "你要是回来晚，我就先不睡。")
        real = conversation.answer

        def without_quota(text, who="", **kw):
            """装配没标配额的样子：回包里没有 action.quota 这一项。"""
            kw.setdefault("transport", _body(reply))
            kw.setdefault("config", CONFIG)
            kw.setdefault("env", ENV)
            payload = real(text, who, **kw)
            payload["action"].pop("quota", None)
            return payload

        with mock.patch.object(conversation, "answer", without_quota):
            status, gated = api.handle("POST", "/chat", {"text": "今天怎么样"},
                                       self.store, brain=self.brain)
        self.assertEqual(200, status)
        gate = gated["gate"]
        self.assertIsNone(gate["quota"], "装配没标时交给闸的是 None，不许猜成 allow")
        self.assertEqual(["not-checked"] * len(gate["parts"]),
                         [row["quota_status"] for row in gate["parts"]],
                         "没标就是这一关没跑，不是通过")
        self.assertEqual("pass", gate["decision"])
        with mock.patch.object(conversation, "answer", without_quota):
            with mock.patch.object(chat, "_gate_round", lambda *args, **kw: None):
                status, plain = api.handle("POST", "/chat", {"text": "今天怎么样"},
                                           self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertEqual([one["text"] for one in plain["chunks"]],
                         [one["text"] for one in gated["chunks"]],
                         "配额没标时 chunks 要与不接闸时逐字相同")

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
        self.assertEqual({"kind", "shape", "verdict_reason", "delivered",
                          "cooling", "topic", "note"}, set(section))
        self.assertFalse(section["cooling"], "没在冷却：这一格要如实写 false，不躲不省")
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
        """正面答了：refusal 只剩「没在冷却」这一格（那四样不加空壳），两本账一行都不添。"""
        with self._topics("猫"):
            status, payload = self._send(text=self.ASKED_CAT, reply=self.ANSWERED)
        self.assertEqual(200, status)
        section = payload["refusal"]
        self.assertEqual({"cooling", "topic", "note"}, set(section),
                         "没判出拒绝就不添那四样，冷却那一格照写")
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

    # —— 冷却里主动拒（这一轮加的） ————————————————

    ASKED_HOSPITAL = "医院那事怎么样了？"

    @contextlib.contextmanager
    def _cooling(self, name: str = "医院"):
        """在临时心根里真造一次冷却：词表指到临时文件、ingest 一次；退出时词表补丁自动还原。

        心那边收话题要先认词表，所以「指词表」与「记一次拒绝」必须在同一个环境补丁里做。
        全程只碰 setUp 里那两个临时根：真实心根、真实家根一个字节都不写。
        """
        with self._topics(name):
            heart_refuse.ingest(name, "refused-topic")
            yield

    def test_a_cooling_topic_is_refused_without_asking_the_model(self):
        """冷却里的話題：她说 refusal 那一句，模型**一次都没调**，两本账一行都不添。

        四件事一起断言，缺一条这条短路就不成立：说的是数据里那句拒绝；refusal 节带 topic /
        remaining_min / line；假 transport 的调用计数是 0；心与我们自己的账行数前后不变（已在冷却，
        再投一次就是重复记账）。
        """
        said = mouth_refusal.say("refused-topic")
        with self._cooling():
            heart_before = len(self._heart_rows())
            capture = []
            status, payload = self._post({"text": self.ASKED_HOSPITAL},
                                         _body(LONG_REPLY, capture))
            heart_after = len(self._heart_rows())
        self.assertEqual(200, status)
        self.assertEqual(said, payload["reply"], "冷却里说的是 refusal 那一句，不是模型那句")
        section = payload["refusal"]
        self.assertTrue(section["cooling"], "refusal.cooling 要如实写 true")
        self.assertEqual("医院", section["topic"])
        self.assertEqual("refused-topic", section["reason"])
        self.assertIsInstance(section["remaining_min"], float)
        self.assertGreater(section["remaining_min"], 0.0, "还剩多久要有数")
        self.assertIn("医院", section["line"], "面板那一句要带上话题名")
        self.assertIn("还剩", section["line"])
        self.assertEqual(said, section["say"])
        self.assertIsNone(section["event"], "冷却里不投事件：这一格必须是 null")
        self.assertEqual([], capture, "冷却里一次模型都不调（验收那一格：调用计数为 0）")
        self.assertEqual(heart_before, heart_after, "心那边的 refusals.json 行数不变")
        self.assertEqual([], self._refusal_rows(), "我们自己的 refuse.jsonl 也不添一行")
        self.assertEqual(said, "".join(one["text"] for one in payload["chunks"]),
                         "chunks 仍走 split + pace：拼回去就是那一句原文")

    def test_the_cooled_refusal_still_goes_through_the_gate(self):
        """那一句拒绝仍过八股闸：条款判了；复述审计没跑（audit-unavailable）；重写一次请求都不发。"""
        with self._cooling():
            capture = []
            status, payload = self._post({"text": self.ASKED_HOSPITAL},
                                         _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        gate = payload["gate"]
        self.assertEqual("pass", gate["decision"])
        self.assertEqual(gate["in_parts"], gate["out_parts"])
        self.assertIn("冷却", gate["note"])
        for part in gate["parts"]:
            self.assertEqual("audit-unavailable", part["audit"]["status"],
                             "没有装配就没有可比的原话：判不了，不是通过")
            self.assertIn(part["rewrite"]["status"], ("not-tried", "failed"),
                          "这一轮一次重写请求都不发：没命中就是 not-tried，命中就是留原话的 failed")
        self.assertEqual([], capture, "连重写那一次也没发")

    def test_the_same_question_outside_the_cooldown_answers_normally(self):
        """同一句、不在冷却里：照常装配与调模型（一次），refusal 节如实写「没在冷却」。"""
        with self._topics("医院"):
            capture = []
            status, payload = self._post({"text": self.ASKED_HOSPITAL},
                                         _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(LONG_REPLY, payload["reply"])
        self.assertEqual(1, len(capture), "这一轮真问了模型一次")
        section = payload["refusal"]
        self.assertFalse(section["cooling"], "没在冷却要说 false，不许省掉这一格")
        self.assertEqual("医院", section["topic"], "认得出话题就报出来")
        self.assertNotIn("kind", section, "没判出拒绝就不添那四样")
        self.assertEqual([], self._heart_rows())

    def test_an_unrecognised_topic_is_not_judged(self):
        """抽不到话题（词表里没有那句里的字）：冷却这一关不判，照常说话，topic 如实 null。"""
        with self._topics("猫"):
            capture = []
            status, payload = self._post({"text": "今天天气不错。"},
                                         _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(LONG_REPLY, payload["reply"])
        self.assertEqual(1, len(capture))
        section = payload["refusal"]
        self.assertFalse(section["cooling"])
        self.assertIsNone(section["topic"], "认不出就是 null，不替她猜一个话题")
        self.assertEqual([], self._heart_rows())

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

        # 四个旧键一个都不许动，只许多出 reconciled 与 rounds（补记读数对页面也冻结）
        self.assertEqual({"turns_loaded", "omitted", "notice", "reconciled", "rounds"},
                         set(payload["continuity"]))
        self.assertEqual(0, payload["continuity"]["turns_loaded"])
        self.assertEqual(0, payload["continuity"]["omitted"])
        self.assertEqual("", payload["continuity"]["notice"], "没省任何一轮时 notice 该是空串")
        self.assertEqual(0, payload["continuity"]["reconciled"], "一轮都没补，却报成补了")
        self.assertEqual([], payload["continuity"]["rounds"], "没有历史，rounds 该是空数组")

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
        self.assertEqual(12, len(continuity["rounds"]), "rounds 要为装进提示词的每一轮给一行")
        self.assertEqual({"ts", "fidelity", "msg_id"}, set(continuity["rounds"][0]))
        self.assertEqual(0, continuity["reconciled"], "造出来的 12 轮都是亲历，不该有补记")
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

    def test_a_round_the_ledger_missed_is_backfilled_over_http(self):
        """脑里今日记了、最近几轮没落的一条：装配前补进来，回包里说清补了几条。"""
        self.brain.ingest(who="主人", text="我上周把片子落在诊室了")   # 今日在役，账上没有
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        continuity = payload["continuity"]
        self.assertEqual(1, continuity["reconciled"], "补了一条却没在回包里说")
        self.assertEqual(recent.RECONCILED, continuity["rounds"][0]["fidelity"],
                         "rounds 里那一轮没标 reconciled")
        self.assertIn(recent.BACKFILL_MARK, capture[0]["messages"][0]["content"],
                      "提示词里没有「（补记）」，模型会把补的当原话")

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

    # —— 豁免：对方要她再说一遍（本轮加的） ————————————

    ASKED = "你刚才说的那句再说一遍"          # 一句实打实的「再说一遍」请求

    def test_a_repeat_the_user_asked_for_is_not_dropped(self):
        """上一轮她说过这一句；这一轮对方说「你刚才说的那句再说一遍」，她原样重复。

        验收一：decision 仍是 pass、text 原样、账本多一行 reason 里带 skipped-by-request、
        repeat_status 记成跳过那个状态——**她重复是对的，别吞**。
        """
        status, first = self._send(text="我回来了", reply=self.HER_LINE)
        self.assertEqual(200, status)
        self.assertEqual("pass", first["gate"]["decision"])
        before = len(self._drops_rows())
        status, payload = self._post({"text": self.ASKED}, _body(self.HER_LINE))
        self.assertEqual(200, status)
        gate = payload["gate"]
        self.assertEqual("pass", gate["decision"], "对方要她重复，这一句不该被判废")
        self.assertEqual(self.HER_LINE, payload["reply"])
        self.assertEqual([self.HER_LINE], [one["text"] for one in payload["chunks"]])
        self.assertEqual([], gate["blocked"])
        part = gate["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual(self.HER_LINE, part["before"])
        self.assertEqual(self.HER_LINE, part["after"], "放行就是原样放行，一个字都不改")
        self.assertEqual(mouth_repeat.STATUS_SKIPPED, part["repeat_status"])
        self.assertEqual("not-tried", part["rewrite"]["status"], "豁免这一泡一次重写都不发")
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "跳过也要留一行账")
        self.assertEqual(SKIP_CLAUSE, rows[-1]["clause_id"], "账本里是数据里那个名字")
        self.assertIn(SKIP_MARK, rows[-1]["reason"])

    def test_the_same_repeat_without_a_request_is_judged_but_kept(self):
        """同一句复读、对方这句没有重复请求（「今天吃啥」）：判据照旧判，但不再丢空。

        **这条断言是被新规矩改准的**：以前这里 dropped。现在二次命中留进门原话（账上一行
        repeat-after-rewrite）——「没有豁免」这件事仍然看得见：repeat_status 是 ok（真的判过、
        判废了），不是 skipped-by-request，账本那行里也不带那个跳过记号。
        """
        status, first = self._send(text="我回来了", reply=self.HER_LINE)
        self.assertEqual(200, status)
        before = len(self._drops_rows())
        status, payload = self._post({"text": "今天吃啥"},
                                     _script(self.HER_LINE, self.HER_LINE + "主人"))
        self.assertEqual(200, status)
        gate = payload["gate"]
        part = gate["parts"][0]
        self.assertEqual("pass", part["decision"], "没要求重复就没有豁免，但也不许丢空")
        self.assertNotEqual([], payload["chunks"], "二次命中非红线：chunks 不许空")
        self.assertEqual([self.HER_LINE], [one["text"] for one in payload["chunks"]])
        self.assertEqual(self.HER_LINE, payload["reply"])
        self.assertEqual("ok", part["repeat_status"], "这一关真的比过了、判废了")
        self.assertEqual("ok", part["rewrite"]["status"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows))
        self.assertEqual(REPEAT_CLAUSE, rows[-1]["clause_id"])
        self.assertIn(AFTER_MARKS["repeat"], rows[-1]["reason"])
        self.assertNotIn(SKIP_MARK, rows[-1]["reason"], "没有豁免：这一行不许带跳过记号")

    def test_a_request_without_a_repeat_records_nothing(self):
        """他虽说了「再说一遍」，但这一句本来就不重复：没有「判废」可放，也就不记那一笔账。"""
        status, first = self._send(text="我回来了", reply=self.HER_LINE)
        self.assertEqual(200, status)
        before = len(self._drops_rows())
        status, payload = self._post({"text": self.ASKED}, _body("碗我放桌上了，主人回来啦。"))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual("ok", part["repeat_status"], "比过了、没重复——不是跳过")
        self.assertEqual(before, len(self._drops_rows()), "没撞上就没有账可记")

    # —— 豁免：对方要她重复时的复述审计（本轮加的） ————————

    SETTING = "嘴上不认，行动先到：拒绝的话说完，事已经做了"   # 魂里那句原文（设定类，照搬就拦）
    BEFORE_LINE = "主人回来啦，我把碗放桌上了。"                # 她上一轮说过的那句（开场就是这几个字）
    RECITED = "主人回来啦，我嘴上不认，行动先到。"               # 这一轮她照做：同一开场 + 复述设定段
    ASKED_AGAIN = "再说一遍你刚才说的是什么"                    # 现场那句（命中请求标记）

    def _soul_store(self, line: str):
        """只带一张方式卡的魂：注入的原文因此只有这一句，复述与不复述一眼分得开。"""
        soul = self.tmp / "soul"
        write_json(soul / "manner.json", {"stance": line})
        return load(soul)

    def test_a_recitation_the_user_asked_for_passes_verbatim(self):
        """现场复现：对方说「再说一遍你刚才说的是什么」，她照做 → 复述设定段 + 同一动作行。

        验收一：decision 仍是 pass、chunks 与 reply 非空、text 与输入逐字相同；账上两行（内容重复
        与复述审计各一行、各带自己的记号）；审计那节 hit 照旧在（面板看得见她引了设定）且
        skipped=true。**她照办的那一下不该被吞**。
        """
        store = self._soul_store(self.SETTING)
        status, first = self._post({"text": "我回来了"}, _body(self.BEFORE_LINE), store=store)
        self.assertEqual(200, status)
        self.assertEqual("pass", first["gate"]["decision"], "上一轮那句既不复读也没复述")
        before = len(self._drops_rows())
        status, payload = self._post({"text": self.ASKED_AGAIN}, _body(self.RECITED), store=store)
        self.assertEqual(200, status)
        gate = payload["gate"]
        self.assertEqual("pass", gate["decision"], "对方要她重复，这一句不该被判废")
        self.assertEqual([self.RECITED], [one["text"] for one in payload["chunks"]])
        self.assertEqual(self.RECITED, payload["reply"])
        self.assertTrue(payload["chunks"], "chunks 不许空")
        self.assertTrue(payload["reply"], "reply 不许空")
        self.assertEqual([], gate["blocked"])
        part = gate["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual(self.RECITED, part["before"])
        self.assertEqual(self.RECITED, part["after"], "放行就是原样放行，一个字都不改")
        self.assertTrue(part["audit"]["skipped"], "审计这一关也按他说的放过")
        self.assertIsNotNone(part["audit"]["hit"], "面板上要看得见她引了设定")
        self.assertEqual("ok", part["audit"]["status"])        # 状态仍是 ok：真的比过了
        self.assertEqual(mouth_repeat.STATUS_SKIPPED, part["repeat_status"])
        self.assertEqual("not-tried", part["rewrite"]["status"], "豁免这两泡一次重写都不发")
        rows = self._drops_rows()
        self.assertEqual(before + 2, len(rows), "两关各记一行账，不合并")
        self.assertEqual(SKIP_CLAUSE, rows[-2]["clause_id"])
        self.assertIn(SKIP_MARK, rows[-2]["reason"])
        self.assertEqual(AUDIT_SKIP_CLAUSE, rows[-1]["clause_id"])
        self.assertIn(AUDIT_SKIP_MARK, rows[-1]["reason"])
        self.assertNotEqual(rows[-2]["reason"], rows[-1]["reason"], "两行各是各的说法")

    def test_the_same_recitation_without_a_request_is_judged_but_kept(self):
        """同一句照搬、对方没有请求（「今天吃啥」）：豁免不许外溢——判据照旧判，但不再丢空。

        **这条断言是被新规矩改准的**：以前这里 dropped（上一轮验收要求「同一句仍 dropped」）。
        规矩改成「二次命中留原话」之后，这一泡是 pass、发的是她那句原话，账上一行
        audit-after-rewrite；「没有豁免」仍然看得见：audit.skipped 是 false、audit.hit 照旧带着。
        """
        store = self._soul_store(self.SETTING)
        before = len(self._drops_rows())
        status, payload = self._post({"text": "今天吃啥"},
                                     _script(self.RECITED, self.RECITED + "主人"), store=store)
        self.assertEqual(200, status)
        gate = payload["gate"]
        part = gate["parts"][0]
        self.assertEqual("pass", part["decision"], "没有豁免，但也不许丢空")
        self.assertNotEqual([], payload["chunks"], "二次命中非红线：chunks 不许空")
        self.assertEqual([self.RECITED], [one["text"] for one in payload["chunks"]])
        self.assertFalse(part["audit"]["skipped"], "没有请求就不许记成跳过")
        self.assertIsNotNone(part["audit"]["hit"])
        self.assertEqual("ok", part["audit"]["status"])
        self.assertEqual(AUDIT_BOOK["verdicts"]["setting"], part["hits"][-1]["clause_id"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows))
        self.assertEqual(AUDIT_BOOK["verdicts"]["setting"], rows[-1]["clause_id"])
        self.assertIn(AFTER_MARKS["audit"], rows[-1]["reason"])

    def test_a_request_that_recites_nothing_records_only_nothing(self):
        """他虽说了「再说一遍」，但这一句本来就没复述也没重复：没有「判废」可放，账一行都不记。"""
        store = self._soul_store(self.SETTING)
        before = len(self._drops_rows())
        status, payload = self._post({"text": self.ASKED_AGAIN}, _body("今天风大，我把窗关好了。"),
                                     store=store)
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertFalse(part["audit"]["skipped"])
        self.assertIsNone(part["audit"]["hit"])
        self.assertEqual(before, len(self._drops_rows()), "没撞上就没有账可记")

    # —— 重写：命中先改一次（本轮加的） ————————————

    def test_a_repeated_line_is_rewritten_instead_of_dropped(self):
        """复读命中 → 假 transport 换个说法 → 通过：chunks 里是新稿，rewrite.status=ok。"""
        status, first = self._send(text="我回来了", reply=self.HER_LINE)
        self.assertEqual(200, status)
        better = "碗搁桌上了，主人可算回来了。"
        before = len(self._drops_rows())
        status, payload = self._post({"text": "我回来了"}, _script(self.HER_LINE, better))
        self.assertEqual(200, status)
        gate = payload["gate"]
        part = gate["parts"][0]
        self.assertEqual("rewritten", part["decision"])
        self.assertEqual("rewritten", gate["decision"])
        self.assertEqual("ok", part["rewrite"]["status"])
        self.assertEqual(REPEAT_CLAUSE, part["hits"][0]["clause_id"])
        self.assertEqual(self.HER_LINE, part["before"])
        self.assertEqual(better, part["after"])
        self.assertTrue(part["rewrite"]["from"], "from 要放一小截原话")
        self.assertTrue(part["rewrite"]["to"], "to 要放一小截新稿")
        self.assertEqual([better], [one["text"] for one in payload["chunks"]])
        self.assertEqual(better, payload["reply"])
        self.assertEqual([], gate["blocked"])
        self.assertEqual(before, len(self._drops_rows()), "改成了就不该记弃言账")

    def test_a_recited_setting_is_rewritten_once(self):
        """复述设定段命中 → 假 transport 换成她自己的说法 → 通过：audit 命中过、最终却没丢。"""
        line = "我嘴上不认，行动先到"
        said = "我嘴上不服软，可该做的都先做了。"
        soul = self.tmp / "soul"
        write_json(soul / "manner.json", {"stance": line})
        store = load(soul)
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你是不是又不承认"},
                                     _script(line, said), store=store)
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("ok", part["audit"]["status"], "审计真的比过了")
        self.assertEqual("复述设定段", part["audit"]["hit"]["kind"], "进门那一次确实撞上了")
        self.assertEqual("rewritten", part["decision"])
        self.assertEqual("ok", part["rewrite"]["status"])
        self.assertEqual([said], [one["text"] for one in payload["chunks"]])
        self.assertEqual(said, payload["reply"])
        self.assertEqual([], payload["gate"]["blocked"])
        self.assertEqual(before, len(self._drops_rows()))

    def test_a_failed_rewrite_keeps_her_line_instead_of_silence(self):
        """超时 / 回空串 / 回与原文逐字相同：三种都保留原文，账本一行带 rewrite-failed，输出不为空。"""
        cases = {
            "timeout": _script_then_raise(self.HER_LINE, TimeoutError("模型没回话")),
            "empty": _script(self.HER_LINE, ""),
            "same": _script(self.HER_LINE, self.HER_LINE),
        }
        for name, transport in cases.items():
            with self.subTest(case=name):
                self._send(text="我回来了", reply=self.HER_LINE)      # 先造一轮历史
                before = len(self._drops_rows())
                status, payload = self._post({"text": "我回来了"}, transport)
                self.assertEqual(200, status)
                gate = payload["gate"]
                part = gate["parts"][0]
                self.assertEqual("pass", part["decision"], "改不动也不许弃")
                self.assertEqual(self.HER_LINE, part["before"])
                self.assertEqual(self.HER_LINE, part["after"], "留着的是原话，一个字不改")
                self.assertEqual([self.HER_LINE], [one["text"] for one in payload["chunks"]])
                self.assertEqual(self.HER_LINE, payload["reply"])
                self.assertTrue(payload["reply"].strip(), "改不动也绝不许输出空")
                self.assertEqual("failed", part["rewrite"]["status"])
                self.assertTrue(part["rewrite"]["why"], "为什么改不动要说出来")
                self.assertTrue(part["hits"], "命中留着：闸本想过改它")
                rows = self._drops_rows()
                self.assertEqual(before + 1, len(rows), "改不动也要记一行账")
                self.assertEqual(REPEAT_CLAUSE, rows[-1]["clause_id"])
                self.assertIn(KEPT_MARK, rows[-1]["reason"])

    def test_a_rewrite_that_drops_a_number_keeps_the_original(self):
        """不合规（丢了数字）：判整段重编，原话留着发，账本仍记一行 rewrite-failed。"""
        line = "主人回来啦，3点我把碗放桌上了。"
        status, first = self._send(text="我回来了", reply=line)
        self.assertEqual(200, status)
        before = len(self._drops_rows())
        status, payload = self._post({"text": "我回来了"},
                                     _script(line, "主人回来啦，三点我把碗放桌上了。"))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual("failed", part["rewrite"]["status"])
        self.assertIn("3", part["rewrite"]["why"], "哪一段数字丢了要说出来")
        self.assertEqual(line, payload["chunks"][0]["text"])
        self.assertEqual(line, payload["reply"])
        self.assertTrue(payload["reply"].strip())
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows))
        self.assertIn(KEPT_MARK, rows[-1]["reason"])

    def test_the_rewrite_budget_comes_from_the_data(self):
        """budget_ms 改成 10 毫秒、假 transport 睡 200 毫秒：超时 → 保留原话，账本带 rewrite-failed。

        这条验的是真超时（线程等出来的硬时限），不是「transport 自己抛」那条路；顺带验
        transport 那一侧的时限也跟着收窄到同一个数。
        """
        root = self._mouth_root()
        book = json.loads((root / "rewrite.json").read_text(encoding="utf-8"))
        book["budget_ms"] = 10
        (root / "rewrite.json").write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        calls = []

        def slow(url, headers, payload, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                data = {"choices": [{"message": {"content": self.HER_LINE}}]}
            else:
                time.sleep(0.2)
                data = {"choices": [{"message": {"content": "碗我放桌上了。"}}]}
            return 200, json.dumps(data, ensure_ascii=False).encode("utf-8")

        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(root)}):
            self._send(text="我回来了", reply=self.HER_LINE)
            before = len(self._drops_rows())
            status, payload = self._post({"text": "我回来了"}, slow)
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("failed", part["rewrite"]["status"])
        self.assertIn("10", part["rewrite"]["why"], "时限取自数据")
        self.assertEqual(self.HER_LINE, payload["reply"])
        self.assertTrue(payload["reply"].strip())
        self.assertGreaterEqual(len(calls), 2)
        self.assertAlmostEqual(0.01, calls[1], places=3, msg="transport 那一侧的时限也一起收窄")
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows))
        self.assertIn(KEPT_MARK, rows[-1]["reason"])

    def test_the_parts_carry_the_rewrite_read_for_every_chunk(self):
        """没命中的那泡也要交代重写读数：四个键齐全、状态是 not-tried（不是空壳）。"""
        status, payload = self._send()
        self.assertEqual(200, status)
        for row in payload["gate"]["parts"]:
            self.assertEqual(REWRITE_KEYS, set(row["rewrite"]))
            self.assertEqual("not-tried", row["rewrite"]["status"])
            self.assertTrue(row["rewrite"]["why"], "没跑也要说一句为什么没跑")

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

    # —— 称呼接进出口（本轮加的）————————————————

    def _fresh_book(self):
        """换一本新的会话账（＝换一个进程）：模块级那本是进程内的，用例之间必须互不串味。"""
        book = mouth_address.SessionAddressBook()
        patcher = mock.patch.object(chat, "ADDRESS_BOOK", book)
        patcher.start()
        self.addCleanup(patcher.stop)
        return book

    def _has_text(self, line: str) -> bool:
        """脑的今日档里有没有这一条原文（投过没投过，查得到才算数）。"""
        return line in [row.get("text") for row in self.brain.today_entries()]

    def test_rename_is_switched_and_sticks_for_the_next_round(self):
        """第一轮「以后叫我老李」：当场换 + 投脑一条亲历记忆 + 记进会话账；第二轮不要求也还在。"""
        book = self._fresh_book()
        table = mouth_address.load_table()
        status, first = self._post({"text": "以后叫我老李", "who": "主人"}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual({"who", "address", "source"}, set(first["addressed"]))
        self.assertEqual("主人", first["addressed"]["who"])
        self.assertEqual("老李", first["addressed"]["address"])
        self.assertEqual(table["source"]["rename"], first["addressed"]["source"])
        self.assertEqual({"主人": "老李"}, book.snapshot(), "会话账上没记下这一笔")
        self.assertTrue(self._has_text(mouth_address.rename_memory("主人", "老李")["text"]),
                        "那条亲历记忆没投进脑")

        status, second = self._post({"text": "今天有点累", "who": "主人"},
                                    _body("那就早点睡，我把灯关了。"))
        self.assertEqual(200, status)
        self.assertEqual("老李", second["addressed"]["address"], "没重新要求，叫法也该还在")
        self.assertEqual(table["source"]["session"], second["addressed"]["source"])

    def test_a_new_process_falls_back_to_the_directory(self):
        """换一个进程（新建一本账）：会话覆盖没了，回到人名录档位——「只活这个会话」。"""
        self._fresh_book()
        table = mouth_address.load_table()
        self._post({"text": "以后叫我老李", "who": "主人"}, _body(LONG_REPLY))
        self._fresh_book()
        status, again = self._post({"text": "今天有点累", "who": "主人"},
                                   _body("那就早点睡，我把灯关了。"))
        self.assertEqual(200, status)
        self.assertEqual(table["source"]["directory"], again["addressed"]["source"])
        self.assertNotEqual("老李", again["addressed"]["address"])

    def test_a_banned_self_name_falls_back_and_is_not_remembered(self):
        """违禁自称过不了 self_guard：退中性叫法，会话账不记、那条记忆也不投——红线没被绕过。"""
        book = self._fresh_book()
        table = mouth_address.load_table()
        banned = table["self_guard"]["words"][0]
        status, payload = self._post({"text": "以后叫我" + banned, "who": "主人"}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual(table["fallback"]["address"], payload["addressed"]["address"])
        self.assertEqual(table["source"]["fallback"], payload["addressed"]["source"])
        self.assertEqual({}, book.snapshot(), "被拦下的叫法不许记进会话账")
        self.assertFalse(self._has_text(mouth_address.rename_memory("主人", banned)["text"]),
                         "被拦下的叫法不许投给脑")

    # —— 借阅读数：这一轮借了家里哪几份词条、各用掉几条（本轮加的）————

    def _borrow_rows(self) -> list:
        path = self.tmp / "house" / "名片盒" / "borrow.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def _readouts(self) -> dict:
        path = self.tmp / "house" / "名片盒" / "借阅读数.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))["readouts"]

    def test_a_round_registers_the_borrow_and_writes_the_readout(self):
        """一轮之后：家里出现借阅条（source 是这一层真读过的那几份），读数按本轮真用掉的条数落盘。"""
        status, payload = self._send()
        self.assertEqual(200, status)
        section = payload["borrow"]
        self.assertEqual({"status", "files", "note"}, set(section))
        self.assertEqual("ok", section["status"], section["note"])
        sources = [one["source"] for one in section["files"]]
        self.assertEqual(["clauses.json", "policy.json"], sources)

        rows = self._borrow_rows()
        self.assertEqual(sources, [row["source"] for row in rows], "一份词条一条借阅条")
        for row in rows:
            self.assertTrue(row["borrow_id"].startswith("借-"), row["borrow_id"])
            self.assertEqual(64, len(row["sha256"]), "认身份靠全量指纹")
            self.assertEqual(row["sha256"][:12], row["version"], "version 是 sha256 前 12 位")

        readouts = self._readouts()
        for one in section["files"]:
            self.assertEqual(one["learned"], readouts[one["borrow_id"]]["learned"])
            self.assertIsInstance(one["learned"], int)
        clauses = next(one for one in section["files"] if one["source"] == "clauses.json")
        self.assertEqual(ENABLED_CLAUSES, clauses["learned"], "有泡可过闸：启用条款逐条比过，整表算上")
        self.assertIn(clauses["borrow_id"], readouts, "读数要真落一行")

        ledger = home_residents.borrow_ledger(root=self.tmp / "house")
        self.assertEqual("已接入", ledger["readout_status"])
        self.assertEqual(2, len(ledger["borrows"]), "一 source 一条")
        for row in ledger["borrows"]:
            self.assertEqual("已接入", row["readout_status"], row)
        self.assertEqual("已接入", home_residents.read_learned(root=self.tmp / "house")["status"])

    def test_the_second_round_registers_nothing_new_and_refreshes_the_readout(self):
        """第二轮：同一 source 只有一条借阅条，读数按本轮实际用到的条目数更新（整表 → 0）。"""
        status, payload = self._send()
        self.assertEqual(200, status)
        first = self._borrow_rows()
        strict = next(row for row in first if row["source"] == "clauses.json")
        self.assertEqual(ENABLED_CLAUSES, self._readouts()[strict["borrow_id"]]["learned"])

        # 第二轮的应答在装配那一步就被整句弃掉（一泡都没出来）：这一轮一条条款都没比过，
        # 读数该更新成 0——0 是一次真实读数（这一轮没用上），不是缺失。
        status, payload = self._post({"text": "今天吃什么"}, _body("总而言之，汤有点咸。"))
        self.assertEqual(200, status)
        self.assertEqual(0, payload["gate"]["in_parts"], "这一句该在装配那一步就被整句弃掉")
        second = self._borrow_rows()
        self.assertEqual([row["borrow_id"] for row in first],
                         [row["borrow_id"] for row in second], "第二轮不许再登记一条")
        self.assertEqual(["clauses.json", "policy.json"], [row["source"] for row in second])
        learned = next(one for one in payload["borrow"]["files"]
                       if one["source"] == "clauses.json")["learned"]
        self.assertEqual(0, learned, "本轮一条条款都没比过")
        self.assertEqual(0, self._readouts()[strict["borrow_id"]]["learned"],
                         "0 也要写进读数单，不许干脆不写")

    def test_a_home_that_cannot_be_written_does_not_eat_her_words(self):
        """家里不可写：她照常说话，返回体 borrow.status=failed 并写清原因——回复一个字节都不少。"""
        house = self.tmp / "house"
        os.chmod(str(house), 0o500)
        self.addCleanup(os.chmod, str(house), 0o700)
        status, payload = self._send()
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(LONG_REPLY, payload["reply"], "投递失败绝不许动她的话")
        self.assertTrue(payload["chunks"], "泡也照发")
        section = payload["borrow"]
        self.assertEqual("failed", section["status"])
        self.assertEqual([], section["files"])
        self.assertIn("PermissionError", section["note"], section["note"])
        self.assertIn("读数", section["note"])
        self.assertEqual([], self._borrow_rows(), "不可写时一条都没落地")
        self.assertEqual({}, self._readouts())

if __name__ == "__main__":
    unittest.main()
