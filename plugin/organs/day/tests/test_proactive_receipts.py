"""主动开口只在通道确认后等待回复。"""
from unittest.mock import Mock, patch

from organs.day import delivery, proactive
from organs.heart import proactive as heart_proactive
from .test_proactive import ProactiveCase, open_thought
from .helpers import at


class ProactiveReceiptTest(ProactiveCase):
    def queue(self):
        self.heard()
        row = self.consider()["plan"]
        return proactive.run_due(row, at('2026-11-05T10:36:00'), root=self.root,
                                 heart_root=self.heart, brain=self.brain,
                                 complete=Mock(return_value='{"send":true,"chunks":["想起你说的事"]}'))

    def test_queue_without_transport_is_not_sent_or_waiting(self):
        out = self.queue()
        self.assertEqual(out['action'], 'queued')
        self.assertFalse(out['sent'])
        self.assertFalse(heart_proactive.overview(self.heart)['pending'])
        self.assertEqual(proactive.settle('ignored', root=self.root, heart_root=self.heart)['ok'], False)
        self.assertEqual(heart_proactive.overview(self.heart)['streak'], 0)

    def test_failed_ack_does_not_start_waiting_success_does_once(self):
        out = self.queue()
        ident = out['delivery_id']
        delivery.ack(ident, 100, root=self.root, heart_root=self.heart, now_ts=at('2026-11-05T10:37:00'))
        self.assertFalse(heart_proactive.overview(self.heart)['pending'])
        result = delivery.ack(ident, 0, root=self.root, heart_root=self.heart, now_ts=at('2026-11-05T10:38:00'))
        self.assertTrue(result['delivered'])
        self.assertTrue(heart_proactive.overview(self.heart)['pending'])
        self.assertEqual(proactive.log_tail(self.root, 1)[0]['action'], 'sent')
        heart_proactive.outcome('replied', root=self.heart, now=at('2026-11-05T10:39:00'))
        for code in [0, 100]:
            again = delivery.ack(ident, code, root=self.root, heart_root=self.heart, now_ts=at('2026-11-05T10:40:00'))
            self.assertTrue(again['delivered'])
        self.assertFalse(heart_proactive.overview(self.heart)['pending'])
        self.assertEqual(len([r for r in proactive.log_lines(self.root) if r['action']=='sent']), 1)

    def test_ordinary_queue_ack_does_not_change_proactive_state(self):
        item = delivery.intake('普通消息', root=self.root)
        self.assertFalse(item['sent'])
        result = delivery.ack(item['item']['id'], 0, root=self.root, heart_root=self.heart)
        self.assertTrue(result['delivered'])
        self.assertFalse(heart_proactive.overview(self.heart)['pending'])

    def test_thought_attempt_and_http_ack_use_confirmed_receipt_and_isolated_root(self):
        from api import day
        open_thought(self.heart)
        with patch('organs.day.proactive.heart_pending.attempt') as attempt:
            out = self.queue()
            attempt.assert_not_called()
            status, result = day.dispatch('/day/ack', {'root': str(self.root)},
                                          {'item_id': out['delivery_id'], 'retcode': 0})
        self.assertEqual(status, 200)
        self.assertTrue(result['delivered'])
        attempt.assert_called_once()
        self.assertEqual(attempt.call_args.args[0], 'p-test')
        self.assertEqual(str(attempt.call_args.kwargs['root']), str(self.heart))
        self.assertTrue(heart_proactive.overview(self.heart)['pending'])
