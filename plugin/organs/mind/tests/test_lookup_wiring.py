"""接线层用例：不联网、不读实盘、**一次真请求都没有**。

判据文件、家的房间、key 全由用例自己造：实盘那份判据天天变，用例不许依赖它；transport 一律
注入假的（真的那个会去 api.deepseek.com——本轮明令不许联网）。判据层的进程内状态（复用窗口、
失败抑制、每轮封顶）用 `search.reset_state()` 隔开，否则上一个用例会把下一个判成"刚查过"。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.hand.tests import helpers
from organs.home import knowledge
from organs.mind import lookup, search
from organs.hand import quota

WHO = "1000xxxx"
NOW = "2026-11-09T21:00:00+09:00"
#: 实盘 base_url 已经含 /v1：接线层只把它往下传，不许再拼一次。
BASE = "https://api.deepseek.com/v1"
CONF = {"base_url": BASE, "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 8000}
QUERY_TEXT = "帮我查今天 T1 赛程"
QUERY = "今天 T1 赛程"


def rules(**over):
    """一份完整的判据：键齐才过结构校验，用例只改自己关心的那一项。"""
    base = {
        "revision": 1,
        "updated": "2026-11-09",
        "enabled": True,
        "ask_markers": ["帮我查"],
        "challenge_markers": ["你这搜索不行"],
        "private_markers": ["生日"],
        "hidden_markers": ["还多久"],
        "volatile_markers": ["赛程"],
        "doubt_markers": ["确定吗"],
        "per_turn_limit": 1,
        "reuse_window_seconds": 300,
        "fail_suppress_seconds": 120,
        "query_max_chars": 128,
    }
    base.update(over)
    return base


def reply(*, status=200, body=None, raw=None):
    """假 transport：记下每次调用的四元组，回一段给定的 body。"""
    calls = []

    def send(url, headers, payload, timeout):
        calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        if raw is not None:
            return status, raw
        return status, json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")

    send.calls = calls
    return send


def one_result(**over):
    item = {"title": "半决赛对阵出炉", "url": "https://example.test/a", "snippet": "三号种子晋级",
            "summary": "官方赛程显示半决赛为 T1 对 GEN", "published_at": "2026-11-09",
            "site_name": "example.test", "authority": 0.9, "rerank_score": 0.4}
    item.update(over)
    return item


class WiringTest(unittest.TestCase):
    """编排层：门 → 手 → 家，三段的顺序与账。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)      # 手的实盘根
        self.home = helpers.make_home(self.root)           # 家的根（房表 + access.json）
        search.reset_state()
        self.addCleanup(search.reset_state)
        # 配置与 key 一律由用例给：不读实盘 .env、不读本机环境变量。
        self._patch(lookup.llm, "resolve_config", lambda config=None: dict(CONF))
        self._patch(lookup.llm, "read_key", lambda config, env=None: "test-key")
        self._patch(lookup.rice, "record_search", mock.Mock(return_value={}))

    def _patch(self, target, name, new):
        patcher = mock.patch.object(target, name, new)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_rules(self, **over):
        path = self.home / "仓库" / "判据" / "search_rules.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rules(**over), ensure_ascii=False), encoding="utf-8")
        return path

    def call(self, *, text=QUERY_TEXT, transport=None, turn_id="turn-1", session_id="s-1", **over):
        options = {"text": text, "who": WHO, "scene": "group", "session_id": session_id,
                   "turn_id": turn_id, "now": NOW, "root": self.root, "transport": transport,
                   "conf": dict(CONF), "intent": {"question": text.removeprefix("帮我查"),
                   "freshness": "volatile", "evidence": [text]}}
        options.update(over)
        return lookup.run(**options)

    def library_files(self):
        return list(knowledge.library_root(self.home).glob("*.json"))

    def test_cancelled_intent_never_sends_original_segment(self):
        self.write_rules()
        sent = reply(body={"results": [one_result()]})
        out = self.call(text="帮我查今天 T1 赛程\n不用查了", intent=None, transport=sent)
        self.assertEqual(0, len(sent.calls))
        self.assertEqual("not_requested", out["state"])

    def test_mixed_segment_sends_only_the_public_question(self):
        self.write_rules()
        sent = reply(body={"results": [one_result()]})
        self.call(text="我的生日是五月六日。今天T1赛程呢？", transport=sent,
                  intent={"question": "2026年11月9日 T1 赛程", "freshness": "volatile",
                          "evidence": ["今天T1赛程呢？"]})
        self.assertEqual(["2026年11月9日 T1 赛程"], [c["payload"]["query"] for c in sent.calls])

    def test_search_has_no_daily_hand_quota_and_records_cost_once(self):
        self.write_rules()
        helpers.write_limits(self.root, 0)
        sent = reply(body={"results": [one_result()]})
        out = self.call(transport=sent)
        self.assertEqual("saved", out["state"])
        self.assertEqual(0, quota.used_today(WHO, now=NOW, root=self.root))
        lookup.rice.record_search.assert_called_once()
        rows = helpers.ledger_lines(self.root)
        self.assertTrue(rows[-1]["params"]["landed"])
        self.assertEqual("turn-1", rows[-1]["params"]["turn_id"])

    def test_failed_save_does_not_claim_reusable_disk_result(self):
        self.write_rules()
        with mock.patch.object(knowledge, "save_web", side_effect=OSError("disk")):
            self.call(transport=reply(body={"results": [one_result()]}))
        sent = reply(body={"results": [one_result()]})
        second = self.call(transport=sent, turn_id="turn-2")
        self.assertNotEqual("reuse", second["action"])

    def test_reuse_is_exact_not_fuzzy_or_from_another_session(self):
        self.write_rules()
        first = self.call(transport=reply(body={"results": [one_result()]}))
        self.call(text="帮我查今天 T1 赛程结果", session_id="s-2", turn_id="other",
                  transport=reply(body={"results": [one_result(url="https://example.test/b")]}))
        second = self.call(transport=reply(body={"results": []}), turn_id="turn-2")
        self.assertEqual([i["card_id"] for i in first["items"]], [i["card_id"] for i in second["items"]])

    def test_question_needing_clarification_does_not_search(self):
        self.write_rules()
        sent = reply(body={"results": []})
        out = self.call(intent={"question": "", "freshness": "unknown", "evidence": [QUERY_TEXT]}, transport=sent)
        self.assertFalse(sent.calls)
        self.assertTrue(out["say"])

    def test_challenged_cached_result_is_searched_again(self):
        self.write_rules()
        self.call(transport=reply(body={"results": [one_result()]}))
        sent = reply(body={"results": [one_result()]})
        self.call(text="你这搜索不行，今天 T1 赛程再查一下", turn_id="turn-2", transport=sent,
                  intent={"question": QUERY, "freshness": "volatile", "evidence": ["今天 T1 赛程再查一下"]})
        self.assertEqual(1, len(sent.calls))

    def test_final_record_keeps_public_query_and_elapsed_time(self):
        self.write_rules()
        with mock.patch.object(lookup.time, "perf_counter", side_effect=[10.0, 10.5]):
            self.call(transport=reply(body={"results": [one_result()]}))
        row = helpers.ledger_lines(self.root)[-1]
        self.assertEqual(500, row["elapsed_ms"])
        self.assertEqual(QUERY, row["params"]["query"])
        self.assertTrue(row["params"]["query_sha256"])

    # ---- 门：不查的四种情形都是零请求 ------------------------------------

    def test_missing_rules_file_means_no_lookup_and_no_http(self):
        sent = reply(body={"results": [one_result()]})
        out = self.call(transport=sent)
        self.assertEqual("off", out["action"])
        self.assertIs(False, out["requested"])
        self.assertIsNone(out["say"], "关着的时候一句话都不加：这是「一键关掉」的那一档")
        self.assertEqual(0, len(sent.calls))
        self.assertEqual(0, helpers.ledger_lines(self.root)[-1]["params"]["calls"])

    def test_privacy_marker_blocks_the_query_and_sends_nothing(self):
        self.write_rules()
        sent = reply(body={"results": [one_result()]})
        out = self.call(text="帮我查我生日那天有什么比赛", transport=sent)
        self.assertEqual("deny", out["action"])
        self.assertEqual("privacy", out["gate"])
        self.assertEqual("这个我不往外问。", out["say"])
        self.assertEqual(0, len(sent.calls))
        self.assertNotIn("生日", out["reason"], "被拒的原文不许进账、也不许进 reason")

    def test_no_turn_id_never_falls_back_to_searching(self):
        self.write_rules()
        sent = reply(body={"results": [one_result()]})
        with mock.patch.object(search, "note_outcome") as note:
            out = self.call(transport=sent, turn_id="")
        self.assertEqual("deny", out["action"])
        self.assertEqual("turn", out["gate"])
        self.assertEqual(0, len(sent.calls))
        note.assert_not_called()

    def test_gate_three_says_it_cannot_look_not_that_it_does_not_know(self):
        self.write_rules()
        sent = reply(body={"results": [one_result()]})
        out = self.call(text="帮我查龙还多久刷新", transport=sent)
        self.assertEqual("deny", out["action"])
        self.assertEqual("verifiable", out["gate"])
        self.assertEqual("这个我这儿看不到。", out["say"])
        self.assertEqual(0, len(sent.calls))

    def test_memory_hit_stops_the_query_before_any_request(self):
        self.write_rules()
        sent = reply(body={"results": [one_result()]})
        out = self.call(transport=sent, memory_hit=True)
        self.assertEqual("deny", out["action"])
        self.assertEqual("memory", out["gate"])
        self.assertEqual(0, len(sent.calls))

    # ---- 查：恰好一次请求，落卡只经 save_web -----------------------------

    def test_search_sends_exactly_one_request_and_lands_the_card(self):
        self.write_rules()
        sent = reply(body={"results": [one_result()]})
        out = self.call(transport=sent)

        self.assertEqual("search", out["action"])
        self.assertIs(True, out["requested"])
        self.assertEqual("saved", out["state"])
        self.assertEqual(1, len(sent.calls), "每轮最多一次：不许重试")
        self.assertEqual(BASE + "/tools/search", sent.calls[0]["url"], "实盘 base_url 已含 /v1，不许拼成 /v1/v1")
        self.assertEqual(8.0, sent.calls[0]["timeout"], "超时取配置里的那个")
        self.assertEqual(QUERY, sent.calls[0]["payload"]["query"], "求助说法不跟着发出去")

        self.assertEqual(1, len(out["items"]))
        item = out["items"][0]
        self.assertEqual("web", item["kind"])
        self.assertEqual("https://example.test/a", item["url"])
        self.assertEqual("2026-11-09", item["published_at"])
        self.assertTrue(item["fetched_at"])

        files = self.library_files()
        self.assertEqual(1, len(files))
        job = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual("web", job["kind"])
        self.assertTrue(job["fetched_at"])
        self.assertEqual("https://example.test/a", job["source"]["url"])
        self.assertEqual(QUERY, job["source"]["query"])
        self.assertTrue(job["cards"][0]["id"])
        self.assertIn("半决赛", job["cards"][0]["claim"], "claim 用服务端 summary")

        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "done", "done"], [row["state"] for row in rows])
        self.assertEqual(1, rows[-1]["params"]["calls"])
        self.assertIs(True, rows[-1]["params"]["landed"])

    def test_cards_are_written_only_through_save_web(self):
        self.write_rules()
        sent = reply(body={"results": [one_result(), one_result(title="另一条", url="https://example.test/b")]})
        inner, written, inside = knowledge.save, [], {"on": False}
        real = knowledge.save_web

        def spy_save(job, root=None):
            written.append({"inside_save_web": inside["on"], "kind": job.get("kind")})
            return inner(job, root=root)

        def spy_save_web(job, root=None):
            inside["on"] = True
            try:
                return real(job, root=root)
            finally:
                inside["on"] = False

        with mock.patch.object(knowledge, "save", spy_save), \
                mock.patch.object(knowledge, "save_web", spy_save_web):
            out = self.call(transport=sent)

        self.assertEqual(2, len(written), "两条结果各落一份网页任务")
        self.assertTrue(all(row["inside_save_web"] for row in written),
                        "每一次写盘都必须落在 save_web 里：绕过它就没有 fetched_at 闸")
        self.assertEqual("saved", out["state"])

    def test_items_share_the_knowledge_search_view_keys(self):
        self.write_rules()
        self.call(transport=reply(body={"results": [one_result()]}))
        mine = self.call(text=QUERY_TEXT, session_id="s-2",
                         transport=reply(body={"results": [one_result()]}))["items"][0]
        found = knowledge.search(QUERY, root=self.home)
        self.assertTrue(found)
        self.assertEqual(set(found[0]), set(mine), "视图键同一套：下游并进 learned 不必写第二套分支")

    def test_the_wiring_source_never_calls_knowledge_save(self):
        source = Path(lookup.__file__).read_text(encoding="utf-8")
        self.assertNotIn("knowledge.save(", source, "落卡只许经 save_web")

    # ---- 没查到 / 查不了 / 存不上，三种说法分开 ---------------------------

    def test_empty_results_land_nothing_and_say_the_empty_line(self):
        self.write_rules()
        out = self.call(transport=reply(body={"results": []}))
        self.assertEqual("failed", out["state"])
        self.assertEqual([], out["items"])
        self.assertEqual("我没查到可靠的", out["say"])
        self.assertNotIn("没有这件事", out["say"])
        self.assertEqual([], self.library_files())

    def test_a_failed_request_lands_nothing_and_says_it_cannot_search(self):
        self.write_rules()
        out = self.call(transport=reply(status=503, body={}))
        self.assertEqual("failed", out["state"])
        self.assertEqual([], out["items"])
        self.assertIn("我现在查不了", out["say"])
        self.assertEqual([], self.library_files())

    def test_missing_credentials_send_nothing_at_all(self):
        self.write_rules()
        self._patch(lookup.llm, "resolve_config", lambda config=None: dict(CONF))
        self._patch(lookup.llm, "read_key", mock.Mock(side_effect=lookup.llm.LLMError("no-key", "还没配 key")))
        sent = reply(body={"results": [one_result()]})
        out = self.call(transport=sent)
        self.assertEqual(0, len(sent.calls), "没 key 不许发请求：发出去只会换一个 401")
        self.assertEqual("not_requested", out["state"], "一次都没发：账上不许记成「发了、失败了」")
        self.assertEqual([], out["items"])
        self.assertTrue(out["say"], "没通路要说得出「查不了」")

    def test_unreadable_config_sends_nothing_at_all(self):
        self.write_rules()
        self._patch(lookup.llm, "resolve_config",
                    mock.Mock(side_effect=lookup.llm.LLMError("config", "配置读不出来")))
        sent = reply(body={"results": [one_result()]})
        out = self.call(transport=sent)
        self.assertEqual(0, len(sent.calls))
        self.assertEqual([], out["items"])
        self.assertEqual("not_requested", out["state"])

    def test_a_failed_save_still_answers_from_memory_and_records_it(self):
        self.write_rules()
        sent = reply(body={"results": [one_result()]})
        with mock.patch.object(knowledge, "save_web",
                               mock.Mock(side_effect=ValueError("网页知识没有抓取时间，不入库"))):
            out = self.call(transport=sent)
        self.assertEqual("searched_not_saved", out["state"])
        self.assertEqual(1, len(out["items"]), "查到了就给人看：这一轮已经花了钱")
        self.assertIn("半决赛", out["items"][0]["card"]["claim"])
        self.assertIn("落卡失败", out["reason"])
        self.assertEqual([], self.library_files())
        self.assertEqual(1, len(sent.calls))

    # ---- 复用：读卡，不再买一次 ------------------------------------------

    def test_reuse_reads_the_saved_card_without_any_http(self):
        self.write_rules()
        first = self.call(transport=reply(body={"results": [one_result()]}))
        self.assertEqual("saved", first["state"])

        sent = reply(body={"results": [one_result()]})
        second = self.call(transport=sent)
        self.assertEqual("reuse", second["action"])
        self.assertEqual(0, len(sent.calls), "复用窗口内不许再发一次")
        self.assertEqual([QUERY], [row["query"] for row in second["items"]])
        self.assertEqual("web", second["items"][0]["kind"])

    def test_a_second_turn_in_the_same_turn_is_refused_by_the_turn_cap(self):
        self.write_rules()
        self.call(transport=reply(body={"results": [one_result()]}))
        sent = reply(body={"results": [one_result()]})
        # 换一段话让复用窗口不命中，剩下的封顶交给 turn_id（同一个 session、同一个回合）。
        out = self.call(text="帮我查今天 T1 赛程结果", session_id="s-1", transport=sent)
        self.assertEqual("deny", out["action"])
        self.assertEqual("throttle", out["gate"])
        self.assertEqual(0, len(sent.calls))

    # ---- 结果回给门 5 ----------------------------------------------------

    def test_note_outcome_records_success_once(self):
        self.write_rules()
        with mock.patch.object(search, "note_outcome",
                               wraps=search.note_outcome) as note:
            self.call(transport=reply(body={"results": [one_result()]}))
        self.assertEqual(1, note.call_count)
        self.assertEqual({"session_id": "s-1", "query": QUERY, "ok": True}, note.call_args.kwargs)

    def test_note_outcome_records_failure_once(self):
        self.write_rules()
        with mock.patch.object(search, "note_outcome",
                               wraps=search.note_outcome) as note:
            self.call(transport=reply(status=500, body={}))
        self.assertEqual(1, note.call_count)
        self.assertEqual({"session_id": "s-1", "query": QUERY, "ok": False}, note.call_args.kwargs)


