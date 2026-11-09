"""硬撑结束后无需新入站消息；只替换模型与协议传输，不发真实QQ。"""
import json
import os
import unittest
from unittest.mock import patch

from api import day
from organs.brain import model
from organs.hand import channel
from organs.heart import rest
from organs.mind import recent
from . import test_chat_api as fixture


class SleepDeliveryTest(unittest.TestCase):
    setUp = fixture.ChatApiTest.setUp
    _answer = fixture.ChatApiTest._answer
    _post = fixture.ChatApiTest._post

    def prepare(self, *, scene='private', qq=True, close=True):
        self.who = '123456789'
        self.day = self.tmp/'day'
        env = patch.dict(os.environ, {'DAY_ROOT':str(self.day)})
        env.start(); self.addCleanup(env.stop)
        if close:
            for _ in range(16):
                self.brain.interact(self.who, ts='2026-11-07T23:00:00+09:00')
        rest.advance(self.tmp/'heart', '2026-11-07T23:00:00+09:00')
        body = {'text':'陪我说会儿话','who':self.who,'scene':scene,'delivery_ack':True}
        if qq:
            body['where'] = {'userId':int(self.who)} if scene == 'private' else {'groupId':12345}
        with patch.object(model,'now_iso',return_value='2026-11-07T23:01:00+09:00'):
            code, _ = self._post(body, fixture._body('好，陪你一会儿。'))
        self.assertEqual(200, code)

    def tick(self, at='2026-11-07T23:16:00+09:00', *, receipt=None, root=None, debug_at=None):
        requests = []
        def post(url, body, timeout):
            requests.append(body)
            result = receipt if receipt is not None else {
                'ok':True,'sent':1,'confirmed_text':body['chunks'][0]['text'],'reason_code':'sent'}
            return 200, json.dumps(result).encode()
        with patch.object(model,'now_iso',return_value=at), \
             patch.object(channel,'_post_json',side_effect=post), \
             patch('organs.mind.llm.complete',side_effect=AssertionError('通知不需要模型')), \
             patch('organs.day.rhythm.tick',return_value={}), \
             patch('organs.day.life.tick',return_value={}), \
             patch('organs.day.phone_browse.tick',return_value={}):
            result = day.run_tick(debug_at or at, root=root, brain=self.brain, store=self.store)
        return requests, result

    def test_timer_notifies_qq_partner_with_current_text_and_actual_receipt_once(self):
        self.prepare()
        self.assertEqual([self.who],rest.state()['notice_targets'])
        self.assertEqual([],self.tick('2026-11-07T23:15:00+09:00')[0])
        self.brain.update_profile({'sleep_notice':'晚安啦，明天见'},note='自己的说法')
        requests, result = self.tick()
        self.assertEqual([{'scene':'private','target':int(self.who),'reply_kind':'sleep_notice',
                          'chunks':[{'text':'晚安啦，明天见'}]}],requests)
        self.assertTrue(result['sleep_notices'][0]['sent'])
        sleeping = rest.state()
        self.assertEqual('deep',sleeping['phase'])
        self.assertTrue(rest.notice_sent(self.who,sleeping['wake_at']))
        record = json.loads(next((recent.path_of().parent/'deliveries').glob(
            result['sleep_notices'][0]['delivery_id']+'.json')).read_text())
        self.assertTrue(record['confirmed'])
        self.assertEqual('晚安啦，明天见',record['text'])
        self.assertEqual('qq:private:'+self.who,record['session_id'])
        self.assertEqual('proactive',recent.load()['turns'][-1].get('mode'),
                         '自动告别不能伪造一条对方的新发言')
        self.assertEqual([],self.tick()[0])
        with patch.object(model,'now_iso',return_value='2026-11-07T23:17:00+09:00'):
            _, reply = self._post({'text':'在吗','who':self.who,'scene':'private','where':{},
                                  'delivery_ack':True},fixture._body('不应调用'))
        self.assertFalse(reply['send'])

    def test_failed_timer_is_recorded_without_repeating_or_claiming_sent(self):
        self.prepare()
        requests,result = self.tick(receipt={'ok':False,'sent':0,'confirmed_text':'','reason_code':'offline'})
        self.assertEqual(1,len(requests))
        self.assertFalse(result['sleep_notices'][0]['sent'])
        self.assertFalse(rest.notice_sent(self.who,rest.state()['wake_at']))
        path=recent.path_of().parent/'deliveries'/(result['sleep_notices'][0]['delivery_id']+'.json')
        record=json.loads(path.read_text())
        self.assertEqual('offline',record['transport_receipt']['reason_code'])
        self.assertFalse(record['confirmed'])
        self.assertEqual([],self.tick()[0])
        with patch.object(model,'now_iso',return_value='2026-11-07T23:17:00+09:00'):
            _, reply=self._post({'text':'还在吗','who':self.who,'scene':'private','where':{},
                                'delivery_ack':True},fixture._body('不应调用'))
        self.assertTrue(reply['send'])
        self.assertEqual('睡觉啦，明天说',reply['reply'])

    def test_inbound_notice_waiting_for_ack_does_not_also_send_from_timer(self):
        self.prepare()
        with patch.object(model,'now_iso',return_value='2026-11-07T23:16:00+09:00'):
            _, reply=self._post({'text':'还在吗','who':self.who,'scene':'private','where':{},
                                'delivery_ack':True},fixture._body('不应调用'))
        self.assertEqual('sleep_notice',reply['reply_kind'])
        self.assertFalse(rest.notice_sent(self.who,rest.state()['wake_at']))
        self.assertEqual([],self.tick()[0], '入站通知仍在等QQ回执，定时器不能再发一条')

    def test_panel_wake_does_not_register_qq_recipient(self):
        self.prepare(qq=False)
        self.assertEqual('holding',rest.state()['phase'])
        self.assertEqual([],self.tick()[0])

    def test_group_does_not_register_or_receive_notice(self):
        self.prepare(scene='group')
        self.assertEqual([],self.tick()[0])

    def test_unfamiliar_private_does_not_register_notice(self):
        self.prepare(close=False)
        self.assertEqual([],self.tick()[0])

    def test_morning_discards_pending_goodnight(self):
        self.prepare()
        self.assertEqual([],self.tick('2026-11-08T08:01:00+09:00')[0])
        self.assertEqual([],rest.state().get('notice_targets',[]))

    def test_alternate_tick_cannot_send_or_consume_live_recipients(self):
        self.prepare()
        before=rest.state()
        self.assertEqual([],self.tick(root=self.tmp/'other-day')[0])
        self.assertEqual(before,rest.state())

    def test_future_debug_tick_cannot_send_before_real_sleep_transition(self):
        self.prepare()
        self.assertEqual([],self.tick('2026-11-07T23:02:00+09:00',
            debug_at='2026-11-07T23:16:00+09:00')[0])
        self.assertEqual([self.who],rest.state()['notice_targets'])
        self.assertEqual([],self.tick('2026-11-07T23:03:00+09:00')[0])
        self.assertEqual(1,len(self.tick()[0]))
