import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from organs.ear import phone_audio


class PhoneAudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.audio=Path(self.tmp.name)/'clip.opus';self.audio.write_bytes(b'test')

    def test_recording_is_playback_only_bounded_and_cannot_control_device(self):
        cfg={'serial':'phone-test','adb_path':'/test/adb','scrcpy_path':'/test/scrcpy'}
        with patch.object(phone_audio.phone,'config',return_value=cfg),patch.object(phone_audio.subprocess,'Popen') as popen:
            phone_audio.start(self.audio,30)
        cmd=popen.call_args.args[0]
        for arg in ('--audio-source=playback','--no-control','--no-video','--no-playback','--require-audio','--time-limit=30'):
            self.assertIn(arg,cmd)
        self.assertNotIn('--audio-source=mic',cmd)
        self.assertEqual('/test/adb',popen.call_args.kwargs['env']['ADB'])

    def test_unconfigured_capture_has_no_device_effect(self):
        with patch.object(phone_audio.phone,'config',return_value={}),patch.object(phone_audio.subprocess,'Popen') as popen:
            self.assertIsNone(phone_audio.start(self.audio,30))
        popen.assert_not_called()

    def test_failed_capture_never_transcribes(self):
        proc=Mock(returncode=1);proc.communicate.return_value=('','audio capture failed')
        with patch.object(phone_audio.subprocess,'run') as transcribe:
            result=phone_audio.finish(proc,self.audio)
        self.assertEqual('failed',result['status']);transcribe.assert_not_called()

    def test_capture_success_uses_existing_local_asr_without_upload(self):
        proc=Mock(returncode=0);proc.communicate.return_value=('','')
        cfg={'enabled':True,'python':'/test/python','model':'/test/model','language':'zh','timeout_seconds':20}
        transcript={'status':'ok','source':'local_asr','transcript':'今天我们来看看','processed_seconds':12}
        with patch.object(phone_audio.hearing,'load_config',return_value=cfg),patch.object(phone_audio.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(transcript))) as run:
            result=phone_audio.finish(proc,self.audio)
        self.assertEqual('今天我们来看看',result['transcript'])
        self.assertEqual('phone_playback',result['recording_source'])
        self.assertEqual(20,run.call_args.kwargs['timeout'])
        self.assertIn(str(self.audio),run.call_args.args[0])
        self.assertEqual('auto',run.call_args.args[0][-1])

    def test_stop_waits_for_recorder_and_kills_only_if_it_does_not_exit(self):
        proc=Mock();proc.poll.return_value=None
        proc.communicate.side_effect=[subprocess.TimeoutExpired('scrcpy',5),('','')]
        phone_audio.stop(proc)
        proc.send_signal.assert_called_once();proc.kill.assert_called_once()

    def test_optional_sound_model_is_passed_to_same_local_transcriber(self):
        proc=Mock(returncode=0);proc.communicate.return_value=('','')
        cfg={'enabled':True,'python':'/test/python','model':'/test/model','sound_model':'/test/yamnet.onnx',
             'timeout_seconds':20}
        heard={'status':'empty','source':'local_asr','transcript':'','sound_events':
               {'status':'ok','source':'local_yamnet','processed_seconds':3,'events':[{'label':'音乐','score':.8}]}}
        with patch.object(phone_audio.hearing,'load_config',return_value=cfg),patch.object(phone_audio.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(heard))) as run:
            result=phone_audio.finish(proc,self.audio)
        self.assertIn('--sound-model',run.call_args.args[0])
        self.assertIn('/test/yamnet.onnx',run.call_args.args[0])
        self.assertEqual(result['sound_events'],heard['sound_events'])

    def test_asr_timeout_is_failure_not_invented_hearing(self):
        proc=Mock(returncode=0);proc.communicate.return_value=('','')
        cfg={'enabled':True,'python':'/test/python','model':'/test/model','language':'zh','timeout_seconds':20}
        with patch.object(phone_audio.hearing,'load_config',return_value=cfg),patch.object(phone_audio.subprocess,'run',side_effect=subprocess.TimeoutExpired('asr',20)):
            result=phone_audio.finish(proc,self.audio)
        self.assertEqual('failed',result['status']);self.assertNotIn('transcript',result)

    def test_subtitle_credit_alone_is_not_usable_dialogue_or_proof_of_silence(self):
        proc=Mock(returncode=0);proc.communicate.return_value=('','')
        cfg={'enabled':True,'python':'/test/python','model':'/test/model','language':'zh','timeout_seconds':20}
        transcript={'status':'ok','source':'local_asr','transcript':'字幕by索兰娅','processed_seconds':11.593}
        with patch.object(phone_audio.hearing,'load_config',return_value=cfg),patch.object(phone_audio.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(transcript))):
            result=phone_audio.finish(proc,self.audio)
        self.assertEqual('unclear',result['status'])
        self.assertEqual('',result['transcript'])
        self.assertEqual('字幕by索兰娅',result['raw_transcript'])
        self.assertNotIn('没有声音',result['reason'])
