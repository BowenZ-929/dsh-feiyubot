"""查：端点拼接、请求体的字段白名单、六类失败的分类，以及「没发请求」与「发了没落账」分开。

全部离线：transport 一律注入假的，**没有任何一条用例会碰真的 api.deepseek.com**（不联网、
不用真 key）。实盘 base_url 是 https://api.deepseek.com/v1，所以「不许拼成 /v1/v1」是这里
最要紧的一条断言——拼错了只会换一个 404，而 404 在账上很像「对面挂了」。
"""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
import urllib.error
from unittest import mock

from organs.hand import actions, ledger, lookup, whitelist

from . import helpers

WHO = "1000xxxx"
BASE = "https://api.deepseek.com/v1"
KEY = "test-key"
QUERY = "今天 T1 赛程是哪几场"


def reply(*, status: int = 200, body=None, raw: bytes = None):
    """假 transport：记下每次调用的四元组，回一段给定的 body。"""
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        if raw is not None:
            return status, raw
        return status, json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")

    transport.calls = calls
    return transport


def explode(error: BaseException):
    """假 transport：这次「没有回答」，抛给定的异常。"""
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append({"url": url, "payload": payload})
        raise error

    transport.calls = calls
    return transport


def one_result(**over):
    """服务端口径的一条结果（多给的键不许被照抄进规范化结果）。"""
    item = {"title": "半决赛对阵出炉", "url": "https://example.test/a", "snippet": "摘要",
            "summary": "长一点的说法", "published_at": "2026-11-08", "site_name": "example.test",
            "authority": 0.87, "rerank_score": 0.42}
    item.update(over)
    return item


