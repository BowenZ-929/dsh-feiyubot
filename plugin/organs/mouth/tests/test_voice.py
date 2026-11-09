import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import wave

from organs.mouth import voice


def wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\0\0" * 24000)
    return buffer.getvalue()


class VoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.dict("os.environ", {"QQAGENT_HOME": str(self.root), "BREEZE_API_KEY": "brz_test_not_real"})
        p.start()
        self.addCleanup(p.stop)
        self.config = dict(voice.load_config(), enabled=True, daily_attempts=2)
        (self.root / "mouth").mkdir()
        (self.root / "mouth/voice.json").write_text(json.dumps(self.config))

    def test_without_current_request_does_not_synthesize(self):
        with patch.object(voice, "synthesize") as synth:
            voice.apply({"send": True, "chunks": [{"text": "晚安"}]}, False, "id")
            synth.assert_not_called()

    def test_sync_official_request_and_complete_wav(self):
        response = MagicMock(status=200)
        response.read.return_value = wav()
        with patch.object(voice.request, "urlopen") as opened:
            opened.return_value.__enter__.return_value = response
            raw, duration = voice.synthesize("晚安。", self.config, "brz_private")
        req = opened.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.breeze.blue/v1/text-to-speech/voc_8yud7hz7c9q2?output_format=wav_24000&delivery=sync")
        self.assertEqual(req.get_header("Xi-api-key"), "brz_private")
        self.assertEqual(req.get_header("User-agent"), "NewQQAgent/1.0")
        self.assertEqual(json.loads(req.data), {"text": "晚安。", "model_id": "breeze-tts-2", "language_code": "zh"})
        self.assertEqual(duration, 1)
        self.assertEqual(raw, wav())

    def test_invalid_and_truncated_audio_is_not_written(self):
        for status, raw in ((202, b'{}'), (200, b'{}'), (200, wav()[:-200]), (200, b"RIFF")):
            response = MagicMock(status=status)
            response.read.return_value = raw
            with patch.object(voice.request, "urlopen") as opened:
                opened.return_value.__enter__.return_value = response
                with self.assertRaises((ValueError, wave.Error, EOFError)):
                    voice.synthesize("晚安", self.config, "key")
        self.assertFalse((self.root / "mouth/audio").exists())

    def test_final_chunks_become_one_record_without_extra_text_message(self):
        payload = {"send": True, "reply": "定稿", "chunks": [{"text": "第一句。"}, {"text": "第二句。"}]}
        with patch.object(voice, "synthesize", return_value=(wav(), 1)) as synth:
            voice.apply(payload, True, "delivery")
        self.assertEqual(synth.call_args.args[0], "第一句。\n第二句。")
        self.assertEqual(len(payload["chunks"]), 1)
        self.assertEqual(payload["chunks"][0]["text"], "第一句。\n第二句。")
        self.assertTrue(payload["chunks"][0]["audio"]["file"].startswith("file:///"))
        self.assertEqual(voice.attempts_today(), 1)

    def test_failed_calls_count_toward_limit_no_retries_or_secret_in_log(self):
        payload = {"send": True, "chunks": [{"text": "好呀。"}]}
        original = copy.deepcopy(payload["chunks"])
        with patch.object(voice, "synthesize", side_effect=TimeoutError("brz_test_not_real")) as synth:
            for _ in range(3):
                voice.apply(payload, True, "delivery")
            self.assertEqual(synth.call_count, 2)
        self.assertEqual(payload["voice"]["reason"], "daily_limit")
        self.assertEqual(payload["chunks"], original)
        self.assertNotIn("brz_test_not_real", (self.root / "mouth/voice.jsonl").read_text())

    def test_silence_mixed_media_and_unsuitable_text_do_not_synthesize(self):
        with patch.object(voice, "synthesize") as synth:
            voice.apply({"send": False, "chunks": [{"text": "晚安"}]}, True, "id")
            voice.apply({"send": True, "chunks": [{"text": "晚安", "image": {"file": "file:///test"}}]}, True, "id")
            for text in ("字" * 121, "代码 `print(1)`", "https://example.com", "[laughing]", ""):
                self.assertEqual(voice.generate(text)["status"], "skipped")
            synth.assert_not_called()
        self.assertEqual(voice.attempts_today(), 0)

    def test_disabled_and_missing_key_do_not_charge(self):
        (self.root / "mouth/voice.json").write_text(json.dumps(dict(self.config, enabled=False)))
        payload = {"send": True, "chunks": [{"text": "晚安。"}]}
        with patch.object(voice, "synthesize") as synth, patch.dict("os.environ", {"BREEZE_API_KEY": ""}):
            voice.apply(payload, True, "id")
            self.assertEqual(payload["voice"]["reason"], "disabled")
            self.assertEqual(voice.generate("晚安")["reason"], "missing_key")
            synth.assert_not_called()
