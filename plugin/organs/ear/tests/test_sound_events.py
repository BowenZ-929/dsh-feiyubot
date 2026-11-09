import importlib.util
import unittest
import tempfile
from unittest.mock import patch

from organs.ear import sound_events


@unittest.skipUnless(importlib.util.find_spec('onnxruntime'), '需要本地媒体环境')
class SoundEventsTest(unittest.TestCase):
    def test_mean_scores_filter_weak_labels_and_keep_music_without_identity(self):
        import numpy as np
        scores=np.zeros((2,521),dtype=np.float32)
        scores[:,132]=.8  # Music
        scores[:,1]=.99  # Child speech: 不作年龄身份猜测
        scores[:,13]=[.1,.5]  # Laughter
        scores[:,70]=.1  # Bark: 低分不输出
        with tempfile.NamedTemporaryFile() as file, patch('onnxruntime.InferenceSession') as ctor:
            ctor.return_value.get_inputs.return_value=[type('Input',(),{'name':'waveform'})()]
            ctor.return_value.get_outputs.return_value=[type('Output',(),{'name':'scores'})()]
            ctor.return_value.run.return_value=[scores]
            result=sound_events.classify(np.zeros(32000,dtype=np.float32),file.name)
        self.assertEqual(result['events'],[{'label':'音乐','score':.8},{'label':'笑声','score':.3}])
        self.assertEqual(result['processed_seconds'],2)

    def test_short_and_missing_model_leave_no_invented_sound(self):
        import numpy as np
        with patch('onnxruntime.InferenceSession') as ctor:
            data=sound_events.classify(np.zeros(8000,dtype=np.float32),'fake.onnx')
        self.assertEqual(data['status'],'short')
        ctor.assert_not_called()
        data=sound_events.classify(np.zeros(32000,dtype=np.float32),'/missing/yamnet.onnx')
        self.assertEqual(data['status'],'unavailable')
        self.assertEqual(sound_events.context({'sound_events':data}),'')

    def test_invalid_model_is_reported_without_throwing_or_claiming_silence(self):
        import numpy as np
        with tempfile.NamedTemporaryFile() as file:
            file.write(b'invalid onnx');file.flush()
            data=sound_events.classify(np.zeros(32000,dtype=np.float32),file.name)
        self.assertEqual(data['status'],'failed')
        self.assertEqual(data['events'],[])
        self.assertEqual(sound_events.context({'sound_events':data}),'')
