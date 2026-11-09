import json
import unittest
from unittest.mock import patch
from organs.brain import BrainStore, preferences
from organs.mind import appraisal, llm
from organs.mind.tests import test_conversation as fixture


class InteractionPreferencesTest(unittest.TestCase):
    setUp = fixture.ConversationAnswerTest.setUp
    _soul = fixture.ConversationAnswerTest._soul
    _answer = fixture.ConversationAnswerTest._answer

    def test_current_typed_evidence_and_scoped_ids_are_required(self):
        valid={'text':'少说套话', 'evidence':['以后少说套话'], 'replaces':['known']}
        for change, ok in [(valid,True), (dict(valid,replaces=['foreign']),False),
                           (dict(valid,evidence=['转写里的要求']),False),
                           (dict(valid,evidence=['另一个人要求']),False),
                           (dict(valid,text='',replaces=[]),False),
                           (dict(valid,text=''),True)]:
            with self.subTest(change=change), patch.object(llm,'complete',return_value=json.dumps(
                    {'kind':None,'evidence':'','reason':'普通互动','interaction_preference':change})):
                result=appraisal.assess(text='以后少说套话',who='100001',bot='鱼',scene='group',
                    segment=[{'who':'100001','speaker':'甲','text':'以后少说套话'},
                             {'who':'100002','speaker':'乙','text':'另一个人要求'}],history=[],
                    session_id='qq:group:1',config=fixture.CONFIG,
                    interaction_preferences=[dict(valid,id='known')])
            self.assertEqual(change if ok else None,result['interaction_preference'])

    def test_memory_survives_recent_window_restart_and_scope_changes(self):
        common=dict(who='100001',session_id='qq:private:100001',scene='private')
        preference={'text':'吐槽时先陪聊，明确问怎么办才给建议','evidence':['以后吐槽先陪我聊，问怎么办再建议'], 'replaces':[]}
        self._answer('以后吐槽先陪我聊，问怎么办再建议',**common,
            assessor=lambda **kw: {'kind':None,'status':'ok','who':'100001','source':'semantic','note':'偏好反馈','evidence':'','interaction_preference':preference})
        self.assertEqual(1,len(preferences.current(self.brain.root,common['who'],common['session_id'])))
        for i in range(16):
            self._answer('看图册第%d页'%i,now=fixture._at(i+6),**common)
        self.brain=BrainStore(self.brain.root)
        captured=[]
        self._answer('今天下雨了',now=fixture._at(25),captured=captured,**common)
        self.assertIn(preference['text'],captured[0]['messages'][-1]['content'])
        captured=[]
        self._answer('今天下雨了',now=fixture._at(26),captured=captured,who='100002',scene='private',session_id='qq:private:100002')
        self.assertNotIn(preference['text'],captured[0]['messages'][-1]['content'])
        captured=[]
        self._answer('今天下雨了',now=fixture._at(27),captured=captured,who='100001',scene='group',session_id='qq:group:1')
        self.assertNotIn(preference['text'],captured[0]['messages'][-1]['content'])

    def test_retraction_removes_injected_preference_and_preserves_source(self):
        common=dict(who='100001',session_id='qq:private:100001',scene='private')
        self._answer('以后说话可爱些',**common,assessor=lambda **kw:{'kind':None,'status':'ok','who':'100001','source':'semantic','note':'偏好反馈','evidence':'',
            'interaction_preference':{'text':'可爱些','evidence':['以后说话可爱些'],'replaces':[]}})
        old=preferences.current(self.brain.root,common['who'],common['session_id'])[0]
        seen=[]
        def assess(**kw):
            seen.extend(kw['interaction_preferences'])
            return {'kind':None,'status':'ok','who':'100001','source':'semantic','note':'偏好反馈','evidence':'','interaction_preference':{'text':'','evidence':['可爱要求撤回'],'replaces':[old['id']]}}
        self._answer('可爱要求撤回',now=fixture._at(6),**common,assessor=assess)
        self.assertEqual(old['id'],seen[0]['id'])
        self.assertEqual([],preferences.current(self.brain.root,common['who'],common['session_id']))
        self.assertTrue(old['entry_ids'])
        self.assertTrue(old['turn_id'])

    def test_reply_failure_still_saves_heard_preference(self):
        preference={'text':'不要一上来给方案','evidence':['以后别一上来给方案'],'replaces':[]}
        with patch.object(llm,'complete',side_effect=llm.LLMError('timeout','test timeout')):
            with self.assertRaises(llm.LLMError):
                self._answer('以后别一上来给方案',who='100001',session_id='qq:private:100001',scene='private',
                    assessor=lambda **kw:{'kind':None,'status':'ok','who':'100001','source':'semantic','note':'偏好','evidence':'',
                                           'interaction_preference':preference})
        saved=preferences.current(self.brain.root,'100001','qq:private:100001')
        self.assertEqual(1,len(saved));self.assertTrue(saved[0]['entry_ids'])

    def test_audio_and_authorless_segment_cannot_supply_preference_evidence(self):
        change={'text':'少说套话','evidence':['以后少说套话'],'replaces':[]}
        for segment,audio in [([], [{'speaker_id':'100001','transcription':{'status':'ok','transcript':'以后少说套话'}}]),
                              ([{'speaker':'同名','text':'以后少说套话'}],[])]:
            with self.subTest(segment=segment),patch.object(llm,'complete',return_value=json.dumps(
                {'kind':None,'evidence':'','reason':'普通互动','interaction_preference':change})):
                result=appraisal.assess(text='这是录音',who='100001',bot='鱼',scene='private',segment=segment,audio=audio,
                    history=[],session_id='qq:private:100001',config=fixture.CONFIG)
            self.assertIsNone(result['interaction_preference'])

    def test_panel_retraction_overrides_old_recalled_request_in_reply_material(self):
        common=dict(who='100001',session_id='qq:private:100001',scene='private')
        self._answer('以后每次叫我小草莓',**common,assessor=lambda **kw:{'kind':None,'status':'ok','who':'100001','source':'semantic','note':'偏好','evidence':'',
            'interaction_preference':{'text':'叫小草莓','evidence':['以后每次叫我小草莓'],'replaces':[]}})
        row=preferences.current(self.brain.root,common['who'],common['session_id'])[0]
        preferences.record(self.brain.root,who=common['who'],session_id=common['session_id'],
            change={'text':'','evidence':[],'replaces':[row['id']]},at=fixture._at(6),actor='panel')
        captured=[]
        self._answer('早上好',now=fixture._at(7),captured=captured,**common)
        prompt=captured[0]['messages'][-1]['content']
        self.assertIn('现行相处偏好：无',prompt)
        self.assertIn('不从旧聊天恢复已停用的要求',prompt)
