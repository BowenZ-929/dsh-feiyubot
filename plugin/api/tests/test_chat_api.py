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
from organs.brain import model as brain_model
from organs.home import anchors as home_anchors
from organs.home import cards
from organs.home import residents as home_residents
from organs.heart import energy
from organs.heart import gate as heart_gate
from organs.heart import model as heart_model
from organs.heart import mood
from organs.heart import refuse as heart_refuse
from organs.heart import store as heart_store
from organs.mind import conversation, events, llm, recent, usage
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
# 私密记忆那一节的判据全在数据里：栏名、取值、两个场景值、边界话、事件形状、记号、摘要截几个字
AUDIENCE = json.loads((MOUTH_DATA / "refusal.json").read_text(
    encoding="utf-8"))["audience"]
# 账本两态那一节（名字与两句「没出门」的说法）：测试里一个字都不写死
STATES = json.loads((MOUTH_DATA / "refusal.json").read_text(encoding="utf-8"))["states"]


def _section(text: str, label: str) -> str:
    """提示词里某一节的正文：从「[标题]」到下一个空行分节为止（段与段之间是空行）。"""
    head = text.index("[%s]" % label) + len(label) + 2
    tail = text.find("\n\n[", head)
    return text[head:tail] if tail >= 0 else text[head:]


def _mark_of(tag: str) -> str:
    """某一类红线在账本里那个记号（按数据里的 mark 与 tag 填）：测试不自己造这个字符串。"""
    return FATAL_BOOK["mark"].format(tag=tag)
KEPT_MARK = "rewrite-failed"            # 改不动、留原话那句 reason 里必须出现的记号
REWRITE_KEYS = {"status", "why", "from", "to"}
# 借阅读数里「条款用掉几条」= 这一轮过闸真比过的条数（启用条款全表）：条数不写死，读数据。
CLAUSES = json.loads((MOUTH_DATA / "clauses.json").read_text(encoding="utf-8"))["clauses"]
# 长度与句末标点那层的判据也全读数据：场景、区间、p90、上限、最少句数、形状条款 id 一个都不写死
STYLE_BOOK = json.loads((MOUTH_DATA / "style_target.json").read_text(encoding="utf-8"))
STYLE_SCENE = STYLE_BOOK["default_scene"]
STYLE_RANGES = STYLE_BOOK["scenes"][STYLE_SCENE]["ranges"]
STYLE_SCENE_LABEL = STYLE_BOOK["scenes"][STYLE_SCENE]["label"]
STYLE_P90 = STYLE_BOOK["scenes"][STYLE_SCENE]["p90"]
STOPPED_CEILING = STYLE_BOOK["stopped"]["ceiling"]
SHAPE_CLAUSE = [one["id"] for one in CLAUSES if one.get("scope") == chat.REPLY_SCOPE][0]
ENABLED_CLAUSES = len([one for one in CLAUSES if one.get("enabled", True)])

#: 一个确定在白天的时刻（夜里窗口按这个串自己的小时判，跑在哪个时区都同一个结论）：
#: 出站闸那两道硬门里夜里排在前面，要真造出 SILENT 就得避开它。
DAYTIME = "2026-11-06T12:00:00+00:00"
#: 一个确定在夜里的时刻（夜里窗口是 NIGHT_START_H=23 到 NIGHT_END_H=8）：
#: 主动那一轮在这里会被心判「不说」，被动那一轮只判「少说」。
NIGHTTIME = "2026-11-06T03:00:00+00:00"
#: 她自己开口那一轮的由头（素材，不是判据）：proactive 的必填入参。
SEED = "今天想问问他吃饭了没有"

#: 通道真发的那个形状：channels/onebot/server.ts 拼的三个键（id / nickname / card），一个不多。
CHANNEL_WHO = {"id": 3425635473, "nickname": "能毕业就是胜利", "card": "小红花的爹"}
CHANNEL_WHO_ID = "3425635473"          # 身份：裸数字号，不加任何前缀

CONTRACT_KEYS = {"ok", "reply", "chunks", "scene", "recalled", "heart", "gate", "audit",
                 # 核·识别这一轮的读数（本轮加的）：五键冻结，/chat 原样透出、不重组。
                 "event",
                 "assembly", "continuity", "banned", "action", "llm", "anchors",
                 "guard", "memory", "addressed", "borrow", "refusal", "audience_held",
                 # 这一轮是谁起的头 + 由头原样（本轮加的）：被动两格也在，形状不随状态变。
                 "mode", "seed",
                 # 嘴这一格（本轮加的）：四个旋钮落到这一轮的实话，页面可读。
                 "tone",
                 # 出站闸那四格（门只回答发不发）：send / reason / level 是门的答案，
                 # limits 交给嘴——四格不论发不发都在，形状不随状态变。
                 "send", "reason", "level", "limits"}