class SearchTest(unittest.TestCase):
    def test_official_date_and_base_without_version(self):
        sent = reply(body={"results": [one_result(date="2026-11-09T00:00:00Z")]})
        got = lookup.search(QUERY, base_url="https://api.deepseek.com", key=KEY, transport=sent)
        self.assertEqual("https://api.deepseek.com/v1/tools/search", sent.calls[0]["url"])
        self.assertEqual("2026-11-09T00:00:00Z", got["results"][0]["published_at"])

    """客户端那一层：发什么、怎么拼、回来的东西怎么折。"""

    def test_ok_normalises_every_result_to_the_eight_frozen_fields(self):
        sent = reply(body={"results": [one_result(extra="不认识这一格")]})
        got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5, transport=sent)
        self.assertEqual("ok", got["state"])
        self.assertEqual(1, got["calls"])
        self.assertIsNone(got["error"])
        self.assertEqual(200, got["status"])
        self.assertEqual(1, len(got["results"]))
        self.assertEqual(set(lookup.RESULT_FIELDS), set(got["results"][0]))
        self.assertEqual("https://example.test/a", got["results"][0]["url"])
        self.assertEqual(0.42, got["results"][0]["rerank_score"])
        self.assertEqual(1, len(sent.calls), "只许发一次：不重试")
        self.assertGreater(sent.calls[0]["timeout"], 0)

    def test_request_body_carries_only_the_three_agreed_fields(self):
        sent = reply(body={"results": [one_result()]})
        lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5, transport=sent)
        payload = sent.calls[0]["payload"]
        self.assertEqual({"query", "count", "need_content"}, set(payload))
        self.assertEqual(QUERY, payload["query"])
        self.assertEqual(5, payload["count"])
        self.assertIs(False, payload["need_content"])

    def test_endpoint_is_base_plus_suffix_and_never_doubles_v1(self):
        for base in (BASE, BASE + "/", BASE + "///"):
            sent = reply(body={"results": []})
            lookup.search(QUERY, base_url=base, key=KEY, timeout=5, count=3, transport=sent)
            url = sent.calls[0]["url"]
            self.assertEqual("https://api.deepseek.com/v1/tools/search", url)
            self.assertNotIn("/v1/v1", url)
            self.assertEqual(3, sent.calls[0]["payload"]["count"])

    def test_unit_test_endpoint_matches_the_shipped_base_url(self):
        """实盘配置里那个 base_url 已经含 /v1；这一条把「多拼一次」钉死在本地。"""
        self.assertEqual("https://api.deepseek.com/v1/tools/search",
                         lookup.endpoint_of("https://api.deepseek.com/v1"))

    def test_authorization_header_is_the_only_secret_sent(self):
        sent = reply(body={"results": [one_result()]})
        lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5, transport=sent)
        headers = sent.calls[0]["headers"]
        self.assertEqual("Bearer " + KEY, headers["authorization"])
        self.assertEqual({"content-type", "accept", "authorization"}, set(headers))

    def test_http_200_with_no_results_is_empty_not_ok(self):
        sent = reply(body={"results": []})
        got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5, transport=sent)
        self.assertEqual("empty", got["state"])
        self.assertIsNone(got["error"])
        self.assertEqual([], got["results"])
        self.assertEqual(1, got["calls"], "空结果也是一次真的请求")

    def test_auth_failures_are_one_class_and_they_are_not_success(self):
        for status in (401, 402, 403):
            with self.subTest(status=status):
                got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5,
                                    transport=reply(status=status, body={}))
                self.assertEqual("error", got["state"])
                self.assertEqual("auth", got["error"])
                self.assertEqual(1, got["calls"])
                self.assertEqual(status, got["status"])

    def test_429_is_rate_limited(self):
        got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5,
                            transport=reply(status=429, body={}))
        self.assertEqual("rate_limited", got["error"])

    def test_5xx_is_server_error(self):
        for status in (500, 503):
            with self.subTest(status=status):
                got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5,
                                    transport=reply(status=status, body={}))
                self.assertEqual("server_error", got["error"])

    def test_timeout_is_its_own_class(self):
        for error in (socket.timeout("timed out"), TimeoutError("timed out"),
                      urllib.error.URLError(socket.timeout("timed out"))):
            with self.subTest(error=type(error).__name__):
                got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=0.01,
                                    transport=explode(error))
                self.assertEqual("timeout", got["error"])
                self.assertEqual(1, got["calls"])

    def test_dns_or_refused_connection_is_unreachable_not_timeout(self):
        got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5,
                            transport=explode(urllib.error.URLError(socket.gaierror("no dns"))))
        self.assertEqual("unreachable", got["error"])
        got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5,
                            transport=explode(urllib.error.URLError("refused")))
        self.assertEqual("unreachable", got["error"])

    def test_non_json_body_is_bad_response(self):
        got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5,
                            transport=reply(raw=b"<html>502</html>"))
        self.assertEqual("error", got["state"])
        self.assertEqual("bad_response", got["error"])

    def test_wrong_structure_is_bad_response_not_silent_empty(self):
        for body in ({"results": {}}, {"results": ["不是对象"]}, ["不是对象"]):
            with self.subTest(body=body):
                got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5,
                                    transport=reply(body=body))
                self.assertEqual("bad_response", got["error"])

    def test_missing_fields_stay_none_instead_of_a_made_up_default(self):
        got = lookup.search(QUERY, base_url=BASE, key=KEY, timeout=5,
                            transport=reply(body={"results": [{"title": "只有标题"}]}))
        item = got["results"][0]
        self.assertEqual("只有标题", item["title"])
        self.assertIsNone(item["url"])
        self.assertIsNone(item["published_at"])

    def test_over_long_query_is_refused_locally_without_any_request(self):
        sent = reply(body={"results": [one_result()]})
        got = lookup.search("字" * 129, base_url=BASE, key=KEY, timeout=5, transport=sent)
        self.assertEqual("error", got["state"])
        self.assertEqual("query_too_long", got["error"])
        self.assertEqual(0, got["calls"])
        self.assertEqual([], sent.calls, "本地就拒：一个字都不许发出去")

    def test_128_chars_is_still_allowed(self):
        sent = reply(body={"results": [one_result()]})
        got = lookup.search("字" * 128, base_url=BASE, key=KEY, timeout=5, transport=sent)
        self.assertEqual("ok", got["state"])
        self.assertEqual(1, len(sent.calls))

    def test_blank_query_is_refused_locally(self):
        for blank in ("", "   ", "\n\t", None):
            with self.subTest(blank=blank):
                sent = reply(body={"results": [one_result()]})
                got = lookup.search(blank, base_url=BASE, key=KEY, timeout=5, transport=sent)
                self.assertEqual("query_blank", got["error"])
                self.assertEqual(0, got["calls"])
                self.assertEqual([], sent.calls)

    def test_missing_endpoint_or_key_sends_nothing(self):
        for kwargs, kind in (({"base_url": ""}, "no_endpoint"),
                             ({"base_url": "   "}, "no_endpoint"),
                             ({"key": ""}, "no_key"),
                             ({"key": None}, "no_key")):
            with self.subTest(kind=kind):
                sent = reply(body={"results": [one_result()]})
                got = lookup.search(QUERY, base_url=kwargs.get("base_url", BASE),
                                    key=kwargs.get("key", KEY), timeout=5, transport=sent)
                self.assertEqual(kind, got["error"])
                self.assertEqual(0, got["calls"])
                self.assertEqual([], sent.calls)

    def test_fingerprint_carries_no_original_text(self):
        mark = lookup.fingerprint(QUERY)
        self.assertEqual(["query_chars", "query_sha256"], sorted(mark))
        self.assertEqual(len(QUERY), mark["query_chars"])
        self.assertNotIn(QUERY, json.dumps(mark, ensure_ascii=False))


