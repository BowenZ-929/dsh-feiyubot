"""嘴的 HTTP 层：四条只读干跑端点、写方法一律 405、未接入如实报。

覆盖的是 api/mouth.py 与 api/core.py 的接线：路由表、返回形状、405/400/404、
brain=None / store=None、心的读数只从查询参数来、闸命中即弃、以及「没有写盘路径」这条自查。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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
