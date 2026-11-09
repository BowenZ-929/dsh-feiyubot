import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.tests.test_learning_api import BV as BASE_BV, SOURCE, CARD
from organs.hand import channel
from organs.hand.tests import helpers
from organs.home import knowledge
from organs.mind import learning, qq_learning

BV = BASE_BV + "?p=2"


class QQLearningTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"HOME_ROOT": str(self.root), "QQAGENT_HOME": str(self.root / "organs")})
        env.start(); self.addCleanup(env.stop)
        self.start = learning.start

    def handle(self, text="", intent=None, who="42", session="qq:group:7"):
        return qq_learning.handle(text, intent=intent, who=who, scene="group", target="7", session_id=session)

    def start_fake(self, *args, **kwargs):
        return self.start(*args, **kwargs, reader=lambda _: copy.deepcopy(SOURCE),
                          complete=lambda _: json.dumps([CARD]))

    def test_start_delivers_sourced_knowledge_to_original_session_and_status_is_readonly(self):
        with patch.object(learning, "start", side_effect=self.start_fake), patch.object(
                channel, "say_text", return_value={"code": channel.OK}) as send:
            result = self.handle(intent={"action": "start", "reference": BV})
            learning._FUTURE.result(timeout=5)
            job = knowledge.read(result["job_id"])
            self.assertEqual("done", job["status"])
            self.assertEqual("sent", job["delivery"]["status"])
            self.assertEqual(1, len(job["deliveries"]))
            self.assertEqual({"who": "42", "scene": "group", "target": 7, "session_id": "qq:group:7"}, job["destination"])
            self.assertIn("行空间", send.call_args.kwargs["text"])
            self.assertIn(job["reference"], send.call_args.kwargs["text"])
            self.assertIn("只读了字幕", send.call_args.kwargs["text"])
            from api import learning as learning_api
            code, listed = learning_api.dispatch("/learning/status", {}, None)
            self.assertEqual(200, code)
            self.assertEqual("sent", listed["jobs"][0]["delivery"]["status"])
        with patch.object(learning, "start") as start, patch.object(channel, "say_text") as send:
            self.assertEqual("done", self.handle("/学到哪")["status"])
            self.assertEqual("missing", self.handle("/学到哪", who="43")["status"])
            self.assertEqual("missing", self.handle("/学到哪", session="qq:group:8")["status"])
            self.assertIsNone(self.handle("分享一下 " + BV))
            start.assert_not_called(); send.assert_not_called()

    def test_continue_uses_only_current_users_job(self):
        job = {"id": "a" * 32, "created_at": "2026-11-07T12:00:00+09:00", "reference": BV,
               "status": "partial", "cards": [], "next_segment": 3,
               "destination": {"who": "42", "session_id": "qq:group:7"}}
        knowledge.save(job)
        with patch.object(learning, "start", return_value={"id": job["id"], "status": "running"}) as start:
            self.assertEqual("missing", self.handle("/继续学习", who="43")["status"])
            start.assert_not_called()
            self.handle("/继续学习")
            self.assertEqual(job["id"], start.call_args.kwargs["ident"])
            from api import learning as learning_api
            status, _ = learning_api.dispatch("/learning/start", {}, {"id": job["id"]})
            self.assertEqual(202, status)
            self.assertEqual(qq_learning.deliver, start.call_args.kwargs["on_done"])

    def test_failed_delivery_is_not_retried_on_status_or_restart(self):
        with patch.object(learning, "start", side_effect=self.start_fake), patch.object(
                channel, "say_text", side_effect=OSError("test channel unavailable")) as send:
            result = self.handle("/学习 " + BV)
            learning._FUTURE.result(timeout=5)
            job = knowledge.read(result["job_id"])
            self.assertEqual("unconfirmed", job["delivery"]["status"])
            self.handle("/学到哪")
            self.assertEqual(1, send.call_count)
            job["delivery"]["status"] = "sending"
            self.assertEqual("unconfirmed", learning.state(job)["delivery"]["status"])

    def test_missing_subtitles_does_not_call_model_and_reports_failure_once(self):
        source = dict(SOURCE, lines=[], note="无可读字幕")
        with patch.object(channel, "say_text", return_value={"code": channel.OK}) as send:
            def start(*args, **kwargs):
                return self.start(*args, **kwargs, reader=lambda _: source, complete=complete)
            with patch.object(learning, "start", side_effect=start), patch.object(learning.llm, "complete") as complete:
                result = self.handle("/学习 " + BV)
                learning._FUTURE.result(timeout=5)
                complete.assert_not_called()
            self.assertEqual("no_subtitles", knowledge.read(result["job_id"])["status"])
            self.assertEqual(1, send.call_count)
            self.assertIn("没拿到可读字幕", send.call_args.kwargs["text"])

    def test_completion_uses_real_local_http_receipt(self):
        with helpers.channel_service(channel_id="learning-test") as server, patch.dict(
                os.environ, {"HAND_CHANNEL_URL": server.url}), patch.object(learning, "start", side_effect=self.start_fake):
            result = self.handle("/学习 " + BV)
            learning._FUTURE.result(timeout=5)
            self.assertEqual("sent", knowledge.read(result["job_id"])["delivery"]["status"])
            self.assertEqual(1, len(server.records))
            self.assertEqual(7, server.records[0]["body"]["target"])
            self.assertEqual("group", server.records[0]["body"]["scene"])

    def test_completion_preserves_partial_and_replaced_receipts_without_retrying(self):
        from api import learning as learning_api
        for sent, stopped, reason, expected in (
            (3, None, "sent", "sent"),
            (1, "ack 失败（echo=test-2）", "sent", "partial"),
            (1, None, "replaced_too_long", "replaced"),
            (0, "连接已断开", "outbound_failed", "unconfirmed"),
        ):
            with self.subTest(expected=expected):
                response = {"ok": sent > 0, "id": "say-test", "sent": sent,
                            "stopped": stopped, "reason_code": reason}
                with helpers.FakeService(helpers.channel_gate(helpers.json_reply(response))) as server, patch.dict(
                        os.environ, {"HAND_CHANNEL_URL": server.url}), patch.object(
                        learning, "start", side_effect=self.start_fake):
                    result = self.handle("/学习 " + BV)
                    learning._FUTURE.result(timeout=5)
                    job = knowledge.read(result["job_id"])
                    self.assertEqual(expected, job["delivery"]["status"])
                    self.assertEqual(stopped, job["delivery"]["receipt"].get("stopped"))
                    self.assertEqual(sent, job["delivery"]["receipt"]["sent"])
                    self.assertEqual(job["delivery"], {k: v for k, v in job["deliveries"][-1].items() if k != "at"})
                    self.assertEqual("done", job["status"], "通知中断不等于资料提炼失败")
                    code, view = learning_api.dispatch("/learning/read", {"id": job["id"]}, None)
                    self.assertEqual(200, code)
                    self.assertEqual(expected, view["delivery"]["status"])
                    self.assertIn('"delivery": "' + expected + '"', qq_learning.context("qq:group:7"))
                    self.handle("/学到哪")
                    self.assertEqual(1, len(server.records), "状态查询不能补发整稿")
                    self.assertEqual(qq_learning.summary(job), job["delivery"]["text"])
