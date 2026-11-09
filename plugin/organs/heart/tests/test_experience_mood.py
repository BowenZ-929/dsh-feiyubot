import tempfile
import unittest
from pathlib import Path

from organs.heart import events,mood,store


class ExperienceMoodTest(unittest.TestCase):
    def test_positive_decays_and_does_not_consume_human_window(self):
        with tempfile.TemporaryDirectory() as root:
            at='2026-11-07T10:00:00+09:00'
            result=mood.apply_experience('positive',source_id='phone:one',note='猫的动作好玩',root=root,now=at)
            self.assertTrue(result['ok'])
            self.assertGreater(result['delta'],0)
            self.assertLessEqual(result['delta'],2)
            before=result['before']
            value=mood.read(root,'2026-11-07T13:00:00+09:00')
            self.assertAlmostEqual(before+result['delta']/2,value['now'],places=1)
            self.assertEqual(before,value['baseline'])
            self.assertTrue(mood.apply_event('care',root=root,now=at,who='42')['ok'])
            record=events.recent(root,now=at)[0]
            self.assertEqual('experience',record['kind'])
            self.assertEqual('phone:one',record['source_id'])
            self.assertEqual('self',record['who'])

    def test_negative_is_bounded_and_same_source_only_applied_once(self):
        with tempfile.TemporaryDirectory() as root:
            at='2026-11-07T10:00:00+09:00'
            result=mood.apply_experience('negative',source_id='phone:two',note='觉得失望',root=root,now=at)
            self.assertEqual(-2,result['delta'])
            path=Path(root)/'mood.json'; original=path.read_bytes()
            again=mood.apply_experience('negative',source_id='phone:two',note='重复',root=root,now=at)
            self.assertEqual('duplicate',again['skipped'])
            self.assertEqual(original,path.read_bytes())
            self.assertEqual(1,len(events.recent(root,now=at)))
            self.assertEqual(0,store.read_json(path,{})['conflict_used'])

    def test_neutral_records_decision_without_creating_mood_change(self):
        with tempfile.TemporaryDirectory() as root:
            result=mood.apply_experience('neutral',source_id='phone:three',note='',root=root,
                                         now='2026-11-07T10:00:00+09:00')
            self.assertEqual(0,result['delta'])
            self.assertFalse((Path(root)/'mood.json').exists())
