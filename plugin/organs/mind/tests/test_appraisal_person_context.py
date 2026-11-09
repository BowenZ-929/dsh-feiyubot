import json
import unittest
from unittest.mock import patch
from organs.mind import appraisal, conversation, llm
from organs.brain import model
from organs.mind.tests import test_conversation as fixture


class AppraisalPersonContextTest(unittest.TestCase):
    setUp = fixture.ConversationAnswerTest.setUp
    _soul = fixture.ConversationAnswerTest._soul
    _answer = fixture.ConversationAnswerTest._answer

    def test_missing_art_home_does_not_stop_plain_chat_or_create_a_home(self):
        result, context, requests = self.capture()
        self.assertEqual([], context['drawing_assets'])
        self.assertEqual(2, len(requests))
        self.assertEqual('看到了。', result['reply'])
        self.assertFalse((self.tmp/'home'/'home').exists())

    def test_invalid_art_home_configuration_is_not_silenced(self):
        from organs.home.store import StoreError
        with patch('organs.home.art.library', side_effect=StoreError('HOME_ROOT_EMPTY')):
            with self.assertRaises(StoreError):
                self.capture()

    def capture(self, **kwargs):
        requests=[]
        def transport(url,headers,payload,timeout):
            requests.append(payload)
            answer='{"kind":null,"evidence":"","reason":"普通聊天"}' if payload['messages'][0]['content'].startswith(appraisal.PROMPT) else '看到了。'
            return 200,json.dumps({'choices':[{'message':{'content':answer}}]}).encode()
        result=self._answer('今天回来了。',who='100001',scene='group',session_id='qq:group:1',
                            now=fixture._at(15),assessor=appraisal.assess,transport=transport,**kwargs)
        return result,json.loads(requests[0]['messages'][1]['content']),requests

    def test_delayed_correction_reaches_understanding_and_is_saved_with_source(self):
        common=dict(scene='group',session_id='qq:group:1')
        old='11月14日下午想去河岸图书馆。'
        current='还是改去松林公园吧，原来的时间不变。'
        first=self._answer(old,who='100001',now=fixture._at(0),**common)
        for i in range(13):
            self._answer(f'植物图册第{i}页挺好看。',who='100002',now=fixture._at(i+1),**common)
        requests=[]
        def transport(url,headers,payload,timeout):
            requests.append(payload)
            answer=(json.dumps({'kind':None,'evidence':'','reason':'明确更正',
                'memory_note':{'text':'11月14日下午改去松林公园。','current':[current],'previous':[old]}},ensure_ascii=False)
                if payload['messages'][0]['content'].startswith(appraisal.PROMPT) else '那下午去公园。')
            return 200,json.dumps({'choices':[{'message':{'content':answer}}]}).encode()
        result=self._answer(current,who='100001',now=fixture._at(15),assessor=appraisal.assess,transport=transport,**common)
        context=json.loads(requests[0]['messages'][1]['content'])
        self.assertIn(old,[row['text'] for row in context['own_history']])
        self.assertIn('[本轮指代理解',requests[1]['messages'][-1]['content'])
        entries=self.brain.person_context('100001','qq:group:1',fixture._at(16))['turns'][-1]['entries']
        row=next(r for r in entries if r.get('quote')==current)
        self.assertIn('11月14日下午改去松林公园',row['note'])
        self.assertEqual([{'turn_id':first['turn_id'],'quote':old}],row['context_refs'])
        self.assertEqual(2,len(requests),'不增加模型阶段')

    def test_supplement_only_contains_own_live_same_session_speech(self):
        common=dict(ts=fixture._at(),turn_id='shared',session_id='qq:group:1',scene='group')
        self.brain.ingest('100001','本人原话。',**common)
        self.brain.ingest('100002','旁人的原话。',**common)
        self.brain.ingest('100001','她的回复，不是本人说的。',side=model.SIDE_SELF,**common)
        for turn,changes in [('other-session',{'session_id':'qq:private:100001'}),
                             ('private',{'audience':model.AUDIENCE_PRIVATE}),
                             ('future',{'ts':fixture._at(30)})]:
            self.brain.ingest('100001',turn,**dict(common,turn_id=turn,**changes))
        stale=self.brain.ingest('100001','已退役的说法',**dict(common,turn_id='stale'))
        self.brain.retire(stale['entry']['id'])
        with patch.object(self.brain,'person_context',wraps=self.brain.person_context) as read:
            _,context,requests=self.capture()
        self.assertEqual(['本人原话。'],[r['text'] for r in context['own_history']])
        self.assertEqual(1,read.call_count,'理解与回复共享一次脑读取')
        self.assertEqual(2,len(requests))

    def test_recent_is_not_duplicated_in_own_history(self):
        self._answer('本人原安排。',who='100001',scene='group',session_id='qq:group:1',now=fixture._at())
        _,context,_=self.capture()
        self.assertEqual(['本人原安排。'],[r['text'] for r in context['own_history']])

    def test_excerpt_budget_does_not_leak_original_or_rendering_labels(self):
        original='旧内容很长。'*300+'末尾本人已更正。'
        self.brain.ingest('100001',original,ts=fixture._at(),turn_id='long',session_id='qq:group:1',scene='group')
        _,context,_=self.capture()
        own=context['own_history']
        self.assertEqual(1,len(own))
        self.assertIn('末尾本人已更正。',own[0]['text'])
        self.assertIn(own[0]['text'],original)
        self.assertNotIn('摘录',own[0]['text'])
        self.assertLessEqual(sum(len(r['text']) for r in own),1000)

    def test_disabled_person_context_does_not_read_extra_history(self):
        self.brain.ingest('100001','以前的安排。',ts=fixture._at(),turn_id='old',session_id='qq:group:1',scene='group')
        assembly=conversation.load_assembly()
        assembly['order'].remove('person_context')
        with patch.object(self.brain,'person_context',wraps=self.brain.person_context) as read:
            _,context,_=self.capture(assembly=assembly)
        self.assertEqual([],context['own_history'])
        read.assert_not_called()

    def test_supplement_cannot_authorize_an_old_drawing_request(self):
        old='给我画一只红猫。'
        with patch.object(llm,'complete',return_value=json.dumps({'kind':None,'evidence':'','reason':'误把旧话当请求',
                'painting':{'action':'start','evidence':[old],'prompt':'红猫'}})):
            result=appraisal.assess(text='今天回来了。',who='100001',bot='测试鱼',scene='group',segment=[],history=[],
                session_id='qq:group:1',config=fixture.CONFIG,
                own_context=[{'text':old,'ts':fixture._at(),'turn_id':'old'}])
        self.assertIsNone(result['painting'])

    def test_overlapping_excerpts_of_one_turn_keep_unique_context_reference(self):
        previous='周六去河岸图书馆。'
        current='改去松林公园。'
        data={'kind':None,'evidence':'','reason':'地点更正','memory_note':{
            'text':'周六改去松林公园。','current':[current],'previous':[previous]}}
        with patch.object(llm,'complete',return_value=json.dumps(data)):
            result=appraisal.assess(text=current,who='100001',bot='测试鱼',scene='group',segment=[],
                history=[{'who':'100001','said':'前段（中段省略）'+previous,'ts':fixture._at(),'turn_id':'long'}],
                own_context=[{'text':'后段'+previous,'ts':fixture._at(),'turn_id':'long'}],config=fixture.CONFIG)
        self.assertEqual([{'turn_id':'long','quote':previous}],result['memory_note'].get('context_refs'))

    def test_material_expiring_this_turn_is_not_cached_into_either_model_stage(self):
        old='早就过时的河岸图书馆计划。'
        self.brain.ingest('100001',old,ts='2020-01-01T14:00:00+09:00',turn_id='old',
                          session_id='qq:group:1',scene='group')
        self.brain.ingest('100002','另一个人的近期闲聊',ts=fixture._at(),turn_id='new',
                          session_id='qq:group:1',scene='group')
        _,context,requests=self.capture()
        self.assertNotIn(old,json.dumps(context,ensure_ascii=False))
        self.assertNotIn(old,json.dumps(requests[1]['messages'],ensure_ascii=False))
        self.assertTrue(any(row.get('turn_id')=='old' for row in self.brain.cold_entries()))


if __name__=='__main__':
    unittest.main()
