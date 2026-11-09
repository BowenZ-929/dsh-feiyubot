import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from organs.home import art
from organs.hand import channel
from organs.hand.tests.test_strokes import PROGRAM
from organs.hand.tests import helpers
from organs.mind import qq_painting, stroke_painting


class QQPaintingTests(unittest.TestCase):
    def test_selected_existing_drawing_uses_server_path_and_defers_sending(self):
        from organs.mind import appraisal, llm, look
        job=self.finish(self.handle('/画画 小鲸鱼'))
        text='刚画好的小鲸鱼再给我看看'
        body={'kind':None,'evidence':'','reason':'索取已有画作','image_request':{
            'name':'小鲸鱼','evidence':[text],'painting_id':job['id'],'_painting_path':'/private/forged.png'}}
        with patch.object(llm,'complete',return_value=json.dumps(body)) as complete:
            result=appraisal.assess(text=text,who='42',bot='鱼',scene='group',segment=[],history=[],session_id='qq:group:700')
        intent=result['image_request']
        self.assertEqual(job['image_path'],intent['_painting_path'])
        self.assertNotIn(job['image_path'],json.dumps(complete.call_args.args[0]))
        calls=len(self.calls)
        prepared=look.handle('',who='42',scene='group',target=700,root=self.root/'hand',intent=intent)
        self.assertTrue(prepared['ok'],prepared)
        self.assertEqual(channel.file_uri(job['image_path']),prepared['image'])
        self.assertEqual(calls,len(self.calls),'只准备，不直接调用QQ发送')
        self.assertIn('已有完成画作',prepared['source'])
        self.assertEqual(job['image_path'],prepared['receipt']['params']['path'])
        body['image_request']['painting_id']='not-available'
        with patch.object(llm,'complete',return_value=json.dumps(body)):
            rejected=appraisal.assess(text=text,who='42',bot='鱼',scene='group',segment=[],history=[],session_id='qq:group:700')
        self.assertIsNone(rejected['image_request'])
        self.assertEqual('unavailable',rejected['status'])

    def test_available_images_only_include_completed_owned_or_public_autonomous_work(self):
        from organs.home import cards
        image=self.root/'drawing.png'
        image.write_bytes(b'fixture')
        cards.append_cards(self.root/'home',role='organ',by='日子线·自主活动',cards=[{
            'room':'工作台','kind':'物件','name':'公开画作回执',
            'params':{'scope':'public','text':'已完成','action':'paint','status':'done','job_id':'public'}},
            {'room':'工作台','kind':'物件','name':'非公开回执',
             'params':{'scope':'private','text':'不应提供','action':'paint','status':'done','job_id':'private'}}])
        for ident,status,destination,path in (
            ('public','done',{},str(image)),
            ('mine','done',{'session_id':'qq:private:42','who':'42'},str(image)),
            ('other','done',{'session_id':'qq:private:99','who':'99'},str(image)),
            ('unattributed','done',{},str(image)),
            ('private','done',{},str(image)),
            ('running','running',{'session_id':'qq:private:42','who':'42'},str(image)),
            ('failed','failed',{'session_id':'qq:private:42','who':'42'},str(image)),
            ('missing','done',{'session_id':'qq:private:42','who':'42'},str(image)+'missing')):
            art.save_painting_job({'id':ident,'created_at':'2026-11-09T12:00:00+09:00','status':status,
                'request':{'prompt':'虚构画作 '+ident},'destination':destination,'image_path':path})
        selected=qq_painting.available_images('qq:private:42','42')
        self.assertEqual({'public','mine'},{j['id'] for j in selected})
        self.assertEqual([],qq_painting.available_images('api:panel','42'))
        for index in range(12):
            cards.append_cards(self.root/'home',role='organ',by='日子线·自主活动',cards=[{
                'room':'工作台','kind':'物件','name':'后来的浏览'+str(index),
                'params':{'scope':'public','text':'浏览完成','action':'browse','status':'done','job_id':'browse'+str(index)}}])
        self.assertEqual({'public','mine'}, {j['id'] for j in qq_painting.available_images('qq:private:42','42')},
                         '后续浏览不能挤走仍可索取的最近画作')
        for index in range(7):
            art.save_painting_job({'id':'new'+str(index),'created_at':f'2026-11-09T13:0{index}:00+09:00',
                'status':'done','request':{'prompt':'松树林'+str(index)},'image_path':str(image),
                'destination':{'session_id':'qq:private:42','who':'42'}})
        latest = qq_painting.available_images('qq:private:42','42')
        self.assertEqual(6,len(latest))
        self.assertNotIn('public',{j['id'] for j in latest})
        found = qq_painting.available_images('qq:private:42','42',query='发虚构画作 public 那一张')
        self.assertEqual(6,len(found))
        self.assertTrue({'public','new6'} <= {j['id'] for j in found})
        private = qq_painting.available_images('qq:private:42','42',query='虚构画作 other private')
        self.assertFalse({'other','private','unattributed','failed','running','missing'} & {j['id'] for j in private})
        self.assertEqual(latest,qq_painting.available_images('qq:private:42','42',query='今天什么天气'))

    def test_painting_search_query_uses_current_speakers_whole_segment(self):
        from organs.mind import appraisal, llm
        segment = [{'who':'42','speaker':'甲','text':'找以前的鲸鱼图'},
                   {'who':'99','speaker':'乙','text':'松树林别人的话'},
                   {'who':'42','speaker':'甲','text':'算了，别发了'}]
        with patch.object(qq_painting,'available_images',return_value=[]) as available, \
             patch.object(llm,'complete',return_value=json.dumps({'kind':None,'evidence':'','reason':'取消索图'})):
            result = appraisal.assess(text='合并后的群聊',who='42',bot='鱼',scene='group',
                segment=segment,history=[],session_id='qq:group:700')
        available.assert_called_once_with('qq:group:700','42',query='找以前的鲸鱼图\n算了，别发了')
        self.assertIsNone(result['image_request'])

    def test_semantic_model_mode_reaches_painting_executor(self):
        from organs.mind import appraisal, llm
        from organs.hand import comfy
        text = '用模型给我生成一张鲸鱼插画，画完检查一下'
        reply = {'kind':None, 'evidence':'', 'reason':'请求生图', 'painting':{
            'action':'start', 'mode':'image', 'evidence':[text], 'prompt':'鲸鱼插画', 'visual_review':True}}
        with patch.object(comfy, 'config', return_value={'enabled':True, 'steps':25}), \
             patch.object(llm, 'complete', return_value=json.dumps(reply)) as complete:
            assessed = appraisal.assess(text=text, who='42', bot='鱼', scene='group',
                segment=[], history=[], session_id='qq:group:700')
        self.assertIn('image', json.loads(complete.call_args.args[0][-1]['content'])['drawing_modes'])
        self.assertEqual('image', assessed['painting']['mode'])
        with patch.object(stroke_painting, 'start', return_value={'id':'test', 'status':'running'}) as start:
            result = qq_painting.handle(text, intent=assessed['painting'], who='42', scene='group',
                target=700, session_id='qq:group:700', soul=None, hand_root=self.root/'hand')
        self.assertEqual('running', result['status'])
        self.assertEqual('image', start.call_args.args[0]['mode'])
        self.assertTrue(start.call_args.args[0]['visual_review'])
        self.assertIn('保留原图', result['say'])
        self.assertNotIn('改一次', result['say'])

    def test_semantic_outfit_selection_reaches_existing_composer(self):
        from organs.mind import appraisal, llm
        outfit = art.save_asset({'name': '白色针织套装', 'category': 'outfits',
                                 'description': '奶白麻花毛衣与蓝格纹裙'})
        text = '画一个穿白色针织套装的小人'
        reply = {'kind': None, 'evidence': '', 'reason': '请求画画', 'painting': {
            'action': 'start', 'evidence': [text], 'prompt': text, 'assets': [outfit['id']]}}
        with patch.object(llm, 'complete', return_value=json.dumps(reply)) as complete:
            assessment = appraisal.assess(text=text, who='42', bot='鱼', scene='group',
                segment=[], history=[], session_id='qq:group:700')
        packet = json.loads(complete.call_args.args[0][-1]['content'])
        self.assertEqual(outfit['id'], packet['drawing_assets'][0]['id'])
        with patch.object(stroke_painting, 'start', return_value={'id':'test', 'status':'running'}) as start:
            result = qq_painting.handle(text, intent=assessment['painting'], who='42', scene='group',
                target=700, session_id='qq:group:700', soul=None, hand_root=self.root/'hand')
            self.assertEqual('running', result['status'])
            composed = start.call_args.args[0]
            self.assertEqual(outfit['id'], composed['selected'][0]['id'])
            self.assertIn('奶白麻花毛衣与蓝格纹裙', composed['final_prompt'])
            assessment['painting']['assets'] = ['missing']
            start.reset_mock()
            result = qq_painting.handle(text, intent=assessment['painting'], who='42', scene='group',
                target=700, session_id='qq:group:700', soul=None, hand_root=self.root/'hand')
            self.assertIn('素材已不存在', result['say'])
            start.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "home").mkdir()
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"HOME_ROOT": str(self.root / "home"), "QQAGENT_HOME": str(self.root / "organs")}).start()
        self.calls = []
        def send(endpoint, body, timeout):
            self.calls.append(body)
            return 200, b'{"ok":true,"sent":1,"id":"test","reason_code":"sent"}'
        patch.object(channel, "_post_json", side_effect=send).start()

    def handle(self, text, who="42", session="qq:group:700", target=700, complete=None):
        return qq_painting.handle(text, who=who, session_id=session, scene="group", target=target, soul=None,
                                 hand_root=self.root / "hand", complete=complete or (lambda m: json.dumps(PROGRAM)))

    def finish(self, result):
        stroke_painting._FUTURE.result(timeout=5)
        return art.painting_job(result["job_id"])

    def test_natural_self_portrait_reuses_character_and_schedule_composition(self):
        from types import SimpleNamespace
        from organs.day import life
        soul = SimpleNamespace(snapshot=lambda: SimpleNamespace(identity=SimpleNamespace(
            display="小红花", appearance="蓝发蓝眼睛")))
        state = {"enabled": True, "current": {"outfit": "白色睡衣", "location": "书房"},
                 "day": "2026-11-07", "plan": {"revision": 1}, "now": "2026-11-07T10:00:00+09:00"}
        with patch.object(life, "view", return_value=state), patch.object(stroke_painting, "start",
                return_value={"id": "test", "status": "running"}) as start:
            qq_painting.handle("", intent={"action": "start", "prompt": "画现在的你", "self_portrait": True},
                who="42", scene="group", target=700, session_id="qq:group:700", soul=soul,
                hand_root=self.root / "hand")
            composed = start.call_args.args[0]
            self.assertIn("蓝发蓝眼睛", composed["final_prompt"])
            self.assertIn("白色睡衣", composed["final_prompt"])
            self.assertIn("书房", composed["final_prompt"])
            self.assertEqual(state["current"], composed["schedule"]["current"])

    def test_real_worker_hand_ledger_and_one_correct_group_send(self):
        result = self.handle("/画画 蓝色三角形")
        job = self.finish(result)
        self.assertEqual("done", job["status"])
        self.assertEqual("sent", job["delivery"]["status"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(700, self.calls[0]["target"])
        self.assertTrue(self.calls[0]["chunks"][0]["image"]["file"].endswith(".png"))
        self.assertIn("确认发出了", self.handle("/画作")["say"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual("", qq_painting.context("qq:group:701"))
        self.assertIn("蓝色三角形", qq_painting.context("qq:group:700"))

    def test_drawing_does_not_block_status_and_only_owner_can_cancel(self):
        entered, release = threading.Event(), threading.Event()
        def complete(messages):
            entered.set(); release.wait(4); return json.dumps(PROGRAM)
        result = self.handle("/画画 小猫", complete=complete)
        self.assertTrue(entered.wait(1))
        try:
            self.assertIn("还在画", self.handle("/画作")["say"])
            self.assertIn("没有你发起", self.handle("/停画", who="43")["say"])
            self.assertIn("没有你发起", self.handle("/停画", session="qq:group:701")["say"])
            self.assertIn("不再发送", qq_painting.handle("", intent={"action": "cancel"},
                who="42", scene="group", target=700, session_id="qq:group:700", soul=None)["say"])
            self.assertIn("停止", self.handle("/画作")["say"])
        finally:
            release.set()
            job = self.finish(result)
        self.assertEqual("cancelled", job["status"])
        self.assertEqual([], self.calls)

    def test_failed_drawing_notifies_once_and_cannot_claim_image_delivered(self):
        job = self.finish(self.handle("/画画 小猫", complete=lambda m: "not json"))
        self.assertEqual("failed", job["status"])
        self.assertEqual("failure_notice", job["delivery"]["kind"])
        self.assertEqual(1, len(self.calls))
        self.assertIn("没画成", self.calls[0]["chunks"][0]["text"])
        self.assertIn("绘图失败", self.handle("/画作")["say"])

    def test_uncertain_receipt_is_not_retried(self):
        with patch.object(channel, "_post_json", side_effect=channel.Unreachable("timeout")) as send:
            job = self.finish(self.handle("/画画 小猫"))
            self.assertEqual(1, send.call_count)
            self.assertEqual("done", job["status"])
            self.assertEqual("unconfirmed", job["delivery"]["status"])
            self.assertIn("不会自动重发", self.handle("/画作")["say"])
            self.assertEqual(1, send.call_count)

    def test_cancel_after_send_started_does_not_claim_message_withdrawn(self):
        entered, release = threading.Event(), threading.Event()
        def send(endpoint, body, timeout):
            entered.set(); release.wait(4)
            return 200, b'{"ok":true,"sent":1,"reason_code":"sent"}'
        with patch.object(channel, "_post_json", side_effect=send):
            result = self.handle("/画画 小猫")
            self.assertTrue(entered.wait(2))
            try:
                self.assertIn("不能保证撤回", self.handle("/停画")["say"])
                self.assertEqual("sending", art.painting_job(result["job_id"])["delivery"]["status"])
            finally:
                release.set()
                job = self.finish(result)
        self.assertEqual("sent", job["delivery"]["status"])

    def test_restart_during_delivery_is_uncertain_without_resubmitting(self):
        job = {"id": "0" * 32, "status": "done", "delivery": {"status": "sending"}}
        with patch.object(channel, "_post_json") as send:
            result = stroke_painting.state(job)
        self.assertEqual("uncertain", result["delivery"]["status"])
        send.assert_not_called()

    def test_no_destination_no_description_and_quoted_command_do_not_start(self):
        with patch.object(stroke_painting, "start") as start:
            self.assertIsNone(self.handle("他说 /画画 小猫"))
            self.assertIsNone(self.handle("/画画 小猫", session="panel"))
            self.assertIn("想画什么", self.handle("/画画")["say"])
            self.assertIn("没确定", self.handle("/画画 小猫", target="")["say"])
        start.assert_not_called()


class QQPaintingHTTPTest(unittest.TestCase):
    def test_image_and_failure_notice_go_through_real_local_http_receipts(self):
        with tempfile.TemporaryDirectory() as temp, helpers.channel_service(channel_id="local-receipt") as server:
            root = Path(temp)
            (root / "home").mkdir()
            with patch.dict("os.environ", {"HOME_ROOT": str(root / "home"), "QQAGENT_HOME": str(root / "organs"),
                                           "HAND_CHANNEL_URL": server.url}):
                for raw, expected in ((json.dumps(PROGRAM), "image"), ("invalid", "failure_notice")):
                    response = qq_painting.handle("/画画 三角形", who="42", scene="private", target="42",
                        session_id="qq:private:42", soul=None, hand_root=root / "hand", complete=lambda m: raw)
                    stroke_painting._FUTURE.result(timeout=5)
                    job = art.painting_job(response["job_id"])
                    self.assertEqual("sent", job["delivery"]["status"])
                    self.assertEqual(expected, job["delivery"]["kind"])
            self.assertEqual(2, len(server.records))
            for row in server.records:
                self.assertEqual("/say", row["path"])
                self.assertEqual("private", row["body"]["scene"])
                self.assertEqual(42, row["body"]["target"])
            self.assertIn("image", server.records[0]["body"]["chunks"][0])
            self.assertIn("没画成", server.records[1]["body"]["chunks"][0]["text"])
