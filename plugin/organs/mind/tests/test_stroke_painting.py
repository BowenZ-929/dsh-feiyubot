import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from organs.home import art
from organs.hand import strokes, comfy
from organs.hand.tests.test_strokes import PROGRAM
from organs.mind import stroke_painting


class PaintingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.request = {"mode":"strokes", "prompt":"三角", "final_prompt":"三角", "aspect":"1:1"}

    def start(self, complete):
        return stroke_painting.start(self.request, home_root=self.root/"home", hand_root=self.root/"hand", complete=complete)

    def test_background_job_and_one_active_request(self):
        entered, release = threading.Event(), threading.Event()
        def complete(messages):
            entered.set(); release.wait(3); return json.dumps(PROGRAM)
        job = self.start(complete)
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaisesRegex(art.ArtError,"已有"):
                self.start(complete)
        finally:
            release.set(); stroke_painting._FUTURE.result(timeout=5)
        result = art.painting_job(job["id"], root=self.root/"home")
        self.assertEqual("done", result["status"])
        self.assertEqual(strokes.render(result["program"]), Path(result["image_path"]).read_bytes())
        self.assertFalse(result["visual_feedback"])
        self.assertEqual(1, result["planning_rounds"])

    def test_model_image_reuses_job_store_without_claiming_strokes(self):
        png = strokes.render(PROGRAM)
        directory = art.library_root(self.root/'home') / 'images'
        directory.mkdir(parents=True)
        (directory/'reference.png').write_bytes(png)
        request = dict(self.request, mode='image', references=[{'file':'reference.png'}])
        with patch.object(comfy, 'config', return_value={'enabled':True, 'steps':25}), \
             patch.object(comfy, 'generate', return_value=(png, {'prompt_id':'local-job', 'seed':42})) as generate:
            job = stroke_painting.start(request, home_root=self.root/'home', hand_root=self.root/'hand')
            stroke_painting._FUTURE.result(timeout=5)
        result = art.painting_job(job['id'], root=self.root/'home')
        self.assertEqual('done', result['status'])
        self.assertEqual(png, Path(result['image_path']).read_bytes())
        self.assertEqual(comfy.MODEL, result['model'])
        self.assertEqual('local-job', result['generation']['prompt_id'])
        self.assertNotIn('program', result)
        self.assertNotIn('model_turn_id', result)
        self.assertEqual(0, result['planning_rounds'])
        self.assertEqual('reference_images', result['input_mode'])
        self.assertEqual([png], generate.call_args.kwargs['references'])
        self.assertEqual(25, generate.call_args.kwargs['steps'])

    def test_review_receives_actual_dimensions_and_vision_does_not_receive_expected_content(self):
        for mode in ('strokes', 'image'):
            with self.subTest(mode=mode):
                seen, checks = [], []
                request = dict(self.request, mode=mode, aspect='3:4', visual_review=True,
                    final_prompt='三角形；参考配色是蓝色；要求比例3:4')
                def inspect(raw, config, key, **kwargs):
                    seen.append(kwargs)
                    return {'status':'ok', 'observation':{'subject':'蓝色三角形'},
                            'image':{'size':[999,999]}}
                def complete(messages):
                    if 'canvas_observation' not in messages[-1]['content']:
                        return json.dumps(PROGRAM)
                    checks.append(json.loads(messages[-1]['content']))
                    return json.dumps({'action':'keep','reason':'保留原图'})
                with patch.object(comfy, 'config', return_value={'enabled':True,'steps':25}), \
                     patch.object(comfy, 'generate', return_value=(strokes.render(PROGRAM,aspect='3:4'),{})), \
                     patch.object(stroke_painting.vision,'load_config',return_value={'enabled':True}), \
                     patch.object(stroke_painting.vision,'read_key',return_value='fixture'), \
                     patch.object(stroke_painting.vision,'describe',side_effect=inspect), \
                     patch.object(stroke_painting.vision,'record'):
                    started=stroke_painting.start(request, home_root=self.root/'home',
                        hand_root=self.root/'hand',complete=complete)
                    stroke_painting._FUTURE.result(timeout=5)
                job=art.painting_job(started['id'],root=self.root/'home')
                self.assertEqual('ok',job['review']['status'])
                self.assertEqual([480,640],checks[0].get('canvas_size'))
                self.assertEqual([480,640],job['review']['canvas_size'])
                self.assertEqual(request['final_prompt'],checks[0]['request'])
                self.assertEqual('',seen[0].get('focus',''))
                self.assertTrue(seen[0].get('painting_review'))

    def test_model_image_review_observes_actual_image_and_preserves_it(self):
        png = strokes.render(PROGRAM)
        for outcome in ('ok', 'failed', 'cancelled'):
            with self.subTest(outcome=outcome):
                pictures, checks, sent = [], [], []
                def inspect(raw):
                    pictures.append(raw)
                    if outcome == 'failed':
                        raise OSError('offline')
                    if outcome == 'cancelled':
                        stroke_painting.cancel(stroke_painting._ACTIVE)
                    return {'status':'ok', 'observation':{'subject':'蓝色三角形', 'scene':'白底'}}
                def complete(messages):
                    checks.append(messages)
                    return json.dumps({'action':'keep', 'reason':'实际图形为蓝色，与要求的红色不符；保留原图并说明。'})
                def deliver(job):
                    sent.append(job['image_path'])
                    return {'status':'sent', 'kind':'image'}
                with patch.object(comfy, 'config', return_value={'enabled':True, 'steps':25}), \
                     patch.object(comfy, 'generate', return_value=(png, {'prompt_id':'local-image'})):
                    started = stroke_painting.start(dict(self.request, mode='image', final_prompt='红色三角形', visual_review=True),
                        home_root=self.root/'home', hand_root=self.root/'hand', complete=complete,
                        inspect_canvas=inspect, on_done=deliver,
                        destination={'who':'42','scene':'private','target':42,'session_id':'qq:private:42'})
                    stroke_painting._FUTURE.result(timeout=5)
                job = art.painting_job(started['id'], root=self.root/'home')
                self.assertEqual(1, len(pictures))
                self.assertEqual(png, pictures[0])
                self.assertEqual('cancelled' if outcome == 'cancelled' else 'done', job['status'])
                self.assertEqual(outcome, job['review']['status'])
                self.assertEqual(png, Path(job['draft_image_path']).read_bytes())
                self.assertNotIn('program', job)
                self.assertEqual([] if outcome == 'cancelled' else [job['image_path']], sent)
                if outcome == 'ok':
                    self.assertEqual(1, len(checks))
                    self.assertIn('红色三角形', checks[0][1]['content'])
                    self.assertIn('蓝色三角形', checks[0][1]['content'])
                    self.assertEqual(job['id'], job['model_turn_id'])
                    self.assertEqual(job['draft_image_path'], job['image_path'])
                    self.assertTrue(job['visual_feedback'])
                    from organs.mind import qq_painting
                    with patch.dict('os.environ', {'HOME_ROOT':str(self.root/'home')}):
                        context = qq_painting.context('qq:private:42')
                        self.assertIn('蓝色三角形', context)
                        self.assertIn('"最终图沿用已观察初稿": true', context)
                        self.assertEqual('', qq_painting.context('qq:private:43'))
                else:
                    self.assertEqual([], checks)

    def test_model_cancel_still_prevents_delivery(self):
        entered, release = threading.Event(), threading.Event()
        def generate(*args, **kwargs):
            entered.set(); release.wait(3)
            self.assertTrue(kwargs['cancel'].is_set())
            raise comfy.ImageError('绘画已取消')
        delivered = []
        with patch.object(comfy, 'config', return_value={'enabled':True, 'steps':25}), \
             patch.object(comfy, 'generate', side_effect=generate):
            job = stroke_painting.start(dict(self.request, mode='image', references=[]),
                home_root=self.root/'home', hand_root=self.root/'hand', on_done=delivered.append)
            self.assertTrue(entered.wait(2))
            try:
                self.assertTrue(stroke_painting.cancel(job['id']))
            finally:
                release.set(); stroke_painting._FUTURE.result(timeout=5)
        self.assertEqual([], delivered)
        self.assertEqual('cancelled', art.painting_job(job['id'], root=self.root/'home')['status'])
        self.assertNotIn('error_detail',art.painting_job(job['id'],root=self.root/'home'))

    def test_failed_job_retains_raw_output_and_allows_next_request(self):
        job = self.start(lambda messages: "not json")
        stroke_painting._FUTURE.result(timeout=3)
        result=art.painting_job(job["id"],root=self.root/"home")
        self.assertEqual("failed",result["status"])
        self.assertEqual("not json",result["raw_response"])
        self.assertEqual('笔触校验',result['error_stage'])
        self.assertEqual('invalid_result',result['error_kind'])
        self.assertEqual([("planning", "running"), ("finished", "failed")],
                         [(e["kind"], e["status"]) for e in result["events"]])
        job=self.start(lambda messages:json.dumps(PROGRAM))
        stroke_painting._FUTURE.result(timeout=3)
        self.assertEqual("done",art.painting_job(job["id"],root=self.root/"home")["status"])

    def test_planning_timeout_has_safe_feedback(self):
        def complete(messages):
            raise stroke_painting.llm.LLMError('timeout','/private/test-secret')
        job=self.start(complete)
        stroke_painting._FUTURE.result(timeout=3)
        result=art.painting_job(job['id'],root=self.root/'home')
        self.assertEqual('failed',result['status'])
        self.assertEqual('规划笔触',result['error_stage'])
        self.assertEqual('timeout',result['error_kind'])
        self.assertEqual('规划笔触：请求超时',result['error_detail'])

    def test_local_generation_failure_is_not_a_bad_stroke_program(self):
        from urllib.error import URLError
        self.request['mode']='image'
        for error,kind in [(comfy.ImageError('本地模型生成失败'),'execution'),
                           (URLError(TimeoutError('/private/test-secret')),'timeout')]:
            with self.subTest(kind=kind), patch.object(comfy,'config',return_value={'enabled':True,'steps':25}), \
                 patch.object(comfy,'generate',side_effect=error):
                job=self.start(None)
                stroke_painting._FUTURE.result(timeout=3)
            result=art.painting_job(job['id'],root=self.root/'home')
            self.assertEqual('本地模型生图',result['error_stage'])
            self.assertEqual(kind,result['error_kind'])
            self.assertNotIn('/private/test-secret',result['error_detail'])

    def test_previous_process_running_state_is_not_silently_resubmitted(self):
        self.assertEqual("interrupted",stroke_painting.state({"id":"0"*32,"status":"running"})["status"])

    def test_disabled_model_image_is_not_started(self):
        self.request['mode']='image'
        with patch.object(comfy,'config',return_value={'enabled':False,'steps':25}), \
             patch.object(comfy,'generate') as generate:
            with self.assertRaises(art.ArtError):
                self.start(lambda messages:'bad')
        generate.assert_not_called()

    def test_visual_review_uses_rendered_draft_and_retains_both_programs(self):
        revised = json.loads(json.dumps(PROGRAM)); revised['background']='#FFEECC'
        calls=[]; pictures=[]
        def complete(messages):
            calls.append(messages)
            return json.dumps(PROGRAM if len(calls)==1 else {'action':'revise','reason':'背景改为暖色','program':revised})
        def inspect(png):
            pictures.append(png)
            return {'status':'ok','observation':{'subject':'三角形','scene':'白色背景'}}
        self.request['visual_review']=True
        job=stroke_painting.start(self.request,home_root=self.root/'home',hand_root=self.root/'hand',
                                  complete=complete,inspect_canvas=inspect)
        stroke_painting._FUTURE.result(timeout=3)
        result=art.painting_job(job['id'],root=self.root/'home')
        self.assertEqual([strokes.render(strokes.validate(PROGRAM))],pictures)
        self.assertEqual('done',result['status'])
        self.assertEqual('revise',result['review']['action'])
        self.assertEqual(strokes.validate(PROGRAM),result['draft_program'])
        self.assertEqual(strokes.validate(revised),result['program'])
        self.assertNotEqual(Path(result['draft_image_path']).read_bytes(),Path(result['image_path']).read_bytes())
        self.assertIn('白色背景',calls[1][1]['content'])
        self.assertTrue(result['visual_feedback'])
        self.assertEqual(2,result['planning_rounds'])

    def test_review_keep_failure_and_stop_preserve_draft(self):
        for outcome in ('keep','invalid','stopped'):
            with self.subTest(outcome=outcome):
                calls=[]
                def complete(messages):
                    calls.append(messages)
                    return json.dumps(PROGRAM if len(calls)==1 else
                        {'action':'keep','reason':'轮廓已经清楚'} if outcome=='keep' else {'action':'revise','reason':'改图','program':{}})
                def inspect(png):
                    if outcome=='stopped':stroke_painting.cancel(stroke_painting._ACTIVE)
                    return {'status':'ok','observation':{'subject':'三角形'}}
                self.request['visual_review']=True
                job=stroke_painting.start(self.request,home_root=self.root/'home',hand_root=self.root/'hand',
                                          complete=complete,inspect_canvas=inspect)
                stroke_painting._FUTURE.result(timeout=3)
                result=art.painting_job(job['id'],root=self.root/'home')
                self.assertEqual(strokes.validate(PROGRAM),result['program'])
                self.assertEqual('cancelled' if outcome=='stopped' else 'done',result['status'])
                self.assertEqual(1 if outcome=='stopped' else 2,len(calls))
                self.assertEqual('cancelled' if outcome=='stopped' else 'failed' if outcome=='invalid' else 'ok',result['review']['status'])

    def test_missing_vision_is_rejected_before_planning(self):
        self.request['visual_review']=True
        with patch.object(stroke_painting.vision,'load_config',return_value={'enabled':False}), \
             patch.object(stroke_painting.vision,'read_key',return_value=''), \
             patch.object(stroke_painting.llm,'complete') as complete:
            with self.assertRaisesRegex(art.ArtError,'看图服务'):
                self.start(complete)
            complete.assert_not_called()

    def test_phase_history_tracks_actual_review_and_delivery_outcomes(self):
        for outcome in ('keep', 'revise', 'failed', 'cancelled'):
            with self.subTest(outcome=outcome):
                calls, sent, waiting = [], [], []
                def complete(messages):
                    calls.append(messages)
                    if len(calls) == 1:
                        return json.dumps(PROGRAM)
                    return json.dumps({'action': outcome, 'reason': '检查理由', 'program': PROGRAM})
                def inspect(png):
                    current = art.painting_job(stroke_painting._ACTIVE, root=self.root/'home')
                    waiting.extend(current.get('events', []))
                    if outcome == 'failed':
                        raise OSError('private service detail')
                    if outcome == 'cancelled':
                        stroke_painting.cancel(stroke_painting._ACTIVE)
                    return {'status': 'ok', 'observation': {'subject': '三角形'}}
                def deliver(job):
                    sent.append(job['id'])
                    return {'status': 'unconfirmed', 'kind': 'image', 'receipt': {'private': 'secret'}}
                self.request['visual_review'] = True
                started = stroke_painting.start(self.request, home_root=self.root/'home', hand_root=self.root/'hand',
                                               complete=complete, inspect_canvas=inspect, on_done=deliver)
                stroke_painting._FUTURE.result(timeout=3)
                job = art.painting_job(started['id'], root=self.root/'home')
                events = job.get('events', [])
                self.assertEqual(('observation', 'running'), (waiting[-1]['kind'], waiting[-1]['status']) if waiting else None)
                self.assertEqual(['planning', 'planned', 'draft', 'observation'], [e['kind'] for e in waiting])
                self.assertTrue(all(e.get('at') for e in events))
                terminal = next(e for e in events if e['kind'] == 'finished')
                self.assertEqual('cancelled' if outcome == 'cancelled' else 'done', terminal['status'])
                if outcome == 'cancelled':
                    self.assertEqual([], sent)
                    self.assertNotIn('delivery', [e['kind'] for e in events])
                else:
                    self.assertEqual([('delivery', 'sending'), ('delivery', 'unconfirmed')],
                                     [(e['kind'], e['status']) for e in events[-2:]])
                    review = [e for e in events if e['kind'] == 'review'][-1]
                    self.assertEqual('failed' if outcome == 'failed' else 'ok', review['status'])
                    self.assertEqual(outcome == 'revise', any(e['kind'] == 'revision' for e in events))
                    if outcome == 'revise':
                        self.assertLess(events.index(review), next(i for i, e in enumerate(events) if e['kind'] == 'revision'))
                self.assertNotIn('secret', json.dumps(events))
                self.assertNotIn('private service detail', json.dumps(events))

    def test_vision_network_failure_keeps_rendered_draft(self):
        self.request['visual_review']=True
        with patch.object(stroke_painting.llm,'complete',return_value=json.dumps(PROGRAM)) as complete:
            job=stroke_painting.start(self.request,home_root=self.root/'home',hand_root=self.root/'hand',
                                      complete=complete,inspect_canvas=lambda png: (_ for _ in ()).throw(OSError('offline')))
            stroke_painting._FUTURE.result(timeout=3)
        result=art.painting_job(job['id'],root=self.root/'home')
        self.assertEqual('done',result['status'])
        self.assertEqual('failed',result['review']['status'])
        self.assertEqual(1,complete.call_count)
        self.assertEqual(result['draft_image_path'],result['image_path'])

    def test_background_model_round_links_planning_review_and_failure(self):
        from organs.mind import llm
        config={'provider':'fake','model':'fake','base_url':'http://example.invalid/v1',
                'key_env':'FAKE_KEY','timeout_ms':1000,'retries':0,'temperature':0,'max_tokens':100}
        replies=[json.dumps(PROGRAM),json.dumps({'action':'keep','reason':'轮廓清楚'})]
        def transport(*args):
            if not replies: raise TimeoutError('synthetic timeout')
            return 200,json.dumps({'choices':[{'message':{'content':replies.pop(0)}}]}).encode()
        with patch.dict('os.environ',{'QQAGENT_TRACE':'1','QQAGENT_HOME_ROOT':str(self.root)}), \
             patch.object(llm,'load_config',return_value=config),patch.object(llm,'read_key',return_value='fake'), \
             patch.object(llm,'default_transport',side_effect=transport):
            with llm.trace_round('f'*32,'caller'):
                first=stroke_painting.start(dict(self.request,visual_review=True),home_root=self.root/'home',
                    hand_root=self.root/'hand',inspect_canvas=lambda _: {'status':'ok','observation':{'subject':'三角形'}})
                stroke_painting._FUTURE.result(timeout=3)
                second=stroke_painting.start(self.request,home_root=self.root/'home',hand_root=self.root/'hand')
                stroke_painting._FUTURE.result(timeout=3)
                self.assertEqual(llm._ROUND_TRACE.get()['turn_id'],'f'*32)
        jobs=[art.painting_job(j['id'],root=self.root/'home') for j in (first,second)]
        self.assertEqual([j['status'] for j in jobs],['done','failed'])
        self.assertEqual([j.get('model_turn_id') for j in jobs],[first['id'],second['id']])
        rows=[json.loads(line) for p in (self.root/'mind/trace').glob('*.jsonl') for line in p.read_text().splitlines()]
        self.assertEqual([r['turn_id'] for r in rows],[first['id'],first['id'],second['id']])
        self.assertEqual([r['call_index'] for r in rows],[1,2,1])
        self.assertEqual([r['purpose'] for r in rows],['规划绘画笔触','检查画布并决定改稿','规划绘画笔触'])
        self.assertEqual(rows[-1]['error'],'timeout')