AUDIT_KEYS = {"decision", "kind", "card_id", "overlap"}
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
        self.brain.ingest(who="3425635473", text=MEMORY_TEXT)
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
        entry = self.brain.ingest(who="3425635473", text=MEMORY_TEXT)
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
        self.assertTrue({"tone", "direction", "bans"} <= set(heart["voice"]))
        self.assertEqual({"state", "reason"}, set(heart["engage"]))
        self.assertTrue(heart["axes"])
        text = json.dumps(heart, ensure_ascii=False)
        self.assertEqual([], [ch for ch in text if ch.isdigit()],
                         "心的回包里有数字：%s" % text)

    def test_the_tone_block_carries_the_four_knobs_to_the_page(self):
        """回包那一格 tone：档位、软度、上限、两个开关、截没截、心接上没有——与装配同一个数。

        心这一轮是真接上的（不是「未接入」）：装配把 mood / energy / offset / engage 都交给了嘴，
        heart_source 里得看得见它们；max_chars 必须与 assembly.reply_budget.chars 是同一个数
        （提示词说的和手里截的是一个口径）。
        """
        status, payload = self._send()
        self.assertEqual(200, status)
        tone = payload["tone"]
        for key in ("tier", "label", "softness", "max_chars", "no_question", "ban_joke",
                    "truncated", "heart_source"):
            self.assertIn(key, tone, "tone 少了 %s：%r" % (key, tone))
        self.assertEqual(payload["assembly"]["reply_budget"]["chars"], tone["max_chars"],
                         "回包的上限与装配那一格不是一个数")
        self.assertIn("已接入", tone["heart_source"], "心没真接进嘴：%r" % (tone["heart_source"],))
        self.assertIn("mood=", tone["heart_source"])
        self.assertIn("energy=", tone["heart_source"])
        self.assertIn("engage=", tone["heart_source"])
        for key in ("no_question", "ban_joke", "truncated"):
            self.assertIsInstance(tone[key], bool, "%s 不是真假值：%r" % (key, tone[key]))
        self.assertIsInstance(tone["softness"], (int, float))
        self.assertTrue(str(tone["label"] or "").strip(), "语气档标签是空的")

    def test_a_cold_mood_round_narrows_the_budget_over_http(self):
        """心情凉那一轮：/chat 上的上限真的收紧，账本上落一行「为什么收紧」。

        心状态直接写文件（与 _double_low 同一种写法）：心情 20（0.2，低档），精力 80 不动——
        双低会判 SILENT、整轮被出站闸拦下，那不是这一条要验的路。
        """
        at = DAYTIME
        self._heart_root_write(mood.MOOD_FILE,
                               {"now": 20.0, "baseline": 60.0, "updated_at": at})
        self._heart_root_write(energy.ENERGY_FILE,
                               {"value": 80.0, "updated_at": at, "tier": "一般"})
        with mock.patch.object(brain_model, "now_iso", lambda: at):
            status, payload = self._send()
        self.assertEqual(200, status)
        rules = conversation.load_assembly()["tone_rules"]
        tier_chars = payload["assembly"]["reply_budget"]["tier_chars"]
        self.assertEqual(min(tier_chars, rules["max_chars_low"]), payload["tone"]["max_chars"],
                         "心情凉了上限却没收紧")
        self.assertTrue(payload["tone"]["ban_joke"], "心情凉却没禁玩笑")
        rows = conversation.read_tone_log()
        self.assertEqual(1, len(rows), "一轮该落一行账：%r" % (rows,))
        self.assertEqual(payload["tone"]["max_chars"], rows[-1]["max_chars"])
        self.assertTrue(rows[-1]["ban_joke"])
        self.assertEqual(payload["tone"]["heart_source"], rows[-1]["heart_source"])

    def _heart_root_write(self, name: str, row) -> None:
        """往临时心根写一份状态文件（tier 之类缺的栏由器官自己补，不在这里手搓）。"""
        write_json(self._heart_root() / name, row)

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
                                    _script(self.HER_LINE, self.HER_LINE + "3425635473"))
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
        recent.append({"ts": "2025-01-15T14:00:00+08:00", "who": "3425635473",
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

    # —— 冒充做过：动作账从回包传进闸（本轮加的） ————————————————

    CLAIM_LINE = "我这就去画"                    # 这一轮手头什么都没做时她会说的那一句
    CLAIM_FIX = "画不动，我没手。"                # 改写之后不再说做过（字数在 rewrit length 区间里）

    def _claim_id(self) -> str:
        """冒充做过那一关在账本里的 clause_id：从数据读，测试不自己造这个名字。"""
        return json.loads((MOUTH_DATA / "claim.json").read_text(encoding="utf-8"))["hit"]["clause_id"]

    def test_the_ledger_shapes_are_three_different_things(self):
        """回包没有这一格 = 空账（这一轮什么都没做）；给了清单就交下去；读不动才是 None。"""
        self.assertEqual([], chat._actions({}), "没有动作账这一格就是空账，不是拿不到")
        self.assertEqual([{"verb": "发"}], chat._actions({"actions": [{"verb": "发"}]}))
        self.assertIsNone(chat._actions({"actions": "坏了"}), "读不动的形状是拿不到，不许猜成空账")

    def test_a_rewritten_claim_survives_as_the_fixed_line(self):
        """命中之后改好了：这一泡是 rewritten、发的是改完那句，claim_status 说这一关真的判过。"""
        status, payload = self._post({"text": "你画好了吗"},
                                     _script(self.CLAIM_LINE, self.CLAIM_FIX))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("ok", part["claim_status"], "给了空账就是判过，不是没跑")
        self.assertEqual("rewritten", part["decision"])
        self.assertEqual(self._claim_id(), part["hits"][0]["clause_id"])
        self.assertEqual([self.CLAIM_FIX], [one["text"] for one in payload["chunks"]])
        self.assertEqual(self.CLAIM_FIX, payload["reply"])

    def test_an_unfixable_claim_is_dropped_and_the_ledger_names_it(self):
        """改不动（重写回来逐字相同）：那一句被删掉，整泡只剩它 → dropped，账本记一笔。"""
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你画好了吗"},
                                     _script(self.CLAIM_LINE, self.CLAIM_LINE))
        self.assertEqual(200, status)
        gate = payload["gate"]
        part = gate["parts"][0]
        self.assertEqual("ok", part["claim_status"])
        self.assertEqual("dropped", part["decision"], "删完只剩空白就是弃这一泡")
        self.assertEqual([], payload["chunks"])
        self.assertEqual("", payload["reply"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "删句弃掉的那一笔也要记账")
        self.assertEqual(self._claim_id(), rows[-1]["clause_id"])
        self.assertIn(self.CLAIM_LINE, rows[-1]["reason"], "账本里读得出删掉的是哪一句")

    def test_an_unreadable_ledger_is_not_checked_not_an_empty_one(self):
        """动作账读不动（None）：这一关没跑，那一泡照发，claim_status 是 not-checked。"""
        real = conversation.answer

        def with_broken_ledger(text, who="", **kw):
            """装配交出的是读不动的动作账：闸该记「拿不到」，不是「账上空着」。"""
            kw.setdefault("transport", _body(self.CLAIM_LINE))
            kw.setdefault("config", CONFIG)
            kw.setdefault("env", ENV)
            payload = real(text, who, **kw)
            payload["actions"] = "坏了"
            return payload

        with mock.patch.object(conversation, "answer", with_broken_ledger):
            status, gated = api.handle("POST", "/chat", {"text": "你画好了吗"},
                                       self.store, brain=self.brain)
        self.assertEqual(200, status)
        part = gated["gate"]["parts"][0]
        self.assertEqual("not-checked", part["claim_status"], "拿不到账不是没做过")
        self.assertEqual("pass", part["decision"])
        self.assertEqual([self.CLAIM_LINE], [one["text"] for one in gated["chunks"]])

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
        self.assertEqual({"delivered", "state", "topic", "until", "remaining_min", "reason"},
                         set(delivered))
        self.assertEqual(STATES[mouth_refusal.STATE_SENT], delivered["state"], "话出门了那一态")
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
        ledger = self._refusal_rows()[0]
        self.assertEqual("hard", ledger["shape"])
        self.assertEqual(STATES[mouth_refusal.STATE_SENT], ledger["state"])
        self.assertEqual(STATES[mouth_refusal.STATE_SENT], section["delivered"]["state"])

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
        # 账本那一列记的是「这句话出没出门」：话真说出去了（出门了），只是没投进心（认不出话题）。
        self.assertTrue(ledger[0]["sent"])
        self.assertEqual(STATES[mouth_refusal.STATE_SENT], ledger[0]["state"])

    # —— 两态：话没出门就不算冷却用过（这一轮加的） ————————————

    def _seed_heart_file(self) -> Path:
        """在心根里造一份 refusals.json 种子：没出门那一轮要拿它验「一个字节都没写」。"""
        path = self._heart_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, {"version": 1, "config": {}, "refusals": []})
        return path

    def _print_of(self, path: Path) -> tuple:
        """一份文件的读数：大小 / mtime / sha256——「读一次」不等于「写一次」。"""
        blob = path.read_bytes()
        return (len(blob), path.stat().st_mtime_ns, hashlib.sha256(blob).hexdigest())

    def _event_of(self) -> dict:
        """现装一条拒绝事件（形状与 why 都取自数据）：投递那一步只认这四个键。"""
        return mouth_refusal.event("topic", mouth_refusal.say("topic"), chat._stamp())

    def test_a_refusal_that_never_left_does_not_burn_a_cooling(self):
        """门说 send=false：账上「没出门」、心一个字节不写、同一话题再问仍然会拒。

        四件事一起断言，缺一条两态就不成立：账本那一行 state/why/sent 说得清没出门；心的
        refusals.json 前后 size/mtime/sha256 一字不差（读一次不等于写一次）；下一轮 refusal.cooling
        仍是 false；拒绝话照说——**那次拒绝没白烧**。
        """
        said = mouth_refusal.say("refused-topic")
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            self._double_low(DAYTIME)
            seed = self._seed_heart_file()
            before = self._print_of(seed)
            with self._topics("猫"):
                status, payload = self._send(text=self.ASKED_CAT, reply=said)
        self.assertEqual(200, status)
        self.assertIs(False, payload["send"], "双低这一轮门说不发")
        delivered = payload["refusal"]["delivered"]
        self.assertFalse(delivered["delivered"], "没出门就不许说投过了")
        self.assertEqual(STATES[mouth_refusal.STATE_UNSENT], delivered["state"])
        self.assertEqual(before, self._print_of(seed),
                         "没出门却写了心的账（size / mtime / sha256 变了）")
        row = self._refusal_rows()[0]
        self.assertFalse(row["sent"])
        self.assertEqual(STATES[mouth_refusal.STATE_UNSENT], row["state"])
        self.assertEqual(STATES["suppressed_why"][mouth_refusal.SUPPRESSED_OUTBOUND], row["why"])
        self.assertEqual(payload["reason"], row["reason"], "那一轮门给的理由要一并落账")
        self.assertEqual(payload["level"], row["level"], "那一轮门给的层级要一并落账")
        # 同一话题再问一次：**冷却没起**，所以她照样拒（对方上一轮压根没听到）
        for name in (mood.MOOD_FILE, energy.ENERGY_FILE):
            (self._heart_root() / name).unlink(missing_ok=True)
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            with self._topics("猫"):
                status2, again = self._send(text=self.ASKED_CAT, reply=said)
        self.assertEqual(200, status2)
        self.assertIs(True, again["send"])
        self.assertFalse(again["refusal"]["cooling"], "上一轮没出门，这一轮不该在冷却里")
        self.assertEqual(said, again["reply"], "拒绝话照说")
        self.assertEqual(STATES[mouth_refusal.STATE_SENT], again["refusal"]["delivered"]["state"])
        self.assertEqual(1, len(self._heart_rows()), "这一回真出门了，冷却才起一次")
        self.assertEqual(2, len(self._refusal_rows()))

    def test_a_refusal_the_mouth_gate_swallowed_is_its_own_case(self):
        """门说发、八股闸把这一泡吞了：也算没出门（不起冷却），而 why 与「门说不发」不同。"""
        seed = self._seed_heart_file()
        before = self._print_of(seed)
        payload = {"send": True, "reason": "门说这一轮发", "level": "engage",
                   "gate": {"decision": "dropped"}, "chunks": []}
        judged = {"kind": mouth_refusal.KIND_HARD, "shape": "topic",
                  "verdict_reason": AUDIENCE["reason"], "asked": self.ASKED_CAT,
                  "event": self._event_of(), "sent": True,
                  "reason": "门说这一轮发", "level": "engage"}
        with self._topics("猫"):
            chat._deliver_judged(judged, payload)
        delivered = judged["delivered"]
        self.assertFalse(delivered["delivered"], "闸吞了就是没出门")
        self.assertEqual(STATES[mouth_refusal.STATE_UNSENT], delivered["state"])
        row = self._refusal_rows()[0]
        self.assertEqual(STATES["suppressed_why"][mouth_refusal.SUPPRESSED_SWALLOWED], row["why"])
        self.assertNotEqual(STATES["suppressed_why"][mouth_refusal.SUPPRESSED_OUTBOUND], row["why"],
                            "两种来由必须分得开：她不想说 / 她自己把话拦了")
        self.assertEqual(before, self._print_of(seed), "闸吞了也不许起冷却（心一个字节都不写）")

    def test_a_missing_send_flag_raises_instead_of_guessing(self):
        """payload 里没有 send、或它不是布尔：当场抛并指名 payload.send——不默认成发了。"""
        for bad in ({}, {"send": 1}, {"send": "true"}, {"send": None}):
            with self.subTest(payload=bad):
                with self.assertRaises(mouth_refusal.RefusalBroken) as caught:
                    chat._send_flag(bad)
                self.assertIn("payload.send", str(caught.exception))

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
        # 这一轮没有装配、没有 EngageInput：出站闸这一关如实写「没判」，四格照给（不许缩水成缺键）
        self.assertEqual(chat.OUTBOUND_UNJUDGED, gate["outbound"]["status"])
        self.assertTrue(gate["outbound"]["why"])
        self.assertIs(True, payload["send"], "门没判不等于不发")
        self.assertIsNone(payload["level"])
        self.assertIsNone(payload["reason"])
        self.assertEqual({}, payload["limits"])

    def test_the_same_question_outside_the_cooldown_answers_normally(self):
        """同一句、不在冷却里：照常装配与调模型（一次），refusal 节如实写「没在冷却」。"""
        with self._topics("医院"):
            capture = []
            status, payload = self._post({"text": self.ASKED_HOSPITAL},
                                         _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(LONG_REPLY, payload["reply"])
        self.assertEqual(1, len([one for one in capture if [
            item["role"] for item in one["messages"]] == ["system", "user"]]),
            "装配那一问只发了一次（其余请求是长度/标点那一层改形发的）")
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
        self.assertEqual(1, len([one for one in capture if [
            item["role"] for item in one["messages"]] == ["system", "user"]]),
            "装配那一问只发了一次（其余请求是长度/标点那一层改形发的）")
        section = payload["refusal"]
        self.assertFalse(section["cooling"])
        self.assertIsNone(section["topic"], "认不出就是 null，不替她猜一个话题")
        self.assertEqual([], self._heart_rows())

    # —— 长度与句末标点（过闸之前先把形修一遍）———————————————

    def test_every_part_carries_a_length_reading_and_the_scene(self):
        """逐泡都带长度读数，gate.style 交代场景 / 档位 / 上限 / 目标区间 / p90 / 句号率。"""
        status, payload = self._send()
        self.assertEqual(200, status)
        section = payload["gate"]["style"]
        self.assertEqual(chat.STYLE_OK, section["status"])
        self.assertEqual(STYLE_SCENE, section["scene"])
        self.assertEqual(STYLE_SCENE_LABEL, section["scene_label"])
        self.assertEqual("%d-%d" % (STYLE_RANGES[str(section["tier"])]["lo"],
                                    STYLE_RANGES[str(section["tier"])]["hi"]),
                         section["target"])
        self.assertEqual(STYLE_P90, section["p90"])
        self.assertIn("stopped", section)
        self.assertEqual(STOPPED_CEILING, section["stopped"]["ceiling"])
        for row in payload["gate"]["parts"]:
            self.assertEqual({"decision", "scene", "target_lo", "target_hi", "chars",
                              "rewrites", "over_hard_cap", "reason"}, set(row["style"]))

    def test_the_scene_comes_from_the_scope(self):
        """scope=group 就按群聊那套区间判：场景从 /chat 的 scope 来，不是另算一遍。"""
        other = "group" if STYLE_SCENE != "group" else "private"
        status, payload = self._post({"text": QUESTION, "scope": other}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        section = payload["gate"]["style"]
        self.assertEqual(other, section["scene"])
        table = STYLE_BOOK["scenes"][other]["ranges"]
        self.assertEqual("%d-%d" % (table[str(section["tier"])]["lo"],
                                    table[str(section["tier"])]["hi"]), section["target"])
        self.assertEqual(STYLE_BOOK["scenes"][other]["p90"], section["p90"])

    def test_too_many_full_stops_gets_rewritten_once(self):
        """整段句号太密：重写一次，改完落到上限里就发改写后的，并按新的整段重新切泡。"""
        dense = "我在呢。" + "你说吧。" + "刚刚在听你讲那件事。"
        better = "我在呢！" + "你说吧？" + "刚刚在听你讲那件事"
        status, payload = self._post({"text": QUESTION}, _script(dense, better))
        self.assertEqual(200, status)
        counted = payload["gate"]["style"]["stopped"]
        self.assertTrue(counted["judged"])
        self.assertEqual("rewritten", counted["decision"])
        self.assertLessEqual(counted["ratio"], counted["ceiling"])
        self.assertEqual(better, payload["reply"], "改完的整段才是要发出去的那段")

    def test_two_sentences_with_full_stops_are_not_judged(self):
        """少于最少句数不判：两句话都带句号很正常——judged=false 不是「判过了没问题」。"""
        status, payload = self._post({"text": QUESTION}, _body("我在呢。你说吧。"))
        counted = payload["gate"]["style"]["stopped"]
        self.assertFalse(counted["judged"])
        self.assertEqual("pass", counted["decision"])
        self.assertLess(counted["sentences"], counted["min_sentences"])

    def test_the_three_part_shape_is_dropped_before_this_layer(self):
        """三件套在装配那一关就按整句判过、命中即弃，所以这一层看到的是空泡。

        这条同时钉住两件事：一是形状条款按**整条回复**判（一泡 1–2 句，看不见它）；
        二是这一层不另判一遍（同一句话判两次不会更好，而那一层连重写器都没有）。
        """
        shaped = "好。" + "你今天干嘛了？" + "我觉得挺好的。"
        status, payload = self._post({"text": QUESTION}, _script(shaped, "随便改一句也行"))
        self.assertEqual(200, status)
        self.assertEqual("dropped", payload["gate"]["decision"])
        self.assertEqual([], payload["chunks"])
        self.assertEqual("", payload["reply"])
        self.assertEqual(SHAPE_CLAUSE, payload["gate"]["hit"]["clause_id"])
        self.assertNotIn("style", payload["gate"], "没有泡进这一层，就没有长度/标点那一节")

    def test_a_plain_reply_is_left_alone(self):
        """一句正常的话一处都不改：没有 style 这一层也能逐字相同的那些断言，靠的就是这条。"""
        line = "我在呢，你说。"
        status, payload = self._post({"text": QUESTION}, _body(line))
        self.assertEqual(line, payload["reply"])
        self.assertEqual("pass", payload["gate"]["style"]["stopped"]["decision"])
        self.assertEqual(1, len(payload["chunks"]))
        self.assertEqual(line, payload["chunks"][0]["text"])


    # —— 出口认 audience：私密记忆在群里守边界（这一轮加的） ——————————

    PRIVATE_TEXT = "我妈住院那事，别到处说"          # 一条真写进临时脑根的记忆（正文不许出群）
    PRIVATE_ASKED = "你上次跟我说你妈住院那事"        # 直问那条记忆的那一句（连续重合过阈值）
    GROUP = AUDIENCE["scopes"]["group"]
    PRIVATE = AUDIENCE["scopes"]["private"]
    PRIVATE_FIELD = AUDIENCE["field"]
    PRIVATE_VALUE = AUDIENCE["private_value"]
    PRIVATE_SAY = AUDIENCE["say"]

    @contextlib.contextmanager
    def _private_memory(self, text: str = PRIVATE_TEXT):
        """在临时脑根里**真写一条记忆**，并让装配那一层的 recalled 带上私密标记。

        记忆这一侧是真的（brain.ingest 落到 setUp 那个临时脑根）；标记只能在这一层补上——
        **audience 的生产者不在本轮**：mind 的 _recall 现在只搬固定那几栏，出口要认的那一栏
        因此得靠这个假 _recall 打上（验收里允许的两条路之一）。
        """
        self.brain.ingest(who="3425635473", text=text)
        real = conversation._recall

        def tagged(brain, said, who, scene=None):
            rows, memory_text = real(brain, said, who, scene)
            for row in rows:
                row[self.PRIVATE_FIELD] = self.PRIVATE_VALUE
            if not any(row.get("text") == text for row in rows):
                rows.append({"id": "私密-被试", "text": text, "display_text": text,
                             "source": "longterm", "source_type": "亲历",
                             "evidence_key": "被试", "score": 1.0, "who": "3425635473",
                             "note_display": "", self.PRIVATE_FIELD: self.PRIVATE_VALUE})
            return rows, memory_text

        with mock.patch.object(conversation, "_recall", tagged):
            yield

    def test_a_private_memory_asked_in_a_group_says_the_boundary_line(self):
        """验收 1：scope=group + 直问 → 出声、说的是数据里那句边界话、正文一个字都不出群。

        四件事一起断言，缺一条这条边界就不成立：reply 非空且含那句边界话；正文不出现（reply 与
        整个回包都不出现）；事件与账本各一行（reason / full_name 取数据里那个 refused-topic，
        账本的 why 带 mark 记号）；audience_held 里看得见被扣下的那一条（id + 一小截摘要，不贴正文）。
        """
        with self._private_memory(), self._topics("住院"):
            status, payload = self._post({"text": self.PRIVATE_ASKED, "scope": self.GROUP},
                                         _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertTrue(payload["reply"], "群里被问到私密条目：她必须出声，沉默是最糟的一种哑")
        self.assertIn(self.PRIVATE_SAY, payload["reply"])
        self.assertNotIn(self.PRIVATE_TEXT, payload["reply"], "边界话里不许出现那条记忆的正文")
        self.assertNotIn(self.PRIVATE_TEXT, json.dumps(payload, ensure_ascii=False),
                         "被扣下的正文一个字都不许往外带（recalled / 读数里都不许有）")
        held = payload["audience_held"]
        self.assertEqual(1, len(held), "扣下的那一条要在读数里看得见")
        self.assertEqual({"id", "text_min", "source"}, set(held[0]))
        self.assertTrue(held[0]["id"])
        self.assertTrue(held[0]["text_min"])
        self.assertLessEqual(len(held[0]["text_min"]),
                             AUDIENCE["criteria"]["excerpt_chars"], "读数只给一小截摘要")
        self.assertEqual([], payload["recalled"], "剔掉了就不再进 recalled")
        rows = self._heart_rows()
        self.assertEqual(1, len(rows), "边界话走拒绝那条路：心那边记一次")
        self.assertEqual(AUDIENCE["event_shape"], rows[0]["reason"])
        ledger = self._refusal_rows()
        self.assertEqual(1, len(ledger), "我们自己的账也记一行")
        self.assertEqual(AUDIENCE["event_shape"], ledger[0]["full_name"])
        self.assertIn(AUDIENCE["mark"], ledger[0]["why"], "reason 里要带 audience 记号")
        self.assertTrue(ledger[0]["sent"])
        section = payload["refusal"]
        self.assertFalse(section["cooling"], "这不是冷却那一轮")
        self.assertEqual("hard", section["kind"])
        self.assertTrue(section["delivered"]["delivered"])
        self.assertEqual("pass", payload["gate"]["decision"], "边界话照样过闸")

    def test_the_same_private_memory_is_spoken_in_private(self):
        """验收 2：同一句 + scope=private → 照常说内容，不剔除、不出声、不投事件。"""
        spoken = "你妈住院那事我记着呢，你放心，不往外说。"
        with self._private_memory(), self._topics("住院"):
            status, payload = self._post({"text": self.PRIVATE_ASKED, "scope": self.PRIVATE},
                                         _body(spoken))
        self.assertEqual(200, status)
        self.assertEqual(spoken, payload["reply"], "私聊里照常说：内容不被边界话替换")
        self.assertIn("住院", payload["reply"], "那条记忆的关键词要能说出来")
        self.assertEqual([], payload["audience_held"], "私聊里不剔除，也没有边界话")
        self.assertEqual(1, len(payload["recalled"]), "私聊里照常召回（这一条仍在 recalled 里）")
        self.assertEqual([], self._refusal_rows(), "没走拒绝就不记账")

    def test_a_group_round_that_does_not_ask_holds_it_silently(self):
        """验收 3：scope=group 但没被问到 → 不主动引用（正文不出现）、也不说边界话，但剔除看得见。"""
        with self._private_memory(), self._topics("住院"):
            status, payload = self._post({"text": "今天吃啥", "scope": self.GROUP},
                                         _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual(LONG_REPLY, payload["reply"], "没被问到就照常说她自己那句")
        self.assertNotIn(self.PRIVATE_SAY, payload["reply"],
                         "不为没发生的事拒绝：这一轮不说边界话")
        self.assertNotIn(self.PRIVATE_TEXT, json.dumps(payload, ensure_ascii=False),
                         "没被问到也不主动引用：正文一个字都不出现")
        self.assertEqual(1, len(payload["audience_held"]), "剔除照做，读数里看得见")
        self.assertEqual([], payload["recalled"])
        self.assertEqual([], self._refusal_rows(), "没被问到不投事件")
        self.assertEqual([], self._heart_rows())

    def test_an_illegal_scope_is_400_and_names_it(self):
        """验收 4：scope 非法值 → 400，message 里点名读到的值，并列出认得的两个。"""
        status, payload = self._post({"text": QUESTION, "scope": "公屏"}, _body(LONG_REPLY))
        self.assertEqual(400, status, "非法场景当场 400")
        self.assertFalse(payload["ok"])
        self.assertIn("公屏", payload["message"], "要点名读到的那个值")
        for name in (self.GROUP, self.PRIVATE):
            self.assertIn(name, payload["message"], "顺带列出认得的两个值")
        self.assertEqual([], self._refusal_rows(), "body 就是错的：一个字都不写")

    def test_a_cooling_topic_beats_the_boundary_line_and_records_once(self):
        """验收 5：已冷却 + 私密被问 → 走冷却那条（不重复投事件，也不说边界话）。

        顺序的理由：冷却在装配前就短路了（那一轮没有 recalled），它也比边界更靠前——让边界话抢在
        冷却前面，就是把一次已经记过的拒绝再说一遍、再投一遍。
        """
        said = mouth_refusal.say(AUDIENCE["event_shape"])
        with self._cooling("住院"):
            heart_before = len(self._heart_rows())
            with self._private_memory():
                status, payload = self._post(
                    {"text": self.PRIVATE_ASKED, "scope": self.GROUP}, _body(LONG_REPLY))
            heart_after = len(self._heart_rows())
        self.assertEqual(200, status)
        self.assertEqual(said, payload["reply"], "冷却优先：说的是冷却那一句，不是边界话")
        self.assertTrue(payload["refusal"]["cooling"])
        self.assertNotIn(self.PRIVATE_SAY, payload["reply"])
        self.assertEqual(heart_before, heart_after, "冷却里不投事件（同一件事不记两遍）")
        self.assertEqual([], self._refusal_rows(), "我们自己的账也不添一行")
        self.assertEqual([], payload["audience_held"], "冷却那一轮没有装配，也没有 recalled")

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
            recent.append({"ts": "2025-01-15T14:0%d:00+08:00" % (index % 10), "who": "3425635473",
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
            recent.append({"ts": "2025-01-15T14:0%d:00+08:00" % index, "who": "3425635473",
                           "said": "第%d句" % index, "reply": "第%d答" % index})
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual(5, payload["continuity"]["turns_loaded"])
        self.assertEqual(0, payload["continuity"]["omitted"])
        self.assertEqual("", payload["continuity"]["notice"])

    def test_a_round_the_ledger_missed_is_backfilled_over_http(self):
        """脑里今日记了、最近几轮没落的一条：装配前补进来，回包里说清补了几条。"""
        self.brain.ingest(who="3425635473", text="我上周把片子落在诊室了")   # 今日在役，账上没有
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
        """家·仓库里登记两张台词卡：端点报成配了，只回 id / phrase / injected / when，example 不进提示词。"""
        home = Path(os.environ["HOME_ROOT"])
        home_anchors.append_anchors(root=home, role="human", by="人", anchors=[
            {"id": "被夸", "phrase": "才知道啊", "example": "哼，才知道啊。",
             "when": "被夸，且不想直接承认高兴", "body": "尾巴翘起来晃了两下"},
            {"id": "认错", "phrase": "记岔了", "example": "……是我记岔了。",
             "when": "发现自己记错了", "body": "低头、尾巴垂下去不敢动"},
        ])
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        anchors = payload["anchors"]
        self.assertEqual({"configured", "count", "available", "injected", "instruction",
                          "items", "skipped", "banned_by_repeat"}, set(anchors))
        self.assertTrue(anchors["configured"])
        self.assertEqual(2, anchors["count"])
        self.assertEqual(conversation.load_assembly()["anchors"]["instruction"],
                         anchors["instruction"])
        by_id = {item["id"]: item for item in anchors["items"]}
        self.assertEqual({"id", "phrase", "injected", "when", "example", "body"},
                         set(by_id["被夸"]),
                         "items 只带这四栏：example 一个字都不许回（审计要用时现读家）")
        self.assertEqual("才知道啊", by_id["被夸"]["phrase"])
        self.assertEqual("被夸，且不想直接承认高兴", by_id["被夸"]["when"])
        system = capture[0]["messages"][0]["content"]
        self.assertIn(by_id["被夸"]["injected"], system)
        self.assertNotIn("尾巴翘起来晃了两下", system, "body 进了提示词")
        self.assertNotIn("哼，才知道啊。", system, "整句例句进了提示词")
        self.assertNotIn("被夸，且不想直接承认高兴", system, "when 进了提示词")

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
                                     _script(self.HER_LINE, self.HER_LINE + "3425635473"))
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
                                     _script(self.RECITED, self.RECITED + "3425635473"), store=store)
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

    #: 五张现行台词卡（新格式）：phrase 是进提示词的那一档，example 不进提示词，when 只回给人看。
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
        skipped = [item for item in self.FIVE if item["id"] in anchors["skipped"]]
        self.assertEqual(3, len(skipped))
        for item in skipped:
            cut = int(math.ceil(len(item["phrase"]) * conf["inject"]["fraction"]))
            self.assertNotIn(item["phrase"][:cut] + conf["inject"]["suffix"], system,
                             "降档了还在注入：%s" % item["id"])

    def test_a_copied_example_is_rewritten_over_http(self):
        """端点这一层：回话照抄 example ≥6 字 → 回包 audit 说 rewritten，模型被叫两次。"""
        self._five_anchors()
        copied = "嗯……行了行了，我又不是玻璃做的。"
        rewrite = "我哪有那么脆，别瞎操心。"
        capture = []
        status, payload = self._post({"text": QUESTION},
                                     _script(copied, rewrite, captured=capture))
        self.assertEqual(200, status)
        self.assertEqual(AUDIT_KEYS, set(payload["audit"]))
        self.assertEqual("example", payload["audit"]["kind"])
        self.assertEqual("被关心", payload["audit"]["card_id"])
        self.assertEqual("rewritten", payload["audit"]["decision"])
        self.assertIn(rewrite, payload["reply"], "改好的稿子没发出去")
        self.assertEqual(2, len(capture), "该有两次模型请求：装配一次、重写一次")

    def test_a_phrase_hit_costs_no_second_request(self):
        """只命中 phrase（≤4 字那一档）→ 回包 audit 说 pass / phrase，模型只被叫一次。"""
        self._five_anchors()
        capture = []
        status, payload = self._post({"text": QUESTION},
                                     _body("才知道什么呀，我才不承认。", capture))
        self.assertEqual(200, status)
        self.assertEqual({"decision": "pass", "kind": "phrase", "card_id": "被夸", "overlap": 3},
                         payload["audit"])
        self.assertEqual(1, len(capture))

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
        status, first = self._post({"text": "以后叫我老李", "who": "3425635473"}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual({"who", "address", "source"}, set(first["addressed"]))
        self.assertEqual("3425635473", first["addressed"]["who"])
        self.assertEqual("老李", first["addressed"]["address"])
        self.assertEqual(table["source"]["rename"], first["addressed"]["source"])
        self.assertEqual({"3425635473": "老李"}, book.snapshot(), "会话账上没记下这一笔")
        self.assertTrue(self._has_text(mouth_address.rename_memory("3425635473", "老李")["text"]),
                        "那条亲历记忆没投进脑")

        status, second = self._post({"text": "今天有点累", "who": "3425635473"},
                                    _body("那就早点睡，我把灯关了。"))
        self.assertEqual(200, status)
        self.assertEqual("老李", second["addressed"]["address"], "没重新要求，叫法也该还在")
        self.assertEqual(table["source"]["session"], second["addressed"]["source"])

    def test_a_new_process_falls_back_to_the_directory(self):
        """换一个进程（新建一本账）：会话覆盖没了，回到人名录档位——「只活这个会话」。"""
        self._fresh_book()
        table = mouth_address.load_table()
        self._post({"text": "以后叫我老李", "who": "3425635473"}, _body(LONG_REPLY))
        self._fresh_book()
        status, again = self._post({"text": "今天有点累", "who": "3425635473"},
                                   _body("那就早点睡，我把灯关了。"))
        self.assertEqual(200, status)
        self.assertEqual(table["source"]["directory"], again["addressed"]["source"])
        self.assertNotEqual("老李", again["addressed"]["address"])

    def test_a_banned_self_name_falls_back_and_is_not_remembered(self):
        """违禁自称过不了 self_guard：退中性叫法，会话账不记、那条记忆也不投——红线没被绕过。"""
        book = self._fresh_book()
        table = mouth_address.load_table()
        banned = table["self_guard"]["words"][0]
        status, payload = self._post({"text": "以后叫我" + banned, "who": "3425635473"}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual(table["fallback"]["address"], payload["addressed"]["address"])
        self.assertEqual(table["source"]["fallback"], payload["addressed"]["source"])
        self.assertEqual({}, book.snapshot(), "被拦下的叫法不许记进会话账")
        self.assertFalse(self._has_text(mouth_address.rename_memory("3425635473", banned)["text"]),
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

    # —— 场合 ————————————————————————————————

    def test_the_scene_section_follows_the_body_for_all_three_states(self):
        """group → 说群里那句，private → 说私聊那句，不传 / 传个没听过的 → 说不知道在哪儿那句。

        「不猜」这一条是验收的重点：不传不是私聊（猜成私聊就是把私事在群里说出去了），
        非法值也不 400——body 没错到说不出话，只是我们不知道这是哪儿。
        """
        conf = conversation.load_assembly()
        label, lines = conf["sections"]["scene"]["label"], conf["scene"]["lines"]
        cases = ((brain_model.SCENE_GROUP, brain_model.SCENE_GROUP),
                 (brain_model.SCENE_PRIVATE, brain_model.SCENE_PRIVATE),
                 (None, brain_model.SCENE_UNKNOWN),
                 ("公屏", brain_model.SCENE_UNKNOWN))
        for scene, expected in cases:
            body = {"text": QUESTION}
            if scene is not None:
                body["scene"] = scene
            capture = []
            status, payload = self._post(body, _body(LONG_REPLY, capture))
            self.assertEqual(200, status, "scene=%r 不该 400：%s" % (scene, payload))
            self.assertEqual({"value": expected}, payload["scene"])
            system = capture[0]["messages"][0]["content"]
            block = _section(system, label)
            self.assertEqual(lines[expected], block.strip().splitlines()[0],
                             "scene=%r 时「场合」那段不是数据里那一句" % (scene,))
            for name, text in lines.items():
                if name != expected:
                    self.assertNotIn(text, system, "别的场合那句也进了提示词：%s" % name)

    def test_the_old_scope_field_still_names_the_scene(self):
        """只给了 scope 的老调用照旧读得出来；两个都给时 scene 说了算（各读各的，不互相顶替）。"""
        status, payload = self._post({"text": QUESTION, "scope": brain_model.SCENE_GROUP},
                                     _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual({"value": brain_model.SCENE_GROUP}, payload["scene"])
        status, payload = self._post({"text": QUESTION, "scope": brain_model.SCENE_GROUP,
                                      "scene": brain_model.SCENE_PRIVATE}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual({"value": brain_model.SCENE_PRIVATE}, payload["scene"],
                         "两个字段都给时 scene 说了算")

    def test_the_three_scenes_reach_the_brain(self):
        """三态各打一次：脑里今日两条（人和她）的 scene 都等于这一轮给的（不传就是 unknown）。"""
        for name, scene, expected in (
                ("group", brain_model.SCENE_GROUP, brain_model.SCENE_GROUP),
                ("private", brain_model.SCENE_PRIVATE, brain_model.SCENE_PRIVATE),
                ("none", None, brain_model.SCENE_UNKNOWN)):
            brain = BrainStore(self.tmp / ("brain-" + name))
            body = {"text": QUESTION}
            if scene is not None:
                body["scene"] = scene
            status, payload = self._post(body, _body(LONG_REPLY), brain=brain)
            self.assertEqual(200, status)
            self.assertEqual({"value": expected}, payload["scene"])
            rows = brain.today_entries()
            self.assertEqual(2, len(rows), "这一轮该落两条（人一句、她一句）：%r" % (rows,))
            self.assertEqual([expected, expected], [row.get("scene") for row in rows],
                             "scene=%r 没落到脑里（两条还不一致）" % (scene,))

    def test_a_private_memory_is_marked_not_retellable_in_a_group(self):
        """group 时脑把 audience=私密 的那条打 retellable=False，那一栏原样透到 recalled[]。

        先打一轮、再写那条记忆：同一个人 10 分钟内的话会被脑并成一条，先写记忆会被这一轮
        自己的原话盖掉（那是脑的合并窗口，这一层不改它）。
        """
        status, _ = self._post({"text": "今天吃啥"}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.brain.ingest(who="3425635473", text=MEMORY_TEXT, audience=AUDIENCE["private_value"])
        status, payload = self._post({"text": QUESTION, "scene": brain_model.SCENE_GROUP},
                                     _body(LONG_REPLY))
        self.assertEqual(200, status)
        row = next(one for one in payload["recalled"] if one["text"] == MEMORY_TEXT)
        self.assertIs(False, row["retellable"], "群里这一条没被标成不可复述")
        self.assertTrue(row.get("retell_note"), "retell_note 空着：判据没透出来")

        brain = BrainStore(self.tmp / "brain-private")
        status, _ = self._post({"text": "今天吃啥"}, _body(LONG_REPLY), brain=brain)
        self.assertEqual(200, status)
        brain.ingest(who="3425635473", text=MEMORY_TEXT, audience=AUDIENCE["private_value"])
        status, payload = self._post({"text": QUESTION, "scene": brain_model.SCENE_PRIVATE},
                                     _body(LONG_REPLY), brain=brain)
        self.assertEqual(200, status)
        row = next(one for one in payload["recalled"] if one["text"] == MEMORY_TEXT)
        self.assertTrue(row["retellable"], "私聊里这一条该是可复述的")
        self.assertNotIn("retell_note", row, "私聊里不该有「不可复述」那条理由")

    # —— 这一小段：段从 body 进来，装配多一节，且不混进 recent ————————————

    def _segment_body(self, system: str) -> str:
        """提示词里「这一小段」那一节的正文档（标题从数据里现读）。"""
        label = conversation.load_assembly()["sections"]["segment"]["label"]
        return _section(system, label).strip("\n")

    def test_segment_reaches_the_assembly_section_in_order(self):
        """body 的 segment（两种形状都收）→ 提示词那节逐行出现、顺序不变，sections 里有 segment。"""
        rows = [{"speaker": "234567890", "text": "我妈住院了"},
                {"speaker": "234567890", "text": "明天要做手术"},
                {"speaker": "123456789", "text": "我陪你去吧"}]
        capture = []
        status, payload = self._post({"text": QUESTION, "segment": rows},
                                     _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        system = capture[0]["messages"][0]["content"]
        body = self._segment_body(system)
        lines = body.splitlines()
        self.assertTrue(body.strip(), "段的正文是空的：这一节没进提示词")
        self.assertEqual(3, len(lines), "三行发言该逐行进提示词：%r" % (lines,))
        for want, have in zip(("我妈住院了", "明天要做手术", "我陪你去吧"), lines):
            self.assertIn(want, have, "这一行没进提示词：%r" % (have,))
        keys = [row["key"] for row in payload["assembly"]["sections"]]
        self.assertIn("segment", keys, "回包的 assembly.sections 里没有 segment")

        # 二元组那种形状走同一条路（归一化只有一份，在 conversation.normalize_segment）
        capture = []
        status, _ = self._post({"text": QUESTION, "segment": [["234567890", "我妈住院了"],
                                                              ["123456789", "我陪你去吧"]]},
                               _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        tuple_lines = self._segment_body(capture[0]["messages"][0]["content"]).strip("\n").splitlines()
        self.assertEqual(2, len(tuple_lines), "二元组那种形状没逐行归一：%r" % (tuple_lines,))
        self.assertIn("我妈住院了", tuple_lines[0])
        self.assertIn("我陪你去吧", tuple_lines[1])

    def test_segment_notice_only_in_a_group_with_two_speakers(self):
        """群聊双人才接那句话；私聊 / unknown / 单人段一律不加。"""
        notice = conversation.load_assembly()["segment"]["notice"][brain_model.SCENE_GROUP]
        two = [["234567890", "我妈住院了"], ["123456789", "我陪你去吧"]]
        one = [["234567890", "我妈住院了"], ["234567890", "明天要做手术"]]
        cases = [(brain_model.SCENE_GROUP, two, True), (brain_model.SCENE_GROUP, one, False),
                 (brain_model.SCENE_PRIVATE, two, False),
                 (brain_model.SCENE_UNKNOWN, two, False)]
        for scene, rows, wanted in cases:
            with self.subTest(scene=scene, wanted=wanted):
                capture = []
                status, _ = self._post({"text": QUESTION, "scene": scene, "segment": rows},
                                       _body(LONG_REPLY, capture))
                self.assertEqual(200, status)
                body = self._segment_body(capture[0]["messages"][0]["content"])
                if wanted:
                    self.assertIn(notice, body, "群聊双人段没接那句话")
                else:
                    self.assertNotIn(notice, body, "这一态不该加那句话")

    def test_text_falls_back_to_the_last_segment_line(self):
        """只给段、没给 text：这一问就是段里最后那一条（不另外编一句）。"""
        rows = [["234567890", "我妈住院了"], ["123456789", "我陪你去吧"]]
        capture = []
        status, _ = self._post({"segment": rows}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual("我陪你去吧", capture[0]["messages"][1]["content"],
                         "没给 text 时这一问该取段里最后那一条")

    def test_segment_does_not_leak_into_the_continuity_ledger(self):
        """段只进提示词那一节，不写进 recent：这一轮落账的只有他这一句，段不计轮。"""
        rows = [["234567890", "我妈住院了"], ["123456789", "我陪你去吧"]]
        capture = []
        status, payload = self._post({"text": QUESTION, "segment": rows},
                                     _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(0, payload["continuity"]["turns_loaded"], "段被当成更早的轮记进 recent 了")
        rows_on_disk = recent.load(root=Path(os.environ["QQAGENT_HOME"]) / "mind")["turns"]
        self.assertEqual([QUESTION], [row["said"] for row in rows_on_disk],
                         "recent 那份账上多了段里的行")

    def test_bad_segment_shape_is_400_and_calls_no_model(self):
        """形状坏 → 400 并点名；一个模型请求都不发（不兜底成空段）。"""
        bad = ["阿黄：我妈住院了",
               {"speaker": "234567890", "text": "我妈住院了"},
               [["234567890"]],
               [{"speaker": "234567890"}],
               [["", "我妈住院了"]],
               [["234567890", 42]]]
        for case in bad:
            with self.subTest(segment=case):
                capture = []
                status, payload = self._post({"text": QUESTION, "segment": case},
                                             _body(LONG_REPLY, capture))
                self.assertEqual(400, status, "坏形状没回 400：%r" % (case,))
                self.assertFalse(payload["ok"])
                self.assertIn(chat.SEGMENT_BROKEN.split("%s")[0], payload["message"])
                self.assertEqual([], capture, "坏形状也发了模型请求")

    # —— 出站闸：这一轮到底发不发（本轮加的） ————————————

    def _heart_root(self) -> Path:
        root = Path(os.environ["HEART_ROOT"])
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _gate_rows(self) -> list:
        """心·gate 的出站账本（一行一判，发也写、不发也写）。"""
        return heart_store.read_jsonl(self._heart_root() / heart_gate.LOG_FILE)

    def _double_low(self, at: str) -> None:
        """把心压到双低（心情 10、精力 10）：/chat 上真能造出 SILENT 的唯一来路。

        装配这一层给的意图恒是被动（conversation._engage → INTENT_PASSIVE），夜里那一道
        因此只判「少说」；要「不说」只能走双低。两个状态文件都写在这一刻，精力就不结算。
        """
        write_json(self._heart_root() / mood.MOOD_FILE,
                   {"now": 10.0, "baseline": 10.0, "updated_at": at})
        write_json(self._heart_root() / energy.ENERGY_FILE,
                   {"value": 10.0, "updated_at": at, "tier": "累"})

    def test_a_double_low_heart_silences_the_round_and_says_why(self):
        """心情与精力双低 → 门判 SILENT：一泡都不发、reply 空、send=false，账上多一行。

        门只回答发不发：这里断言的是「她这句没出去」与「门说了为什么」，
        怎么想的（heart.engage）与怎么说的一概不动。
        """
        # 双低与夜间是两道**硬门**，门取先列的那一道定态（夜里排在前面，被动回复只判「少说」）。
        # 所以这一刻必须钉在一个确定是白天的时刻，否则这一轮判出来的会是 night 而不是双低。
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            self._double_low(DAYTIME)
            before = len(self._gate_rows())
            status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual([], payload["chunks"], "她这轮不想说：一泡都不该发")
        self.assertEqual("", payload["reply"], "没发出去的话不许留在 reply 里")
        self.assertIs(False, payload["send"])
        self.assertEqual(heart_gate.LEVEL_ENGAGE, payload["level"], "双低是人级拦的")
        self.assertEqual(heart_model.REASON_DOUBLE_LOW, payload["reason"])
        rows = self._gate_rows()
        self.assertEqual(before + 1, len(rows), "门发也写、不发也写：这一轮该多一行")
        row = rows[-1]
        self.assertEqual(False, row["send"])
        self.assertEqual(heart_gate.LEVEL_ENGAGE, row["level"])
        self.assertEqual(heart_model.STATE_SILENT, row["state"])
        self.assertEqual(heart_model.REASON_DOUBLE_LOW, row["reason"])
        self.assertIsNone(row["group"], "私聊不写群号")
        self.assertNotIn("_gate_input", payload, "私有键漏进了回包")

    def test_the_group_quota_stops_the_third_line_in_the_same_group(self):
        """同一个群每小时 2 条（窗口 60 分）→ 第三条被群级拦下；换一个群照样有额度。"""
        write_json(self._heart_root() / heart_gate.CONFIG_FILE,
                   {"group_quota_per_hour": 2, "group_window_min": 60})
        group = {"scene": brain_model.SCENE_GROUP, "where": {"groupId": "88776655"}}
        for index in range(2):
            status, payload = self._post(dict(group, text="第 %d 句" % index),
                                         _body("第 %d 句的回话，碗我放桌上了。" % index))
            self.assertEqual(200, status)
            self.assertTrue(payload["chunks"], "配额内的这一轮该发：%r" % (payload["reason"],))
            self.assertIs(True, payload["send"])
        status, payload = self._post(dict(group, text="第三句"),
                                     _body("第三句的回话，碗我放桌上了。"))
        self.assertEqual(200, status)
        self.assertEqual([], payload["chunks"], "同群第三条该被群配额拦下")
        self.assertEqual("", payload["reply"])
        self.assertIs(False, payload["send"])
        self.assertEqual(heart_gate.LEVEL_GROUP, payload["level"])
        self.assertEqual(heart_gate.REASON_GROUP_QUOTA, payload["reason"])
        row = self._gate_rows()[-1]
        self.assertEqual(group["where"]["groupId"], row["group"])
        self.assertEqual(2, row["quota_used"], "拦下这一行的用量该是窗口里已有的那两条")
        self.assertEqual(heart_gate.REASON_GROUP_QUOTA, row["reason"])
        other = {"scene": brain_model.SCENE_GROUP, "where": {"groupId": "11223344"}}
        status, payload = self._post(dict(other, text="新群里问一句"),
                                     _body("新群里我照常说，碗我放桌上了。"))
        self.assertEqual(200, status)
        self.assertTrue(payload["chunks"], "换一个群该有额度")
        self.assertIs(True, payload["send"])
        self.assertEqual(other["where"]["groupId"], self._gate_rows()[-1]["group"])
        # 没有 where 就退到老字段 body["group"]；场合不是 group 就一律私聊（哪怕 body 里塞了群号）
        status, payload = self._post({"scene": brain_model.SCENE_GROUP, "group": "55667788",
                                      "text": "老字段那一问"},
                                     _body("老字段我也照常说，碗我放桌上了。"))
        self.assertEqual(200, status)
        self.assertTrue(payload["chunks"])
        self.assertEqual("55667788", self._gate_rows()[-1]["group"], "没有 where 该退到老字段 group")
        status, payload = self._post({"scene": brain_model.SCENE_PRIVATE, "group": "88776655",
                                      "text": "私聊里带了个群号"},
                                     _body("私聊里带群号也还是私聊，碗我放桌上了。"))
        self.assertEqual(200, status)
        self.assertTrue(payload["chunks"], "带群号的私聊仍是私聊：不许被群配额拦下")
        self.assertIsNone(self._gate_rows()[-1]["group"], "场合不是 group 就不占群额度")

    def test_private_rounds_never_touch_the_group_window(self):
        """私聊 12 轮：每一轮都发；group_window.json 一个字节都不出现（group=None 不占不写）。"""
        for index in range(12):
            status, payload = self._post({"text": "第 %d 句" % index},
                                         _body("第 %d 句的回话，碗我放桌上了。" % index))
            self.assertEqual(200, status)
            self.assertIs(True, payload["send"], "私聊第 %d 轮该发" % index)
            self.assertTrue(payload["chunks"], "私聊第 %d 轮该有话说：%r" % (index, payload["reason"]))
            self.assertIsNone(self._gate_rows()[-1]["group"], "私聊不写群号")
        self.assertFalse((self._heart_root() / heart_gate.WINDOW_FILE).exists(),
                         "私聊不占不写：群窗账一个字节都不该出现")

    def test_the_private_key_never_reaches_the_response(self):
        """装配交回的私有键只在核里过一手：回包里没有它，而且整份回包是能序列化的 JSON。"""
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertNotIn("_gate_input", payload, "私有键漏进了回包")
        json.dumps(payload, ensure_ascii=False, sort_keys=True)   # 带对象就抛 TypeError

    def test_a_missing_engage_input_is_reported_not_silently_ignored(self):
        """拿不到 EngageInput（装配没交出来）→ 照常发 + 一行 WARNING + gate 一节写明未判。"""
        transport = _body(LONG_REPLY)
        stack = self._answer(transport)
        with stack:
            inner = conversation.answer

            def stripped(text, who="", **kw):
                got = inner(text, who, **kw)
                got.pop("_gate_input", None)          # 装配那一头没交出来
                return got

            with mock.patch.object(conversation, "answer", stripped):
                with self.assertLogs(chat.LOGGER_NAME, level="WARNING") as caught:
                    status, payload = api.handle("POST", "/chat", {"text": QUESTION},
                                                 self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertTrue(any("出站闸未判" in line for line in caught.output),
                        "门缺席要响一声：%r" % (caught.output,))
        self.assertTrue(payload["chunks"], "门缺席不该让她开不了口")
        self.assertIs(True, payload["send"])
        section = payload["gate"]["outbound"]
        self.assertEqual(chat.OUTBOUND_UNJUDGED, section["status"], "回包里要如实说这一关没判")
        self.assertTrue(section["why"], "没判也要说清为什么")
        self.assertNotIn("_gate_input", payload)

    # —— 事件（核·识别）：/chat 触发的这一轮是 passive ——————————

    def test_the_event_rides_along_and_the_row_says_passive(self):
        """「你真厉害」走真链路：回包 event 五键原样透出、账本行 mode=passive。

        判据三件：① 识别与投递真的跑了（kind=praise、applied=True、心情上行）；
        ② /chat 是人发消息触发的 → 这一轮落账的 mode 必须是 "passive"（她自己开口那条路
        不经过本文件）；③ 账上的依据（note）一个字都不许进提示词。
        """
        self.brain.ingest(who="3425635473", text=MEMORY_TEXT)
        capture = []
        before = mood.read()["now"]
        status, payload = self._post({"text": "你真厉害", "scene": "private"},
                                     _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        event = payload["event"]
        self.assertEqual({"kind", "who", "applied", "reason", "mode"}, set(event),
                         "回包 event 的五个键对外冻结：%r" % (event,))
        self.assertEqual("praise", event["kind"], "夸没识别出来：%r" % (event,))
        self.assertTrue(event["applied"], "心没接住：%r" % (event,))
        self.assertEqual("passive", event["mode"], "/chat 触发的这一轮是 passive")
        self.assertGreater(mood.read()["now"], before, "夸了却没上行")
        row = events.rows()[-1]
        self.assertEqual("praise", row["kind"])
        self.assertEqual("passive", row["mode"], "账上那一行不是这一轮落下的")
        self.assertNotIn(row["note"], capture[0]["messages"][0]["content"],
                         "事件账上的依据进了提示词：%r" % (row["note"],))

    # —— 主动开口（proactive）：她自己起头那一轮 ——————————

    def test_proactive_without_seed_is_400_and_calls_no_model(self):
        """proactive 缺由头 → 400 并点名 seed；一个模型请求都不发（空由头不是「她想开口」）。"""
        capture = []
        for body in ({"mode": "proactive"},
                     {"mode": "proactive", "seed": "   "},
                     {"mode": "proactive", "seed": 3, "text": QUESTION}):
            with self.subTest(body=body):
                status, payload = self._post(body, _body(LONG_REPLY, capture))
                self.assertEqual(400, status, "缺由头却没拦：%r" % (payload,))
                self.assertIn("seed", payload["message"], "400 没点名 seed：%r" % (payload,))
        self.assertEqual([], capture, "由头缺了还是把请求发出去问了模型")

    def test_an_unknown_mode_falls_back_to_passive(self):
        """非法 mode（大写 / 空串 / None / 非字符串）→ 按 passive 走，不 400：与 scene 同一个口径。"""
        for bad in ("self-talk", "PROACTIVE", "", None, 3):
            with self.subTest(mode=bad):
                capture = []
                status, payload = self._post({"text": QUESTION, "mode": bad},
                                             _body(LONG_REPLY, capture))
                self.assertEqual(200, status, "非法 mode 被拦了：%r" % (payload,))
                self.assertEqual("passive", payload["mode"], "非法值没回落 passive")
                self.assertEqual(QUESTION, capture[0]["messages"][1]["content"],
                                 "回落 passive 之后 user 那一问该是对方原话")
                self.assertEqual({"text": "", "reason": ""}, payload["seed"],
                                 "被动那一轮不该记下由头")

    def test_a_proactive_round_reaches_the_prompt_with_an_empty_question(self):
        """proactive 走真链路：主动那份模板、user 空、段那句是「这一段没有别的发言」。

        顺手钉住「段给了也忽略」：body 里塞一份完全正常的段，段那一节仍是那句实话。
        """
        conf = conversation.load_assembly()
        capture = []
        status, payload = self._post({"mode": "proactive", "seed": SEED, "reason": "evening",
                                      "segment": [["234567890", "我妈住院了"]]},
                                     _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual("proactive", payload["mode"], "回包的 mode 不是 proactive")
        self.assertEqual({"text": SEED, "reason": "evening"}, payload["seed"],
                         "回包没记下由头与理由码")
        prompt = capture[0]["messages"][0]["content"]
        self.assertIn(conf["prompt"]["proactive"].split("{sections}")[0].strip(), prompt,
                      "提示词没用 prompt.proactive 那份模板")
        self.assertNotIn(conf["prompt"]["system"].split("{sections}")[0].strip(), prompt,
                         "主动那一轮还在用被动的模板")
        self.assertIn(SEED, prompt, "由头没进提示词")
        self.assertEqual("", capture[0]["messages"][1]["content"],
                         "主动那一轮 user 那一问该是空串（没有对方的话）")
        self.assertEqual(conversation.NO_SEGMENT, self._segment_body(prompt),
                         "主动那一轮的段不是「这一段没有别的发言」")
        self.assertEqual(conf["order"], payload["assembly"]["order"], "装配的几段被改了")

    def test_a_night_time_proactive_round_is_not_sent(self):
        """同样由她起头：夜里出站闸判不发（reason=night），chunks 清空——**这不是空产出**。"""
        with mock.patch.object(brain_model, "now_iso", lambda: NIGHTTIME):
            status, payload = self._post({"mode": "proactive", "seed": SEED},
                                         _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual("proactive", payload["mode"])
        self.assertIs(False, payload["send"], "夜里主动该判不发：%r" % (payload.get("reason"),))
        self.assertEqual(heart_model.REASON_NIGHT, payload["reason"], "不是夜里那道硬门拦的")
        self.assertEqual([], payload["chunks"])
        self.assertEqual("", payload["reply"])
        self.assertNotIn("outbound", payload["gate"],
                         "send=false 被记成了空产出（那是两件事）")

    def test_an_empty_reply_is_reported_as_core_empty(self):
        """门说发、装配却一个字都没出来 → gate.outbound 记 core_empty，chunks 与 reply 都是空。

        素材是红线那一句（唯一不许留原话的一类）：这一轮 send 是 true，可 reply 是空串——
        与「门说不发」的那一轮必须分得开。
        """
        # 钉在一个确定是白天的时刻：夜里那一道硬门先判「不发」，这一条要的是「门说发」那一态。
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            status, payload = self._post({"mode": "proactive", "seed": SEED},
                                         _body("我手机号是 13812345678，你记一下"))
        self.assertEqual(200, status)
        self.assertIs(True, payload.get("send"), "这一轮门说发，只是没话可发：%r"
                      % (payload.get("reason"),))
        self.assertEqual("", payload["reply"], "空的那一轮还是把话交出去了")
        self.assertEqual([], payload["chunks"])
        self.assertEqual({"status": chat.CORE_EMPTY}, payload["gate"]["outbound"],
                         "空产出没记成 core_empty：%r" % (payload["gate"].get("outbound"),))

    # —— who 的形状：通道发的是对象，脑要的是号 ————————————————

    def _post_day(self, body, reply: str = "今天挺好的，我记着了。"):
        """钉在白天再打一次：夜里那道硬门会判「不发」，落脑就看不到她那句。"""
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            return self._post(body, _body(reply))

    def test_a_channel_who_object_is_recorded_as_the_number_not_the_placeholder(self):
        """实盘那个 bug 的靶子：对象进 who，落库的 who 必须是号，「人」一个都不许有。"""
        status, payload = self._post_day({"text": MEMORY_TEXT, "who": dict(CHANNEL_WHO),
                                          "scene": "private"})
        self.assertEqual(200, status)
        self.assertTrue(payload.get("ok"))
        rows = self.brain.today_entries()
        whos = [row["who"] for row in rows]
        self.assertIn(CHANNEL_WHO_ID, whos, "他说的那句没记在他号下：%r" % (whos,))
        self.assertNotIn("人", whos, "who 仍然落到了占位符「人」——_who 没认对象形状")
        hers = [row["who"] for row in rows if row.get("side") == brain_model.SIDE_SELF]
        self.assertEqual([self.store.snapshot().identity.display], hers,
                         "她那句的 who 不该跟着他变：%r" % (hers,))

    def test_the_nickname_and_card_become_aliases_on_his_card(self):
        """昵称 / 群名片是**别名**：身份仍是号，别名登记进人名录，一条记忆都不多写。"""
        status, payload = self._post_day({"text": MEMORY_TEXT, "who": dict(CHANNEL_WHO),
                                          "scene": "private"})
        self.assertEqual(200, status)
        person = self.brain.lookup(CHANNEL_WHO_ID)
        self.assertIsNotNone(person, "号下没有卡：别名没登记进人名录")
        self.assertEqual(CHANNEL_WHO["nickname"], person.get("display"), "昵称没落成显示名")
        self.assertIn(CHANNEL_WHO["card"], person.get("aliases") or [], "群名片没进别名表")
        self.assertEqual(CHANNEL_WHO_ID, person.get("id"), "身份被别名顶掉了")
        # 登记别名不许写记忆条：一轮两人各一条，一条都不许多。
        self.assertEqual(2, len(payload["memory"]["wrote"]),
                         "登记别名顺手写了记忆：%r" % (payload["memory"]["wrote"],))
        self.assertEqual(2, len(self.brain.today_entries()))
        # 名片段取到东西，且昵称在里头（它就落成了「称呼」）
        text = conversation._person_text(self.brain, CHANNEL_WHO_ID)
        self.assertTrue(text.strip(), "「关于他」整段是空的")
        self.assertIn(CHANNEL_WHO["nickname"], text)
        self.assertIn(CHANNEL_WHO["card"], text, "别名没交代进名片段")

    def test_his_sentence_comes_back_when_recall_runs_under_the_same_who(self):
        """召回按 who 加权：同一 who 下说的那句，换一句问也要回得来（不是靠「人」）。"""
        status, _first = self._post_day({"text": "记住小红花", "who": dict(CHANNEL_WHO),
                                         "scene": "private"})
        self.assertEqual(200, status)
        status, second = self._post_day({"text": "我刚才说的小红花你记得吗",
                                         "who": dict(CHANNEL_WHO), "scene": "private"})
        self.assertEqual(200, status)
        rows = second.get("recalled") or []
        hit = [row for row in rows if "小红花" in str(row.get("text") or "")]
        self.assertTrue(hit, "那句没被召回来：%r" % ([row.get("text") for row in rows],))
        self.assertEqual(CHANNEL_WHO_ID, hit[0].get("who"), "召回来的那条不在他号下")

    def test_string_object_and_missing_who_all_land_on_the_same_rule(self):
        """三种形状走同一条路：串原样过口径、对象取 id、都没有就是占位符「人」。"""
        cases = [
            ("字符串", {"who": CHANNEL_WHO_ID}, CHANNEL_WHO_ID),
            ("对象", {"who": dict(CHANNEL_WHO)}, CHANNEL_WHO_ID),
            ("对象里 id 是串", {"who": {"id": CHANNEL_WHO_ID}}, CHANNEL_WHO_ID),
            ("什么都没有", {}, "人"),                  # 不知道是谁：照旧兜底「人」，不猜
        ]
        for index, (label, extra, want) in enumerate(cases):
            body = {"text": "三种形状第%d号" % index, "scene": "private"}
            body.update(extra)
            status, payload = self._post_day(body)
            self.assertEqual(200, status, "%s：%r" % (label, payload.get("message")))
            row = [one for one in self.brain.today_entries()
                   if one.get("text") == body["text"]][0]
            self.assertEqual(want, row["who"], "%s 的 who 不对" % label)

    def test_broken_who_shapes_fall_back_to_empty_without_raising(self):
        """错误形状（数字 / 数组 / 对象里没 id / 字面昵称）一律空串——不猜、不抛、不 400。"""
        for bad in (123, [], {"nickname": "x"}, {"id": "能毕业就是胜利"}, {"id": True},
                    "能毕业就是胜利", None):
            self.assertEqual("", chat._who({"who": bad}), "错误形状没回落空串：%r" % (bad,))
            self.assertEqual([], chat._aliases_of({"who": bad}),
                             "认不出身份还交出了别名：%r" % (bad,))
        for body in ({}, {"who": ""}, None, [], "x"):
            self.assertEqual("", chat._who(body))
            self.assertEqual([], chat._aliases_of(body))
        # 真打一次：坏形状不 400、不抛，落库照旧是占位符
        for index, bad in enumerate((123, [], {"nickname": "x"})):
            body = {"text": "坏形状第%d号" % index, "who": bad, "scene": "private"}
            status, _payload = self._post_day(body)
            self.assertEqual(200, status, "坏形状把这一轮拦下了：%r" % (bad,))
            row = [one for one in self.brain.today_entries()
                   if one.get("text") == body["text"]][0]
            self.assertEqual("人", row["who"], "坏形状的 who 不是占位符：%r" % (bad,))
        people = self.brain._people_data().get("people", {})
        self.assertEqual([], [one for one in people if not one.isdigit()],
                         "坏形状在名录里建了卡：%r" % (sorted(people),))


if __name__ == "__main__":
    unittest.main()
