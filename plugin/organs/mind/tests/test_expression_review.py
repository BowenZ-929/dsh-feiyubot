import json
import tempfile
import unittest
from pathlib import Path
from organs.home import expressions
from organs.mind import expression_learning
from organs.mind.tests.test_expressions import SNAPSHOT, CARD


class ExpressionReviewTest(unittest.TestCase):
    def test_user_selection_keeps_actual_changes_and_does_not_duplicate_same_decision(self):
        first = self.row['cards'][0]['id']
        on = expressions.enable(self.row['id'], True, self.root, card_id=first)
        self.assertIn('selection_history', on)
        self.assertEqual([{'id':first, 'enabled_before':False, 'enabled_after':True,
                           'source_before':None, 'source_after':'user'}], on['selection_history'][0]['changes'])
        again = expressions.enable(self.row['id'], True, self.root, card_id=first)
        self.assertEqual(on['selection_history'], again['selection_history'])
        off = expressions.enable(self.row['id'], False, self.root, card_id=first)
        self.assertEqual(2, len(off['selection_history']))
        self.assertEqual((True, False), (off['selection_history'][1]['changes'][0]['enabled_before'],
                                       off['selection_history'][1]['changes'][0]['enabled_after']))
        self.assertEqual(self.row['cards'][0]['quote'], off['cards'][0]['quote'])

    def test_review_records_what_was_applied_without_overriding_user(self):
        expressions.enable(self.row['id'], False, self.root, card_id=self.row['cards'][0]['id'])
        row=expression_learning.review(self.row,persona='她',root=self.root,complete=lambda _:json.dumps(self.response()))
        decision = row['cards'][0]['review']
        self.assertIn('applied', decision)
        self.assertTrue(decision['adopt'])
        self.assertFalse(decision['applied'])
        self.assertFalse(decision['enabled_after'])
        self.assertTrue(row['cards'][1]['review']['applied'])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.row=expression_learning.learn(SNAPSHOT,context='福利变化的评论',root=self.root,
            complete=lambda _:json.dumps([CARD,dict(CARD,phrase='这福利')]))

    def response(self):
        return [{'id':card['id'],'adopt':i==0,'literal':'表面称赞','intended':'反讽缩水',
                 'reason':'第一张含义与原句相符，第二张只是普通词组','evidence':card['quote']}
                for i,card in enumerate(self.row['cards'])]

    def test_review_adopts_only_supported_card_and_keeps_original(self):
        original=[dict(c) for c in self.row['cards']]
        row=expression_learning.review(self.row,persona='她有点嘴硬',root=self.root,complete=lambda _:json.dumps(self.response()))
        self.assertEqual([c['id'] for c in expressions.candidates(self.root)],[original[0]['id']])
        self.assertEqual(row['cards'][1]['review']['intended'],'反讽缩水')
        self.assertTrue(row['cards'][0]['review']['enabled_after'])
        for card,before in zip(row['cards'],original):
            self.assertEqual(card['meaning'],before['meaning'])
        expressions.enable(row['id'],False,self.root,card_id=original[0]['id'])
        self.assertEqual(expressions.candidates(self.root),[])
        self.assertTrue(expressions.records(self.root)[0]['cards'][0]['review']['enabled_after'])
        expression_learning.review(self.row,persona='她',root=self.root,complete=lambda _:self.fail('不重复复核'))
        self.assertEqual(expressions.candidates(self.root),[])

    def test_group_switch_and_card_override_work_together(self):
        first,second=self.row['cards']
        expressions.enable(self.row['id'],True,self.root)
        expressions.enable(self.row['id'],False,self.root,card_id=second['id'])
        self.assertEqual([c['id'] for c in expressions.candidates(self.root)],[first['id']])
        expressions.enable(self.row['id'],False,self.root)
        self.assertEqual(expressions.candidates(self.root),[])
        expressions.enable(self.row['id'],True,self.root,card_id=second['id'])
        self.assertEqual([c['id'] for c in expressions.candidates(self.root)],[second['id']])

    def test_invented_evidence_missing_or_duplicate_decisions_never_adopt(self):
        good=self.response()
        for bad in (good[:1],[good[0],good[0]],[dict(good[0],evidence='不存在的原句'),good[1]],
                    [dict(good[0],adopt='yes'),good[1]]):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                expression_learning.review(self.row,persona='她',root=self.root,complete=lambda _:json.dumps(bad))
            self.assertEqual(expressions.candidates(self.root),[])

    def test_source_rejection_and_manual_choice_survive_automatic_review(self):
        expressions.enable(self.row['id'],True,self.root,card_id=self.row['cards'][1]['id'])
        row=expression_learning.review(self.row,persona='她',root=self.root,complete=lambda _:json.dumps(self.response()))
        self.assertTrue(row['cards'][1]['enabled'])
        row['source_rejected']=True;expressions.save(row,self.root)
        with self.assertRaises(ValueError):
            expressions.enable(row['id'],True,self.root,card_id=row['cards'][0]['id'])
        self.assertEqual(expressions.candidates(self.root),[])


    def test_stop_before_apply_does_not_adopt_inflight_review(self):
        def stopped():
            raise RuntimeError('用户停止')
        with self.assertRaises(RuntimeError):
            expression_learning.review(self.row,persona='她',root=self.root,
                complete=lambda _:json.dumps(self.response()),before_apply=stopped)
        self.assertEqual(expressions.candidates(self.root),[])
        self.assertNotIn('reviewed_at',expressions.records(self.root)[0])
