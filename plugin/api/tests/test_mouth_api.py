"""嘴的 HTTP 层：四条只读干跑端点、写方法一律 405、未接入如实报。

覆盖的是 api/mouth.py 与 api/core.py 的接线：路由表、返回形状、405/400/404、
brain=None / store=None、心的读数只从查询参数来、闸命中即弃、以及「没有写盘路径」这条自查。

第三批第二轮又给 /mouth/gate 加了四个可选参数（inject / refuse / tier / hard_cap），
**没有新路由**：这里逐条守住「不给新参数时与从前逐字一致」、三本账各记一行、
以及「没比过的审计不许显示成通过」这三件事。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from api import api
from api import core as api_core
from api import mouth as mouth_api
from api.tests.helpers import copy_fixture
from organs.brain import BrainStore
from organs.mouth import gate as mouth_gate
from organs.soul import load

MOUTH_ROUTES = ("/mouth/split", "/mouth/address", "/mouth/tone", "/mouth/gate")
SOURCE = Path(mouth_api.__file__).read_text(encoding="utf-8")

# 300 字中文：有停顿符号，切得出 2–3 泡（条数上限与硬切阈值都在 policy.json，这里只是素材）
TEXT_300 = ("昨晚下了一场雨，早上路边的梧桐叶全亮了。我端着豆浆站在街口，看扫地的大爷把落叶推成一堆。"
            "他推得很慢，风一吹又散开，他也不恼，重新推一遍。公交来了又走，我没上车，"
            "想着今天反正不赶时间，就看他推完那堆叶子。他推完直起腰，冲我点了点头，"
            "说这树真能掉。我说是啊，一场雨就是一地。他笑了笑，说你要不要也来推两下，"
            "扫帚还有一把。我摇头说算了，我手上还拿着豆浆呢。他说那下回吧，"
            "下回你早点来，叶子刚掉的时候最好推，软。我说好，就真的记住了这句话，"
            "一整个上午都没再想起别的事。我又想起来，他说话的时候手上那根扫帚一直没放下，"
            "就那么斜着抵在地上。天色慢慢亮起来，街上的人多了一点，谁也没注意到我们俩在说叶子。"
            "风又起了。")[:300]

# —— /mouth/gate 那四个可选参数的素材（都不踩条款；字数按字符算，标点也算一个字） ——————
GATE_PASS_TEXT = "走吧，路口那家面馆还开着。"
SOUL_LINE = "她说话从来不绕弯子，也不替别人做决定。"        # 一段「魂」原文：照搬它就是复述
OVER_TARGET_TEXT = "走吧，路口那家面馆还开着，我请你吃碗热的，吃完再慢慢走回去。"   # 30 字
CAP_80_TEXT = ("走吧，路口那家面馆还开着。我请你吃一碗热的，吃完再慢慢走回去，路上不用赶，"
               "风也不大，路灯刚亮，街上没什么人，正好说说话。你要是不想动，我就端过来。"
               "我在楼下等你。")                                                    # 80 字

# 不给新参数时的基线（改动前实测的那一份）：键集合只许多一节 audit，其余逐字不许变。
GATE_BASELINE = {
    "ok": True, "readonly": True, "agent": "mouth", "hit": None, "decision": "pass",
    "reason": "没命中任何条款，这句照发。", "text": GATE_PASS_TEXT, "rewriter": False,
    "disabled_ids": ["explainer-connector"],
}
GATE_BASELINE_NOTE = ("这一轮没有重写器：命中就弃（decision=dropped），api 层不代写一次；"
                      "弃掉的 text 是空串，不会被当成 pass 发出去。drops 这一节是弃言账本的读数："
                      "today 是今天弃了几次（pass 的那次也照报），exists=false 表示账本还没建、"
                      "errors 是读不出来的坏行数；disabled_ids 是这次被停用、没参与匹配的条款。")


def _inj(kind: str, text: str) -> str:
    """把一段注入原文编成 inject 的查询串取值（URL 编码的 JSON 数组）。"""
    return quote(json.dumps([{"kind": kind, "text": text}], ensure_ascii=False))


class MouthApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 每个测试一套自己的弃言账本：账本是按天累计的，共用一份就会让「今天几次」串味
        self._trace = Path(self._tmp.name) / "trace"
        self._env = os.environ.get("MOUTH_TRACE_DIR")
        os.environ["MOUTH_TRACE_DIR"] = str(self._trace)
        self.addCleanup(self._restore_trace_env)
        self.store = load(copy_fixture("valid", self._tmp.name))
        self.brain = BrainStore(Path(self._tmp.name) / "brain")
        for _ in range(20):                      # 今天连续二十次互动 → 读数 20 → 挚友档
            self.brain.interact("u-familiar", display="小林")

    def _restore_trace_env(self) -> None:
        """把 MOUTH_TRACE_DIR 还原成进来时的样子（别的测试不该被这份账本影响）。"""
        if self._env is None:
            os.environ.pop("MOUTH_TRACE_DIR", None)
        else:
            os.environ["MOUTH_TRACE_DIR"] = self._env

    def _ledger(self) -> Path:
        return mouth_api.drops.path_of()

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
              store: Any = "default", brain: Any = "default") -> Tuple[int, Dict[str, Any]]:
        status, payload = api.handle(
            method, path, body,
            self.store if store == "default" else store,
            brain=self.brain if brain == "default" else brain)
        json.dumps(payload, ensure_ascii=False)
        return status, payload

    # —— 1. 四条端点各自的 200 与返回形状 ————————————————————

    def test_split_returns_full_shape(self):
        status, payload = self._call("GET", "/mouth/split?text=先说这一句。再说这一句。")
        self.assertEqual(200, status)
        for key in ("ok", "readonly", "agent", "parts", "summary", "chars",
                    "visible_chars", "seed", "note"):
            self.assertIn(key, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readonly"])
        self.assertIsInstance(payload["parts"], list)
        for part in payload["parts"]:
            for key in ("text", "gap_before", "send_at", "degraded", "reason"):
                self.assertIn(key, part)

    def test_address_returns_full_shape(self):
        status, payload = self._call("GET", "/mouth/address?who=u-familiar")
        self.assertEqual(200, status)
        for key in ("ok", "readonly", "agent", "who", "display", "tier", "relation",
                    "address", "frequency", "banned_self", "note"):
            self.assertIn(key, payload)
        self.assertEqual("u-familiar", payload["who"])
        self.assertEqual("小林", payload["display"])
        self.assertIsInstance(payload["relation"], int)

    def test_tone_returns_full_shape(self):
        status, payload = self._call("GET", "/mouth/tone?who=u-familiar")
        self.assertEqual(200, status)
        for key in ("ok", "readonly", "agent", "addressee", "tone", "heart", "note"):
            self.assertIn(key, payload)
        for key in ("who", "display", "tier", "relation", "address",
                    "frequency", "banned_self", "note"):
            self.assertIn(key, payload["addressee"])
        for key in ("tier", "label", "line", "stance", "self_name",
                    "heart_source", "softness", "ban_joke"):
            self.assertIn(key, payload["tone"])

    def test_gate_returns_full_shape(self):
        status, payload = self._call("GET", "/mouth/gate?text=走吧，路口那家面馆还开着。")
        self.assertEqual(200, status)
        for key in ("ok", "readonly", "agent", "hit", "decision", "reason",
                    "text", "rewriter", "disabled_ids", "drops", "note"):
            self.assertIn(key, payload)
        for key in ("today", "last_clause_id", "line", "trace", "exists", "errors"):
            self.assertIn(key, payload["drops"])       # pass 的那次也带这一节
        self.assertIsNone(payload["hit"])
        self.assertFalse(payload["rewriter"])
        self.assertIsInstance(payload["disabled_ids"], list)

    # —— 2. 300 字中文：2–3 泡、首条 0.0、其余 > 0 ————————————————

    def test_split_300_chars_makes_two_or_three_parts(self):
        self.assertEqual(300, len(TEXT_300))
        status, payload = self._call("GET", "/mouth/split?text=" + TEXT_300 + "&seed=demo")
        self.assertEqual(200, status)
        parts = payload["parts"]
        self.assertIn(len(parts), (2, 3))
        self.assertEqual(0.0, parts[0]["gap_before"])
        for part in parts[1:]:
            self.assertGreater(part["gap_before"], 0.0)
        self.assertTrue(payload["summary"])
        self.assertEqual(300, payload["chars"])
        self.assertGreater(payload["visible_chars"], 0)
        self.assertLess(payload["visible_chars"], payload["chars"])
        self.assertEqual("".join(part["text"] for part in parts), TEXT_300)

    # —— 3. 非 GET 一律 405，措辞里看得出「只读」 ————————————————

    def test_non_get_is_405_with_readonly_wording(self):
        for route in MOUTH_ROUTES:
            for method in ("POST", "PUT", "DELETE"):
                status, payload = self._call(method, route, {})
                self.assertEqual(405, status, "%s %s" % (method, route))
                self.assertIn("只读", payload["message"])
                self.assertIn("嘴的干跑端点", payload["message"])

    # —— 4. 缺参要响 ————————————————————————————————

    def test_missing_or_blank_text_is_400(self):
        for route in ("/mouth/split", "/mouth/gate"):
            for path in (route, route + "?text=", route + "?text=%20%20%20"):
                status, payload = self._call("GET", path)
                self.assertEqual(400, status, path)
                self.assertFalse(payload["ok"])
                self.assertIn("text", payload["message"])

    # —— 5. 人名录没有这个人：200 + 回退，不是 404、不是 0 ——————————

    def test_unknown_who_falls_back_with_note(self):
        for route in ("/mouth/address", "/mouth/tone"):
            status, payload = self._call("GET", route + "?who=nobody-at-all")
            self.assertEqual(200, status, route)
            called = payload["addressee"] if "addressee" in payload else payload
            self.assertIn("回退", called["note"])
            self.assertEqual("生人", called["tier"])
            self.assertEqual(1, called["relation"])
            self.assertEqual("你", called["address"])

    # —— 6. 未载入如实报 ——————————————————————————————

    def test_brain_none_reports_unconnected(self):
        for route in ("/mouth/address", "/mouth/tone"):
            status, payload = self._call("GET", route + "?who=u-familiar", brain=None)
            self.assertEqual(200, status, route)
            self.assertEqual("未接入", payload["status"])
            self.assertTrue(payload["readonly"])
            self.assertIn("未接入", payload["message"])

    def test_store_none_reports_unconnected(self):
        status, payload = self._call("GET", "/mouth/tone?who=u-familiar", store=None)
        self.assertEqual(200, status)
        self.assertEqual("未接入", payload["status"])
        self.assertTrue(payload["readonly"])

    # —— 7. 心的读数只从查询参数来 ——————————————————————

    def test_tone_heart_unconnected_by_default(self):
        status, payload = self._call("GET", "/mouth/tone?who=u-familiar")
        self.assertEqual(200, status)
        self.assertIsNone(payload["heart"])
        self.assertIn("未接入", payload["tone"]["heart_source"])

    def test_tone_heart_from_query_changes_softness(self):
        _, plain = self._call("GET", "/mouth/tone?who=u-familiar")
        status, tired = self._call("GET", "/mouth/tone?who=u-familiar&energy=15")
        self.assertEqual(200, status)
        self.assertIn("已接入", tired["tone"]["heart_source"])
        self.assertNotEqual(plain["tone"]["softness"], tired["tone"]["softness"])
        self.assertEqual(15.0, tired["heart"]["energy"])

    # —— 8. 熟人与生人：同一条路径，读数不同 ————————————————————

    def test_tone_familiar_vs_stranger_differ(self):
        _, familiar = self._call("GET", "/mouth/tone?who=u-familiar")
        _, stranger = self._call("GET", "/mouth/tone?who=nobody-at-all")
        self.assertNotEqual(familiar["addressee"]["address"],
                            stranger["addressee"]["address"])
        self.assertNotEqual(familiar["tone"]["line"], stranger["tone"]["line"])
        self.assertGreater(familiar["addressee"]["relation"],
                           stranger["addressee"]["relation"])

    # —— 9. 闸：命中就弃（没有重写器），没命中放行 ——————————————————

    def test_gate_drops_customer_service_line(self):
        status, payload = self._call(
            "GET", "/mouth/gate?text=" + "您好，请问有什么可以帮您的吗？")
        self.assertEqual(200, status)
        self.assertIsNotNone(payload["hit"])
        for key in ("clause_id", "kind", "reason", "matched"):
            self.assertIn(key, payload["hit"])
        self.assertEqual("courtesy-open", payload["hit"]["clause_id"])
        self.assertTrue(payload["hit"]["reason"])
        self.assertEqual("dropped", payload["decision"])
        self.assertEqual("", payload["text"])
        self.assertIn("重写器", payload["reason"])

    def test_gate_passes_normal_line(self):
        status, payload = self._call(
            "GET", "/mouth/gate?text=走吧，路口那家面馆还开着。")
        self.assertEqual(200, status)
        self.assertIsNone(payload["hit"])
        self.assertEqual("pass", payload["decision"])
        self.assertEqual("走吧，路口那家面馆还开着。", payload["text"])

    # —— 9b. 弃言账本：弃了几次、最近一次是谁、账本在不在、坏行几行 ——————

    DROP_TEXT = "您好，请问有什么可以帮您的吗？"       # 这一轮没有重写器：命中就弃
    PASS_TEXT = "走吧，路口那家面馆还开着。"

    def test_gate_drops_are_counted_and_visible(self):
        """连喂 3 条被弃的话 → today=3，最近一次命中那条条款，人话里看得到 3 与 id。"""
        for _ in range(3):
            status, payload = self._call("GET", "/mouth/gate?text=" + self.DROP_TEXT)
            self.assertEqual(200, status)
            self.assertEqual("dropped", payload["decision"])
        section = payload["drops"]
        self.assertEqual(3, section["today"])
        self.assertEqual("courtesy-open", section["last_clause_id"])
        self.assertIn("3", section["line"])
        self.assertIn("courtesy-open", section["line"])
        self.assertTrue(section["exists"])
        self.assertEqual(0, section["errors"])
        self.assertEqual(str(self._trace / "drops.jsonl"), section["trace"])
        self.assertEqual(3, len(self._ledger().read_text(encoding="utf-8").splitlines()))

    def test_pass_still_reports_the_drops_section(self):
        """再喂 1 条正常话：today 不涨，但这一节照带——弃过几次不该只有被弃那次才看得见。"""
        for _ in range(3):
            self._call("GET", "/mouth/gate?text=" + self.DROP_TEXT)
        status, payload = self._call("GET", "/mouth/gate?text=" + self.PASS_TEXT)
        self.assertEqual(200, status)
        self.assertEqual("pass", payload["decision"])
        self.assertEqual(3, payload["drops"]["today"], "pass 不该让 today 涨")
        self.assertEqual(3, len(self._ledger().read_text(encoding="utf-8").splitlines()),
                         "pass 不该往账本里添行")

    def test_broken_line_is_counted_not_hidden(self):
        """手写一行非法 JSON：坏行跳过并计入 errors，其余计数照常，端点不抛错。"""
        self._call("GET", "/mouth/gate?text=" + self.DROP_TEXT)
        ledger = self._ledger()
        ledger.write_text(ledger.read_text(encoding="utf-8") + "{这不是 JSON\n", encoding="utf-8")
        status, payload = self._call("GET", "/mouth/gate?text=" + self.PASS_TEXT)
        self.assertEqual(200, status)
        self.assertEqual(1, payload["drops"]["errors"])
        self.assertEqual(1, payload["drops"]["today"])
        self.assertIn("读不出来", payload["drops"]["line"])

    def test_missing_ledger_is_zero_and_says_so(self):
        """账本还不存在 = 今天还没记过：0 次 + exists=false 的一句人话，不是 500、不是抛错。"""
        self.assertFalse(self._ledger().exists())
        status, payload = self._call("GET", "/mouth/gate?text=" + self.PASS_TEXT)
        self.assertEqual(200, status)
        section = payload["drops"]
        self.assertEqual(0, section["today"])
        self.assertFalse(section["exists"])
        self.assertIsNone(section["last_clause_id"])
        self.assertIn("今天还没有因闸弃言", section["line"])

    def test_unwritable_ledger_is_loud(self):
        """账本写不进去就回 500：记不上账还说「今天 0 次」，正是这本账要防的事。"""
        blocker = Path(self._tmp.name) / "blocked"      # 拿一个文件占住目录的位置
        blocker.write_text("x", encoding="utf-8")
        os.environ["MOUTH_TRACE_DIR"] = str(blocker)
        status, payload = self._call("GET", "/mouth/gate?text=" + self.DROP_TEXT)
        self.assertEqual(500, status)
        self.assertFalse(payload["ok"])
        self.assertIn("blocked", payload["message"])

    def test_gate_reports_disabled_clauses_and_skips_them(self):
        """默认停用的那条不参与匹配，但「有条款被停用」要在返回里看得见。"""
        status, payload = self._call("GET", "/mouth/gate?text=" + "总之先这样吧，明天再说。")
        self.assertEqual(200, status)
        self.assertIn("explainer-connector", payload["disabled_ids"])
        self.assertIsNone(payload["hit"])
        self.assertEqual("pass", payload["decision"])

    # —— 9c. rename：当场换称呼、返回 memory_draft、GET 不 remember ——————————

    def test_address_rename_switches_and_hands_out_memory_draft(self):
        """rename 给了就当场换：返回里 renamed=true、source=本轮改名、外加一份投给脑的草稿。"""
        status, payload = self._call("GET", "/mouth/address?who=u-familiar&rename=小王")
        self.assertEqual(200, status)
        table = mouth_api.address.load_table()
        self.assertEqual("小王", payload["address"])
        self.assertTrue(payload["renamed"])
        self.assertEqual(table["source"]["rename"], payload["source"])
        draft = payload["memory_draft"]
        self.assertEqual({"who", "text", "source_type", "ts"}, set(draft))
        self.assertEqual("u-familiar", draft["who"])
        self.assertEqual(table["self_guard"]["rename_memory_text"].format(address="小王"), draft["text"])
        self.assertEqual("亲历", draft["source_type"])

    def test_tone_rename_changes_the_line(self):
        """同一句语气口径行，带上 rename 之后「对谁」那一格跟着换，档位不动。"""
        _, plain = self._call("GET", "/mouth/tone?who=u-familiar")
        status, called = self._call("GET", "/mouth/tone?who=u-familiar&rename=小王")
        self.assertEqual(200, status)
        self.assertEqual(plain["tone"]["tier"], called["tone"]["tier"], "换称呼不许连档位一起换")
        self.assertNotEqual(plain["tone"]["line"], called["tone"]["line"])
        self.assertIn("小王", called["tone"]["line"])
        self.assertEqual("小王", called["addressee"]["address"])
        self.assertTrue(called["renamed"])
        self.assertEqual("u-familiar", called["memory_draft"]["who"])

    def test_without_rename_nothing_is_added_but_the_new_fields(self):
        """没给 rename：返回与从前一致——不多 renamed/memory_draft，addressee 只多两个可选字段。"""
        status, payload = self._call("GET", "/mouth/address?who=u-familiar")
        self.assertEqual(200, status)
        self.assertEqual({"ok", "readonly", "agent", "who", "display", "tier", "relation",
                          "address", "frequency", "banned_self", "note",
                          "renamed", "source"}, set(payload))
        self.assertFalse(payload["renamed"])
        self.assertEqual("人名录", payload["source"])
        status, voice = self._call("GET", "/mouth/tone?who=u-familiar")
        self.assertEqual({"ok", "readonly", "agent", "addressee", "tone", "heart", "note"},
                         set(voice))
        self.assertFalse(voice["addressee"]["renamed"])
        self.assertEqual("人名录", voice["addressee"]["source"])

    def test_get_never_remembers_the_rename(self):
        """端点只干跑：查过一次改了名的称呼，下一次不带 rename 时仍是名录原样（会话不落账）。"""
        self._call("GET", "/mouth/address?who=u-familiar&rename=小王")
        _, again = self._call("GET", "/mouth/address?who=u-familiar")
        self.assertEqual("小林", again["address"], "GET 把改名记下来了")
        self.assertEqual("人名录", again["source"])
        self.assertNotIn("remember(", SOURCE, "api 层不许调 remember：会话覆盖只在库层提供")

    def test_rename_to_a_banned_self_word_still_falls_back(self):
        """改名不许绕过自称红线：rename 落在违禁自称里 → 仍是回退词，note 说明被哪条拦下。"""
        table = mouth_api.address.load_table()
        banned_word = table["self_guard"]["words"][0]
        status, payload = self._call("GET", "/mouth/address?who=u-familiar&rename=" + banned_word)
        self.assertEqual(200, status)
        self.assertEqual(table["fallback"]["address"], payload["address"])
        self.assertEqual(banned_word, payload["banned_self"])
        self.assertFalse(payload["renamed"])
        self.assertNotIn("memory_draft", payload, "被拦下的改名不该给出一份投给脑的草稿")
        self.assertEqual(table["self_guard"]["rename_note"], payload["note"],
                         "note 没说是哪一条拦下的")

    # —— 9d. /mouth/gate 的四个可选参数（没有新端点，顺序：拒绝 → 闸 → 长度） ————————

    def test_gate_without_new_params_keeps_the_old_shape(self):
        """不给 inject/refuse/tier：旧键旧值逐字不变（note 与 drops 也算），只多一节 audit。"""
        status, payload = self._call("GET", "/mouth/gate?text=" + GATE_PASS_TEXT)
        self.assertEqual(200, status)
        self.assertEqual({"ok", "readonly", "agent", "hit", "decision", "reason", "text",
                          "rewriter", "disabled_ids", "drops", "note", "audit"}, set(payload),
                         "不给新参数时键集合只许多一节 audit")
        for key, want in GATE_BASELINE.items():
            self.assertEqual(want, payload[key], key)
        trace = str(self._trace / "drops.jsonl")
        self.assertEqual({"today": 0, "last_clause_id": None, "trace": trace, "exists": False,
                          "errors": 0,
                          "line": "今天还没有因闸弃言（账本 %s 还不存在，等于今天还没记过）。" % trace},
                         payload["drops"])
        self.assertEqual(GATE_BASELINE_NOTE, payload["note"], "note 变了：不给新参数时返回要逐字一致")
        self.assertEqual("audit-unavailable", payload["audit"]["status"],
                         "没给 inject 就是没比过，audit 要如实报 audit-unavailable")

    def test_gate_refuse_topic_says_one_of_the_three_lines(self):
        """refuse=topic：说法取自数据、闸判 pass、事件四键齐、sent=false（这层没有心）、账本多一行。"""
        ledger = mouth_api.refusal.path_of()
        self.assertFalse(ledger.exists())
        status, payload = self._call("GET", "/mouth/gate?refuse=topic")   # 拒绝不要求 text
        self.assertEqual(200, status)
        section = payload["refusal"]
        self.assertEqual({"text", "shape", "full_name", "event", "sent",
                          "gate_decision", "reason"}, set(section))
        said = [mouth_api.refusal.say(one) for one in ("topic", "mood", "nobody")]
        self.assertIn(section["text"], said, "拒绝说法必须来自 data/refusal.json 那三句")
        self.assertEqual("topic", section["shape"])
        self.assertEqual("refused-topic", section["full_name"])
        self.assertEqual("pass", section["gate_decision"])
        self.assertFalse(section["sent"], "HTTP 这层没有心：sent 必须如实是 false")
        self.assertEqual({"ts", "shape", "full_name", "why"}, set(section["event"]))
        rows = ledger.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(rows), "拒了就要在 refuse.jsonl 里留一行")
        row = json.loads(rows[0])
        self.assertEqual(section["event"]["ts"], row["ts"])
        self.assertEqual("refused-topic", row["full_name"])
        self.assertFalse(row["sent"])

    def test_gate_refuse_unknown_shape_is_400_and_names_it(self):
        """refuse 给了个数据里没有的形状：400 并指名，不静默挑一个、也不往账本里写。"""
        status, payload = self._call("GET", "/mouth/gate?refuse=bogus")
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("bogus", payload["message"])
        self.assertFalse(mouth_api.refusal.path_of().exists(), "认不得的形状不该进拒绝账本")

    def test_gate_inject_recited_soul_is_dropped(self):
        """inject 给一段魂、文本照搬 → 弃掉；audit.status=ok 且命中「复述设定段」。"""
        status, payload = self._call(
            "GET", "/mouth/gate?text=" + SOUL_LINE + "&inject=" + _inj("魂", SOUL_LINE))
        self.assertEqual(200, status)
        self.assertEqual("dropped", payload["decision"])
        self.assertEqual("", payload["text"], "弃掉的话不许留半句")
        self.assertEqual("ok", payload["audit"]["status"])
        self.assertEqual("复述设定段", payload["audit"]["hit"]["kind"])
        self.assertEqual("魂", payload["audit"]["hit"]["seg_kind"])
        self.assertGreaterEqual(payload["audit"]["lcs_len"], payload["audit"]["threshold"])
        self.assertEqual(1, payload["drops"]["today"], "弃了照旧进弃言账本")

    def test_gate_without_inject_reports_audit_unavailable(self):
        """没给 inject：审计判不了，status 是 audit-unavailable（**不等于 ok**），这句照样 pass。"""
        status, payload = self._call("GET", "/mouth/gate?text=" + GATE_PASS_TEXT)
        self.assertEqual(200, status)
        self.assertNotEqual("ok", payload["audit"]["status"], "没比过的审计不许报成通过")
        self.assertEqual("audit-unavailable", payload["audit"]["status"])
        self.assertIsNone(payload["audit"]["hit"])
        self.assertEqual("pass", payload["decision"])

    def test_gate_bad_inject_is_400_with_the_array_wording(self):
        """inject 读不出 JSON、或解出来不是数组：都是 400，且说清「inject 要一个 JSON 数组」。"""
        for raw in ("{不是JSON", '{"kind": "魂"}'):
            path = "/mouth/gate?text=" + GATE_PASS_TEXT + "&inject=" + quote(raw)
            status, payload = self._call("GET", path)
            self.assertEqual(400, status, raw)
            self.assertIn("inject 要一个 JSON 数组", payload["message"], raw)
            self.assertFalse(mouth_api.drops.path_of().exists(), "400 不该往弃言账本里写")

    def test_gate_unknown_inject_kind_is_400_with_library_wording(self):
        """kind 不认识：400，并且 message 里是 audit 的报错原文（不许 500 蒙混、不许当设定类）。"""
        status, payload = self._call(
            "GET", "/mouth/gate?text=" + GATE_PASS_TEXT + "&inject=" + _inj("胡写", SOUL_LINE))
        self.assertEqual(400, status)
        self.assertIn("胡写", payload["message"])
        self.assertIn("复述审计", payload["message"])
        self.assertIn("不认识", payload["message"])

    def test_gate_style_over_target_at_tier1_passes_at_tier4(self):
        """同一句 30 字：tier=1 超目标（改不了、放行并记账），tier=4 落在区间里 → pass。"""
        status, first = self._call(
            "GET", "/mouth/gate?text=" + OVER_TARGET_TEXT + "&tier=1&hard_cap=150")
        self.assertEqual(200, status)
        self.assertEqual({"decision", "target_lo", "target_hi", "chars", "rewrites",
                          "over_hard_cap", "reason"}, set(first["style"]))
        self.assertEqual("over_target", first["style"]["decision"])
        self.assertEqual(OVER_TARGET_TEXT, first["text"], "这一轮没有重写器：text 原样")
        self.assertEqual(0, first["style"]["rewrites"], "不许伪造一次改写")
        self.assertEqual((8, 24), (first["style"]["target_lo"], first["style"]["target_hi"]))
        self.assertFalse(first["style"]["over_hard_cap"])
        self.assertIn("改不了", first["style"]["reason"])
        self.assertIn("拒绝 → 闸", first["note"], "给了新参数时 note 要说清顺序")
        rows = mouth_api.style.path_of().read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(rows), "超目标要进长度账本，pass 那次不记")
        self.assertEqual("over_target", json.loads(rows[0])["decision"])

        status, fourth = self._call(
            "GET", "/mouth/gate?text=" + OVER_TARGET_TEXT + "&tier=4&hard_cap=150")
        self.assertEqual(200, status)
        self.assertEqual((24, 120), (fourth["style"]["target_lo"], fourth["style"]["target_hi"]))
        self.assertEqual("pass", fourth["style"]["decision"], "同一句在第 4 档的区间里")
        self.assertEqual(1, len(mouth_api.style.path_of().read_text(
            encoding="utf-8").splitlines()), "pass 不该往长度账本里添行")

    def test_gate_style_over_hard_cap_keeps_the_text(self):
        """80 字撞上核给的 60 字上限：over_hard_cap=true，但嘴不截断，text 原样交核。"""
        status, payload = self._call(
            "GET", "/mouth/gate?text=" + CAP_80_TEXT + "&tier=4&hard_cap=60")
        self.assertEqual(200, status)
        self.assertEqual(80, len(CAP_80_TEXT))
        self.assertTrue(payload["style"]["over_hard_cap"])
        self.assertEqual("over_target", payload["style"]["decision"])
        self.assertEqual(CAP_80_TEXT, payload["text"], "截断是核的事，嘴不截")
        self.assertEqual(80, payload["style"]["chars"])

    def test_gate_hard_cap_without_tier_is_400_and_tier_alone_is_allowed(self):
        """只给 hard_cap 不给 tier → 400（没档位就没有目标区间）；只给 tier 是允许的。"""
        status, payload = self._call("GET", "/mouth/gate?text=" + GATE_PASS_TEXT + "&hard_cap=60")
        self.assertEqual(400, status)
        self.assertIn("tier", payload["message"])
        self.assertIn("不许猜", payload["message"])
        self.assertNotIn("style", payload, "400 的那次不该带 style 那一节")
        status, payload = self._call("GET", "/mouth/gate?text=" + CAP_80_TEXT + "&tier=4")
        self.assertEqual(200, status)
        self.assertFalse(payload["style"]["over_hard_cap"],
                         "核没给上限就不许报超上限（不拿目标区间冒充上限）")
        self.assertEqual(CAP_80_TEXT, payload["text"])

    def test_new_gate_params_do_not_bypass_405_or_missing_text(self):
        """新参数不改变既有行为：非 GET 仍 405；没 refuse 又缺 text 仍 400。"""
        status, payload = self._call("POST", "/mouth/gate?refuse=topic&tier=1&hard_cap=150", {})
        self.assertEqual(405, status)
        self.assertIn("只读", payload["message"])
        for path in ("/mouth/gate?inject=" + _inj("魂", SOUL_LINE),
                     "/mouth/gate?tier=1&hard_cap=150",
                     "/mouth/gate?text=%20%20%20&tier=4"):
            status, payload = self._call("GET", path)
            self.assertEqual(400, status, path)
            self.assertIn("text", payload["message"], path)

    # —— 10. 未知端点 ————————————————————————————————

    def test_unknown_mouth_route_is_404(self):
        status, payload = self._call("GET", "/mouth/nope")
        self.assertEqual(404, status)
        self.assertFalse(payload["ok"])

    # —— 11. 只读自查 ————————————————————————————————

    def test_mouth_routes_are_all_get(self):
        self.assertEqual({"/mouth/split", "/mouth/address", "/mouth/tone", "/mouth/gate"},
                         set(mouth_api.MOUTH_ROUTES))
        self.assertEqual({"GET"}, set(mouth_api.MOUTH_ROUTES.values()))
        for route, method in mouth_api.MOUTH_ROUTES.items():
            self.assertEqual("GET", api_core.ROUTES[route], route)
            self.assertEqual(method, api_core.ROUTES[route], route)

    def test_mouth_module_has_no_write_paths(self):
        for banned in ("os.remove", "os.unlink", "unlink(", "write_text", "json.dump",
                       "open(", "shutil", "tempfile", ".mkdir", "rmtree"):
            self.assertNotIn(banned, SOURCE, banned)

    def test_mouth_gate_has_no_rewriter(self):
        """闸自己那份也核一遍：不给 rewrite 就是 dropped，api 层没有伪造改写的机会。"""
        clauses = json.loads(
            (Path(mouth_gate.__file__).resolve().parent / "data" / "clauses.json")
            .read_text(encoding="utf-8"))["clauses"]
        verdict = mouth_gate.enforce("您好，请问有什么可以帮您的吗？", clauses, None)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)


if __name__ == "__main__":
    unittest.main()
