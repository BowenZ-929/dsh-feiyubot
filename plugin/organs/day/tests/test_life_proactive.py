"""连续生活日程与旧主动开口条件的联动回归。"""
import json
from unittest.mock import Mock

from organs.day import delivery, life, plan, proactive
from .test_proactive import ProactiveCase, NOW, caps, schedule_a_day
from .helpers import at


class LifeProactiveTest(ProactiveCase):
    def test_schedule_created_during_generation_prevents_empty_day_draft(self):
        added = plan.add([{'kind': 'proactive', 'at': at('2026-11-05T10:35:00'),
            'note': '还没有安排', 'reason_code': 'unscheduled', 'priority': 40, 'who': 'guest'}],
            root=self.root, now_ts=at(NOW), config=caps())
        def answer(*args, **kwargs):
            schedule_a_day(self.root)
            return '{"send":true,"chunks":["今天没安排"]}'
        complete = Mock(side_effect=answer)
        out = proactive.run_due(added['added'][0], at('2026-11-05T10:36:00'), root=self.root,
            heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual(('declined', 'source-resolved'), (out['action'], out.get('reason')))
        self.assertEqual(1, complete.call_count)
        self.assertEqual([], delivery.load(self.root)['items'])
        self.assertEqual([], plan.load(self.root)['pending'])

    def make_life(self, enabled=True):
        life.configure(enabled, root=self.root, now=at(NOW))
        raw = {'title': '在家的一天', 'items': [{'start': 0, 'end': 240, 'activity': '睡觉'},
                                                 {'start': 240, 'end': 1440, 'activity': '读书'}]}
        result = life.generate('2026-11-05', persona='爱看书', root=self.root, now=at(NOW),
                               complete=Mock(return_value=json.dumps(raw)))
        self.assertEqual(result['status'], 'ready')
        return result

    def codes(self, now=NOW):
        return [r['reason_code'] for r in proactive.sources(at(now), root=self.root,
                    heart_root=self.heart, brain=self.brain)]

    def test_enabled_ready_life_prevents_false_empty_day_reason_without_writing(self):
        self.make_life()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertTrue(proactive._scheduled_today(self.root, at(NOW)))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertNotIn(proactive.REASON_UNSCHEDULED, self.codes())
        self.assertFalse((self.root / 'schedule_log.jsonl').exists())

    def test_disabled_or_failed_life_does_not_claim_a_current_plan(self):
        self.make_life(enabled=False)
        self.assertIn(proactive.REASON_UNSCHEDULED, self.codes())
        life.configure(True, root=self.root, now=at(NOW))
        path = self.root / 'life-days/2026-11-05.json'
        value = json.loads(path.read_text())
        value.update(status='failed', items=[])
        path.write_text(json.dumps(value))
        self.assertIn(proactive.REASON_UNSCHEDULED, self.codes())
        schedule_a_day(self.root)
        self.assertNotIn(proactive.REASON_UNSCHEDULED, self.codes())

    def test_previous_day_plan_does_not_block_tomorrow(self):
        self.make_life()
        self.assertIn(proactive.REASON_UNSCHEDULED, self.codes('2026-11-06T10:30:00'))

    def test_queued_empty_day_reason_is_rechecked_before_calling_model(self):
        row = {'id': 'pl-life', 'kind': 'proactive', 'at': at('2026-11-05T10:35:00'),
               'note': '还没有安排', 'reason_code': 'unscheduled', 'priority': 40, 'who': 'guest'}
        added = plan.add([row], root=self.root, now_ts=at(NOW), config=caps())
        self.assertEqual(1, len(added['added']))
        self.make_life()
        complete = Mock(return_value='{"send":true,"chunks":["今天没安排"]}')
        out = proactive.run_due(added['added'][0], at('2026-11-05T10:36:00'), root=self.root,
                                heart_root=self.heart, brain=self.brain, complete=complete)
        complete.assert_not_called()
        self.assertEqual(out['reason'], 'source-resolved')
        self.assertFalse(out['sent'])
        self.assertEqual(delivery.load(self.root)['items'], [])
        self.assertEqual(plan.load(self.root)['pending'], [])
        self.assertEqual(proactive.log_tail(self.root, 1)[0]['reason'], 'source-resolved')
