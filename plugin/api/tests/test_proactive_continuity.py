"""真实聊天/回执/下轮装配；仅模型响应替身，不发QQ或访问模型。"""
import unittest
from datetime import timedelta
from unittest.mock import patch

from api import api
from organs.brain import model
from organs.heart import proactive
from organs.mind import recent
from . import test_chat_api as fixture


class ProactiveContinuityTest(unittest.TestCase):
    setUp = fixture.ChatApiTest.setUp
    _answer = fixture.ChatApiTest._answer
    _post = fixture.ChatApiTest._post

    def draft(self, scene='private'):
        body = {'mode': 'proactive', 'seed': '想聊聊今天的书店',
                'who': fixture.CHANNEL_WHO, 'scene': scene, 'scope': scene,
                'delivery_ack': True}
        if scene == 'group':
            body['where'] = {'groupId': 70001}
        status, result = self._post(body, fixture._body('我还惦记着那家书店。想去看看。'))
        self.assertEqual(200, status)
        return result['delivery_id']

    def ack(self, ident, text='我还惦记着那家书店。'):
        code, result = api.handle('POST', '/chat/ack', {'delivery_id': ident, 'text': text},
                                  self.store, brain=self.brain)
        self.assertEqual((200, True), (code, result['ok']))

    def test_confirmed_proactive_text_is_remembered_then_reply_settles_wait(self):
        token = self.draft()
        self.assertFalse(proactive.overview()['pending'])
        self.assertFalse(any(r.get('side') == model.SIDE_SELF for r in self.brain.today_entries()))
        self.ack(token)
        rows = self.brain.today_entries()
        self.assertEqual(['我还惦记着那家书店。'], [r['quote'] for r in rows])
        saved = recent.load()['turns'][-1]
        self.assertEqual('proactive', saved.get('mode'))
        self.assertEqual('', saved['said'])
        rendered = recent.render([saved])
        self.assertIn('她主动说', rendered)
        self.assertNotIn(str(fixture.CHANNEL_WHO['id']) + '：', rendered)
        self.assertNotIn('想去看看', rendered)
        state = proactive.overview()
        self.assertTrue(state['pending'])
        self.assertEqual(token, state['history'][-1]['context']['delivery_id'])
        capture = []
        status, reply = self._post({'text': '周末一起去呀', 'who': fixture.CHANNEL_WHO,
                                   'scene': 'private', 'delivery_ack': True},
                                  fixture._body('好呀。', capture))
        self.assertEqual(200, status)
        self.assertIn('她主动说', "\n\n".join(row["content"] for row in capture[0]['messages']))
        self.assertIn('我还惦记着那家书店。', "\n\n".join(row["content"] for row in capture[0]['messages']))
        self.assertFalse(proactive.overview()['pending'])
        self.assertEqual('replied', proactive.overview()['history'][-1]['outcome'])
        self.ack(token)
        self.assertFalse(proactive.overview()['pending'])
        self.assertEqual(1, len(proactive.overview()['history']))

    def test_other_people_and_group_chatter_cannot_settle_original_group_wait(self):
        token = self.draft('group')
        self.ack(token)
        self.assertTrue(proactive.overview()['pending'])
        cases = [
            {'who': {'id': 987654321}, 'where': {'groupId': 70001}, 'directed': True},
            {'who': fixture.CHANNEL_WHO, 'where': {'groupId': 70002}, 'directed': True},
            {'who': fixture.CHANNEL_WHO, 'where': {'groupId': 70001}, 'directed': False},
            {'who': fixture.CHANNEL_WHO, 'where': {'groupId': 70001}},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                self._post(dict(fields, text='今天吃了面', scene='group', delivery_ack=True), fixture._body('嗯。'))
                self.assertTrue(proactive.overview()['pending'])
        self._post({'who': fixture.CHANNEL_WHO, 'where': {'groupId': 70001},
                    'scene': 'group', 'directed': True, 'text': '鱼，周末一起去呀', 'delivery_ack': True},
                   fixture._body('好呀。'))
        self.assertFalse(proactive.overview()['pending'])

    def test_later_private_return_clears_pause_without_rewriting_ignored_history(self):
        import warnings
        for _ in range(3):
            self.ack(self.draft())
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                proactive.outcome('ignored',now=model.now_iso())
        before=proactive.overview()
        self.assertTrue(before['muted'])
        for body in [
            {'who':{'id':987654321},'scene':'private'},
            {'who':fixture.CHANNEL_WHO,'scene':'group','where':{'groupId':70001},'directed':True},
        ]:
            self._post(dict(body,text='我来啦',delivery_ack=True),fixture._body('嗯。'))
            self.assertTrue(proactive.overview()['muted'])
        self._post({'text':'刚忙完，来找你聊会儿','who':fixture.CHANNEL_WHO,'scene':'private','delivery_ack':True},fixture._body('好呀。'))
        after=proactive.overview()
        self.assertFalse(after['muted'])
        self.assertEqual(0,after['streak'])
        self.assertEqual(before['history'],after['history'])
        self.assertFalse(after['pending'])

    def test_group_return_after_ignored_requires_same_person_and_directed_message(self):
        import warnings
        for _ in range(3):
            self.ack(self.draft('group'))
            with warnings.catch_warnings():
                warnings.simplefilter('ignore');proactive.outcome('ignored',now=model.now_iso())
        before=proactive.overview()['history']
        for fields in [
            {'who':fixture.CHANNEL_WHO,'where':{'groupId':70001},'directed':False},
            {'who':{'id':987654321},'where':{'groupId':70001},'directed':True},
            {'who':fixture.CHANNEL_WHO,'where':{'groupId':70002},'directed':True},
        ]:
            self._post(dict(fields,text='今天吃了面',scene='group',delivery_ack=True),fixture._body('嗯。'))
            self.assertTrue(proactive.overview()['muted'])
        self._post({'who':fixture.CHANNEL_WHO,'where':{'groupId':70001},'directed':True,
                    'text':'鱼，我回来啦','scene':'group','delivery_ack':True},fixture._body('好呀。'))
        self.assertFalse(proactive.overview()['muted'])
        self.assertEqual(before,proactive.overview()['history'])

    def test_empty_ack_and_old_unattributed_wait_do_not_gain_new_meaning(self):
        self.ack(self.draft(), '')
        self.assertFalse(proactive.overview()['pending'])
        self.assertEqual([], self.brain.today_entries())
        proactive.sent(now=model.now_iso())
        before = proactive.overview()['history']
        self._post({'text': '你好', 'who': fixture.CHANNEL_WHO, 'scene': 'private'}, fixture._body('你好呀。'))
        self.assertEqual(before, proactive.overview()['history'])

    def test_invalid_directed_is_rejected_before_model_or_feedback(self):
        with patch('organs.mind.conversation.answer', side_effect=AssertionError('不应生成')):
            status, result = api.handle('POST', '/chat', {'text': '你好', 'directed': 'true'},
                                       self.store, brain=self.brain)
        self.assertEqual(400, status)
        self.assertIn('directed', result['message'])

    def test_missing_recipient_or_scene_cannot_occupy_the_real_wait(self):
        for fields in ({}, {'scene': 'private'}, {'who': fixture.CHANNEL_WHO},
                       {'who': fixture.CHANNEL_WHO, 'scene': 'group'}):
            with self.subTest(fields=fields):
                code, payload = self._post(dict(fields, mode='proactive', seed='想聊聊书店', delivery_ack=True),
                                           fixture._body('想去书店。'))
                self.assertEqual(200, code)
                self.ack(payload['delivery_id'])
                self.assertFalse(proactive.overview()['pending'])
        self.ack(self.draft())
        self.assertTrue(proactive.overview()['pending'])

    def test_input_before_confirmed_send_cannot_settle_future_wait(self):
        token = self.draft()
        noon = model.parse_ts(model.now_iso())
        with patch.object(model, 'now_iso', return_value=(noon + timedelta(minutes=10)).isoformat()):
            self.ack(token)
        for minutes, pending in ((5, True), (11, False)):
            with patch.object(model, 'now_iso', return_value=(noon + timedelta(minutes=minutes)).isoformat()):
                self._post({'text': '周末一起去', 'who': fixture.CHANNEL_WHO, 'scene': 'private'},
                           fixture._body('好呀。'))
            self.assertEqual(pending, proactive.overview()['pending'])
