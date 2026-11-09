"""思考参数必须进入实际 HTTP 请求，推理与最终回复分开保留。"""
import json
from unittest import mock

from organs.mind import llm
from .test_llm import LLMTest, FakeTransport, MESSAGES, conf, ok


class ThinkingTest(LLMTest):
    def test_high_thinking_reaches_transport(self):
        transport = FakeTransport(ok("323"))
        result = llm.complete(
            MESSAGES, config=conf(model="deepseek-flash", thinking={"type": "enabled"},
                                  reasoning_effort="high", max_tokens=8192),
            env={"QQAGENT_LLM_KEY": "test-key"}, transport=transport)
        sent = transport.calls[0]["payload"]
        self.assertEqual(sent["thinking"], {"type": "enabled"})
        self.assertEqual(sent["reasoning_effort"], "high")
        self.assertEqual(sent["max_tokens"], 8192)
        self.assertEqual(result, "323")

    def test_bad_thinking_configuration_is_rejected_before_transport(self):
        for fields in ({"thinking": {"type": "yes"}}, {"thinking": True},
                       {"reasoning_effort": "hihg"}):
            with self.subTest(fields=fields), self.assertRaises(llm.LLMConfigError):
                llm.resolve_config(conf(**fields))

    def test_trace_preserves_long_reasoning_but_reply_is_only_content(self):
        reasoning = "推理测试" * 6000
        body = json.dumps({"model": "deepseek-flash", "choices": [{
            "message": {"content": "323", "reasoning_content": reasoning},
            "finish_reason": "stop"}], "usage": {"total_tokens": 42}}, ensure_ascii=False)
        with mock.patch.object(llm, "_trace_dir", return_value=self.tmp / "trace"):
            reply = llm.complete(
                MESSAGES, config=conf(model="deepseek-flash", thinking={"type": "enabled"},
                                      reasoning_effort="high"),
                env={"QQAGENT_LLM_KEY": "test-key"},
                transport=FakeTransport((200, body.encode())))
        row = json.loads(next((self.tmp / "trace").glob("*.jsonl")).read_text())
        self.assertEqual(json.loads(row["raw"])["choices"][0]["message"]["reasoning_content"], reasoning)
        self.assertEqual(row["request"]["reasoning_effort"], "high")
        self.assertNotIn("test-key", json.dumps(row))
        self.assertEqual(reply, "323")
