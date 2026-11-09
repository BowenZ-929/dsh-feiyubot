import io
import json
import threading
import unittest
from unittest.mock import patch
from PIL import Image

from organs.hand import comfy


class ComfyTest(unittest.TestCase):
    def test_reference_upload_preserves_exif_orientation_and_white_transparency(self):
        jpeg = io.BytesIO()
        source = Image.new('RGB', (80, 40), 'red')
        exif = source.getexif(); exif[274] = 6
        source.save(jpeg, format='JPEG', exif=exif)
        transparent = io.BytesIO()
        Image.new('RGBA', (20, 10), (0, 0, 0, 0)).save(transparent, format='PNG')
        uploaded = []
        def read(route, data=None, content_type=None):
            self.assertEqual('/upload/image', route)
            png = data.split(b'\r\n\r\n', 1)[1].rsplit(b'\r\n--', 1)[0]
            with Image.open(io.BytesIO(png)) as image:
                uploaded.append((image.size, image.getpixel((0, 0))))
            return json.dumps({'name':str(len(uploaded))+'.png', 'subfolder':''}).encode()
        cancel = threading.Event()
        def submit(*args):
            cancel.set(); return {'prompt_id':'owned-job'}
        with patch.object(comfy, '_read', side_effect=read), patch.object(comfy, '_api', side_effect=submit):
            with self.assertRaises(comfy.ImageError):
                comfy.generate('hello', width=512, height=512, references=[jpeg.getvalue(), transparent.getvalue()], cancel=cancel)
        self.assertEqual((40, 80), uploaded[0][0])
        self.assertEqual((255, 255, 255), uploaded[1][1])

    def test_text_graph_preserves_requested_canvas_and_prompt(self):
        graph = comfy.workflow('鲸鱼抱饭碗', width=768, height=1024, seed=42, steps=25)
        self.assertEqual('鲸鱼抱饭碗', graph['encode']['inputs']['prompt'])
        self.assertEqual({'width':768, 'height':1024, 'batch_size':1}, graph['latent']['inputs'])
        self.assertEqual(['latent', 0], graph['sample']['inputs']['latent_image'])
        self.assertEqual(25, graph['sample']['inputs']['steps'])
        self.assertEqual(['sample', 0], graph['decode']['inputs']['samples'])

    def test_references_are_conditioning_not_a_replacement_canvas(self):
        graph = comfy.workflow('换衣服', width=768, height=1024, seed=42, steps=25,
                               references=['wardrobe.png', 'character.png'])
        self.assertEqual('wardrobe.png', graph['ref1']['inputs']['image'])
        self.assertEqual('character.png', graph['ref2']['inputs']['image'])
        self.assertEqual(['ref1', 0], graph['encode']['inputs']['images.image_1'])
        self.assertEqual(['vae', 0], graph['encode']['inputs']['vae'])
        self.assertEqual(['latent', 0], graph['sample']['inputs']['latent_image'])

    def test_result_uses_own_history_and_fetches_the_saved_image(self):
        calls, submitted = [], []
        def api(route, body=None):
            calls.append((route, body))
            if route == '/prompt':
                return {'prompt_id':'owned-job', 'node_errors':{}}
            return {'owned-job':{'status':{'status_str':'success', 'completed':True},
                'outputs':{'save':{'images':[{'filename':'our image.png', 'subfolder':'', 'type':'output'}]}}}}
        with patch.object(comfy, '_api', side_effect=api), patch.object(comfy, '_read', return_value=b'png') as read:
            png, info = comfy.generate('hello', width=512, height=512, cancel=threading.Event(),
                                       steps=17, seed=8123, submitted=submitted.append)
        self.assertEqual(b'png', png)
        self.assertEqual('owned-job', info['prompt_id'])
        self.assertEqual(calls[0][1]['prompt'], info['workflow'])
        self.assertEqual(8123, info['workflow']['sample']['inputs']['seed'])
        self.assertEqual(17, info['workflow']['sample']['inputs']['steps'])
        self.assertEqual([info], submitted)
        self.assertEqual('/history/owned-job', calls[1][0])
        self.assertIn('filename=our+image.png', read.call_args.args[0])

    def test_cancel_targets_own_prompt_without_global_interrupt(self):
        cancel = threading.Event()
        calls = []
        def api(route, body=None):
            calls.append((route, body))
            if route == '/prompt':
                cancel.set()
                return {'prompt_id':'owned-job'}
            return {}
        with patch.object(comfy, '_api', side_effect=api), self.assertRaises(comfy.ImageError):
            comfy.generate('hello', width=512, height=512, cancel=cancel)
        self.assertIn(('/queue', {'delete':['owned-job']}), calls)
        self.assertIn(('/interrupt', {'prompt_id':'owned-job'}), calls)

    def test_failed_generation_is_not_a_finished_image(self):
        replies = [{'prompt_id':'owned-job'}, {'owned-job':{'status':{'status_str':'error'},
                    'outputs':{}, 'private_error':'do not expose'}}]
        with patch.object(comfy, '_api', side_effect=replies), self.assertRaisesRegex(comfy.ImageError, '生成失败'):
            comfy.generate('hello', width=512, height=512, cancel=threading.Event())

    def test_accepted_workflow_remains_available_when_generation_fails(self):
        submitted=[]
        replies=[{'prompt_id':'owned-job'}, {'owned-job':{'status':{'status_str':'error'}}}]
        with patch.object(comfy,'_api',side_effect=replies) as api:
            with self.assertRaises(comfy.ImageError):
                comfy.generate('失败样例',width=768,height=1024,submitted=submitted.append)
        self.assertEqual(api.call_args_list[0].args[1]['prompt'],submitted[0]['workflow'])
        self.assertEqual('owned-job',submitted[0]['prompt_id'])

    def test_timeout_cancels_own_job_and_does_not_resubmit(self):
        with patch.object(comfy, '_api', side_effect=[{'prompt_id':'owned-job'}, {}, {}]) as api:
            with self.assertRaisesRegex(comfy.ImageError, '超时') as failure:
                comfy.generate('hello', width=512, height=512, timeout=0)
        self.assertEqual('timeout',failure.exception.kind)
        self.assertEqual(['/prompt', '/queue', '/interrupt'], [c.args[0] for c in api.call_args_list])

    def test_already_cancelled_request_does_not_submit(self):
        event = threading.Event(); event.set()
        with patch.object(comfy, '_api') as api, self.assertRaisesRegex(comfy.ImageError, '取消'):
            comfy.generate('hello', width=512, height=512, cancel=event)
        api.assert_not_called()
