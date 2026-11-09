import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from organs.heart import changes, store
from organs.mind import events


class EmotionOriginTest(unittest.TestCase):
    def test_group_comfort_from_another_person_reaches_heart_with_own_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(store, 'default_root', return_value=root / 'heart'), \
                    patch.dict('os.environ', {'QQAGENT_HOME': str(root)}):
                replies = [('A', 'insult', '你真没用'), ('B', 'care', '别难受了，休息一下'),
                           ('C', None, '小林，别难受了，休息一下')]
                for index, (who, kind, text) in enumerate(replies):
                    result = events.record(text, scene='group', who=who,
                        at=f'2026-11-07T12:{index * 2:02d}:00+09:00', root=root / 'mind',
                        turn_id=str(index) * 32,
                        interpretation={'kind': kind, 'who': who, 'evidence': text if kind else '',
                                        'note': '关心其他人' if kind is None else '针对bot',
                                        'status': 'ok', 'source': 'semantic'})
                    self.assertEqual(kind is not None, result['applied'], result)
                rows = changes.read_history()['rows']
                self.assertEqual(2, len(rows))
                self.assertEqual(['A', 'B'], [r['origin']['who'] for r in rows])
                care = rows[1]
                self.assertEqual('care', care['source'])
                self.assertEqual('1' * 32, care['origin']['turn_id'])
                self.assertEqual('group', care['origin']['scene'])
                self.assertGreater(care['changes']['心情']['after'], care['changes']['心情']['before'])

    def test_same_second_authors_and_blocked_events_keep_their_own_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            heart = root / 'heart'
            with patch.object(store, 'default_root', return_value=heart):
                for number, (who, kind) in enumerate([('qq:a', 'praise'), ('qq:b', 'insult'), ('qq:c', 'praise'), ('qq:d', None)]):
                    turn = str(number) * 32
                    explanation = {'kind': kind, 'who': who, 'note': '针对她的这句' if kind else '转述他人',
                                   'evidence': '原话属于' + who, 'status': 'ok', 'source': 'semantic'}
                    result = events.record('虚构测试段', scene='group', who=who, at='2026-11-07T12:00:00+09:00',
                                           root=root / 'mind', interpretation=explanation, turn_id=turn)
                    self.assertEqual(turn, result['turn_id'])
                rows = changes.read_history()['rows']
                self.assertEqual(2, len(rows))
                for number, row in enumerate(rows):
                    origin = row['origin']
                    self.assertEqual(str(number) * 32, origin['turn_id'])
                    self.assertEqual(['qq:a', 'qq:b'][number], origin['who'])
                    self.assertEqual('原话属于' + origin['who'], origin['evidence'])
                    self.assertEqual('group', origin['scene'])
                ledger = events.rows(root / 'mind')
                self.assertEqual('cooldown', ledger[2]['reason'])
                self.assertFalse(ledger[2]['applied'])
                self.assertFalse(ledger[3]['applied'])
                before = (heart / 'changes.jsonl').read_bytes()
                self.assertEqual(2, changes.view({'date': '2026-11-07'})['total'])
                self.assertEqual(before, (heart / 'changes.jsonl').read_bytes())

    def test_old_change_has_no_invented_origin(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(store, 'default_root', return_value=Path(folder)):
            changes.record(None, at='2026-11-07T12:00:00+09:00', group='mood', reason='旧记录',
                           before={'心情': 50}, after={'心情': 60}, source='praise')
            self.assertNotIn('origin', changes.read_history()['rows'][0])
