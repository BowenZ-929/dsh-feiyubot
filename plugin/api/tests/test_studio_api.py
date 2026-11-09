import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from api import api
from organs.home.tests.test_art import picture


class StudioApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.dict(os.environ, {"HOME_ROOT": str(self.root), "QQAGENT_HOME": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_complete_local_draft_flow_without_model_or_qq_calls(self):
        with patch("organs.mind.llm.complete") as llm, patch("organs.hand.channel.say_image") as send:
            status, data = api.handle("GET", "/studio/assets", None, None)
            self.assertEqual((200, []), (status, data["assets"]))
            status, asset = api.handle("POST", "/studio/assets/save", {"name": "小猫", "category": "characters", "images": [picture()]}, None)
            self.assertEqual(200, status)
            request = {"prompt": "画一只小猫", "mode": "strokes", "assets": [asset["id"]]}
            status, preview = api.handle("POST", "/studio/preview", request, None)
            self.assertEqual(200, status)
            self.assertEqual(0, len(preview["references"]))
            status, draft = api.handle("POST", "/studio/drafts/save", request, None)
            self.assertEqual("draft", draft["status"])
            self.assertEqual(1, len(api.handle("GET", "/studio/drafts", None, None)[1]["drafts"]))
            self.assertTrue(api.handle("GET", "/studio/assets", None, None)[1]["assets"][0]["previews"])
            llm.assert_not_called()
            send.assert_not_called()

    def test_stroke_job_and_frame_api(self):
        import json, base64
        from organs.hand.tests.test_strokes import PROGRAM
        from organs.mind import stroke_painting
        with patch("organs.mind.llm.complete", return_value=json.dumps(PROGRAM)):
            status, job = api.handle("POST", "/studio/strokes/start", {"prompt":"画三角", "mode":"strokes"}, None)
            self.assertEqual(202, status)
            stroke_painting._FUTURE.result(timeout=5)
        status, complete = api.handle("GET", "/studio/strokes/read?id=" + job["id"], None, None)
        self.assertEqual((200, "done"), (status, complete["status"]))
        status, final = api.handle("GET", "/studio/strokes/read?id=" + job["id"] + "&step=2", None, None)
        self.assertEqual(complete["image"], final["image"])
        status, blank = api.handle("GET", "/studio/strokes/read?id=" + job["id"] + "&step=0", None, None)
        self.assertNotEqual(final["image"], blank["image"])
        self.assertEqual(400, api.handle("GET", "/studio/strokes/read?id=" + job["id"] + "&step=3", None, None)[0])
        self.assertEqual(400, api.handle("GET", "/studio/strokes/read?id=../outside", None, None)[0])
        self.assertEqual(1, len(api.handle("GET", "/studio/strokes", None, None)[1]["jobs"]))

    def test_actual_submitted_parameters_survive_job_storage_and_later_reads(self):
        from organs.hand import comfy, strokes
        from organs.hand.tests.test_strokes import PROGRAM
        from organs.mind import stroke_painting
        for success in (True,False):
            with self.subTest(success=success):
                submitted=[]
                def backend(route,body=None):
                    if route=='/prompt':
                        submitted.append(body['prompt'])
                        return {'prompt_id':'isolated-accepted'}
                    return {'isolated-accepted':{'status':{'status_str':'success' if success else 'error'},
                        'outputs':{'save':{'images':[{'filename':'result.png','subfolder':'','type':'output'}]}}}}
                with patch.object(comfy,'config',return_value={'enabled':True,'steps':17}), \
                     patch.object(comfy,'_api',side_effect=backend), \
                     patch.object(comfy,'_read',return_value=strokes.render(PROGRAM)):
                    status,job=api.handle('POST','/studio/strokes/start',
                        {'prompt':'隔离参数验证','mode':'image'},None)
                    self.assertEqual(202,status)
                    stroke_painting._FUTURE.result(timeout=5)
                with patch.object(comfy,'MODEL','later-model'):
                    status,saved=api.handle('GET','/studio/strokes/read?id='+job['id'],None,None)
                self.assertEqual(200,status)
                self.assertEqual('done' if success else 'failed',saved['status'])
                self.assertEqual(submitted[0],saved['generation']['workflow'])
                self.assertEqual(17,saved['generation']['workflow']['sample']['inputs']['steps'])
                self.assertNotEqual('later-model',saved['generation']['workflow']['unet']['inputs']['unet_name'])

    def test_invalid_input_and_wrong_method(self):
        self.assertEqual(400, api.handle("POST", "/studio/assets/save", [], None)[0])
        self.assertEqual(400, api.handle("POST", "/studio/preview", {"prompt": ""}, None)[0])
        self.assertEqual(405, api.handle("POST", "/studio/assets", {}, None)[0])

    def test_local_model_capability_and_completed_image_without_strokes(self):
        from organs.hand import comfy, strokes
        from organs.hand.tests.test_strokes import PROGRAM
        from organs.mind import stroke_painting
        with patch.object(comfy, 'config', return_value={'enabled':True, 'steps':25}), \
             patch.object(comfy, 'generate', return_value=(strokes.render(PROGRAM), {'prompt_id':'local-job'})):
            data = api.handle('GET', '/studio/assets', None, None)[1]
            self.assertIn('image', data['generation_modes'])
            status, job = api.handle('POST', '/studio/strokes/start', {'prompt':'鲸鱼', 'mode':'image'}, None)
            self.assertEqual(202, status)
            stroke_painting._FUTURE.result(timeout=5)
        status, result = api.handle('GET', '/studio/strokes/read?id='+job['id'], None, None)
        self.assertEqual((200, 'done'), (status, result['status']))
        self.assertTrue(result['image'].startswith('data:image/png;base64,'))
        self.assertNotIn('program', result)
        self.assertEqual(400, api.handle('GET', '/studio/strokes/read?id='+job['id']+'&step=1', None, None)[0])

    def test_review_api_returns_both_images_and_final_replay(self):
        import json
        from organs.hand.tests.test_strokes import PROGRAM
        from organs.mind import stroke_painting
        revised=json.loads(json.dumps(PROGRAM));revised['background']='#EECCFF'
        with patch('organs.mind.llm.complete',side_effect=[json.dumps(PROGRAM),json.dumps({'action':'revise','reason':'背景修改','program':revised})]), \
             patch('organs.eye.vision.load_config',return_value={'enabled':True}), \
             patch('organs.eye.vision.read_key',return_value='test'), \
             patch('organs.eye.vision.describe',return_value={'status':'ok','observation':{'subject':'三角形'}}):
            status,job=api.handle('POST','/studio/strokes/start',{'prompt':'三角形','mode':'strokes','visual_review':True},None)
            self.assertEqual(202,status)
            stroke_painting._FUTURE.result(timeout=5)
        status,result=api.handle('GET','/studio/strokes/read?id='+job['id'],None,None)
        self.assertEqual(200,status)
        self.assertEqual('ok',result['review']['status'],result['review'])
        self.assertTrue(result['draft_image']!=result['image'],'修改后应返回不同的成图')
        self.assertEqual('背景修改',result['review']['reason'])
        final=api.handle('GET','/studio/strokes/read?id='+job['id']+'&step='+str(result['stroke_count']),None,None)[1]
        self.assertEqual(result['image'],final['image'])
        row=api.handle('GET','/studio/strokes',None,None)[1]['jobs'][0]
        self.assertNotIn('draft_program',row)
        self.assertNotIn('review',row)
