import json
import unittest
from pathlib import Path
from unittest.mock import patch
from organs.brain.tests.helpers import fresh_store
from organs.day import growth, phone_browse, solitude

AT = '2026-11-08T12:00:00+09:00'
AWAKE = {'asleep': False, 'energy': {'value': 80}, 'mood': {'tier': '平常'}}


class AutonomyTest(unittest.TestCase):
    def test_read_own_painting_records_new_observation_without_rewriting_old_job(self):
        from organs.day import autonomy
        from organs.home import art, cards
        from organs.hand import store as hand_store
        from organs.eye import vision
        from organs.heart import events
        import io
        from PIL import Image
        data=io.BytesIO();Image.new('RGB',(16,16),'blue').save(data,format='PNG')
        image=hand_store.land_draw(data.getvalue(),root=self.root/'hand')
        ident='d'*32
        job={'id':ident,'status':'done','created_at':AT,'image_path':str(image),
             'request':{'prompt':'旧画面要求不能当视觉输入'},'review':{'reason':'原检查未确认图案'}}
        art.save_painting_job(job,root=self.root/'home')
        card=cards.append_cards(self.root/'home',role='organ',by='日子线·自主活动',cards=[{
            'room':'工作台','kind':'物件','name':'已完成画作',
            'params':{'scope':'public','text':'旧观察未确认图案','action':'paint','status':'done','job_id':ident}}])['cards'][0]
        observed={'status':'ok','observation':{'subject':'白毛衣上有蓝色鲸鱼','scene':'桌旁',
            'visible_text':'','meaning':'','uncertainty':'鞋未入画','sensitive':False},'image':{'sha256':'fixture'}}
        with patch.object(vision,'load_config',return_value={'enabled':True}),patch.object(vision,'read_key',return_value='test-key'),patch.object(vision,'describe',return_value=observed) as describe,patch.object(vision,'record') as record:
            result=self.tick(json.dumps({'action':'read','reason':'再看一眼自己的画','args':{'id':card['card_id']},'share':True}))
        self.assertEqual('done',result['status'])
        self.assertEqual('painting',result['receipt']['kind'])
        self.assertEqual('白毛衣上有蓝色鲸鱼',result['receipt']['observation']['subject'])
        self.assertEqual(card['card_id'],result['receipt']['based_on'])
        self.assertEqual(ident,result['receipt']['job_id'])
        describe.assert_called_once_with(data.getvalue(),{'enabled':True},'test-key',painting_review=True)
        self.assertEqual(ident,record.call_args.args[0]['painting_id'])
        self.assertEqual('painting_observation',autonomy.own_activities(self.root/'home')[-1]['action'])
        self.assertIn('白毛衣上有蓝色鲸鱼',autonomy.context(root=self.root))
        material=growth.materials(self.brain,AT,root=self.root)
        self.assertTrue(any(r.get('job_id')==ident and r.get('action')=='painting_observation' for r in material))
        self.assertEqual(job,art.painting_job(ident,root=self.root/'home'))
        self.assertEqual(data.getvalue(),image.read_bytes())
        self.assertFalse((self.root/'proactive_own_doing.jsonl').exists())
        cid=result['receipt']['card_id']
        response={'action':'idle','reason':'停一会儿','args':{},'reaction':{'id':cid,'effect':'positive',
            'impression':'重新看清了图案','evidence':'白毛衣上有蓝色鲸鱼'}}
        later=self.tick(json.dumps(response),'2026-11-09T12:00:00+09:00')
        self.assertTrue(later['reaction']['mood']['ok'])
        self.assertEqual('autonomy:'+cid,events.recent(self.root/'heart',now='2026-11-09T12:00:00+09:00')[0]['source_id'])

    def test_painting_read_does_not_offer_private_unfinished_external_or_missing_files(self):
        from organs.home import art, cards
        from organs.hand import store as hand_store
        from organs.eye import vision
        for index,condition in enumerate(('private','unfinished','external','missing','disabled')):
            with self.subTest(condition=condition):
                root=self.root/condition;home=root/'home'
                growth.configure(True,root=root)
                image=hand_store.land_draw(b'local-image',root=root/'hand')
                if condition=='external':
                    image=root/'outside.png';image.write_bytes(b'outside')
                if condition=='missing':image.unlink()
                ident=str(index)*32
                art.save_painting_job({'id':ident,'status':'running' if condition=='unfinished' else 'done',
                    'created_at':AT,'image_path':str(image),'request':{'prompt':'画作'}},root=home)
                card=cards.append_cards(home,role='organ',by='日子线·自主活动',cards=[{
                    'room':'工作台','kind':'物件','name':'画作','params':{'scope':'private' if condition=='private' else 'public',
                    'text':'画作','action':'paint','status':'done','job_id':ident}}])['cards'][0]
                calls=[]
                def choose(messages):
                    calls.append(messages)
                    return json.dumps({'action':'read','reason':'看看','args':{'id':card['card_id']}})
                from organs.day import autonomy
                with patch.object(vision,'load_config',return_value={'enabled':condition!='disabled'}),patch.object(vision,'read_key',return_value='test-key'),patch.object(vision,'describe') as describe:
                    result=autonomy.tick(AT,root=root,brain=self.brain,complete=choose)
                self.assertEqual('failed',result['status'])
                self.assertEqual([],json.loads(calls[0][1]['content'])['paintings'])
                self.assertNotIn(str(image),calls[0][1]['content'])
                describe.assert_not_called()

    def test_painting_read_failure_keeps_old_receipt_and_does_not_create_observation(self):
        from organs.day import autonomy
        from organs.home import art, cards
        from organs.hand import store as hand_store
        from organs.eye import vision
        image=hand_store.land_draw(b'local-image',root=self.root/'hand');ident='e'*32
        art.save_painting_job({'id':ident,'status':'done','created_at':AT,'image_path':str(image),
                              'request':{'prompt':'画作'}},root=self.root/'home')
        card=cards.append_cards(self.root/'home',role='organ',by='日子线·自主活动',cards=[{
            'room':'工作台','kind':'物件','name':'画作','params':{'scope':'public','text':'原观察',
            'action':'paint','status':'done','job_id':ident}}])['cards'][0]
        before=autonomy.own_activities(self.root/'home')
        for i,outcome in enumerate((OSError('private-path'),{'status':'sensitive'},IndexError('private-provider-envelope'))):
            with patch.object(vision,'load_config',return_value={'enabled':True}),patch.object(vision,'read_key',return_value='test-key'),patch.object(vision,'describe',side_effect=outcome if isinstance(outcome,Exception) else None,return_value=outcome),patch.object(vision,'record'):
                result=self.tick(json.dumps({'action':'read','reason':'重看','args':{'id':card['card_id']}}),f'2026-11-{8+i:02d}T12:00:00+09:00')
            self.assertEqual('failed',result['status'])
            self.assertNotIn('receipt',result)
            self.assertNotIn('private-path',json.dumps(result))
            self.assertEqual(before,autonomy.own_activities(self.root/'home'))

    def test_failed_painting_worker_feedback_survives_settlement(self):
        from organs.day import autonomy
        from organs.mind import stroke_painting, llm
        def fail(messages):
            raise llm.LLMError('timeout','/private/test-secret')
        job=stroke_painting.start({'mode':'strokes','prompt':'小鲸鱼','final_prompt':'小鲸鱼','aspect':'1:1'},
            home_root=self.root/'home',hand_root=self.root/'hand',complete=fail)
        stroke_painting._FUTURE.result(timeout=3)
        solitude.append_log(self.root,{'at':AT,'event':'autonomy_result','model_turn_id':'paint-choice',
            'action':'paint','status':'queued','share':False,'receipt':{'id':job['id']}})
        finished=autonomy.settle(AT,root=self.root)[0]
        self.assertEqual('规划笔触：请求超时',finished['error_detail'])
        self.tick(json.dumps({'action':'idle','reason':'先休息','args':{}}),'2026-11-08T15:00:00+09:00')
        packet=json.loads(self.calls[0][1]['content'])
        self.assertEqual('timeout',packet['recent_choices'][-1]['error_kind'])
        self.assertIn('规划笔触：请求超时',json.dumps(packet['experiences'],ensure_ascii=False))
        self.assertNotIn('/private/test-secret',json.dumps(packet,ensure_ascii=False))

    def test_async_failure_reaches_work_receipt_and_next_choice_without_raw_error(self):
        from organs.day import autonomy
        feedback={'error_stage':'画面理解','error_kind':'timeout','error_detail':'画面理解：请求超时'}
        phone_browse.save({'id':'failed-view','created_at':AT,'status':'failed','origin':'autonomy',
            'observations':[],'note':'/private/test-secret',**feedback},self.root)
        solitude.append_log(self.root,{'at':AT,'event':'autonomy_result','model_turn_id':'choice',
            'action':'browse','status':'queued','share':False,'receipt':{'id':'failed-view'}})
        finished=autonomy.settle(AT,root=self.root)[0]
        for key,value in feedback.items():
            self.assertEqual(value,finished[key])
        self.assertIn(feedback['error_detail'],autonomy.own_activities(self.root/'home')[0]['text'])
        self.assertEqual([],autonomy.settle(AT,root=self.root))
        self.tick(json.dumps({'action':'idle','reason':'先歇歇','args':{}}),'2026-11-08T15:00:00+09:00')
        packet=json.loads(self.calls[0][1]['content'])
        self.assertEqual('timeout',packet['recent_choices'][-1]['error_kind'])
        self.assertEqual('画面理解',packet['recent_choices'][-1]['error_stage'])
        self.assertNotIn('/private/test-secret',json.dumps(packet,ensure_ascii=False))

    def test_failed_activity_and_idle_are_available_to_next_choice_without_fake_work(self):
        failed = self.tick(json.dumps({'action':'write','reason':'想写一点','args':{'text':'正文','finish':True}}))
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(failed.get('action'),'write')
        self.assertIn('完成需要指定自己的打算',failed.get('error_detail',''))
        self.tick(json.dumps({'action':'idle','reason':'想歇一会儿','args':{}}),'2026-11-08T15:00:00+09:00')
        self.tick(json.dumps({'action':'idle','reason':'继续休息','args':{}}),'2026-11-09T12:00:00+09:00')
        packet=json.loads(self.calls[0][1]['content'])
        self.assertEqual([(r['action'],r['status']) for r in packet['recent_choices']],
                         [('write','failed'),('idle','done')])
        self.assertIn('完成需要指定自己的打算',packet['recent_choices'][0]['error_detail'])
        self.assertEqual(packet['writings'],[])

    def test_recent_choices_are_bounded_and_do_not_copy_args_reasons_or_future(self):
        for i in range(10):
            solitude.append_log(self.root,{'at':f'2026-11-07T12:{i:02d}:00+09:00',
                'event':'autonomy_result','model_turn_id':str(i),'action':'plan','status':'done',
                'reason':'在屋里散步是未核验理由','args':{'private':'不应复制的参数'}})
        solitude.append_log(self.root,{'at':'2026-11-10T12:00:00+09:00',
            'event':'autonomy_result','action':'write','status':'done'})
        self.tick(json.dumps({'action':'idle','reason':'歇一会儿','args':{}}))
        recent=json.loads(self.calls[0][1]['content'])['recent_choices']
        self.assertEqual(len(recent),8)
        self.assertEqual(recent[0]['model_turn_id'],'2')
        self.assertNotIn('private',json.dumps(recent))
        self.assertTrue(all('reason' not in r and r['action']=='plan' for r in recent))

    def test_paused_plan_rejection_is_visible_and_can_be_resumed_next_time(self):
        from organs.home import intentions
        plan=intentions.apply({'name':'纸船小诗','status':'paused'},root=self.root/'home')
        failed=self.tick(json.dumps({'action':'write','reason':'继续','args':{
            'text':'小纸船慢慢漂','intention_id':plan['card_id'],'finish':True}}))
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(intentions.listing(self.root/'home')[0]['status'],'paused')
        resumed=self.tick(json.dumps({'action':'plan','reason':'先恢复','args':{
            'card_id':plan['card_id'],'status':'active'}}),'2026-11-09T12:00:00+09:00')
        self.assertEqual(resumed['status'],'done')
        packet=json.loads(self.calls[0][1]['content'])
        self.assertIn('进行中卡片',packet['recent_choices'][0]['error_detail'])
        self.assertEqual(intentions.listing(self.root/'home')[0]['status'],'active')
        self.assertEqual(packet['writings'],[])

    def test_autonomous_painting_uses_existing_reference_library(self):
        from organs.home import art
        outfit = art.save_asset({'name':'雪花披风', 'category':'outfits',
                                'description':'深蓝披风配白绒边'}, root=self.root/'home')
        choice = {'action':'paint', 'reason':'想画一套冬装', 'args': {
            'prompt':'画一个穿雪花披风的小人', 'assets':[outfit['id']]}}
        with patch('organs.mind.stroke_painting.start', return_value={'id':'test', 'status':'queued'}) as start:
            result = self.tick(json.dumps(choice))
            self.assertEqual('queued', result['status'])
            composed = start.call_args.args[0]
            self.assertEqual(composed['mode'],'strokes')
            self.assertIn('深蓝披风配白绒边', composed['final_prompt'])
            self.assertEqual(outfit['id'], json.loads(self.calls[0][1]['content'])['drawing_assets'][0]['id'])
            self.assertNotIn('destination', start.call_args.kwargs)

    def test_autonomous_painting_can_choose_enabled_local_model(self):
        choice = {'action':'paint','reason':'想画水彩','args':{'mode':'image','prompt':'一只小鲸鱼'}}
        with patch('organs.hand.comfy.config', return_value={'enabled':True,'steps':25}), \
             patch('organs.mind.stroke_painting.start', return_value={'id':'test','status':'running'}) as start:
            result = self.tick(json.dumps(choice))
        self.assertEqual(result['status'],'queued')
        self.assertEqual(start.call_args.args[0]['mode'],'image')
        self.assertEqual(result['mode'],'image')
        packet = json.loads(self.calls[0][1]['content'])
        self.assertEqual(packet['drawing_modes'],['strokes','image'])
        self.assertNotIn('destination',start.call_args.kwargs)

    def test_autonomous_painting_can_choose_whether_to_inspect_canvas(self):
        for selected in (True, False):
            with self.subTest(selected=selected), patch('organs.hand.comfy.config', return_value={'enabled':True,'steps':25}), \
                 patch('organs.mind.stroke_painting.start', return_value={'id':'test','status':'running'}) as start:
                choice = {'action':'paint','reason':'自己决定是否回看','args':{
                    'mode':'image','prompt':'小鲸鱼','visual_review':selected}}
                result = self.tick(json.dumps(choice), '2026-11-08T12:00:00+09:00' if selected else '2026-11-09T12:00:00+09:00')
                self.assertEqual('queued', result['status'])
                self.assertEqual(selected, start.call_args.args[0]['visual_review'])
                self.assertNotIn('on_done', start.call_args.kwargs)
                self.assertNotIn('destination', start.call_args.kwargs)

    def test_disabled_model_not_offered_or_started(self):
        choice = {'action':'paint','reason':'想试试','args':{'mode':'image','prompt':'小鲸鱼'}}
        with patch('organs.hand.comfy.config', return_value={'enabled':False,'steps':25}), \
             patch('organs.mind.stroke_painting.start') as start:
            result = self.tick(json.dumps(choice))
        self.assertEqual(result['status'],'failed')
        start.assert_not_called()
        self.assertEqual(json.loads(self.calls[0][1]['content'])['drawing_modes'],['strokes'])

    def setUp(self):
        self.brain, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'day'
        growth.configure(True, root=self.root)
        self.addCleanup(patch.stopall)
        patch('organs.day.phone_browse._feeling', return_value=AWAKE).start()
        patch('organs.day.phone_browse._ACTIVE', None).start()
        patch('organs.mind.stroke_painting._ACTIVE', None).start()
        patch('organs.hand.phone.config', return_value={'serial': 'test', 'isolated_for_bot': False}).start()

    def tick(self, output, at=AT):
        from organs.day import autonomy
        self.calls = []
        def complete(messages):
            self.calls.append(messages)
            return output
        return autonomy.tick(at, root=self.root, brain=self.brain, persona='当前性情', complete=complete)

    def test_own_work_is_present_once_without_losing_actual_or_private_boundaries(self):
        from organs.home import cards
        work = cards.append_cards(self.root/'home', role='organ', by='日子线·自主写作', cards=[{
            'room':'工作台', 'kind':'物件', 'name':'童话',
            'params':{'scope':'public', 'text':'纸船变成了月亮。'}}])['cards'][0]
        public = self.brain.ingest('12345678', '公开分享薄荷照片', ts=AT,
                                   scene='group', audience='公开')['entry']
        self.brain.ingest('12345678', '私聊紫茄子', ts=AT, scene='private',
                          audience='公开', session_id='qq:private:12345678')
        self.brain.ingest('12345678', '群内当事人内容', ts=AT, scene='group', audience='当事人')
        result = self.tick(json.dumps({'action':'idle','reason':'想歇一歇','args':{}}))
        self.assertEqual('done', result['status'])
        packet = json.loads(self.calls[0][1]['content'])
        entries = packet['experiences'] + packet['writings']
        self.assertEqual(1, sum(r['id'] == work['card_id'] for r in entries))
        self.assertEqual('纸船变成了月亮。', packet['writings'][0]['text'])
        self.assertIn(work['card_id'], packet['reaction_ids'])
        self.assertIn(public['id'], [r['id'] for r in packet['experiences']])
        self.assertNotIn('私聊紫茄子', json.dumps(packet, ensure_ascii=False))
        self.assertNotIn('群内当事人内容', json.dumps(packet, ensure_ascii=False))

    def test_idle_and_failure_use_opportunity_without_fake_work(self):
        result = self.tick(json.dumps({'action': 'idle', 'reason': '想闲着', 'args': {}}))
        self.assertEqual(result['action'], 'idle')
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('receipt', result)
        self.assertEqual(self.tick('{}')['reason'], 'interval')
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(self.tick('bad', '2026-11-08T15:00:00+09:00')['status'], 'failed')
        self.assertEqual(self.tick('{}', '2026-11-08T18:00:00+09:00')['reason'], 'daily_cap')

    def test_rest_and_busy_do_not_consume(self):
        with patch('organs.day.phone_browse._feeling', return_value=dict(AWAKE, asleep=True)):
            self.assertEqual(self.tick('{}')['reason'], 'rest')
        with patch('organs.day.phone_browse._ACTIVE', 'busy'):
            self.assertEqual(self.tick('{}')['reason'], 'busy')
        self.assertEqual(solitude.log_lines(self.root), [])

    def test_old_automatic_jobs_count_without_double_counting_new_jobs(self):
        phone_browse.save({'id': 'old', 'origin': 'automatic', 'created_at': AT, 'status': 'done'}, self.root)
        self.assertEqual(self.tick('{}')['reason'], 'interval')
        self.tick(json.dumps({'action': 'idle', 'reason': '休息', 'args': {}}), '2026-11-08T15:00:00+09:00')
        self.assertEqual(self.tick('{}', '2026-11-08T18:00:00+09:00')['reason'], 'daily_cap')

    def test_private_input_and_unverified_phone_never_reach_selector(self):
        self.brain.ingest('12345678', '秘密紫茄子', ts=AT, scene='private', session_id='qq:private:12345678')
        result = self.tick(json.dumps({'action': 'browse', 'reason': '想看', 'args': {}}))
        self.assertEqual(result['status'], 'failed')
        prompt = json.loads(self.calls[0][1]['content'])
        self.assertNotIn('browse', prompt['available'])
        self.assertNotIn('秘密紫茄子', json.dumps(self.calls, ensure_ascii=False))

    def test_disconnected_phone_is_not_offered_and_recovers_when_connected(self):
        phone_browse.configure(True, self.root)
        with patch('organs.hand.phone.config', return_value={'serial':'test','isolated_for_bot':True}), \
             patch('organs.hand.phone.status', return_value={'connected':False,'state':'disconnected'}) as status:
            self.assertEqual(self.tick('{"action":"idle","reason":"休息","args":{}}')['status'], 'done')
            packet = json.loads(self.calls[0][1]['content'])
            self.assertNotIn('browse', packet['available'])
            self.assertFalse(packet['phone']['available'])
            self.assertEqual('disconnected', packet['phone']['state'])
            status.return_value = {'connected':True,'state':'device'}
            self.tick('{"action":"idle","reason":"休息","args":{}}', '2026-11-08T15:00:00+09:00')
            packet = json.loads(self.calls[0][1]['content'])
            self.assertIn('browse', packet['available'])
            self.assertTrue(packet['phone']['available'])

    def test_adb_query_failure_does_not_block_non_phone_life(self):
        from adapters.adb import AdbError
        phone_browse.configure(True, self.root)
        with patch('organs.hand.phone.config', return_value={'serial':'test','isolated_for_bot':True}), \
             patch('organs.hand.phone.status', side_effect=AdbError('设备查询失败')):
            result = self.tick('{"action":"idle","reason":"休息","args":{}}')
        self.assertEqual('done', result['status'])
        packet = json.loads(self.calls[0][1]['content'])
        self.assertNotIn('browse', packet['available'])
        self.assertEqual('unconfirmed', packet['phone']['state'])

    def test_disabled_or_unisolated_phone_is_not_queried(self):
        with patch('organs.hand.phone.status', side_effect=AssertionError('不应读设备')):
            with patch('organs.hand.phone.config', return_value={'serial':'test','isolated_for_bot':True}):
                self.tick('{"action":"idle","reason":"休息","args":{}}')
                self.assertEqual('disabled', json.loads(self.calls[0][1]['content'])['phone']['state'])
            phone_browse.configure(True, self.root)
            self.tick('{"action":"idle","reason":"休息","args":{}}', '2026-11-08T15:00:00+09:00')
            self.assertEqual('not_isolated', json.loads(self.calls[0][1]['content'])['phone']['state'])

    def test_writing_receipt_can_finish_own_plan(self):
        from organs.home import intentions
        own = intentions.apply({'name': '写首短诗'}, root=self.root / 'home')
        result = self.tick(json.dumps({'action': 'write', 'reason': '有个开头', 'args': {
            'text': '月亮沉进杯底。', 'intention_id': own['card_id'], 'finish': True}}))
        self.assertEqual(result['status'], 'done')
        self.assertTrue(result['receipt']['card_id'])
        self.assertEqual(intentions.listing(self.root / 'home')[0]['status'], 'done')

    def test_run_tick_replaces_both_legacy_automatic_paths(self):
        from api import day
        from organs.soul import load
        soul = load(Path(__file__).resolve().parents[2] / 'soul' / 'data')
        with patch('organs.day.autonomy.tick', return_value={'action': 'idle'}) as chosen, \
             patch('organs.day.solitude.consider') as old_idle, \
             patch('organs.day.phone_browse.tick') as old_phone, \
             patch('organs.day.life.tick', return_value={}), \
             patch('organs.day.rhythm._settle_night', return_value={}), \
             patch('organs.day.proactive.settle_due', return_value={}), \
             patch('organs.day.proactive.consider', return_value={}):
            day.run_tick(AT, root=self.root, brain=self.brain, store=soul)
        chosen.assert_called_once()
        old_idle.assert_not_called()
        old_phone.assert_not_called()

    def test_disable_while_selecting_prevents_execution(self):
        from organs.day import autonomy
        def complete(_):
            growth.configure(False, root=self.root)
            return json.dumps({'action':'write','reason':'想写','args':{'text':'不应写出'}})
        result = autonomy.tick(AT, root=self.root, brain=self.brain, complete=complete)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(autonomy.own_writings(self.root / 'home'), [])

    def test_disable_while_selecting_also_prevents_async_painting(self):
        from organs.day import autonomy
        def complete(_):
            growth.configure(False, root=self.root)
            return json.dumps({'action':'paint','reason':'画月亮','args':{'prompt':'一轮月亮'}})
        with patch('organs.mind.stroke_painting.start', return_value={'id':'should-not-start'}) as start:
            result = autonomy.tick(AT, root=self.root, brain=self.brain, complete=complete)
        start.assert_not_called()
        self.assertEqual(result['status'], 'interrupted')

    def test_active_plan_stays_in_context_after_many_abandoned_plans(self):
        from organs.home import intentions
        home = self.root / 'home'
        active = intentions.apply({'name':'长期兴趣'}, root=home)
        for index in range(21):
            intentions.apply({'name':'短打算' + str(index), 'status':'abandoned'}, root=home)
        self.tick(json.dumps({'action':'idle','reason':'今天歇歇','args':{}}))
        plans = json.loads(self.calls[0][1]['content'])['intentions']
        self.assertIn(active['card_id'], [p['card_id'] for p in plans])

    def test_share_is_optional_and_only_actual_work_can_raise_a_candidate(self):
        from organs.day import proactive
        output = {'action':'write','reason':'写个开头','args':{'text':'月亮落进杯里。'}, 'share':True}
        result = self.tick(json.dumps(output))
        self.assertEqual(result['status'], 'done')
        self.assertTrue(proactive.open_own_doing(self.root, AT))
        before = proactive.own_doing_lines(self.root)
        result = self.tick(json.dumps({'action':'idle','reason':'歇着','args':{},'share':True}),
                           '2026-11-08T15:00:00+09:00')
        self.assertEqual(result['action'], 'idle')
        self.assertEqual(proactive.own_doing_lines(self.root), before)

    def test_chat_context_contains_current_plan_and_actual_writing(self):
        from organs.day import autonomy
        from organs.home import intentions
        intentions.apply({'name':'写月亮短诗','status':'paused'}, root=self.root / 'home')
        self.tick(json.dumps({'action':'write','reason':'想到一行','args':{'text':'月亮落进杯里。'}}))
        text = autonomy.context(root=self.root)
        self.assertIn('写月亮短诗', text)
        self.assertIn('paused', text)
        self.assertIn('月亮落进杯里', text)

    def test_browse_sharing_uses_actual_public_observation_and_confirmed_link_once(self):
        from organs.day import autonomy, proactive
        for status in ('done', 'partial'):
            with self.subTest(status=status):
                root = self.root/status
                growth.configure(True,root=root)
                job = {'id':'watched','created_at':AT,'status':status,'origin':'autonomy','scope':'public',
                       'title':'猫打哈欠','platform':'douyin',
                       'observations':[{'status':'ok','observation':{'subject':'猫闭眼张嘴'}}],
                       'audio':{'status':'unavailable'},
                       'video_source':{'status':'ok','url':'https://v.douyin.com/test/'}}
                phone_browse.save(job,root)
                solitude.append_log(root,{'at':AT,'event':'autonomy_result','model_turn_id':'choice',
                    'action':'browse','status':'queued','share':True,'receipt':{'id':'watched'}})
                self.assertEqual(status,autonomy.settle(AT,root=root)[0]['status'])
                candidate = proactive.open_own_doing(root,AT)
                self.assertIsNotNone(candidate)
                self.assertEqual('browse',candidate['action'])
                self.assertEqual('watched',candidate['job_id'])
                self.assertIn('猫闭眼张嘴',candidate['because'])
                self.assertIn('未完整观看',candidate['because'])
                self.assertIn('https://v.douyin.com/test/',candidate['because'])
                self.assertEqual([],autonomy.settle(AT,root=root))
                self.assertEqual(1,len(proactive.own_doing_lines(root)))

    def test_browse_share_respects_choice_outcome_privacy_and_current_growth_switch(self):
        from organs.day import autonomy, proactive
        cases = [{'share':False}, {'status':'failed'}, {'status':'stopped'}, {'origin':'acceptance'},
                 {'scope':'private'}, {'observations':[]}, {'enabled':False}]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                root = self.root/str(index)
                growth.configure(changes.get('enabled',True),root=root)
                job = {'id':'watched','created_at':AT,'status':'done','origin':'autonomy','scope':'public',
                       'title':'不应分享','observations':[{'status':'ok','observation':{'subject':'猫'}}]}
                job.update({k:v for k,v in changes.items() if k not in ('share','enabled')})
                phone_browse.save(job,root)
                solitude.append_log(root,{'at':AT,'event':'autonomy_result','model_turn_id':'choice',
                    'action':'browse','status':'queued','share':changes.get('share',True),'receipt':{'id':'watched'}})
                autonomy.settle(AT,root=root)
                self.assertEqual([],proactive.own_doing_lines(root))

    def test_selector_receives_current_time_and_real_material_update_times(self):
        from organs.day import autonomy
        from organs.home import cards, intentions
        home = self.root / 'home'
        yesterday = '2026-11-07T18:00:00+09:00'
        with patch('organs.home.cards._now', return_value=yesterday):
            intentions.apply({'name':'接着写月亮短诗'}, root=home)
            cards.append_cards(home, role='organ', by='日子线·自主写作', cards=[{
                'room':'工作台','kind':'物件','name':'月亮初稿','params':{'scope':'public','text':'月亮落在杯底。'}}])
            cards.append_cards(home, role='organ', by='日子线·自主活动', cards=[{
                'room':'工作台','kind':'物件','name':'一次画作','params':{'scope':'public','text':'逐笔绘画：失败'}}])
        self.tick(json.dumps({'action':'idle','reason':'先想想','args':{}}))
        prompt = json.loads(self.calls[0][1]['content'])
        self.assertEqual(prompt.get('now'), AT)
        self.assertEqual(prompt['intentions'][0].get('updated_at'), yesterday)
        self.assertEqual(prompt['writings'][0].get('recorded_at'), yesterday)
        activity = next(r for r in prompt['experiences'] if r['text']=='逐笔绘画：失败')
        self.assertEqual(activity.get('recorded_at'), yesterday)

    def test_active_drafts_survive_recent_writing_limit_and_keep_intention_links(self):
        from organs.day import autonomy
        from organs.home import cards, intentions
        home = self.root / 'home'
        plan = intentions.apply({'name':'慢慢写月亮短诗'}, root=home)
        old = cards.append_cards(home, role='organ', by='日子线·自主写作', cards=[{
            'room':'工作台','kind':'物件','name':'老初稿','params':{'scope':'public','text':'旧稿：月亮沉进杯底。','intention_id':plan['card_id']}}])['cards'][0]
        for i in range(14):
            cards.append_cards(home, role='organ', by='日子线·自主写作', cards=[{
                'room':'工作台','kind':'物件','name':'另一件事'+str(i),'params':{'scope':'public','text':'无关随笔'+str(i)}}])
        self.tick(json.dumps({'action':'idle','reason':'先想一想','args':{}}))
        writings = json.loads(self.calls[0][1]['content'])['writings']
        self.assertIn(old['card_id'], [w['id'] for w in writings])
        self.assertEqual(next(w for w in writings if w['id']==old['card_id'])['intention_id'], plan['card_id'])
        self.assertLessEqual(len(writings), 12)
        context = autonomy.context(root=self.root)
        self.assertIn('旧稿：月亮沉进杯底。', context)

    def test_next_day_revision_preserves_original_and_links_completed_version(self):
        from organs.day import autonomy
        from organs.home import cards, intentions
        home = self.root / 'home'
        plan = intentions.apply({'name':'写月亮短诗'}, root=home)
        first = self.tick(json.dumps({'action':'write','reason':'写开头','args':{
            'text':'月亮沉进杯底。','intention_id':plan['card_id'],'finish':False}}))
        original = first['receipt']['card_id']
        second = self.tick(json.dumps({'action':'write','reason':'给昨天那句添一个结尾','args':{
            'text':'月亮沉进杯底。\n我把夜色轻轻端起。','intention_id':plan['card_id'],
            'based_on':original,'finish':True}}), '2026-11-09T12:00:00+09:00')
        self.assertEqual(second['status'], 'done')
        self.assertEqual(second['receipt']['based_on'], original)
        writings = autonomy.own_writings(home)
        self.assertEqual(next(w['text'] for w in writings if w['id']==original), '月亮沉进杯底。')
        revised = next(w for w in writings if w['id']==second['receipt']['card_id'])
        self.assertEqual(revised['based_on'], original)
        self.assertEqual(revised['intention_id'], plan['card_id'])
        self.assertEqual(intentions.listing(home)[0]['status'], 'done')
        self.assertEqual(intentions.listing(home)[0]['sources'], [revised['id']])

    def test_revision_rejects_unknown_and_other_plans_original_without_writing(self):
        from organs.home import cards, intentions
        home = self.root / 'home'
        first = intentions.apply({'name':'月亮诗'}, root=home)
        second = intentions.apply({'name':'鲸鱼诗'}, root=home)
        original = cards.append_cards(home, role='organ', by='日子线·自主写作', cards=[{
            'room':'工作台','kind':'物件','name':'鲸鱼原稿',
            'params':{'scope':'public','text':'鲸鱼游过。','intention_id':second['card_id']}}])['cards'][0]
        for i, ident in enumerate(('不存在的原稿', original['card_id'])):
            with self.subTest(ident=ident):
                before = cards._read_room_rows(home, '工作台')[0]
                result = self.tick(json.dumps({'action':'write','reason':'修改月亮诗','args':{
                    'text':'声称写完','intention_id':first['card_id'],'based_on':ident,'finish':True}}),
                    '2026-11-%02dT12:00:00+09:00' % (8+i))
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(cards._read_room_rows(home, '工作台')[0], before)
                self.assertTrue(all(p['status']=='active' for p in intentions.listing(home)))

    def test_standalone_writing_can_be_revised_without_creating_a_plan(self):
        from organs.day import autonomy
        from organs.home import intentions
        first = self.tick(json.dumps({'action':'write','reason':'随手记一句','args':{'text':'风吹过。'}}))
        second = self.tick(json.dumps({'action':'write','reason':'改改那句','args':{
            'text':'风从窗边吹过。','based_on':first['receipt']['card_id']}}), '2026-11-09T12:00:00+09:00')
        self.assertEqual(second['status'], 'done')
        self.assertEqual(second['receipt']['based_on'], first['receipt']['card_id'])
        self.assertEqual(intentions.listing(self.root / 'home'), [])
        self.assertEqual(len(autonomy.own_writings(self.root / 'home')), 2)

    def test_malformed_legacy_writing_link_does_not_break_context_or_tick(self):
        from organs.day import autonomy
        from organs.home import cards
        home = self.root / 'home'
        bad = cards.append_cards(home, role='organ', by='日子线·自主写作', cards=[{
            'room':'工作台','kind':'物件','name':'旧坏关联',
            'params':{'scope':'public','text':'旧稿','intention_id':[]}}])['cards'][0]
        before = cards._read_room_rows(home, '工作台')[0]
        result = self.tick(json.dumps({'action':'idle','reason':'休息','args':{}}))
        self.assertEqual(result['status'], 'done')
        self.assertNotIn(bad['card_id'], [w['id'] for w in autonomy.own_writings(home)])
        self.assertIsInstance(autonomy.context(root=self.root), str)
        self.assertEqual(cards._read_room_rows(home, '工作台')[0], before)

    def test_completed_writing_can_affect_mood_once_in_later_choice(self):
        from organs.day import autonomy
        from organs.heart import mood, events
        first = self.tick(json.dumps({'action':'write','reason':'想试童话','args':{'text':'纸船飞到了月亮上。'}}))
        cid = first['receipt']['card_id']
        output = {'action':'idle','reason':'歇会儿','args':{}, 'reaction':{
            'id':cid, 'effect':'positive','impression':'写出这个画面有点开心','evidence':'纸船飞到了月亮上'}}
        second = self.tick(json.dumps(output), '2026-11-09T12:00:00+09:00')
        self.assertEqual(second['status'], 'done')
        self.assertIn(cid, json.loads(self.calls[0][1]['content'])['reaction_ids'])
        self.assertGreater(second['reaction']['mood']['delta'], 0)
        self.assertLessEqual(second['reaction']['mood']['delta'], 2)
        entries = events.recent(self.root/'heart', now='2026-11-09T12:00:00+09:00')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['source_id'], 'autonomy:'+cid)
        self.assertEqual(entries[0]['who'], 'self')
        self.assertEqual(solitude.log_lines(self.root)[-1]['reaction'], second['reaction'])
        third = self.tick(json.dumps(output), '2026-11-10T12:00:00+09:00')
        self.assertNotIn(cid, json.loads(self.calls[0][1]['content'])['reaction_ids'])
        self.assertEqual(third['status'], 'done')
        self.assertIn('reaction_error', third)
        self.assertNotIn('reaction', third)
        self.assertEqual(len(events.recent(self.root/'heart', now='2026-11-09T12:00:00+09:00')), 1)
        self.assertEqual(autonomy.own_writings(self.root/'home')[0]['text'], '纸船飞到了月亮上。')

    def test_actual_failed_painting_can_have_neutral_feeling(self):
        from organs.day import autonomy
        from organs.home import art
        from organs.heart import events
        ident = 'c' * 32
        art.save_painting_job({'id':ident,'status':'failed','created_at':AT,
                              'request':{'prompt':'三角涂鸦'}}, root=self.root/'home')
        solitude.append_log(self.root, {'at':AT,'event':'autonomy_result','model_turn_id':ident,
            'action':'paint','status':'queued','receipt':{'id':ident}})
        receipt = autonomy.settle(AT, root=self.root)[0]
        cid = receipt['receipt']['card_id']
        result = self.tick(json.dumps({'action':'idle','reason':'休息','args':{},'reaction':{
            'id':cid,'effect':'neutral','impression':'','evidence':''}}))
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['reaction']['mood']['delta'], 0)
        self.assertFalse((self.root/'heart'/'mood.json').exists())
        self.assertEqual(events.recent(self.root/'heart', now=AT)[0]['effect'], 'neutral')

    def test_reaction_rejects_unoffered_sources_and_invented_evidence(self):
        from organs.home import cards
        from organs.heart import events
        home = self.root/'home'
        public = cards.append_cards(home, role='organ', by='日子线·自主写作', cards=[{
            'room':'工作台','kind':'物件','name':'自己的故事','params':{'scope':'public','text':'月亮上的纸船'}}])['cards'][0]['card_id']
        private = cards.append_cards(home, role='organ', by='日子线·自主写作', cards=[{
            'room':'工作台','kind':'物件','name':'私有','params':{'scope':'private','text':'私有正文'}}])['cards'][0]['card_id']
        phone = cards.append_cards(home, role='organ', by='日子线·自主活动', cards=[{
            'room':'工作台','kind':'物件','name':'已有手机感想','params':{
                'scope':'public','text':'片段看过了','action':'browse','status':'done'}}])['cards'][0]['card_id']
        for index, (cid, evidence) in enumerate([(private,'私有正文'),(phone,'片段看过了'),
                                              ('未完成的新画','三角'),(public,'不在原稿里的话')]):
            with self.subTest(cid=cid):
                result = self.tick(json.dumps({'action':'idle','reason':'歇会儿','args':{},'reaction':{
                    'id':cid,'effect':'negative','impression':'失望','evidence':evidence}}),
                    '2026-11-%02dT12:00:00+09:00' % (9+index))
                self.assertEqual(result['status'], 'done')
                self.assertIn('reaction_error', result)
                self.assertNotIn('reaction', result)
                prompt = json.loads(self.calls[0][1]['content'])
                self.assertEqual(prompt.get('reaction_ids'), [public])
                self.assertNotIn('私有正文', json.dumps(prompt,ensure_ascii=False))
                self.assertEqual(events.recent(self.root/'heart', now=AT), [])

    def test_spliced_reaction_does_not_cancel_valid_revision_or_accept_invalid_work(self):
        from organs.day import autonomy
        from organs.home import cards
        from organs.heart import events
        home = self.root/'home'
        original = '我把那页纸折了一下，又展开。折痕斜过四划。我把它收进抽屉最上面。'
        cid = cards.append_cards(home, role='organ', by='日子线·自主写作', cards=[{
            'room':'工作台','kind':'物件','name':'原稿',
            'params':{'scope':'public','text':original}}])['cards'][0]['card_id']
        reaction = {'id':cid,'effect':'positive','impression':'给一天找了个落点',
                    'evidence':'我把那页纸折了一下，又展开。……我把它收进抽屉最上面。'}
        output = {'action':'write','reason':'续写','args':{'text':'纸上的折痕还在。','based_on':cid},
                  'reaction':reaction}
        result = self.tick(json.dumps(output))
        self.assertEqual('done', result['status'])
        self.assertEqual(cid, result['receipt']['based_on'])
        self.assertNotIn('reaction', result)
        self.assertIn('reaction_error', result)
        self.assertEqual(result['reaction_error'], solitude.log_lines(self.root)[-1]['reaction_error'])
        works = autonomy.own_writings(home)
        self.assertEqual([original, '纸上的折痕还在。'], [w['text'] for w in works])
        self.assertEqual([], events.recent(self.root/'heart', now=AT))
        self.assertFalse((self.root/'heart'/'mood.json').exists())
        output['args']['text'] = ''
        invalid = self.tick(json.dumps(output), '2026-11-09T12:00:00+09:00')
        self.assertEqual('failed', invalid['status'])
        self.assertIn('reaction_error', invalid)
        self.assertEqual(works, autonomy.own_writings(home))
        self.assertEqual([], events.recent(self.root/'heart', now=AT))
