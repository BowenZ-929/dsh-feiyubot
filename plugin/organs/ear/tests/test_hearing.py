import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from organs.ear import asr, hearing
from api import api


class HearingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.dict("os.environ", {"QQAGENT_HOME": str(self.root)})
        p.start()
        self.addCleanup(p.stop)

    def test_uncertain_segments_are_not_presented_as_clear_words(self):
        data = asr.result([{"text": "下午见", "uncertain": False},
                           {"text": "猜测的人名", "uncertain": True}], 4, 4, False, "small", "zh")
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["transcript"], "下午见[听不清]")
        rows = hearing.observe([{"kind": "audio", "speaker": "甲", "messageId": 12,
                                 "transcription": data}], session_id="qq:private:1", delivery_id="d")
        context = hearing.context(rows)
        self.assertNotIn("猜测的人名", context)
        self.assertIn("不一定是录音中的说话者", context)
        self.assertEqual(data["speaker_identity"], "unknown")
        self.assertEqual(data["original_author"], "unknown")
        saved = json.loads((self.root / "ear/observations.jsonl").read_text())
        self.assertEqual(saved["transcription"]["segments"][1]["text"], "猜测的人名")

    def test_empty_short_long_and_all_unclear_are_distinct(self):
        self.assertEqual(asr.result([], .5, .5, False, "small", "zh")["status"], "short")
        self.assertEqual(asr.result([], 5, 5, False, "small", "zh")["status"], "empty")
        self.assertEqual(asr.result([{"text": "猜的", "uncertain": True}], 5, 5, False, "small", "zh")["status"], "unclear")
        data = asr.result([{"text": "仅前半段", "uncertain": False}], 61, 30, True, "small", "zh")
        self.assertEqual(data["status"], "partial")
        self.assertTrue(data["duration_is_lower_bound"])

    def test_nonverbal_sound_reaches_chat_without_inventing_words(self):
        data = asr.result([], 4, 4, False, 'small', 'auto')
        data['sound_events'] = {'status':'ok', 'source':'local_yamnet', 'processed_seconds':4,
                                'events':[{'label':'音乐', 'score':.8}]}
        self.assertTrue(hearing.validate(data))
        text = hearing.context([{'speaker':'甲','message_id':12,'transcription':data}])
        self.assertIn('可能有音乐', text)
        self.assertIn('未获得可靠转写', text)
        self.assertNotIn('本轮只提供文字转写', text)
        for events in ([{'label':'他很伤心','score':.9}], [{'label':'音乐','score':float('nan')}],
                       [{'label':'音乐','score':2}], 'Music'):
            data['sound_events']['events'] = events
            self.assertFalse(hearing.validate(data))

    def test_switch_records_changes_and_rejects_bad_transcript(self):
        status, data = api.handle("GET", "/ear/status", None, None)
        self.assertEqual(status, 200)
        self.assertFalse(data["config"]["enabled"])
        self.assertFalse((self.root / "ear").exists())
        api.handle("POST", "/ear/config", {"enabled": False}, None)
        page = api.handle("GET", "/observe/records?source=ear", None, None)[1]
        self.assertFalse(page["rows"][0]["record"]["after"])
        for value in (None, [], {"status": "invented"}, {"status": "ok", "transcript": 3},
                      {"status": "ok", "transcript": "假转写"}):
            self.assertFalse(hearing.validate(value))
        self.assertEqual(api.handle("POST", "/ear/config", {"enabled": "yes"}, None)[0], 400)
