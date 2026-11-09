"""检索判据层的离线用例：不联网、不读实盘、不发真请求。

每个用例自己造一份临时家（`root=` 直接给，不走 HOME_ROOT）：实盘那份判据
会天天变，用例不许依赖它。判据内容也由用例写——词表在代码里没有默认值，
只有测试能替「人」把这份文件写出来。
"""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import organs.mind.search as search
import organs.soul as soul_organ
from organs.soul import bounds as soul_bounds
from organs.soul.store import load as load_soul

HERE = Path(search.__file__).resolve()
NOW = 1000.0
#: 魂的红线数据与正负样本：对拍只读它，**不进生产路径**（search.py 里一行 soul 都没有，
#: 由 SourceCase 看住）。魂是判据，执行器官留自己的出口副本，副本要与判据对得上。
SOUL_DATA = Path(soul_organ.__file__).resolve().parent / "data"
SOUL_SAMPLES = Path(soul_organ.__file__).resolve().parent / "fixtures" / "bounds_samples.jsonl"
LEAK_ITEM = "bounds.no-user-info-leak"


def rules(**over):
    """一份完整的判据：键齐才过结构校验，用例只改自己关心的那一项。"""
    base = {
        "revision": 3,
        "updated": "2026-11-09",
        "enabled": True,
        "ask_markers": ["帮我查", "到底", "是不是真的", "多少钱"],
        "challenge_markers": ["你这搜索不行", "记错", "查得不对"],
        "private_markers": ["生日", "真名", "住址"],
        "hidden_markers": ["还多久", "现在多少", "现在在哪", "在想什么"],
        "volatile_markers": ["赛程", "价格", "版本", "在售", "排行"],
        "doubt_markers": ["确定吗", "核实"],
        "per_turn_limit": 1,
        "reuse_window_seconds": 300,
        "fail_suppress_seconds": 120,
        "query_max_chars": 128,
    }
    base.update(over)
    return base


