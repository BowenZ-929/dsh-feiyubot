import hashlib
import unittest
from pathlib import Path

from organs.brain.tests.helpers import fresh_store


class PersonContextTest(unittest.TestCase):
    def setUp(self):
        self.brain, self.tmp = fresh_store()
        self.addCleanup(self.tmp.cleanup)

    def put(self, who, text, turn, minute=0, **extra):
        args = dict(ts=f'2026-11-08T12:{minute:02d}:00+09:00',
                    session_id='qq:group:1', turn_id=turn, scene='group')
        args.update(extra)
        return self.brain.ingest(who, text, **args)['entry']

    def read(self, **extra):
        args = dict(who='100001', session_id='qq:group:1', now_ts='2026-11-08T13:00:00+09:00')
        args.update(extra)
        return self.brain.person_context(**args)

    def test_other_people_do_not_displace_person_and_same_turn_keeps_all_authors(self):
        self.put('100001', '伤人的话', 'conflict')
        self.put('100001', '我刚才错了，对不起', 'repair', 1)
        self.put('100002', '我只是来打个招呼', 'repair', 1)
        self.put('100003', '我在回答小周', 'repair', 3, side='她说')
        for minute in range(4, 20):
            self.put('100002', '小周的闲聊', f'other-{minute}', minute)
        got = self.read()
        self.assertEqual(['conflict', 'repair'], [r['turn_id'] for r in got['turns']])
        self.assertEqual(['100001', '100002', '100003'], [r['who'] for r in got['turns'][-1]['entries']])

    def test_same_person_other_session_and_unlinked_or_future_rows_are_not_used(self):
        self.put('100001', '本群', 'valid')
        self.put('100001', '其他群', 'elsewhere', 1, session_id='qq:group:2')
        self.put('100001', '私聊', 'private', 2, session_id='qq:private:100001', scene='private')
        self.put('100001', '旧记录没有来源', '', 3, session_id='')
        self.put('100001', '未来', 'future', ts='2026-11-09T12:00:00+09:00')
        self.assertEqual(['valid'], [r['turn_id'] for r in self.read()['turns']])
        self.assertEqual([], self.read(session_id='')['turns'])

    def test_archived_active_rows_work_retired_rows_do_not_and_read_writes_nothing(self):
        old = self.put('100001', '旧日原话', 'old', ts='2026-11-07T12:00:00+09:00')
        gone = self.put('100001', '退役原话', 'gone', 1)
        self.put('100001', '新原话', 'new', 2)
        self.brain.forget(gone['id'], '2026-11-08T12:03:00+09:00')
        root = Path(self.tmp.name)
        snapshot = lambda: {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
        before = snapshot()
        got = self.read()
        self.assertEqual(['old', 'new'], [r['turn_id'] for r in got['turns']])
        self.assertEqual(old['id'], got['turns'][0]['entries'][0]['id'])
        self.assertEqual(before, snapshot())

    def test_recent_limit_uses_human_turn_time_not_late_self_confirmation(self):
        for minute in range(4):
            self.put('100001', f'第{minute}轮', f'turn-{minute}', minute)
        self.put('100003', '第一轮的晚到确认', 'turn-0', 20, side='她说')
        got = self.read()
        self.assertEqual(['turn-1', 'turn-2', 'turn-3'], [r['turn_id'] for r in got['turns']])
        self.assertEqual(1, got['omitted'])
