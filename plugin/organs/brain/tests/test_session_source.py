"""会话仅作为原话出处；换日与容量降级不能丢失它。"""
import json
import unittest
from organs.brain.tests.helpers import fresh_store
from organs.brain.store import TIERS_FILE


class SessionSourceTest(unittest.TestCase):
    def test_session_survives_rollover_and_capacity_demotion(self):
        for rollover in (True, False):
            with self.subTest(rollover=rollover):
                brain, tmp = fresh_store()
                self.addCleanup(tmp.cleanup)
                if not rollover:
                    (brain.root / TIERS_FILE).write_text(json.dumps(dict(
                        brain._tiers(), today_max=1, today_demote_count=1)))
                entry = brain.ingest('100000001', '刚才提到的书', scene='group',
                    session_id='qq:group:70001', ts='2026-11-07T10:00:00+09:00')['entry']
                brain.ingest('100000001', '在另一个群聊的内容', scene='group',
                    session_id='qq:group:70002', ts='2026-11-07T10:01:00+09:00')
                if rollover:
                    brain.today_entries('2026-11-08T10:00:00+09:00')
                archived = next(row for row in brain.longterm_active_entries() if row['id'] == entry['id'])
                self.assertEqual('qq:group:70001', archived['session_id'])
                self.assertEqual(entry['quote'], archived['quote'])
                self.assertEqual(entry['scene'], archived['scene'])

    def test_legacy_unknown_session_stays_absent(self):
        brain, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        entry = brain.ingest('100000001', '旧原话', scene='group', ts='2026-11-07T10:00:00+09:00')['entry']
        brain.today_entries('2026-11-08T10:00:00+09:00')
        self.assertNotIn('session_id', entry)
        self.assertNotIn('session_id', brain.longterm_active_entries()[0])