class ConversationCapabilityTest(unittest.TestCase):
    def test_qq_capability_uses_receipt_without_claiming_search_is_unimplemented(self):
        from organs.mind import conversation
        from organs.mind.tests.test_note_display import NoteDisplayTest, CONFIG, ENV, _transport
        fixture = NoteDisplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        helpers.make_home(fixture.tmp/'home'/'hand')
        job = lookup._job('虚构赛程',one_result(summary='虚构赛程：下午三点'),NOW)
        item = knowledge._view(job,job['cards'][0],0,[])
        receipts = [
            lookup._result('search','ok','已查到',items=[item],requested=True,state='saved'),
            lookup._result('search','ok','保存失败',items=[item],requested=True,state='searched_not_saved'),
            lookup._result('reuse','reuse','同会话复用',items=[item]),
            lookup._result('search','ok','超时',say='这次没查成。',requested=True,state='failed'),
            lookup._result('off','config','开关关闭'),
            lookup._result('deny','intent','没有当前查询请求'),
        ]
        for receipt in receipts:
            with self.subTest(action=receipt['action'],state=receipt['state']):
                captured=[]
                with mock.patch.object(lookup,'run',return_value=receipt):
                    out=conversation.answer('这次能查到赛程吗？','100001',store=fixture.store,
                        brain=fixture.brain,scene='private',session_id='qq:private:100001',
                        config=CONFIG,env=ENV,transport=_transport('知道啦。',captured),now=NOW)
                prompt=captured[-1]['messages'][0]['content']
                self.assertNotIn('通用网页搜索、游戏数据库查询尚未接入',prompt)
                self.assertIn('以本轮联网检索回执为准',prompt)
                self.assertIn('reuse 是此前的查询，不要说刚重新搜过',prompt)
                self.assertEqual(receipt,out['lookup'])
                self.assertIn('"state": "'+receipt['state']+'"',prompt)
                self.assertIn('"requested": '+json.dumps(receipt['requested']),prompt)
                all_messages=json.dumps(captured[-1]['messages'],ensure_ascii=False)
                self.assertEqual(bool(receipt['items']),'虚构赛程：下午三点' in all_messages)


if __name__ == "__main__":
    unittest.main()