class LookupActionTest(unittest.TestCase):
    """动作层：三层闸 → 先落 reserved 再动手 → 结果行写不进就改判 failed。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)

    def call(self, *, query=QUERY, transport=None, count=5, **over):
        options = {"who": WHO, "query": query, "count": count, "base_url": BASE, "key": KEY,
                   "timeout": 5, "now": helpers.NOW, "root": self.root,
                   "transport": transport}
        options.update(over)
        return actions.lookup(**options)

    def raw_ledger(self) -> str:
        return helpers.ledger_path(self.root).read_text(encoding="utf-8")

    def test_success_is_a_receipt_plus_two_ledger_lines(self):
        sent = reply(body={"results": [one_result(), one_result(title="另一条")]})
        out = self.call(transport=sent)
        self.assertEqual("done", out["state"])
        self.assertEqual("OK", out["code"])
        self.assertEqual("lookup", out["action"])
        self.assertEqual({"state", "action", "code", "say", "who", "card_id", "detail",
                          "ledger_id"}, set(out))
        self.assertEqual(2, out["detail"]["results_count"])
        self.assertEqual(1, out["detail"]["calls"])
        self.assertIs(False, out["detail"]["landed"], "本批不落卡：账上不许假装存过")
        self.assertEqual(200, out["detail"]["http"])

        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "done"], [row["state"] for row in rows])
        self.assertEqual(rows[0]["ledger_id"], rows[1]["ledger_id"])
        self.assertEqual("lookup", rows[1]["action"])
        self.assertEqual(1, rows[1]["params"]["calls"])
        self.assertEqual(2, rows[1]["params"]["results_count"])
        self.assertEqual(5, rows[1]["params"]["count"])

    def test_the_original_query_never_reaches_the_ledger(self):
        self.call(transport=reply(body={"results": [one_result()]}))
        text = self.raw_ledger()
        self.assertNotIn(QUERY, text, "账里不许出现 query 原文：门0 还没实现，账更不能照抄")
        row = helpers.ledger_lines(self.root)[-1]
        self.assertEqual(len(QUERY), row["params"]["query_chars"])
        self.assertEqual(lookup.fingerprint(QUERY)["query_sha256"], row["params"]["query_sha256"])

    def test_empty_results_are_a_finished_action_with_its_own_code(self):
        out = self.call(transport=reply(body={"results": []}))
        self.assertEqual("done", out["state"])
        self.assertEqual("LOOKUP_EMPTY", out["code"])
        self.assertEqual("我没查到可靠的", out["say"])
        self.assertNotIn("没有这件事", out["say"])
        self.assertEqual(["reserved", "done"], [r["state"] for r in helpers.ledger_lines(self.root)])
        self.assertEqual(0, helpers.ledger_lines(self.root)[-1]["params"]["results_count"])
        self.assertEqual(1, helpers.ledger_lines(self.root)[-1]["params"]["calls"])

    def test_auth_failure_is_classified_and_proves_a_request_went_out(self):
        out = self.call(transport=reply(status=401, body={}))
        self.assertEqual("failed", out["state"])
        self.assertEqual("LOOKUP_AUTH", out["code"])
        self.assertIn("我现在查不了", out["say"])
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "failed"], [row["state"] for row in rows])
        self.assertEqual("auth", rows[-1]["origin"])
        self.assertEqual(1, rows[-1]["params"]["calls"], "发了、失败了：账上要看得出来")

    def test_every_remote_class_has_its_own_code(self):
        cases = ((429, "LOOKUP_RATE_LIMITED"), (503, "LOOKUP_SERVER_ERROR"))
        for status, code in cases:
            with self.subTest(status=status):
                out = self.call(transport=reply(status=status, body={}))
                self.assertEqual("failed", out["state"])
                self.assertEqual(code, out["code"])

    def test_timeout_and_bad_response_are_distinguishable_on_the_account(self):
        slow = self.call(transport=explode(socket.timeout("timed out")))
        broken = self.call(transport=reply(raw=b"not json"))
        self.assertEqual("LOOKUP_TIMEOUT", slow["code"])
        self.assertEqual("LOOKUP_BAD_RESPONSE", broken["code"])
        origins = [row["origin"] for row in helpers.ledger_lines(self.root)
                   if row["state"] == "failed"]
        self.assertEqual(["timeout", "bad_response"], origins,
                         "两类失败在账上要分得开：一个是对面没回，一个是回的不是东西")

    def test_unconfigured_endpoint_fails_without_a_request(self):
        sent = reply(body={"results": [one_result()]})
        out = self.call(base_url="", transport=sent)
        self.assertEqual("failed", out["state"])
        self.assertEqual("LOOKUP_UNAVAILABLE", out["code"])
        self.assertEqual([], sent.calls)
        self.assertEqual(0, out["detail"]["calls"])
        self.assertEqual(0, helpers.ledger_lines(self.root)[-1]["params"]["calls"])

    def test_over_long_query_is_refused_and_nothing_is_sent(self):
        sent = reply(body={"results": [one_result()]})
        out = self.call(query="字" * 129, transport=sent)
        self.assertEqual("refused", out["state"])
        self.assertEqual("QUERY_TOO_LONG", out["code"])
        self.assertIn("128", out["say"])
        self.assertEqual([], sent.calls, "本地拒：一次请求都不许发")
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "refused"], [row["state"] for row in rows])
        self.assertEqual(0, rows[-1]["params"]["calls"], "「没发请求」必须写在账上")

    def test_blank_query_is_refused_not_failed(self):
        out = self.call(query="   ", transport=reply(body={"results": []}))
        self.assertEqual("refused", out["state"])
        self.assertEqual("MISSING_QUERY", out["code"])
        self.assertIn("？", out["say"])

    def test_lookup_does_not_use_daily_write_quota(self):
        helpers.write_limits(self.root, 0)
        sent = reply(body={"results": [one_result()]})
        out = self.call(transport=sent)
        self.assertEqual("done", out["state"])
        self.assertEqual(1, len(sent.calls))

    def test_unwritable_result_row_turns_a_real_search_into_a_failure(self):
        """查到了但账没记上：可能已经计费，所以人话要说清，绝不说是成功。"""
        real = ledger.append
        calls = {"count": 0}

        def flaky(entry, *, root=None):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise OSError("盘满了（测试注入）")
            return real(entry, root=root)

        with mock.patch.object(ledger, "append", flaky):
            out = self.call(transport=reply(body={"results": [one_result()]}))
        self.assertEqual("failed", out["state"])
        self.assertEqual("LEDGER_WRITE_FAILED", out["code"])
        self.assertEqual("result", out["detail"]["stage"])
        self.assertIn("计费", out["say"], "已经发出去的要说清可能花过钱")
        self.assertEqual(1, out["detail"]["calls"])
        self.assertEqual(1, out["detail"]["results_count"])

    def test_unwritable_reserved_row_sends_no_request(self):
        def boom(entry, *, root=None):
            raise OSError("盘满了（测试注入）")

        sent = reply(body={"results": [one_result()]})
        with mock.patch.object(ledger, "append", boom):
            out = self.call(transport=sent)
        self.assertEqual("failed", out["state"])
        self.assertEqual("LEDGER_WRITE_FAILED", out["code"])
        self.assertEqual("reserved", out["detail"]["stage"])
        self.assertEqual([], sent.calls, "账落不下去就不动手：一次请求都不发")
        self.assertIn("没动手", out["say"])

    def test_lookup_is_on_the_whitelist_and_costs_no_grant(self):
        self.assertIn("lookup", whitelist.ACTIONS)
        from organs.hand import permit
        self.assertFalse(permit.needs_grant("lookup"))


if __name__ == "__main__":
    unittest.main()
