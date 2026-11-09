import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from api import day
from organs.heart import events, pending, report, rest, store
from organs.heart.tests.test_pending import seed_energy


class ThoughtLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.heart = self.root / 'heart'
        p = patch.dict('os.environ', {'QQAGENT_HOME':str(self.root), 'HEART_ROOT':str(self.heart)})
        p.start(); self.addCleanup(p.stop)
        p = patch('organs.mind.llm.complete', side_effect=AssertionError('离线测试不得调用模型'))
        p.start(); self.addCleanup(p.stop)

    def test_real_tick_derives_then_actual_sleep_satisfies_rest(self):
        awake = '2026-11-07T22:00:00+09:00'
        seed_energy(self.heart, 20, awake)
        day.run_tick(awake, root=self.root, heart_root=self.heart)
        rows = pending.overview(self.heart, awake)['thoughts']
        self.assertEqual(['wanted_rest'], [r['kind'] for r in rows])
        self.assertEqual('open', rows[0]['state'])
        thought_id = rows[0]['id']
        for at in ('2026-11-07T23:00:00+09:00','2026-11-07T23:01:00+09:00'):
            day.run_tick(at, root=self.root, heart_root=self.heart)
        rows = pending.overview(self.heart, at)['thoughts']
        self.assertEqual(1,len(rows)); self.assertEqual(thought_id,rows[0]['id'])
        self.assertEqual('settled',rows[0]['state'])
        self.assertEqual('2026-11-07T23:00:00+09:00',rows[0]['settled_at'])
        self.assertEqual('asleep',rest.state(self.heart)['phase'])

    def test_praise_satisfies_once_without_bypassing_emotion_cooldown(self):
        at = '2026-11-07T12:00:00+09:00'
        for _ in range(3): events.deliver('ignored',root=self.heart,now=at)
        pending.derive(self.heart,at)
        self.assertTrue(report.report_event('praise',who='alice',root=self.heart,now=at)['applied'])
        self.assertEqual('settled',pending.overview(self.heart,at)['thoughts'][0]['state'])
        later = '2026-11-07T12:01:00+09:00'
        for _ in range(3): events.deliver('ignored',root=self.heart,now=later)
        pending.derive(self.heart,later)
        before = (self.heart/'mood.json').read_bytes()
        result = report.report_event('praise',who='bob',root=self.heart,now=later)
        self.assertEqual('cooldown',result['reason']); self.assertFalse(result['applied'])
        self.assertEqual(['settled','settled'],[r['state'] for r in pending.overview(self.heart,later)['thoughts']])
        self.assertEqual(before,(self.heart/'mood.json').read_bytes())
        ledger=store.read_jsonl(self.heart/events.EVENTS_FILE)
        self.assertEqual(1,sum(r['kind']=='praise' for r in ledger))
        pending.derive(self.heart,later)
        self.assertEqual(2,len(pending.overview(self.heart,later)['thoughts']))
        new = '2026-11-07T12:02:00+09:00'
        for _ in range(2): events.deliver('ignored',root=self.heart,now=new)
        self.assertEqual([],pending.derive(self.heart,new)['created'])
        events.deliver('ignored',root=self.heart,now=new)
        self.assertEqual(1,len(pending.derive(self.heart,new)['created']))

    def test_derivation_respects_effective_sleep_window_and_recovery_cap(self):
        seed_energy(self.heart,90,'2026-11-07T08:00:00+09:00')
        with patch('organs.heart.sleep.overview',return_value={'effective_window':[1,10],'wake_cap':95}):
            pending.derive(self.heart,'2026-11-07T09:00:00+09:00')
        self.assertEqual(95,store.read_json(self.heart/'energy.json',{})['value'])
