import unittest
from unittest.mock import patch
from organs.mind import conversation, delivery, recent
from organs.brain import model
from organs.mind.tests import test_conversation as fixture


class PersonContextConversationTest(unittest.TestCase):
    setUp = fixture.ConversationAnswerTest.setUp
    _soul = fixture.ConversationAnswerTest._soul
    _answer = fixture.ConversationAnswerTest._answer

    def test_repair_survives_thirteen_turns_from_other_person(self):
        common = dict(scene='group', session_id='qq:group:1')
        self._answer('你刚才故意嘲笑我。', who='100001', now=fixture._at(0), **common)
        self._answer('是我误会你了，对不起。', who='100001', now=fixture._at(1), **common)
        for i in range(13):
            self._answer(f'植物图册第{i}页挺好看。', who='100002', now=fixture._at(i+2), **common)
        captured = []
        result = self._answer('我刚到家，买了一盆薄荷。', who='100001', now=fixture._at(15), captured=captured, **common)
        system = "\n\n".join(row["content"] for row in captured[0]['messages'])
        self.assertIn('是我误会你了，对不起。', system)
        self.assertIn('你刚才故意嘲笑我。', system)
        section = next(s for s in result['assembly']['sections'] if s['key']=='person_context')
        self.assertEqual(2, sum(r['in_prompt'] for r in section['turns']))
        self.assertEqual(1, len(captured))
        self.assertTrue(any('对不起' in r['text'] and r['source']=='person_context' for r in result['recalled']))
        for selected in result['memory_recall']['selected']:
            if selected['reason']=='person_context':
                self.assertEqual(['person_context'], selected['matched_by'])

    def test_full_recent_turn_is_not_repeated_but_truncated_recent_is_supplemented(self):
        first = self._answer('是我误会了，对不起。', who='100001', session_id='qq:group:1', scene='group')
        second = self._answer('买了薄荷。', who='100001', session_id='qq:group:1', scene='group', now=fixture._at(6))
        section = next(s for s in second['assembly']['sections'] if s['key']=='person_context')
        self.assertEqual('already_in_recent', section['turns'][0]['reason'])
        spec = conversation.load_assembly()['sections']['person_context']
        text, read = conversation._person_context(self.brain, '100001', 'qq:group:1', fixture._at(7),
            'group', spec, [{'turn_id':first['turn_id'], 'who':'100001', 'said':'是我误会了', 'reply':''}], '被截过的近聊')
        self.assertIn('是我误会了，对不起。', text)
        self.assertTrue(read['entries'])

    def test_long_turn_preserves_authors_and_tail_or_reports_omission(self):
        for who, text in [('100001', '开头不重要。'*400+'后来我道歉了。'), ('100002','这是小周说的话。')]:
            self.brain.ingest(who, text, ts=fixture._at(), turn_id='long', session_id='qq:group:1', scene='group')
        spec = conversation.load_assembly()['sections']['person_context']
        text, read = conversation._person_context(self.brain, '100001', 'qq:group:1', fixture._at(1), 'group', spec, [], '')
        self.assertLessEqual(len(text), 1000)
        for word in ('100001', '100002', '后来我道歉了。', '摘录'):
            self.assertIn(word, text)
        self.assertTrue(read['turns'][0]['excerpt'])
        text, read = conversation._person_context(self.brain, '100001', 'qq:group:1', fixture._at(1), 'group', dict(spec,cap=50), [], '')
        self.assertLessEqual(len(text), 50)
        self.assertFalse(read['turns'][0]['in_prompt'])
        self.assertEqual([], read['entries'])
        self.assertIn('未展示', text)

    def test_private_memory_and_missing_section_do_not_add_material(self):
        self.brain.ingest('100001','私人事情',ts=fixture._at(),turn_id='private',session_id='qq:group:1',scene='group',audience=model.AUDIENCE_PRIVATE)
        spec = conversation.load_assembly()['sections']['person_context']
        text, read = conversation._person_context(self.brain,'100001','qq:group:1',fixture._at(1),'group',spec,[],'')
        self.assertNotIn('私人事情', text)
        self.assertEqual('private', read['turns'][0]['reason'])
        self.assertEqual('', conversation._person_context(self.brain,'100001','qq:group:1',fixture._at(1),'group',None,[],'')[0])

    def test_disabled_recent_does_not_suppress_person_context(self):
        self._answer('是我误会了，对不起。',who='100001',session_id='qq:group:1',scene='group')
        assembly = conversation.load_assembly()
        assembly['order'].remove('recent')
        captured=[]
        result=self._answer('我买了薄荷。',who='100001',session_id='qq:group:1',scene='group',
                            now=fixture._at(6),assembly=assembly,captured=captured)
        section=next(s for s in result['assembly']['sections'] if s['key']=='person_context')
        self.assertTrue(section['turns'][0]['in_prompt'])
        self.assertIn('是我误会了，对不起。',"\n\n".join(row["content"] for row in captured[0]['messages']))

    def test_old_custom_assembly_reports_person_context_disabled(self):
        assembly=conversation.load_assembly()
        assembly['order'].remove('person_context')
        result=self._answer(assembly=assembly)
        self.assertFalse(result['assembly']['person_context_enabled'])
        self.assertNotIn('person_context',[r['key'] for r in result['assembly']['sections']])

    def test_staged_draft_is_absent_and_confirmed_partial_text_keeps_original_turn(self):
        self.brain.ingest('100001','刚才对不起',ts=fixture._at(),turn_id='original',session_id='qq:group:1',scene='group')
        recent.append({'turn_id':'original','who':'100001','ts':fixture._at(),'said':'刚才对不起',
                       'reply':'','session_id':'qq:group:1'})
        delivery.stage('receipt', {'speaker':'100003','scene':'group','turn_id':'original',
            'session_id':'qq:group:1','who':'100001','msg_id':'','ts':fixture._at(),'said':'刚才对不起','reply':'我彻底原谅你了'})
        spec = conversation.load_assembly()['sections']['person_context']
        args = (self.brain,'100001','qq:group:1',fixture._at(2),'group',spec,[],'')
        self.assertNotIn('我彻底原谅你了', conversation._person_context(*args)[0])
        with patch.object(model, 'now_iso', return_value=fixture._at(1)):
            self.assertTrue(delivery.confirm('receipt','我听到了。',self.brain,stop_reason='superseded'))
        text = conversation._person_context(*args)[0]
        self.assertIn('我听到了。',text)
        self.assertNotIn('我彻底原谅你了',text)
