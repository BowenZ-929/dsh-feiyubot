"""计划→真实聊天装配→手→模拟协议回执→脑；不联网、不发送QQ。"""
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from api import day
from api import api
from organs.brain import model
from organs.day import delivery, proactive
from organs.heart import proactive as feelings
from organs.hand import channel
from organs.mind import recent
from . import test_chat_api as fixture


class ProactiveDispatchTest(unittest.TestCase):
    setUp = fixture.ChatApiTest.setUp
    _answer = fixture.ChatApiTest._answer
    _post = fixture.ChatApiTest._post

    def prepare(self, *, scene='private', target=3425635473, enabled=True):
        self.day_root = self.tmp / 'day'
        self.day_root.mkdir()
        active_day=patch.dict(os.environ,{'DAY_ROOT':str(self.day_root)})
        active_day.start();self.addCleanup(active_day.stop)
        self.home_root=self.tmp/'house'
        self.who = str(fixture.CHANNEL_WHO['id'])
        self.route = {'who': self.who, 'scene': scene, 'target': target}
        self.config = dict(proactive.load_config(self.day_root), delivery={'enabled': enabled, 'routes': [self.route]})
        self.save_config()
        self.session = f'qq:{scene}:{target}'
        self.now = model.now_iso()
        earlier = (model.parse_ts(self.now) - timedelta(minutes=6)).isoformat()
        self.brain.ingest(self.who, '周末想去书店', ts=earlier, scene=scene, session_id=self.session)
        self.plan = proactive.consider(earlier, root=self.day_root, heart_root=self.tmp/'heart',
                                       brain=self.brain)['plan']
        self.assertIsNotNone(self.plan)
        self.requests = []
        self.model_calls = 0

    def save_config(self):
        (self.day_root/'proactive.json').write_text(json.dumps(self.config), encoding='utf-8')

    def tick(self, receipt=None, during_model=None, captured=None, reply='还想去那家书店。想找本画册。'):
        receipt = receipt or {'ok': True, 'sent': 1, 'id': 'fake-say', 'confirmed_text': '还想去那家书店。',
                              'stopped': None, 'reason_code': 'sent'}
        def post(url, body, timeout):
            self.requests.append(body)
            return 200, json.dumps(receipt).encode()
        def model_reply(url, headers, payload, timeout):
            self.model_calls += 1
            if captured is not None:
                captured.append(payload)
            if during_model:
                during_model()
            return fixture._body(reply)(url, headers, payload, timeout)
        with self._answer(model_reply), patch.object(channel, '_post_json', side_effect=post), \
             patch('organs.day.proactive_ask.ask', return_value={'ok':True,'send':True,'text':'旧草稿','chunks':['旧草稿']}), \
             patch('organs.day.life.tick', return_value={}), patch('organs.day.phone_browse.tick', return_value={}):
            return day.run_tick(self.now, root=self.day_root, heart_root=self.tmp/'heart',
                                home_root=self.home_root, brain=self.brain, store=self.store)['proactive']

    def test_model_can_decline_proactive_message_without_send_memory_wait_or_retry(self):
        self.prepare()
        before=self.brain.today_entries()
        captured=[]
        result=self.tick(reply='[不主动开口]',captured=captured)
        self.assertEqual({'declined':1},result['actions'])
        self.assertEqual('model-declined',result['rows'][0]['reason'])
        self.assertEqual([],self.requests)
        self.assertEqual(before,self.brain.today_entries())
        self.assertFalse(feelings.overview()['pending'])
        self.assertEqual('',recent.load()['turns'][-1]['reply'])
        self.assertEqual('',recent.load()['turns'][-1]['said'])
        self.assertIn('[不主动开口]',captured[0]['messages'][0]['content'])
        self.tick(reply='现在又想说了')
        self.assertEqual(1,self.model_calls)
        self.assertEqual([],self.requests)

    def test_completed_own_story_survives_real_chat_gate_and_receipt(self):
        from organs.day import growth, solitude
        from organs.home import cards
        self.prepare()
        growth.configure(True,root=self.day_root)
        work='小鱼穿上雨靴去赶海，捡到一颗会唱歌的贝壳。'
        cards.append_cards(solitude._home_root(),role='organ',by='日子线·自主写作',cards=[{
            'room':'工作台','kind':'物件','name':'雨靴小鱼短篇',
            'params':{'text':work,'scope':'public'}}])
        reply='我刚写了个小故事：小鱼穿上雨靴去赶海，捡到一颗会唱歌的贝壳。结果它只会唱一个走调的音。'
        def receipt(url,body,timeout):
            self.requests.append(body)
            return 200,json.dumps({'ok':True,'sent':len(body['chunks']),'id':'fake',
                'confirmed_text':''.join(c['text'] for c in body['chunks']),'reason_code':'sent'}).encode()
        with self._answer(fixture._body(reply)), patch.object(channel,'_post_json',side_effect=receipt), \
             patch('organs.day.life.tick',return_value={}),patch('organs.day.phone_browse.tick',return_value={}):
            result=day.run_tick(self.now,root=self.day_root,heart_root=self.tmp/'heart',
                home_root=self.home_root,brain=self.brain,store=self.store)['proactive']
        self.assertEqual({'sent':1},result['actions'])
        sent=''.join(c['text'] for call in self.requests for c in call['chunks'])
        self.assertEqual(reply,sent)
        self.assertEqual(reply,''.join(r['quote'] for r in self.brain.today_entries() if r['side']==model.SIDE_SELF))

    def test_plan_partial_delivery_memory_and_reply_use_one_real_chat_chain(self):
        self.prepare()
        delivery.intake('旧的待说草稿', to=self.who, root=self.day_root)
        before = delivery.pending_file(self.day_root).read_bytes()
        result = self.tick({'ok':True,'sent':1,'id':'fake-say','stopped':'send_failed',
                            'confirmed_text':'还想去那家书店。','reason_code':'send_failed'})
        self.assertEqual({'partial':1}, result['actions'])
        self.assertEqual((self.route['scene'],self.route['target']),
                         (self.requests[0]['scene'],self.requests[0]['target']))
        self.assertEqual(before, delivery.pending_file(self.day_root).read_bytes())
        own = [r for r in self.brain.today_entries() if r['side']==model.SIDE_SELF]
        self.assertEqual(['还想去那家书店。'], [r['quote'] for r in own])
        self.assertEqual(self.session, own[0]['session_id'])
        row = result['rows'][0]
        self.assertEqual(own[0]['turn_id'],row['turn_id'])
        self.assertTrue(feelings.overview()['pending'])
        self.tick()
        self.assertEqual(1, len(self.requests), '已消费计划不重发，旧队列也不补发')
        self._post({'text':'好，周末去','who':fixture.CHANNEL_WHO,'scene':'private',
                    'where':{'userId':self.route['target']}},fixture._body('好呀。'))
        self.assertFalse(feelings.overview()['pending'])

    def test_disabled_keeps_existing_draft_behavior_without_sending(self):
        self.prepare(enabled=False)
        self.assertEqual({'queued':1},self.tick()['actions'])
        self.assertEqual([],self.requests)
        self.assertFalse(feelings.overview()['pending'])

    def test_browse_choice_reaches_chat_with_observation_link_and_actual_ack(self):
        from organs.day import autonomy, growth, phone_browse, solitude
        self.prepare()
        proactive._consume(self.plan,self.now,self.day_root,{'action':'test-setup'})
        self.now = (model.parse_ts(self.now)+timedelta(minutes=20)).isoformat()
        watched_at = (model.parse_ts(self.now)-timedelta(minutes=6)).isoformat()
        self.brain.ingest(self.store.snapshot().identity.display, '那周末再聊书店', ts=watched_at,
                          side=model.SIDE_SELF,scene='private',session_id=self.session)
        growth.configure(True,root=self.day_root)
        phone_browse.save({'id':'watched','created_at':watched_at,'status':'done',
            'origin':'autonomy','scope':'public','platform':'douyin','title':'猫打哈欠',
            'observations':[{'status':'ok','observation':{'subject':'猫闭眼张嘴'}}],
            'video_source':{'status':'ok','url':'https://v.douyin.com/test/'}},self.day_root)
        solitude.append_log(self.day_root,{'at':watched_at,'event':'autonomy_result',
            'model_turn_id':'choice','action':'browse','status':'queued','share':True,'receipt':{'id':'watched'}})
        autonomy.settle(watched_at,root=self.day_root,home_root=self.home_root)
        with patch.object(proactive,'_scheduled_today',return_value=True):
            chosen = proactive.consider(watched_at,root=self.day_root,heart_root=self.tmp/'heart',brain=self.brain)
        self.assertEqual('own_doing',chosen['reason_code'])
        self.assertIsNotNone(chosen['plan'])
        captured=[]
        with patch.object(model,'now_iso',return_value=self.now):
            result=self.tick({'ok':True,'sent':1,'id':'fake','confirmed_text':'刚看见一只猫打哈欠。',
                              'reason_code':'sent'},captured=captured)
        self.assertEqual({'sent':1},result['actions'])
        packet=json.dumps(captured,ensure_ascii=False)
        self.assertIn('猫闭眼张嘴',packet)
        self.assertIn('https://v.douyin.com/test/',packet)
        self.assertIn('未完整观看',packet)
        self.assertEqual(self.route['target'],self.requests[0]['target'])
        self.assertEqual('刚看见一只猫打哈欠。',recent.load()['turns'][-1]['reply'])

    def test_enable_preserves_options_and_does_not_send_or_replay_old_plan(self):
        self.prepare(enabled=False)
        self.config['custom_marker'] = '保留'; self.save_config()
        with patch('organs.day.model.now_iso', return_value=self.now):
            status, result = api.handle('POST', '/day/proactive/enable',
                {'private_qq':self.who}, self.store, brain=self.brain)
        self.assertEqual(200, status)
        saved = json.loads((self.day_root/'proactive.json').read_text())
        self.assertEqual('保留', saved['custom_marker'])
        self.assertEqual(self.who, saved['owner'])
        self.assertEqual({'enabled':True, 'routes':[self.route], 'enabled_at':self.now}, result['delivery'])
        before = proactive.log_path(self.day_root).read_bytes()
        status, repeated = api.handle('POST', '/day/proactive/enable',
            {'private_qq':self.who}, self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertEqual(result, repeated)
        self.assertEqual(before, proactive.log_path(self.day_root).read_bytes())
        result = self.tick()
        self.assertEqual({'blocked':1}, result['actions'])
        self.assertEqual('predates-enabling', result['rows'][0]['reason'])
        self.assertEqual([], self.requests)
        self.assertEqual(0, self.model_calls)

    def test_recipient_change_and_disable_record_exact_before_and_after(self):
        self.prepare(enabled=False)
        with patch('organs.day.model.now_iso',return_value=self.now):
            proactive.enable_delivery(self.who,self.day_root)
        first=json.loads(proactive.log_path(self.day_root).read_text().splitlines()[-1])
        self.assertEqual(first['changes']['enabled'],{'before':False,'after':True})
        self.assertEqual(first['changes']['owner'],{'before':self.config['owner'],'after':self.who})
        self.assertEqual(first['changes']['enabled_at'],{'before':None,'after':self.now})
        proactive.enable_delivery('123456789',self.day_root)
        changed=json.loads(proactive.log_path(self.day_root).read_text().splitlines()[-1])
        self.assertEqual(changed['changes']['routes'],{'before':[self.route],
            'after':[{'who':'123456789','scene':'private','target':123456789}]})
        self.assertNotIn('enabled',changed['changes'])
        proactive.disable_delivery(self.day_root)
        stopped=json.loads(proactive.log_path(self.day_root).read_text().splitlines()[-1])
        self.assertEqual(stopped['changes'],{'enabled':{'before':True,'after':False}})
        before=proactive.log_path(self.day_root).read_bytes()
        proactive.disable_delivery(self.day_root)
        self.assertEqual(proactive.log_path(self.day_root).read_bytes(),before)
        self.assertEqual(self.requests,[])

    def test_invalid_enable_does_not_change_settings(self):
        self.prepare(enabled=False)
        before = (self.day_root/'proactive.json').read_bytes()
        for body in ({}, {'private_qq':1234567}, {'private_qq':'主人'}, {'private_qq':'000'},
                     {'private_qq':'123456\n'}, {'private_qq':'１２３４５６'},
                     {'private_qq':'123456', 'routes':[]}):
            with self.subTest(body=body):
                status, _ = api.handle('POST','/day/proactive/enable',body,self.store,brain=self.brain)
                self.assertEqual(400,status)
                self.assertEqual(before,(self.day_root/'proactive.json').read_bytes())

    def test_enabled_new_plan_still_uses_actual_delivery_chain(self):
        self.prepare()
        self.config['delivery']['enabled_at'] = (model.parse_ts(self.plan['created_at'])-timedelta(seconds=1)).isoformat()
        self.save_config()
        self.assertEqual({'sent':1},self.tick()['actions'])
        self.assertEqual(1,len(self.requests))

    def test_reenable_or_change_recipient_starts_a_new_delivery_window(self):
        from api.proactive_delivery import route_for
        self.prepare(enabled=False)
        first = '2026-11-09T10:00:00+09:00'
        later = '2026-11-09T11:00:00+09:00'
        with patch('organs.day.model.now_iso',return_value=first):
            proactive.enable_delivery(self.who,self.day_root)
        proactive.disable_delivery(self.day_root)
        with patch('organs.day.model.now_iso',return_value=later):
            result = proactive.enable_delivery(self.who,self.day_root)
        self.assertEqual(later,result['delivery']['enabled_at'])
        self.assertEqual((None,'predates-enabling'),route_for(
            {'who':self.who,'created_at':first},proactive.load_config(self.day_root)))
        self.assertEqual((None,'predates-enabling'),route_for(
            {'who':self.who},proactive.load_config(self.day_root)))
        with patch('organs.day.model.now_iso',return_value=later):
            result = proactive.enable_delivery('123456789',self.day_root)
        self.assertEqual([{'who':'123456789','scene':'private','target':123456789}],result['delivery']['routes'])
        self.assertIsNone(route_for({'who':self.who,'created_at':later},proactive.load_config(self.day_root))[0])

    def test_enabling_second_is_excluded_but_the_next_second_is_allowed(self):
        from api.proactive_delivery import route_for
        self.prepare()
        self.config['delivery']['enabled_at'] = self.now
        row = {'who':self.who,'created_at':self.now}
        self.assertEqual((None,'predates-enabling'),route_for(row,self.config))
        row['created_at'] = (model.parse_ts(self.now)+timedelta(seconds=1)).isoformat()
        self.assertEqual((self.route,''),route_for(row,self.config))

    def test_target_and_unfinished_source_follow_the_allowed_private_route(self):
        self.prepare()
        other = '123456789'
        self.brain.ingest(other, '别把我的话转给别人', ts=self.now, scene='private', session_id='qq:private:'+other)
        with patch.object(proactive,'_people',return_value=[
                {'id':other,'familiarity':100,'last_interaction':self.now},
                {'id':self.who,'familiarity':1,'last_interaction':self.now}]):
            self.assertEqual(self.who,proactive.pick_who(self.now,brain=self.brain,config=self.config)['who'])
        sources = proactive.sources(self.now,root=self.day_root,heart_root=self.tmp/'heart',brain=self.brain)
        talk = next(r for r in sources if r['reason_code']==proactive.REASON_UNFINISHED)
        self.assertEqual(self.who,talk['who'])
        self.assertEqual(self.session,talk['source']['session_id'])

    def test_group_source_cannot_be_redirected_to_private(self):
        self.prepare(scene='group',target=70001)
        self.config['delivery']['routes']=[{'who':self.who,'scene':'private','target':int(self.who)}]
        self.save_config()
        result=self.tick()
        self.assertEqual({'blocked':1},result['actions'])
        self.assertEqual([],self.requests)
        self.assertFalse(any(r['side']==model.SIDE_SELF for r in self.brain.today_entries()))

    def test_source_changes_during_generation_cancel_the_draft(self):
        self.prepare()
        def changed():
            self.brain.ingest(self.who,'我已经去过了',ts=self.now,scene='private',session_id=self.session)
        result=self.tick(during_model=changed)
        self.assertEqual({'declined':1},result['actions'])
        self.assertEqual('source-changed',result['rows'][0]['reason'])
        self.assertEqual([],self.requests)
        self.assertEqual('',recent.load()['turns'][-1]['reply'])
        self.assertFalse(feelings.overview()['pending'])

    def test_switch_off_during_generation_cancels_send(self):
        self.prepare()
        def switched():
            status,result=api.handle('POST','/day/proactive/disable',{},self.store,brain=self.brain)
            self.assertEqual(200,status)
            self.assertFalse(result['delivery']['enabled'])
        result=self.tick(during_model=switched)
        self.assertEqual({'held':1},result['actions'])
        self.assertEqual([],self.requests)

    def test_disable_preserves_routes_and_existing_parameters(self):
        self.prepare()
        self.config={key:self.config[key] for key in ('owner','delivery')}
        self.config['custom_marker']='保留这项';self.save_config()
        status,result=api.handle('POST','/day/proactive/disable',{},self.store,brain=self.brain)
        self.assertEqual(200,status)
        saved=json.loads((self.day_root/'proactive.json').read_text())
        expected=dict(self.config,delivery=dict(self.config['delivery'],enabled=False))
        self.assertEqual(expected,saved)
        self.assertFalse(result['delivery']['enabled'])

    def test_old_channel_without_confirmed_text_stays_unconfirmed_and_never_retries(self):
        self.prepare()
        result=self.tick({'ok':True,'sent':1,'id':'old-say','reason_code':'sent'})
        self.assertEqual({'unconfirmed':1},result['actions'])
        self.assertFalse(feelings.overview()['pending'])
        self.assertFalse(any(r['side']==model.SIDE_SELF for r in self.brain.today_entries()))
        self.tick()
        self.assertEqual(1,len(self.requests))

    def test_matching_group_route_is_confirmed_in_the_original_group(self):
        self.prepare(scene='group',target=70001)
        self.assertEqual({'sent':1},self.tick()['actions'])
        self.assertEqual(('group',70001),(self.requests[0]['scene'],self.requests[0]['target']))
        self.assertEqual('qq:group:70001',feelings.overview()['history'][-1]['context']['session_id'])

    def test_replaced_text_is_remembered_as_the_replacement(self):
        self.prepare()
        result=self.tick({'ok':True,'sent':1,'id':'fake','confirmed_text':'这段有点长，我再想想。',
                          'reason_code':'replaced_too_long'})
        self.assertEqual({'replaced':1},result['actions'])
        self.assertEqual('这段有点长，我再想想。',recent.load()['turns'][-1]['reply'])

    def test_blocked_transport_cannot_claim_a_nonempty_confirmation(self):
        self.prepare()
        result=self.tick({'ok':False,'sent':0,'id':'fake','confirmed_text':'不应记住这句',
                          'reason_code':'quota_exceeded'})
        self.assertEqual({'unconfirmed':1},result['actions'])
        self.assertFalse(any(r['side']==model.SIDE_SELF for r in self.brain.today_entries()))
        self.assertFalse(feelings.overview()['pending'])

    def test_existing_wait_holds_new_plan_before_any_model_call(self):
        self.prepare()
        feelings.sent(now=self.now)
        self.assertEqual({'held':1},self.tick()['actions'])
        self.assertEqual(0,self.model_calls)
        self.assertEqual([],self.requests)

    def test_invalid_private_route_cannot_send_to_a_different_person(self):
        self.prepare()
        self.config['delivery']['routes'][0]['target']=999999
        self.save_config()
        self.assertEqual({'blocked':1},self.tick()['actions'])
        self.assertEqual(0,self.model_calls)
        self.assertEqual([],self.requests)

    def test_falling_asleep_during_generation_stops_sending(self):
        self.prepare()
        clock=[self.now]
        def sleeping():
            clock[0]=model.parse_ts(self.now).replace(hour=23,minute=30).isoformat()
        with patch.object(model,'now_iso',side_effect=lambda:clock[0]):
            self.tick(during_model=sleeping)
        self.assertEqual([],self.requests)
        self.assertFalse(feelings.overview()['pending'])

    def task(self, progress):
        from organs.day import plan
        from organs.day.tests.test_proactive import home_at
        from organs.home import cards
        home=home_at(self.day_root)
        self.home_root=home
        active_home=patch.dict(os.environ,{'HOME_ROOT':str(home)})
        active_home.start();self.addCleanup(active_home.stop)
        task=cards.append_cards(root=home,role='agent',by='agent',cards=[{
            'kind':'物件','room':'工作台','name':'整理书架','progress':progress,
            'params':{'task':True,'due':self.now}}])['cards'][0]
        state=plan.load(self.day_root)
        row=next(r for r in state['pending'] if r['id']==self.plan['id'])
        row.update(reason_code='pending_task',note='整理书架',task={
            'card_id':task['card_id'],'name':task['name'],'due':self.now,'when':'12:00'})
        plan.save(state,self.day_root)
        return home, task

    def test_completed_task_is_checked_before_spending_a_model_call(self):
        self.prepare()
        self.task(100)
        result=self.tick()
        self.assertEqual({'declined':1},result['actions'])
        self.assertEqual(0,self.model_calls)
        self.assertEqual([],self.requests)

    def test_alternate_heart_profile_cannot_write_feedback_to_default_profile(self):
        self.prepare()
        other=self.tmp/'other-heart'
        with patch.dict(os.environ,{'HEART_ROOT':str(other)}):
            result=self.tick()
        self.assertEqual({'blocked':1},result['actions'])
        self.assertEqual('alternate-runtime-roots',result['rows'][0]['reason'])
        self.assertEqual(0,self.model_calls)
        self.assertEqual([],self.requests)
        self.assertFalse(other.exists())

    def test_alternate_day_profile_cannot_use_default_mind(self):
        self.prepare()
        with patch.dict(os.environ,{'DAY_ROOT':str(self.tmp/'other-day')}):
            result=self.tick()
        self.assertEqual({'blocked':1},result['actions'])
        self.assertEqual(0,self.model_calls)
        self.assertEqual([],self.requests)

    def test_alternate_home_profile_cannot_supply_the_wrong_personal_context(self):
        self.prepare()
        other=self.tmp/'other-house';other.mkdir()
        with patch.dict(os.environ,{'HOME_ROOT':str(other)}):
            result=self.tick()
        self.assertEqual({'blocked':1},result['actions'])
        self.assertEqual(0,self.model_calls)
        self.assertEqual([],self.requests)

    def test_task_completed_during_generation_is_not_reminded(self):
        from organs.home import cards
        self.prepare()
        home,task=self.task(0)
        def completed():
            cards.set_progress(root=home,role='agent',by='agent',room='工作台',
                               card_id=task['card_id'],progress=100,reason='测试已完成')
        result=self.tick(during_model=completed)
        self.assertEqual({'declined':1},result['actions'])
        self.assertEqual('task-changed',result['rows'][0]['reason'])
        self.assertGreater(self.model_calls,0)
        self.assertEqual([],self.requests)
