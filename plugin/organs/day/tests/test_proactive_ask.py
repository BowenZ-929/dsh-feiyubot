"""交给核的那一层：提示词参数化、回执只认一段 JSON、失败按 kind 分岔。

一个真网络请求都不发：complete / transport / env 全注入。
"""
from __future__ import annotations

import json
import unittest

from organs.day import proactive_ask
from organs.mind import LLMError

CTX = {"who": "qq:3425635473", "because": "今天晚上她说想去看海", "tone": ""}


class PromptTest(unittest.TestCase):
    def test_persona_and_source_are_context_not_new_template_or_recipient(self):
        ctx = dict(CTX, persona='她是小岚', now='2026-11-05T10:36:00+09:00',
                   state={'mood': '平常', 'energy': '够用'}, reason_code='unfinished',
                   source={'who':'qq:200', 'session_id':'qq:group:70001'}, root='/private-test')
        messages = proactive_ask.build_messages(ctx, {'system':'S:{who}', 'user':'U:{because}'})
        self.assertIn('她是小岚', messages[0]['content'])
        self.assertIn('S:qq:3425635473', messages[0]['content'])
        self.assertIn('qq:group:70001', messages[1]['content'])
        self.assertIn('qq:200', messages[1]['content'])
        self.assertIn('U:今天晚上她说想去看海', messages[1]['content'])
        self.assertNotIn('/private-test', str(messages))

    def test_messages_carry_who_because_and_the_receipt_shape(self):
        messages = proactive_ask.build_messages(CTX)
        self.assertEqual(["system", "user"], [row["role"] for row in messages])
        self.assertIn("qq:3425635473", messages[0]["content"])
        self.assertIn("今天晚上她说想去看海", messages[1]["content"])
        self.assertIn("chunks", messages[1]["content"])

    def test_prompts_and_parsing_come_from_the_config(self):
        config = {"ask": {"system": "S:{who}", "user": "U:{because}",
                          "send_key": "说吗", "chunks_key": "段", "max_chunks": 2,
                          "chunk_join": " "}}
        messages = proactive_ask.build_messages(CTX, config)
        self.assertEqual("S:qq:3425635473", messages[0]["content"])
        self.assertEqual("U:今天晚上她说想去看海", messages[1]["content"])
        parsed = proactive_ask.parse('{"说吗": true, "段": ["一", "二", "三"]}', config)
        self.assertEqual(["一", "二"], parsed["chunks"], "max_chunks 也从配置来")
        self.assertEqual("一 二", parsed["text"], "连法也从配置来")


class ParseTest(unittest.TestCase):
    def test_plain_json(self):
        parsed = proactive_ask.parse('{"send": true, "chunks": ["一句", " "] }')
        self.assertTrue(parsed["send"])
        self.assertEqual(["一句"], parsed["chunks"])
        self.assertEqual("一句", parsed["text"])

    def test_fenced_json_is_unwrapped(self):
        fence = chr(96) * 3
        reply = chr(10).join(["好的：", json.dumps({"send": True, "chunks": ["嗯"]}), fence])
        parsed = proactive_ask.parse(fence + "json" + reply)
        self.assertEqual(["嗯"], parsed["chunks"])

    def test_send_false_is_honoured(self):
        parsed = proactive_ask.parse('{"send": false, "chunks": []}')
        self.assertFalse(parsed["send"])
        self.assertEqual("", parsed["text"])

    def test_a_bare_sentence_is_a_parse_error(self):
        for bad in ("我今晚不想说话", "", "{", '{"chunks": ["嗯"]}', '{"send": "false"}'):
            with self.assertRaises(proactive_ask.ParseError):
                proactive_ask.parse(bad)


class AskTest(unittest.TestCase):
    def test_success_returns_the_merged_text(self):
        seen = {}

        def complete(messages, **kwargs):
            seen["messages"] = messages
            seen["temperature"] = kwargs.get("temperature")
            return '{"send": true, "chunks": ["你还没睡吗", "我有点想你"]}'

        out = proactive_ask.ask(CTX, complete=complete,
                               config={"ask": {"temperature": 0.3}})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["send"])
        self.assertEqual(["你还没睡吗", "我有点想你"], out["chunks"])
        self.assertIn("你还没睡吗", out["text"])
        self.assertEqual(2, len(seen["messages"]), "system + user 两段")
        self.assertEqual(0.3, seen["temperature"], "温度从 proactive.json 的 ask 那一块来")

    def test_llm_error_keeps_its_kind_and_is_not_retried(self):
        calls = []

        def boom(messages, **kwargs):
            calls.append(1)
            raise LLMError("no-key", "没有可用的 key")

        out = proactive_ask.ask(CTX, complete=boom)
        self.assertFalse(out["ok"])
        self.assertEqual("no-key", out["error_kind"])
        self.assertEqual(1, len(calls), "主动开口不做退避重试")

    def test_other_exceptions_become_a_kind_too(self):
        def boom(messages, **kwargs):
            raise RuntimeError("线断了")

        out = proactive_ask.ask(CTX, complete=boom)
        self.assertEqual("exception", out["error_kind"])
        self.assertIn("线断了", out["error"])

    def test_unparseable_reply_is_parse_kind(self):
        out = proactive_ask.ask(CTX, complete=lambda messages, **kw: "我不知道")
        self.assertFalse(out["ok"])
        self.assertEqual("parse", out["error_kind"])
        self.assertEqual("我不知道", out["raw"], "回执原文留着，人看得出核说了什么")


if __name__ == "__main__":
    unittest.main()