class HomeCase(unittest.TestCase):
    """临时家：判据写在 仓库/判据/search_rules.json，别的房间一个不建。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "仓库").mkdir()
        search.reset_state()

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, payload, *, text=None):
        folder = self.home / "仓库" / "判据"
        folder.mkdir(exist_ok=True)
        rules_file = folder / "search_rules.json"
        body = text if text is not None else json.dumps(payload, ensure_ascii=False)
        rules_file.write_text(body, encoding="utf-8")
        return rules_file

    def ask(self, text, **over):
        return search.decide(text=text, who="u1", scene="group", session_id="s1",
                             root=self.home, rules=rules(**over))

    def at(self, when):
        return mock.patch.object(search, "_now", return_value=when)

    def note(self, query, *, ok, when=NOW):
        """记账也得在同一只假钟上：真钟比假钟快，跨过去就把复用窗判成过期。"""
        with self.at(when):
            search.note_outcome(session_id="s1", query=query, ok=ok)


class OffCase(HomeCase):
    """没有判据、坏判据、关着开关，三种都必须是关闭，且都不许抛。"""

    def test_missing_file_is_off_and_creates_nothing(self):
        folder = self.home / "仓库" / "判据"
        self.assertIsNone(search.load(self.home))
        self.assertFalse(search.enabled(self.home))
        out = search.decide(text="帮我查今天 T1 赛程", who="u1", scene="group",
                            session_id="s1", root=self.home)
        self.assertEqual(out["action"], "off")
        self.assertEqual(out["gate"], "off")
        self.assertIsNone(out["query"])
        self.assertIn("还没落", out["reason"])
        self.assertFalse(folder.exists(), "判据目录不许被程序造出来")

    def test_broken_json_is_off(self):
        self.put(None, text='{"revision": 3,')
        out = search.decide(text="帮我查今天 T1 赛程", who="u1", scene="group",
                            session_id="s1", root=self.home)
        self.assertEqual((out["action"], out["gate"]), ("off", "off"))
        self.assertIn("不是 JSON", out["reason"])

    def test_wrong_shape_is_off(self):
        payload = rules()
        del payload["volatile_markers"]
        self.put(payload)
        out = search.decide(text="帮我查今天 T1 赛程", who="u1", scene="group",
                            session_id="s1", root=self.home)
        self.assertEqual(out["action"], "off")
        self.assertIn("volatile_markers", out["reason"])

    def test_top_level_list_is_off(self):
        self.put([1, 2, 3])
        self.assertIsNone(search.load(self.home))
        self.assertFalse(search.enabled(self.home))

    def test_switch_off_stops_everything(self):
        self.put(rules(enabled=False))
        self.assertFalse(search.enabled(self.home))
        out = search.decide(text="帮我查今天 T1 赛程", who="u1", scene="group",
                            session_id="s1", root=self.home)
        self.assertEqual((out["action"], out["gate"]), ("off", "off"))
        self.assertIn("开关", out["reason"])

    def test_home_root_failure_is_off_not_crash(self):
        with mock.patch.object(search.home_store, "home_root",
                               side_effect=search.home_store.StoreError("HOME_ROOT_EMPTY")):
            self.assertIsNone(search.load())
            self.assertFalse(search.enabled())
            out = search.decide(text="帮我查今天 T1 赛程", who="u1", scene="group",
                                session_id="s1")
        self.assertEqual(out["action"], "off")
        self.assertIn("家的根", out["reason"])


class GateCase(HomeCase):
    """六道门各一条，外加门的顺序。"""

    def test_gate0_privacy_by_number(self):
        out = search.decide(text="帮我查一下 13800001234 是谁的", who="u1", scene="group",
                            session_id="s1", root=self.home, rules=rules())
        self.assertEqual((out["action"], out["gate"]), ("deny", "privacy"))
        self.assertIsNone(out["query"])

    def test_gate0_privacy_by_marker(self):
        self.assertEqual(self.ask("他住址在哪儿")["gate"], "privacy")

    def test_order_privacy_beats_ask(self):
        out = self.ask("帮我查一下 13800001234 这个号是谁的")
        self.assertEqual(out["gate"], "privacy", "带号码的求助必须走门0，不许落到门1")

    def test_order_privacy_beats_challenge(self):
        out = self.ask("你把我生日记错了")
        self.assertEqual((out["action"], out["gate"]), ("deny", "privacy"),
                         "指认答错的例外不越过门0")

    def test_gate1_ask_passes(self):
        self.assertEqual(self.ask("帮我查今天 T1 赛程")["action"], "search")

    def test_gate1_pure_sigh_denied(self):
        out = self.ask("faker 没用啊")
        self.assertEqual((out["action"], out["gate"]), ("deny", "ask"))
        self.assertIsNone(out["query"])

    def test_gate1_challenge_passes(self):
        out = self.ask("你这搜索不行啊，现在版本多少号")
        self.assertEqual(out["action"], "search")
        self.assertEqual(out["gate"], "throttle", "门5 放行才对：裁决落在让它落地的那道门")

    def test_gate2_memory_hit_denied(self):
        out = search.decide(text="帮我查今天 T1 赛程", who="u1", scene="group",
                            session_id="s1", memory_hit=True, root=self.home, rules=rules())
        self.assertEqual((out["action"], out["gate"]), ("deny", "memory"))
        self.assertIsNone(out["query"])

    def test_gate3_hidden_denied_with_its_own_words(self):
        out = self.ask("帮我查这个龙还多久刷")
        self.assertEqual((out["action"], out["gate"]), ("deny", "verifiable"))
        self.assertIn("看不到", out["refuse_say"])
        self.assertNotIn("不知道", out["refuse_say"], "「看不到」不是「不知道」")
        self.assertIsNone(out["query"])

    def test_order_memory_before_verifiable(self):
        out = search.decide(text="龙还多久", who="u1", scene="group", session_id="s1",
                            memory_hit=True, root=self.home, rules=rules(
                                ask_markers=["龙还多久"]))
        self.assertEqual(out["gate"], "memory")

    def test_gate4_volatile_passes(self):
        self.assertEqual(self.ask("帮我查现在价格")["action"], "search")

    def test_gate4_stable_denied(self):
        out = self.ask("帮我查勾股定理")
        self.assertEqual((out["action"], out["gate"]), ("deny", "freshness"))

    def test_gate4_doubt_signal_passes(self):
        self.assertEqual(self.ask("帮我查勾股定理，确定吗")["action"], "search")


class QueryCase(HomeCase):
    """query 只在 search 时非空，长度与内容都有边界。"""

    def test_query_trims_ask_prefix_and_tail(self):
        out = self.ask("帮我查今天 T1 赛程？")
        self.assertEqual(out["query"], "今天 T1 赛程")

    def test_query_length_capped(self):
        out = self.ask("帮我查赛程" + "很长的补充" * 60)
        self.assertEqual(len(out["query"]), 128)

    def test_query_length_under_cap_kept(self):
        out = self.ask("帮我查赛程")
        self.assertEqual(out["query"], "赛程")

    def test_empty_query_denied(self):
        out = self.ask("帮我查赛程？？", ask_markers=["帮我查赛程"])
        self.assertEqual(out["action"], "deny")
        self.assertIsNone(out["query"])
        self.assertIn("太空", out["reason"])

    def test_query_never_carries_whitespace_runs(self):
        out = self.ask("帮我查   今天\t赛程")
        self.assertEqual(out["query"], "今天 赛程")


class ThrottleCase(HomeCase):
    """门5：复用、同轮第二次、失败抑制，三条都要在，且失败不是永久拉黑。"""

    def search_once(self, text="帮我查今天 T1 赛程", when=NOW, turn_id=None):
        with self.at(when):
            return search.decide(text=text, who="u1", scene="group", session_id="s1",
                                 root=self.home, rules=rules(), turn_id=turn_id)

    def test_reuse_within_window(self):
        self.assertEqual(self.search_once()["action"], "search")
        self.note("今天 T1 赛程", ok=True)
        out = self.search_once(when=NOW + 60)
        self.assertEqual((out["action"], out["gate"]), ("reuse", "throttle"))
        self.assertEqual(out["query"], "今天 T1 赛程", "复用要报出读哪一条的键")

    def test_reuse_expires_after_window(self):
        self.search_once()
        self.note("今天 T1 赛程", ok=True)
        self.assertEqual(self.search_once(when=NOW + 400)["action"], "search")

    def test_reuse_keyed_by_session(self):
        self.search_once()
        self.note("今天 T1 赛程", ok=True)
        with self.at(NOW + 10):
            other = search.decide(text="帮我查今天 T1 赛程", who="u2", scene="group",
                                  session_id="s2", root=self.home, rules=rules())
        self.assertEqual(other["action"], "search", "别的会话不许蹭这个复用键")

    def test_fail_suppressed_within_window(self):
        self.note("今天 T1 赛程", ok=False)
        out = self.search_once(when=NOW + 30)
        self.assertEqual((out["action"], out["gate"]), ("deny", "throttle"))
        self.assertIsNone(out["query"])

    def test_fail_suppression_is_not_a_permanent_blacklist(self):
        self.note("今天 T1 赛程", ok=False)
        self.assertEqual(self.search_once(when=NOW + 200)["action"], "search")

    def test_second_request_same_turn_denied(self):
        with self.at(NOW):
            first = search.decide(text="帮我查今天 T1 赛程", who="u1", scene="group",
                                  session_id="s1", root=self.home, rules=rules(),
                                  turn_id="t-1")
            second = search.decide(text="帮我查现在价格", who="u1", scene="group",
                                   session_id="s1", root=self.home, rules=rules(),
                                   turn_id="t-1")
        self.assertEqual(first["action"], "search")
        self.assertEqual((second["action"], second["gate"]), ("deny", "throttle"))
        self.assertIn("这一轮", second["reason"])

    def test_new_turn_allowed_again(self):
        with self.at(NOW):
            search.decide(text="帮我查今天 T1 赛程", who="u1", scene="group",
                          session_id="s1", root=self.home, rules=rules(), turn_id="t-1")
            nxt = search.decide(text="帮我查现在价格", who="u1", scene="group",
                                session_id="s1", root=self.home, rules=rules(),
                                turn_id="t-2")
        self.assertEqual(nxt["action"], "search")

    def test_state_lives_only_in_memory(self):
        self.search_once()
        self.note("今天 T1 赛程", ok=True)
        self.assertEqual(self.search_once(when=NOW + 10)["action"], "reuse")
        search.reset_state()
        self.assertEqual(self.search_once(when=NOW + 20)["action"], "search",
                         "状态一清就重来：它不在任何文件里")


class PrivacyWordsCase(HomeCase):
    """reason 与 refuse_say 都不许把被拒原文抄进去。"""

    SECRET = "13800001234"

    def test_reason_and_refusal_carry_no_original_text(self):
        out = self.ask("帮我查 %s 这个号是谁的，他住址在哪" % self.SECRET)
        self.assertEqual(out["gate"], "privacy")
        for field in ("reason", "refuse_say"):
            self.assertNotIn(self.SECRET, out[field])
        self.assertNotIn(self.SECRET, json.dumps(out, ensure_ascii=False))

    def test_every_denial_reason_is_plain(self):
        cases = ["faker 没用啊", "帮我查龙还多久", "帮我查勾股定理", "帮我查今天 T1 赛程"]
        for text in cases:
            out = self.ask(text)
            self.assertNotIn("勾股定理", out["reason"])
            self.assertNotIn("faker", out["reason"].lower())
            self.assertLess(len(out["reason"]), 60)


class LeakShapeCase(HomeCase):
    """门0 的形状：拦号码与身份证，不拦价格、番号、日期、观看数、版本号。

    宽松的代价是「她连价格都不查」，而账上只留一个 privacy——挡与没挡分不出来，
    排查成本比误挡本身还高。
    """

    ORDINARY = ("12999", "2026-11-09", "12345", "128726 次观看", "3:2", "v4.1")
    PRIVATE = ("13812345678", "440101199001011234", "a.b@c.com")

    def test_ordinary_numbers_are_not_private(self):
        for sample in self.ORDINARY:
            out = self.ask("帮我查价格 %s" % sample)
            self.assertNotEqual(out["gate"], "privacy", "%s 被门0 误伤了" % sample)
            self.assertEqual(out["action"], "search", sample)

    def test_phone_id_and_email_are_private(self):
        for sample in self.PRIVATE:
            out = self.ask("帮我查 %s 是谁的" % sample)
            self.assertEqual((out["action"], out["gate"]), ("deny", "privacy"), sample)
            self.assertNotIn(sample, out["reason"])

    def test_shapes_stand_without_any_words(self):
        """词表是空的也得挡住号码与身份证：形状不靠人写对判据文件才生效。"""
        for sample in ("13812345678", "440101199001011234"):
            self.assertTrue(search._private(sample.lower(), []), sample)


class SoulParityCase(unittest.TestCase):
    """对拍：门0 的判定不比魂 `bounds.no-user-info-leak` 更松（更严允许，更松就红）。

    与魂对齐的那一半是**形状**；真名／住址／私聊片段本来就住在判据文件里，
    所以对拍时用一份含那几个词的词表，模拟人已经写好判据的样子。
    """

    WORDS = ["住址", "真名", "私聊记录", "QQ号", "手机号", "身份证"]

    def setUp(self):
        self.items = [item for item in load_soul(SOUL_DATA).snapshot().bounds.items
                      if item.id == LEAK_ITEM]
        self.assertTrue(self.items, "魂里没有这条判据了，对拍失去意义")

    def soul_hits(self, text):
        return bool(soul_bounds.match(self.items, text))

    def mine(self, text):
        return search._private(search._flat(text).lower(), self.WORDS)

    def samples(self):
        texts = []
        for line in SOUL_SAMPLES.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["item"] == LEAK_ITEM:
                texts.append(row["text"])
        return texts + list(LeakShapeCase.PRIVATE) + list(LeakShapeCase.ORDINARY)

    def test_never_looser_than_soul(self):
        for text in self.samples():
            if self.soul_hits(text):
                self.assertTrue(self.mine(text), "魂说这是泄露，门0 却放行：%s" % text)

    def test_no_overblocking_where_soul_is_silent(self):
        """反过来也钉一下：魂说不是泄露的，我不许判成泄露（否则她又什么都查不了）。"""
        for text in self.samples():
            if not self.soul_hits(text) and text != "a.b@c.com":
                self.assertFalse(self.mine(text), "魂说不是泄露，门0 却挡住：%s" % text)

    def test_email_is_the_one_place_we_are_stricter(self):
        self.assertFalse(self.soul_hits("a.b@c.com"), "魂那条没有邮箱形状")
        self.assertTrue(self.mine("a.b@c.com"), "门0 比魂多挡一个，方向允许")


class ReadOnlyCase(HomeCase):
    """判据文件只读：读、判、记账都不许动它，也不许造出新文件。"""

    def snapshot(self):
        return sorted((str(item.relative_to(self.home)), item.stat().st_size,
                       item.stat().st_mtime_ns)
                      for item in self.home.rglob("*"))

    def test_rules_file_untouched(self):
        rules_file = self.put(rules())
        before = self.snapshot()
        content = rules_file.read_text(encoding="utf-8")
        search.load(self.home)
        search.enabled(self.home)
        self.ask("帮我查今天 T1 赛程")
        self.ask("他住址在哪儿")
        self.note("今天 T1 赛程", ok=True)
        self.ask("帮我查今天 T1 赛程", memory_hit=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(rules_file.read_text(encoding="utf-8"), content)

    def test_no_new_file_in_home(self):
        self.put(rules())
        before = self.snapshot()
        for text in ("帮我查今天 T1 赛程", "faker 没用啊", "龙还多久"):
            self.ask(text)
        self.assertEqual(self.snapshot(), before)


class SourceCase(unittest.TestCase):
    """源码断言：判据层没有写文件的调用，也不认得网络与别的器官。"""

    WRITE_CALLS = frozenset({"write_text", "write_bytes", "write", "mkdir", "makedirs",
                             "unlink", "rename", "replace", "touch", "copy", "copy2",
                             "copyfile", "symlink_to", "chmod", "open"})
    WRITE_MODES = frozenset({"w", "a", "x", "w+", "a+", "x+", "r+", "wb", "ab", "xb",
                             "wb+", "ab+", "xb+", "wt", "at"})
    NETWORK = ("urllib", "socket", "http", "requests", "httpx")
    FORBIDDEN_ORGANS = ("hand", "conversation", "appraisal", "soul")

    def tree(self):
        return ast.parse(HERE.read_text(encoding="utf-8"))

    def test_no_write_calls(self):
        for node in ast.walk(self.tree()):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            self.assertNotIn(name, self.WRITE_CALLS, "判据层不许碰盘：%s" % name)
            for keyword in node.keywords:
                if keyword.arg == "mode":
                    value = getattr(keyword.value, "value", None)
                    self.assertNotIn(value, self.WRITE_MODES)

    def test_no_network_and_no_other_organs(self):
        source = HERE.read_text(encoding="utf-8").lower()
        for token in self.NETWORK:
            self.assertNotIn(token, source, "判据层不许联网：%s" % token)
        imported = set()
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
                imported.add(node.module or "")
        for name in imported:
            for organ in self.FORBIDDEN_ORGANS:
                self.assertNotIn(organ, name, "判据层不许认识 %s" % organ)

    def test_only_reads_the_rules_file(self):
        source = HERE.read_text(encoding="utf-8")
        self.assertIn("read_text", source)
        self.assertIn("search_rules.json", source)


if __name__ == "__main__":
    unittest.main()
