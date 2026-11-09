"""假平台/假模型测试完整学习闭环；凭据与引用边界不靠模型自觉。"""
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from api import api
from organs.eye import bilibili
from organs.home import knowledge
from organs.mind import learning

BV = "BV1tRxt68EZa"
SOURCE = {"meta": {"bvid": BV, "page": 2, "title": "四个子空间", "owner": "示例作者",
                   "url": "https://www.bilibili.com/video/"+BV+"?p=2", "auto_subtitle": True},
          "lines": [{"t": 53.2, "end": 56, "text": "矩阵的行空间由矩阵的行向量张成"}], "status": "read", "note": "字幕资料"}
CARD = {"title": "行空间", "claim": "行空间由行向量张成", "tags": ["行空间", "矩阵"],
        "evidence": [{"t": 53.2, "quote": "行空间由矩阵的行向量张成"}]}


class LearningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"HOME_ROOT": str(self.root), "QQAGENT_HOME": str(self.root / "organs")})
        patch.start(); self.addCleanup(patch.stop)

    def test_reader_uses_selected_part_and_never_sends_cookie_to_subtitle_host(self):
        calls = []
        def fetch(url, cookie):
            calls.append((url, cookie))
            if "/nav" in url:
                return {"code": -101, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/"+"a"*32+".png", "sub_url": "https://i0.hdslb.com/"+"b"*32+".png"}}}
            if "/view?" in url:
                return {"code": 0, "data": {"title": "示例", "pages": [{"page": 1, "cid": 111}, {"page": 2, "cid": 222}]}}
            if "/v2?" in url:
                self.assertIn("cid=222", url)
                return {"code": 0, "data": {"subtitle": {"subtitles": [{"lan": "en", "subtitle_url": "https://aisubtitle.hdslb.com/en"},
                                                                       {"lan": "ai-zh", "subtitle_url": "//aisubtitle.hdslb.com/zh"}]}}}
            self.assertTrue(url.endswith("/zh"))
            return {"body": [{"from": 53.2, "to": 56, "content": "字幕原文"}]}
        with mock.patch.object(bilibili.time, "sleep"), mock.patch.object(bilibili, "resolve_short", return_value=SOURCE["meta"]["url"]) as resolve:
            got = bilibili.read_video("https://b23.tv/AbCd123", cookie="private-test-cookie", fetch=fetch)
        resolve.assert_called_once_with("https://b23.tv/AbCd123")
        self.assertEqual(2, got["meta"]["page"])
        self.assertTrue(got["meta"]["auto_subtitle"])
        self.assertEqual("private-test-cookie", calls[2][1])
        self.assertEqual("", calls[3][1])
        self.assertNotIn("private-test-cookie", json.dumps(got))

    def test_external_address_and_redirect_are_rejected(self):
        self.assertEqual((BV, 2), bilibili.video_ref("https://www.bilibili.com/video/"+BV+"?p=2#reply"))
        with self.assertRaises(bilibili.BiliError):
            bilibili.video_ref(BV+"?p=wrong")
        for url in ("http://aisubtitle.hdslb.com/a", "https://evil.test/a", "https://api.bilibili.com.evil.test/a", "https://127.0.0.1/a"):
            with self.assertRaises(bilibili.BiliError):
                bilibili.get_json(url, "secret")
        with self.assertRaises(bilibili.BiliError):
            bilibili.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")

    def test_short_share_normalization_and_redirect_destination(self):
        from urllib.error import HTTPError
        short = "https://b23.tv/AbCd123"
        for value in (short, "b23.tv/AbCd123", "【分享视频】http://b23.tv/AbCd123?share=tracking"):
            self.assertEqual(short, bilibili.reference_url(value))
        for value in ("https://b23.tv.evil.test/AbCd123", "https://evil.test/b23.tv/AbCd123", "https://b23.tv@evil.test/AbCd123"):
            with self.assertRaises(bilibili.BiliError):
                bilibili.reference_url(value)
        target = SOURCE["meta"]["url"] + "&tracking=discard"
        with mock.patch.object(bilibili.request, "build_opener") as build:
            opener = build.return_value
            opener.open.side_effect = HTTPError(short, 302, "redirect", {"Location": target}, io.BytesIO())
            self.assertEqual(SOURCE["meta"]["url"], bilibili.resolve_short(short))
            req = opener.open.call_args.args[0]
            self.assertEqual(short, req.full_url)
            self.assertIsNone(req.get_header("Cookie"))
            self.assertIsInstance(build.call_args.args[0], bilibili.ShortRedirect)
            self.assertEqual(1, opener.open.call_count, "不访问目标页")
            for target in ("https://evil.test/video/"+BV, "https://b23.tv/Other", "https://www.bilibili.com.evil.test/video/"+BV,
                           "https://user@www.bilibili.com/video/"+BV, "http://www.bilibili.com/video/"+BV,
                           "https://www.bilibili.com/opus/123", "https://www.bilibili.com/video/"+BV+"?p=oops"):
                opener.open.side_effect = HTTPError(short, 302, "redirect", {"Location": target}, io.BytesIO())
                with self.assertRaises(bilibili.BiliError):
                    bilibili.resolve_short(short)

    def test_short_resolution_runs_in_worker_and_preserves_both_sources(self):
        short = "https://b23.tv/AbCd123"
        entered, release = threading.Event(), threading.Event()
        def reader(value):
            self.assertEqual(short, value)
            entered.set()
            release.wait(timeout=3)
            return dict(copy.deepcopy(SOURCE), lines=[])
        with mock.patch.object(bilibili, "read_video", side_effect=reader):
            try:
                status, started = api.handle("POST", "/learning/start", {"reference": short}, None)
                self.assertEqual(202, status)
                self.assertTrue(entered.wait(timeout=1))
                self.assertFalse(learning._FUTURE.done(), "入口已返回，解析仍在后台")
                job = knowledge.read(started["id"])
                self.assertEqual(short, job["reference"])
            finally:
                release.set()
                learning._FUTURE.result(timeout=5)
            job = knowledge.read(started["id"])
            self.assertEqual("no_subtitles", job["status"])
            self.assertEqual(short, job["submitted_reference"])
            self.assertEqual(SOURCE["meta"]["url"], job["reference"])

    def test_failed_short_resolution_never_calls_subtitle_api_or_model(self):
        complete = mock.Mock()
        with mock.patch.object(bilibili, "resolve_short", side_effect=bilibili.BiliError("短链接暂时读不到")) as resolve:
            started = learning.start("https://b23.tv/AbCd123", complete=complete)
            learning._FUTURE.result(timeout=5)
        job = knowledge.read(started["id"])
        self.assertEqual("failed", job["status"])
        self.assertEqual([], job["cards"])
        self.assertEqual("https://b23.tv/AbCd123", job["reference"])
        self.assertEqual(1, resolve.call_count)
        complete.assert_not_called()

    def test_evidence_requires_real_time_and_verbatim_substring(self):
        good, rejected = learning.validate_cards([CARD], SOURCE["lines"], SOURCE["meta"], 0)
        self.assertEqual([], rejected)
        self.assertEqual(SOURCE["meta"]["url"]+"&t=53", good[0]["evidence"][0]["url"])
        for change in ({"t": 0}, {"quote": "视频里没有说过的话"}, {"t": True}, {"quote": ""}):
            bad = copy.deepcopy(CARD);bad["evidence"][0].update(change)
            cards, rejected = learning.validate_cards([CARD, bad], SOURCE["lines"], SOURCE["meta"], 0)
            self.assertEqual(1, len(cards))
            self.assertEqual(1, len(rejected))

    def test_knowledge_english_matching_ignores_case_and_keeps_original_evidence(self):
        cards = [
            {"id": "alloy", "title": "INCONEL 625", "claim": "Ni 58%", "tags": ["合金"],
             "evidence": [{"t": 1, "quote": "INCONEL 625: Ni 58%"}]},
            {"id": "vision", "title": "视觉模型", "claim": "图像理解", "tags": ["Qwen-VL"],
             "evidence": [{"t": 2, "quote": "Qwen-VL"}]},
        ]
        knowledge.save({"id": "f" * 32, "created_at": "2026-11-08T12:00:00+09:00",
                        "source": copy.deepcopy(SOURCE), "cards": cards})
        before = {p: p.read_bytes() for p in knowledge.library_root().glob("*")}
        for query, expected in (("inconel ni", cards[0]), ("INCONEL NI", cards[0]),
                                ("qwen-vl", cards[1]), ("QWEN-VL", cards[1])):
            with self.subTest(query=query):
                hits = knowledge.search(query)
                self.assertEqual(1, len(hits))
                self.assertEqual(expected, hits[0]["card"])
                self.assertEqual(SOURCE["meta"]["owner"], hits[0]["owner"])
                self.assertEqual("B站字幕提炼，视频观点未独立核验", hits[0]["source"])
        self.assertEqual([], knowledge.search("今天吃什么"))
        self.assertEqual(before, {p: p.read_bytes() for p in knowledge.library_root().glob("*")})

    def test_job_can_be_recalled_with_source_and_invalid_evidence_is_not_learned(self):
        started = learning.start(BV, reader=lambda _: copy.deepcopy(SOURCE), complete=lambda _: json.dumps([CARD]))
        learning._FUTURE.result(timeout=5)
        job = knowledge.read(started["id"])
        self.assertEqual("done", job["status"])
        hits = knowledge.search("行空间是什么")
        self.assertEqual(1, len(hits))
        self.assertEqual("示例作者", hits[0]["owner"])
        self.assertEqual([], knowledge.search("今天吃什么"))
        before = {p: p.read_bytes() for p in knowledge.library_root().glob("*")}
        for path in ("/learning/status", "/learning/read?id="+job["id"], "/learning/search?q=行空间"):
            status, data = api.handle("GET", path, None, None)
            self.assertEqual(200, status)
        self.assertEqual(before, {p: p.read_bytes() for p in knowledge.library_root().glob("*")})
        bad = copy.deepcopy(CARD);bad["evidence"][0]["quote"] = "伪造证据"
        started = learning.start(BV, reader=lambda _: copy.deepcopy(SOURCE), complete=lambda _: json.dumps([bad]))
        learning._FUTURE.result(timeout=5)
        failed = knowledge.read(started["id"])
        self.assertEqual(("failed", []), (failed["status"], failed["cards"]))
        self.assertEqual(1, len(knowledge.search("行空间是什么")), "失败的新任务不能覆盖旧的有效知识")

    def test_three_segment_budget_and_resume_preserve_completed_segments(self):
        source = copy.deepcopy(SOURCE)
        source["lines"] = [{"t": i*10, "end": i*10+5, "text": str(i)+"内容"*600} for i in range(12)]
        calls = []
        def complete(messages):
            lines = json.loads(messages[-1]["content"])["lines"]
            calls.append(lines[0]["t"])
            return json.dumps([dict(CARD, evidence=[{"t": lines[0]["t"], "quote": lines[0]["text"][:10]}])])
        started = learning.start(BV, reader=lambda _: source, complete=complete)
        learning._FUTURE.result(timeout=5)
        job = knowledge.read(started["id"])
        self.assertEqual(("partial", 3, 4), (job["status"], job["next_segment"], job["total_segments"]))
        learning.start(ident=job["id"], complete=complete)
        learning._FUTURE.result(timeout=5)
        job = knowledge.read(job["id"])
        self.assertEqual(("done", 4, 4), (job["status"], job["next_segment"], len(job["cards"])))
        self.assertEqual(4, len(set(calls)))

    def test_no_subtitles_never_calls_model_and_reads_cannot_start_jobs(self):
        complete = mock.Mock()
        source = dict(SOURCE, lines=[], status="no_subtitles", note="未取得字幕")
        started = learning.start(BV, reader=lambda _: source, complete=complete)
        learning._FUTURE.result(timeout=5)
        self.assertEqual("no_subtitles", knowledge.read(started["id"])["status"])
        complete.assert_not_called()
        self.assertEqual(405, api.handle("GET", "/learning/start", None, None)[0])
        self.assertEqual(400, api.handle("POST", "/learning/start", {"reference": "http://localhost"}, None)[0])
        self.assertEqual(400, api.handle("GET", "/learning/read?id=../secret", None, None)[0])

    def test_background_learning_budget_does_not_modify_chat_configuration(self):
        before = learning.llm.resolve_config()
        with mock.patch.object(learning.llm, "complete", return_value=json.dumps([CARD])) as complete:
            started = learning.start(BV, reader=lambda _: copy.deepcopy(SOURCE))
            learning._FUTURE.result(timeout=5)
        config = complete.call_args.kwargs["config"]
        self.assertEqual((16384, 120000), (config["max_tokens"], config["timeout_ms"]))
        self.assertEqual(before["reasoning_effort"], config["reasoning_effort"])
        self.assertEqual(before, learning.llm.resolve_config())
        self.assertEqual("done", knowledge.read(started["id"])["status"])


if __name__ == "__main__":
    unittest.main()
