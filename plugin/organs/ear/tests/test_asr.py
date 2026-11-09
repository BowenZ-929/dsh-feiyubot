import importlib.util
import io
import wave
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from organs.ear import asr


class AutoLanguageTest(unittest.TestCase):

    @unittest.skipUnless(importlib.util.find_spec('faster_whisper'), '需要本地媒体运行环境')
    def test_sound_classifier_shares_long_clip_cutoff_and_missing_model_keeps_transcript(self):
        from organs.ear import sound_events
        buffer=io.BytesIO()
        with wave.open(buffer,'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(b'\0\0'*16000*65)
        part=SimpleNamespace(start=0,end=1,text='hello',avg_logprob=-.2,no_speech_prob=.01)
        real=sound_events.classify
        with patch('faster_whisper.WhisperModel') as ctor,patch.object(sound_events,'classify',wraps=real) as classify:
            ctor.return_value.transcribe.return_value=([part],SimpleNamespace(language='en'))
            data=asr.transcribe(buffer.getvalue(),'fake-model',sound_model='/missing/model.onnx')
        self.assertEqual(len(classify.call_args.args[0]),30*16000)
        self.assertEqual(data['processed_seconds'],30)
        self.assertEqual(data['transcript'],'hello')
        self.assertEqual(data['sound_events']['status'],'unavailable')

    @unittest.skipUnless(importlib.util.find_spec('faster_whisper'), '需要本地媒体运行环境')
    def test_auto_language_uses_detection_and_records_detected_language(self):
        buffer=io.BytesIO()
        with wave.open(buffer,'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(b'\0\0'*32000)
        part=SimpleNamespace(start=0,end=1,text='Together we stand.',avg_logprob=-0.2,no_speech_prob=0.01)
        with patch('faster_whisper.WhisperModel') as constructor:
            constructor.return_value.transcribe.return_value=([part],SimpleNamespace(language='en',language_probability=.98))
            data=asr.transcribe(buffer.getvalue(),'fake-model',language='auto')
        self.assertIsNone(constructor.return_value.transcribe.call_args.kwargs['language'])
        self.assertEqual(data['language'],'en')
        self.assertEqual(data['transcript'],'Together we stand.')

