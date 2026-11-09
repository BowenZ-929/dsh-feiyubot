import json
import unittest
from pathlib import Path
from unittest.mock import patch
from organs.brain.tests.helpers import fresh_store
from organs.day import growth

AT = '2026-11-08T12:00:00+09:00'


class GrowthTest(unittest.TestCase):
    def test_limit_changes_are_audited_without_a_toggle_or_duplicate_event(self):
        initial = growth.limits(self.root)
        count = len(growth.config(self.root)['changes'])
        with patch('organs.day.model.now_iso', return_value=AT):
            result = growth.configure(True, root=self.root, interval_minutes=30, daily_cap=24)
        self.assertEqual(len(result['changes']), count + 1)
        self.assertEqual(result['changes'][-1]['changes'], {
            'interval_minutes': {'before':initial['interval_minutes'], 'after':30},
            'daily_cap': {'before':initial['daily_cap'], 'after':24}})
        self.assertEqual(result['changes'][-1]['at'], AT)
        same = growth.configure(True, root=self.root, interval_minutes=30, daily_cap=24)
        self.assertEqual(same['changes'], result['changes'])
        before = (self.root/'growth.json').read_bytes()
        with self.assertRaises(ValueError):
            growth.configure(True, root=self.root, interval_minutes=60, daily_cap=25)
        self.assertEqual((self.root/'growth.json').read_bytes(), before)

    def setUp(self):
        self.brain, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'day'
        growth.configure(True, root=self.root)

    def test_reflection_excludes_private_and_records_a_single_opportunity(self):
        public = self.brain.ingest('12345678', '公开讨论种薄荷', ts=AT, scene='group', audience='公开')['entry']
        self.brain.ingest('87654321', '私聊密码紫茄子', ts=AT, scene='private', session_id='qq:private:87654321')
        prompts = []
        def complete(messages):
            prompts.append(messages)
            return json.dumps({'patch': {'likes': '想试试薄荷'}, 'note': '这是我的选择', 'sources': [public['id']]})
        result = growth.reflect(AT, root=self.root, brain=self.brain, persona='自己的性情', complete=complete)
        self.assertEqual(result['status'], 'ok')
        self.assertNotIn('紫茄子', json.dumps(prompts, ensure_ascii=False))
        self.assertEqual(self.brain.current_profile()['likes'], '想试试薄荷')
        growth.reflect(AT, root=self.root, brain=self.brain, persona='', complete=complete)
        self.assertEqual(len(prompts), 1)

    def test_failure_consumes_opportunity_and_never_changes_profile(self):
        prompts = []
        def complete(messages):
            prompts.append(messages)
            return '{bad'
        result = growth.reflect(AT, root=self.root, brain=self.brain, persona='', complete=complete)
        self.assertEqual(result['status'], 'failed')
        growth.reflect(AT, root=self.root, brain=self.brain, persona='', complete=complete)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.brain.current_profile(), {})

    def test_forged_sources_rejected_and_no_change_does_not_append(self):
        result = growth.reflect(AT, root=self.root, brain=self.brain, persona='', complete=lambda _: json.dumps({
            'patch': {'likes': 'B'}, 'note': '伪造依据', 'sources': ['private-secret']}))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.brain.selfnarration_versions(), [])
        result = growth.reflect('2026-11-09T12:00:00+09:00', root=self.root, brain=self.brain,
            persona='', complete=lambda _: json.dumps({'patch': {}, 'note': '保持', 'sources': []}))
        self.assertFalse(result['changed'])
        self.assertEqual(self.brain.selfnarration_versions(), [])

    def test_disable_does_not_stop_manual_browse(self):
        with patch('organs.day.phone_browse.view', return_value={'active': 'manual', 'jobs': [
            {'id': 'manual', 'origin': 'manual'}]}), patch('organs.day.phone_browse.stop') as stop:
            growth.configure(False, root=self.root)
        stop.assert_not_called()

    def test_disable_during_reflection_does_not_write_profile(self):
        def complete(_):
            growth.configure(False, root=self.root)
            return json.dumps({'patch': {'likes': '新喜好'}, 'note': '想试', 'sources': []})
        result = growth.reflect(AT, root=self.root, brain=self.brain, complete=complete)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(self.brain.current_profile(), {})

    def test_actual_own_writing_can_inform_next_day_growth(self):
        from organs.day import autonomy
        from organs.home import cards
        home = self.root / 'home'
        written = cards.append_cards(home, role='organ', by='日子线·自主写作', cards=[{
            'room':'工作台', 'kind':'物件', 'name':'随笔-测试',
            'params':{'scope':'public', 'text':'今天写了月亮的短诗。'}}])['cards'][0]
        captured = []
        def complete(messages):
            captured.extend(messages)
            return json.dumps({'patch':{'views':'我想继续尝试短诗'}, 'note':'写过后还想试', 'sources':[written['card_id']]})
        result = growth.reflect('2026-11-09T12:00:00+09:00', root=self.root, brain=self.brain, complete=complete)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('今天写了月亮的短诗', json.dumps(captured, ensure_ascii=False))

    def test_she_decides_suggestions_and_keeps_history_across_nights(self):
        proposals = [{'id': 'p1', 'target': 'manner.stance', 'value': '先接话', 'reason': '程序关键词', 'evidence': ['private-secret']},
                     {'id': 'p2', 'target': 'manner.stance', 'value': '每次都道歉'},
                     {'id': 'p3', 'target': 'manner.stance', 'value': '少说话'}]
        (self.brain.root/'proposals.json').write_text(json.dumps({'night_of':'old','proposals':proposals}))
        prompts = []
        def complete(messages):
            prompts.extend(messages)
            return json.dumps({'patch':{'stance':'先接话，不必每次道歉'},'note':'我想这样试试','sources':[],
                'decisions':[{'id':'p1','decision':'accept','reason':'我愿意先接话'},
                             {'id':'p2','decision':'reject','reason':'没有必要次次道歉'},
                             {'id':'p3','decision':'defer','reason':'还没想好'}]})
        result = growth.reflect(AT, root=self.root, brain=self.brain, complete=complete)
        self.assertEqual(result['status'], 'ok')
        self.assertNotIn('private-secret', json.dumps(prompts))
        self.assertEqual(self.brain.current_profile()['stance'], '先接话，不必每次道歉')
        rows = self.brain.proposals()['proposals']
        self.assertEqual([r['status'] for r in rows], ['accepted','rejected','pending'])
        self.assertEqual(rows[0]['actor'], '她自己')
        self.assertEqual(rows[2]['decision_reason'], '还没想好')
        self.brain.run_night('2026-11-09T12:00:00+09:00')
        rows = self.brain.proposals()['proposals']
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['decision_reason'], '我愿意先接话')

    def test_bad_decision_does_not_partially_change_profile(self):
        (self.brain.root/'proposals.json').write_text(json.dumps({'proposals':[{'id':'p1','target':'manner.stance','value':'轻声'}]}))
        result = growth.reflect(AT, root=self.root, brain=self.brain, complete=lambda _: json.dumps({
            'patch':{'stance':'轻声'},'note':'试试','sources':[],
            'decisions':[{'id':'forged','decision':'accept','reason':'想要'}]}))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.brain.current_profile(),{})
        self.assertNotIn('status',self.brain.proposals()['proposals'][0])

    def test_sleep_notice_is_growable_but_not_blank_or_long(self):
        self.brain.update_profile({'sleep_notice':'我先睡啦，明天见'},note='想这样说')
        self.assertEqual(self.brain.current_profile()['sleep_notice'],'我先睡啦，明天见')
        for value in ['', ' ', '字'*61]:
            with self.assertRaises(ValueError):
                self.brain.update_profile({'sleep_notice':value},note='不能变成静默或长文')

    def test_accepted_suggestion_reaches_shared_persona(self):
        from organs.mind import persona
        from organs.soul import load
        from api.tests.helpers import copy_fixture
        soul = load(copy_fixture('valid', self.root.parent/'soul-test'))
        (self.brain.root/'proposals.json').write_text(json.dumps({'proposals':[{'id':'p1','target':'manner.stance','value':'先接话'}]}))
        raw = {'patch':{'stance':'先听懂，再自然接话'},'note':'自己决定试试','sources':[],
               'decisions':[{'id':'p1','decision':'accept','reason':'想试试'}]}
        result = growth.reflect(AT,root=self.root,brain=self.brain,complete=lambda _:json.dumps(raw))
        self.assertEqual(result['status'],'ok')
        with patch('organs.day.growth.enabled',return_value=True):
            self.assertIn('【待人姿态】先听懂，再自然接话',persona.render(soul,self.brain))
        self.assertIn('【红线】',persona.render(soul,self.brain,root=self.root))

    def test_missing_final_stance_or_protected_patch_rejected_before_write(self):
        (self.brain.root/'proposals.json').write_text(json.dumps({'proposals':[{'id':'p1','target':'manner.stance','value':'先接话'}]}))
        for index, patch_value in enumerate([{}, {'stance':'接话','permissions':'all'}]):
            result = growth.reflect('2026-11-%02dT12:00:00+09:00' % (10+index),root=self.root,brain=self.brain,
                complete=lambda _, p=patch_value:json.dumps({'patch':p,'note':'试试','sources':[],
                    'decisions':[{'id':'p1','decision':'accept','reason':'想要'}]}))
            self.assertEqual(result['status'],'failed')
            self.assertEqual(self.brain.current_profile(),{})
