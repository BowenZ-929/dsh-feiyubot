import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from organs.mind import rice, llm


class RiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.dict('os.environ', {'QQAGENT_HOME': str(self.root)}).start()
        (self.root / 'mind').mkdir()
        (self.root / 'mind/rice.json').write_text(json.dumps({'enabled': True, 'owner_session': 'qq:private:42'}))
        self.now = datetime.fromisoformat('2026-11-07T11:00:00+09:00')
        self.body = {'id': 'a', 'model': 'deepseek-flash', 'usage': {'prompt_tokens': 1000,
                     'prompt_cache_hit_tokens': 200, 'prompt_cache_miss_tokens': 800,
                     'completion_tokens': 500, 'completion_tokens_details': {'reasoning_tokens': 400}, 'total_tokens': 1500}}

    def test_usage_deduplicates_and_reasoning_is_not_extra_food(self):
        for _ in range(2):
            rice.record(self.body, now=self.now)
        today = rice.snapshot(now=self.now)['daily']
        self.assertEqual(today['calls'], 1)
        self.assertEqual(today['total_tokens'], 1500)
        self.assertEqual(today['reasoning_tokens'], 400)
        self.assertEqual(today['cost_cny_min'], '0.002804')
        self.assertEqual(today['cost_cny_max'], '0.002804')
        self.assertEqual(rice.snapshot(day='2026-11-06', now=self.now)['daily']['calls'], 0)

    def test_unknown_model_is_unpriced_and_peak_has_holiday_uncertainty(self):
        at = datetime.fromisoformat('2026-11-06T10:00:00+08:00')
        rice.record(self.body, now=at)
        self.body.update(id='b', model='unknown')
        rice.record(self.body, now=at)
        day = rice.snapshot(day='2026-11-06', now=self.now)['daily']
        self.assertEqual(day['unpriced_calls'], 1)
        self.assertEqual(day['cost_cny_min'], '0.002804')
        self.assertEqual(day['cost_cny_max'], '0.005608')

    def test_balance_cache_keeps_currencies_and_stale_state_without_secret(self):
        payload = {'is_available': True, 'balance_infos': [{'currency': 'CNY', 'total_balance': '12.3', 'granted_balance': '0', 'topped_up_balance': '12.3'}, {'currency': 'USD', 'total_balance': '1', 'granted_balance': '0', 'topped_up_balance': '1'}]}
        with patch.object(rice, 'query_balance', return_value=payload) as query:
            rice.refresh(now=self.now)
            rice.refresh(now=self.now)
            self.assertEqual(query.call_count, 1)
        later = datetime.fromisoformat('2026-11-07T11:06:00+09:00')
        with patch.object(rice, 'query_balance', side_effect=OSError('SECRET')):
            rice.refresh(now=later)
        state = rice.snapshot(now=later)
        self.assertTrue(state['balance']['stale'])
        self.assertEqual(len(state['balance']['data']['balance_infos']), 2)
        self.assertNotIn('SECRET', json.dumps(state))
        self.assertIn('12.3', rice.context('qq:private:42', later))
        self.assertNotIn('12.3', rice.context('qq:group:42', later))
        self.assertIn('大白饭', rice.context('qq:group:42', later))
        self.assertIn('12.3', rice.context('qq:private:42', later.isoformat()))

    def test_consumed_tokens_recorded_even_when_reply_is_empty_and_trace_off(self):
        cfg = llm.load_config()
        self.body['choices'] = [{'message': {'content': ''}, 'finish_reason': 'length'}]
        with patch.object(llm, 'read_key', return_value='fake'), patch.dict('os.environ', {'QQAGENT_TRACE': '0'}):
            with self.assertRaises(llm.LLMError):
                llm.complete([{'role': 'user', 'content': 'fiction'}], config=cfg,
                             transport=lambda *args: (200, json.dumps(self.body).encode()))
        self.assertEqual(rice.snapshot()['daily']['calls'], 1)

    def test_rice_metaphor_does_not_prescribe_a_stubborn_personality(self):
        text = rice.context('qq:group:42', self.now)
        self.assertIn('大白饭', text)
        self.assertNotIn('嘴硬', text)
        self.assertNotIn('性情', text)
