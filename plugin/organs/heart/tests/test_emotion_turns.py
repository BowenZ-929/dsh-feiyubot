"""不同方向的真实事件不被整人封窗，重复刺激仍遵守现有冷却。"""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from organs.heart import report, mood


class EmotionTurnsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        shutil.copy(Path(report.__file__).parent / 'data/emotion_contract.json', self.root)

    def test_praise_insult_care_from_same_person_all_reach_mood(self):
        rows = [report.report_event(kind, root=self.root, who='A',
                                   now=f'2026-11-08T12:0{i}:00+09:00')
                for i, kind in enumerate(('praise', 'insult', 'care'))]
        self.assertEqual([True, True, True], [r['applied'] for r in rows])
        self.assertGreater(rows[0]['mood_after'], rows[0]['mood_before'])
        self.assertLess(rows[1]['mood_after'], rows[1]['mood_before'])
        self.assertGreater(rows[2]['mood_after'], rows[2]['mood_before'])

    def test_same_kind_cools_then_can_apply_again_in_same_hour(self):
        rows = [report.report_event('praise', root=self.root, who='A',
                                   now=f'2026-11-08T12:{minute:02d}:00+09:00')
                for minute in (0, 1, 10)]
        self.assertEqual([True, False, True], [r['applied'] for r in rows])
        self.assertEqual('cooldown', rows[1]['reason'])
        self.assertLess(rows[2]['mood_after'] - rows[2]['mood_before'],
                        rows[0]['mood_after'] - rows[0]['mood_before'])

    def test_old_hour_window_does_not_lock_new_event_or_reset_values(self):
        path = self.root / 'mood.json'
        path.write_text(json.dumps({'version': 1, 'now': 40, 'baseline': 60,
            'updated_at': '2026-11-08T12:00:00+09:00', 'day': '2026-11-08',
            'conflict_used': 1, 'window': {'A': '2026-11-08T12'}}))
        before = path.read_bytes()
        self.assertEqual(40, mood.read(self.root, now='2026-11-08T12:00:00+09:00')['now'])
        self.assertEqual(before, path.read_bytes())
        result = report.report_event('care', root=self.root, who='A', now='2026-11-08T12:00:00+09:00')
        self.assertTrue(result['applied'], result)
        self.assertEqual(40, result['mood_before'])
        self.assertEqual(1, json.loads(path.read_text())['conflict_used'])
