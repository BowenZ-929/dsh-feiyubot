"""主动计划必须仍有原来的念头；全部使用隔离状态与模型替身。"""
from unittest.mock import Mock

from organs.day import delivery, plan, proactive
from organs.heart import pending, proactive as feedback
from .test_proactive import ProactiveCase, open_thought
from .helpers import at


class ThoughtPlanTest(ProactiveCase):
    def prepare(self):
        self.heard()
        open_thought(self.heart)
        row = self.consider()['plan']
        self.assertEqual('thought', row['reason_code'])
        return row

    def test_satisfied_thought_cancels_plan_without_generating_or_waiting(self):
        row = self.prepare()
        pending.settle('praise', root=self.heart, now=at('2026-11-05T10:32:00'), reward=False)
        complete = Mock(return_value='{"send":true,"chunks":["还想被夸"]}')
        out = proactive.run_due(row, at('2026-11-05T10:36:00'), root=self.root,
                                heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual(('declined', 'source-resolved', False),
                         (out['action'], out.get('reason'), out['sent']))
        complete.assert_not_called()
        self.assertEqual([], delivery.load(self.root)['items'])
        self.assertFalse(feedback.overview(self.heart)['pending'])
        self.assertEqual([], plan.load(self.root)['pending'])
        self.assertEqual('source-resolved', proactive.log_tail(self.root, 1)[0]['reason'])
        again = proactive.run_due(row, at('2026-11-05T10:37:00'), root=self.root,
                                  heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual('not-pending', again['skipped'])
        complete.assert_not_called()

    def test_elapsed_thought_is_rechecked_at_execution(self):
        row = self.prepare()
        complete = Mock(return_value='{"send":true,"chunks":["还想被夸"]}')
        out = proactive.run_due(row, at('2026-11-05T13:31:00'), root=self.root,
                                heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual('source-resolved', out.get('reason'))
        complete.assert_not_called()
        self.assertEqual([], delivery.load(self.root)['items'])

    def test_another_open_thought_cannot_revive_the_old_plan(self):
        row = self.prepare()
        pending.settle('praise', root=self.heart, now=at('2026-11-05T10:32:00'), reward=False)
        from organs.heart.tests.test_pending import seed_energy
        seed_energy(self.heart, 20, at('2026-11-05T10:33:00'))
        fresh = pending.derive(self.heart, at('2026-11-05T10:33:00'))
        self.assertEqual(['wanted_rest'], [r['kind'] for r in fresh['open']])
        complete = Mock(return_value='{"send":true,"chunks":["还想被夸"]}')
        out = proactive.run_due(row, at('2026-11-05T10:36:00'), root=self.root,
                                heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual('source-resolved', out.get('reason'))
        complete.assert_not_called()
        self.assertEqual([], delivery.load(self.root)['items'])

    def test_current_thought_still_queues_and_attempt_waits_for_receipt(self):
        row = self.prepare()
        complete = Mock(return_value='{"send":true,"chunks":["想听你夸一句"]}')
        out = proactive.run_due(row, at('2026-11-05T10:36:00'), root=self.root,
                                heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual('queued', out['action'])
        self.assertFalse(out['sent'])
        self.assertEqual('想听你夸一句', delivery.load(self.root)['items'][0]['text'])
        self.assertEqual(0, pending.overview(self.heart, at('2026-11-05T10:36:00'))['thoughts'][0]['attempts'])
        self.assertFalse(feedback.overview(self.heart)['pending'])
        delivery.ack(out['delivery_id'], 0, root=self.root, heart_root=self.heart,
                     now_ts=at('2026-11-05T10:37:00'))
        self.assertEqual(1, pending.overview(self.heart, at('2026-11-05T10:37:00'))['thoughts'][0]['attempts'])
        self.assertTrue(feedback.overview(self.heart)['pending'])

    def test_thought_satisfied_during_generation_does_not_queue_the_old_draft(self):
        row = self.prepare()
        def answer(*args, **kwargs):
            pending.settle('praise', root=self.heart, now=at('2026-11-05T10:36:00'), reward=False)
            return '{"send":true,"chunks":["想听你夸一句"]}'
        complete = Mock(side_effect=answer)
        out = proactive.run_due(row, at('2026-11-05T10:36:00'), root=self.root,
                                heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual(('declined', 'source-resolved'), (out['action'], out.get('reason')))
        self.assertTrue(out['row']['after_generation'])
        self.assertEqual(1, complete.call_count)
        self.assertEqual([], delivery.load(self.root)['items'])
        self.assertEqual([], plan.load(self.root)['pending'])
        self.assertFalse(feedback.overview(self.heart)['pending'])
        self.assertEqual(0, pending.overview(self.heart, at('2026-11-05T10:36:00'))['thoughts'][0]['attempts'])
