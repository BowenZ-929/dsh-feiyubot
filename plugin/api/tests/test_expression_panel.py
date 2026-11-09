"""管理表达的真实落盘开关；隔离语料库，不触发手机或模型。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import api
from organs.home import expressions
from organs.mind import expression_learning, llm
from organs.mind.tests.test_expressions import SNAPSHOT, CARD


class ExpressionPanelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"HOME_ROOT": str(self.root), "QQAGENT_HOME": str(self.root / "organs"),
                                     "DAY_ROOT": str(self.root / "organs/day")})
        env.start(); self.addCleanup(env.stop)
        self.row = expression_learning.learn(SNAPSHOT, context="游戏福利评论", complete=lambda _: json.dumps([CARD]))

    def test_browse_links_use_exact_acquisition_events_without_writing(self):
        folder = self.root / 'organs/day/phone-sessions'
        folder.mkdir(parents=True)
        event = {'kind': 'expressions', 'record_id': self.row['id']}
        jobs = [
            {'id': 'first', 'created_at': '2026-11-08T10:00:00+09:00', 'status': 'done',
             'title': '相同标题', 'model_turn_id': 'turn-first', 'events': [event, event], 'private_debug': '不应返回'},
            {'id': 'second', 'created_at': '2026-11-08T11:00:00+09:00', 'status': 'partial', 'events': [event]},
            {'id': 'unrelated', 'created_at': '2026-11-08T12:00:00+09:00', 'status': 'done',
             'title': '相同标题', 'events': [{'kind': 'expressions', 'record_id': 'other'},
                                         {'kind': 'expression_review', 'record_id': self.row['id']}]},
        ]
        for job in jobs:
            if job['id'] == 'first':
                job['video_source'] = {'status': 'ok', 'url': 'https://b23.tv/X6f5iyP'}
            (folder / (job['id'] + '.json')).write_text(json.dumps(job))
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(llm, 'complete') as complete:
            status, data = api.handle('GET', '/learning/expressions', None, None)
        self.assertEqual(200, status)
        self.assertEqual([
            {'id': 'second', 'created_at': jobs[1]['created_at'], 'title': '', 'status': 'partial', 'model_turn_id': '', 'video_source': {}},
            {'id': 'first', 'created_at': jobs[0]['created_at'], 'title': '相同标题', 'status': 'done', 'model_turn_id': 'turn-first', 'video_source': jobs[0]['video_source']},
        ], data['records'][0].get('browse_sessions'))
        complete.assert_not_called()
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_list_preserves_evidence_and_reads_never_write_or_call_model(self):
        expressions.record_use(self.row['cards'], session_id='test-session', delivery_id='one')
        expressions.record_use(self.row['cards'], session_id='test-session', delivery_id='two')
        before = {p: p.read_bytes() for p in expressions.library_root().glob('*')}
        with patch.object(llm, 'complete') as complete:
            status, data = api.handle('GET', '/learning/expressions', None, None)
        self.assertEqual(200, status)
        complete.assert_not_called()
        row = data['records'][0]
        self.assertEqual([], row.get('browse_sessions'))
        self.assertFalse(row['enabled'])
        self.assertEqual(CARD['quote'], row['cards'][0]['quote'])
        self.assertEqual(SNAPSHOT['at'], row['source']['at'])
        self.assertEqual(2, row['cards'][0]['offered_count'])
        self.assertTrue(row['cards'][0]['last_offered_at'])
        self.assertNotIn('test-session', json.dumps(data))
        self.assertEqual(before, {p: p.read_bytes() for p in expressions.library_root().glob('*')})

    def test_sent_match_counts_only_selected_delivered_text_without_exposing_chat(self):
        from organs.brain import BrainStore
        from organs.mind import delivery, recent
        brain = BrainStore(self.root/'organs/brain')
        card = self.row['cards'][0]
        for number, case in enumerate(('pending','empty','other','matched','unselected','sleep')):
            ident = f'{number:032x}'
            record = {'ts':'2026-11-09T12:00:00+09:00','turn_id':ident,'msg_id':ident,
                      'who':'private-person','speaker':'鱼','scene':'private','session_id':'private-session'}
            if case != 'unselected':
                record['expression_references'] = [{'id':card['id'],'phrase':card['phrase']}]
            if case == 'sleep':
                record['sleep_notice'] = {'who':'private-person','wake_at':'2026-11-10T08:00:00+09:00','text':card['phrase']}
            delivery.stage(ident,record)
            recent.append(dict(record, said='虚构输入', reply=''))
            if case != 'pending':
                text = '' if case == 'empty' else '只有前半句送到了' if case == 'other' else card['phrase']+' 私聊秘密尾句'
                self.assertTrue(delivery.confirm(ident,text,brain))
                self.assertTrue(delivery.confirm(ident,text,brain))
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        status,data=api.handle('GET','/learning/expressions',None,None)
        self.assertEqual(200,status)
        found=data['records'][0]['cards'][0]
        self.assertEqual(1,found.get('sent_match_count'))
        self.assertEqual(f'{3:032x}',found.get('last_sent_turn_id'))
        self.assertTrue(found.get('last_sent_at'))
        for secret in ('private-person','private-session','私聊秘密尾句'):
            self.assertNotIn(secret,json.dumps(data,ensure_ascii=False))
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_offered_reference_links_its_pending_turn_without_claiming_sent_match(self):
        from organs.mind import delivery
        card = self.row['cards'][0]
        expressions.record_use([card], session_id='private-session', delivery_id='a'*32)
        delivery.stage('a'*32, {'turn_id':'b'*32, 'text':'private-draft',
                              'session_id':'private-session', 'confirmed':False})
        before = {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(llm, 'complete') as complete:
            status, data = api.handle('GET', '/learning/expressions', None, None)
        self.assertEqual(200, status)
        found = data['records'][0]['cards'][0]
        self.assertEqual('b'*32, found.get('last_offered_turn_id'))
        self.assertEqual(1, found['offered_count'])
        self.assertEqual(0, found['sent_match_count'])
        self.assertNotIn('private-draft', json.dumps(data))
        self.assertNotIn('private-session', json.dumps(data))
        complete.assert_not_called()
        self.assertEqual(before, {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        expressions.record_use([card], session_id='private-session', delivery_id='missing')
        _, data = api.handle('GET', '/learning/expressions', None, None)
        self.assertIsNone(data['records'][0]['cards'][0].get('last_offered_turn_id'))

    def test_latest_match_uses_confirmation_time_and_original_phrase_snapshot(self):
        from organs.mind import recent
        card = self.row['cards'][0]
        folder = recent.path_of().parent/'deliveries'
        folder.mkdir(parents=True)
        # 后遍历的文件有更大的本地时刻，但它实际更早；当前卡片已改词也不改历史。
        paths = [folder/'new.json', folder/'old.json']
        for path, at, turn in zip(paths, ('2026-11-09T04:00:00+00:00','2026-11-09T12:00:00+09:00'), ('a'*32,'b'*32)):
            path.write_text(json.dumps({'confirmed':True,'confirmed_at':at,'turn_id':turn,'text':'历史原句',
                'expression_references':[{'id':card['id'],'phrase':'历史原句'}]}))
        with patch.object(Path, 'glob', return_value=iter(paths)):
            # 只约束回执文件顺序，表达库读取沿用实际实现。
            from api import learning as endpoint
            with patch.object(expressions, 'records', return_value=[self.row]), \
                 patch.object(endpoint.phone_browse, 'view', return_value={'jobs':[]}):
                status, data = endpoint.dispatch('/learning/expressions', {}, None)
        self.assertEqual(200,status)
        found=data['records'][0]['cards'][0]
        self.assertEqual(2,found['sent_match_count'])
        self.assertEqual('a'*32,found['last_sent_turn_id'])
        self.assertEqual('2026-11-09T04:00:00+00:00',found['last_sent_at'])

    def test_toggle_controls_real_candidates_and_can_be_reversed(self):
        for enabled in (True, False):
            status, data = api.handle('POST', '/learning/expressions/set', {'id': self.row['id'], 'enabled': enabled}, None)
            self.assertEqual(200, status)
            self.assertEqual(enabled, data['enabled'])
            self.assertEqual(1 if enabled else 0, len(expressions.candidates()))
        self.assertEqual(405, api.handle('GET', '/learning/expressions/set', None, None)[0])

    def test_invalid_or_unknown_requests_do_not_change_library(self):
        before = {p: p.read_bytes() for p in expressions.library_root().glob('*')}
        for body in (None, {}, {'id': '../secret', 'enabled': True}, {'id': self.row['id'], 'enabled': 'true'},
                     {'id': self.row['id'], 'enabled': 1}, {'id': self.row['id'], 'enabled': True, 'cards': []}):
            self.assertEqual(400, api.handle('POST', '/learning/expressions/set', body, None)[0])
        self.assertEqual(404, api.handle('POST', '/learning/expressions/set', {'id': '0'*32, 'enabled': True}, None)[0])
        self.assertEqual(before, {p: p.read_bytes() for p in expressions.library_root().glob('*')})

    def test_rejected_source_cannot_be_enabled_or_selected_but_can_be_disabled(self):
        self.row['source_rejected'] = True
        self.row['enabled'] = True  # 旧开关不能让已驳回来源进入挑选。
        expressions.save(self.row)
        self.assertEqual([], expressions.candidates())
        status, data = api.handle('POST', '/learning/expressions/set', {'id': self.row['id'], 'enabled': True}, None)
        self.assertEqual(400, status)
        self.assertIn('来源', data['message'])
        status, data = api.handle('POST', '/learning/expressions/set', {'id': self.row['id'], 'enabled': False}, None)
        self.assertEqual(200, status)
        self.assertFalse(data['enabled'])

    def test_empty_extraction_is_visible_but_not_adoptable(self):
        self.row['cards'] = []
        expressions.save(self.row)
        self.assertEqual(200, api.handle('GET', '/learning/expressions', None, None)[0])
        self.assertEqual(400, api.handle('POST', '/learning/expressions/set', {'id': self.row['id'], 'enabled': True}, None)[0])


    def test_single_card_override_keeps_other_card_and_review_visible(self):
        second=dict(self.row['cards'][0],id=self.row['id']+':1',phrase='这福利')
        self.row['cards'].append(second)
        self.row['cards'][0]['review']={'adopt':False,'reason':'模型认为不适合'}
        expressions.save(self.row)
        body={'id':self.row['id'],'card_id':second['id'],'enabled':True}
        status,data=api.handle('POST','/learning/expressions/set',body,None)
        self.assertEqual(status,200)
        self.assertEqual([c['id'] for c in expressions.candidates()],[second['id']])
        row=api.handle('GET','/learning/expressions',None,None)[1]['records'][0]
        self.assertFalse(row['cards'][0]['enabled'])
        self.assertEqual(row['cards'][0]['review']['reason'],'模型认为不适合')
        self.assertEqual(row['cards'][1]['selection_source'],'user')
        self.assertEqual(api.handle('POST','/learning/expressions/set',dict(body,card_id='unknown'),None)[0],400)
