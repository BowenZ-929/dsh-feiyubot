import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from organs.mind import rice, llm


class RiceTest(unittest.TestCase):
    def test_timeout_keeps_billing_unknown(self):
        rice.record_search({'calls': 1, 'http': None, 'results_count': 0}, now=self.now)
        state = rice.snapshot(now=self.now)['search']
        self.assertEqual(1, state['unknown_calls'])
        row = json.loads((self.root / 'mind/rice-usage/2026-11-07.jsonl').read_text())
        self.assertEqual('unknown', row['billing_status'])

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

    def test_search_accounting_separates_calls_from_billed_and_keeps_both_published_prices(self):
        for detail in ({'calls': 1, 'http': 200, 'results_count': 3},
                       {'calls': 1, 'http': 200, 'results_count': 0},
                       {'calls': 1, 'http': 500, 'results_count': 0},
                       {'calls': 0, 'http': None, 'results_count': 0}):
            rice.record_search(detail, now=self.now)
        state = rice.snapshot(now=self.now)
        # 调用次数与计费次数分列：只发了三次请求（本地就拒那次 calls=0 不算调用），
        # 其中只有「200 且结果非空」那一次计费。
        self.assertEqual((3, 1), (state['search']['calls'], state['search']['billed_calls']))
        ledger = (self.root / 'mind/rice-usage/2026-11-07.jsonl').read_text().splitlines()
        self.assertEqual(4, len(ledger), '四次都落在同一本日账上，靠 kind 分列')
        self.assertEqual({'USD': '0.00075'}, state['search']['costs'])
        self.assertEqual(0, state['daily']['calls'], '检索不许混进 token 那一本账')
        self.assertEqual(0, state['daily']['total_tokens'])
        price = state['search_price']
        self.assertEqual('unconfirmed', price['status'])
        self.assertEqual({'USD', 'CNY'}, set(price['quotes']))
        self.assertEqual(('0.00075', '0.005'), (price['quotes']['USD']['unit_price'],
                                                price['quotes']['CNY']['unit_price']))
        for quote in price['quotes'].values():
            self.assertTrue(quote['price_url'].startswith('https://api-docs.deepseek.com/'))
            self.assertEqual('2026-11-09', quote['checked_at'])
        self.assertIn('待确认', price['note'], '两版不一致时落账要看得出没核准')
        first = json.loads((self.root / 'mind/rice-usage/2026-11-07.jsonl').read_text().splitlines()[0])
        self.assertEqual(('search', 'USD', '0.00075', 'unconfirmed'),
                         (first['kind'], first['currency'], first['unit_price'], first['price_status']))
        self.assertEqual(('https://api-docs.deepseek.com/guides/search/', '2026-11-09'),
                         (first['price_url'], first['checked_at']))
        self.assertEqual((1, 1, '0.00075'), (first['calls'], first['billed_calls'], first['cost']))
        self.assertIn('待确认', first['note'])

    def test_search_price_can_be_overridden_with_source_page_and_check_date(self):
        config = {'enabled': True, 'search_price': {
            'currency': 'CNY', 'unit_price': '0.004',
            'price_url': 'https://example.test/pricing', 'checked_at': '2026-11-10'}}
        (self.root / 'mind/rice.json').write_text(json.dumps(config))
        rice.record_search({'calls': 1, 'http': 200, 'results_count': 2}, now=self.now)
        rice.record_search({'calls': 1, 'http': 200, 'results_count': 0}, now=self.now)
        state = rice.snapshot(now=self.now)
        self.assertEqual('configured', state['search_price']['status'])
        self.assertEqual({'CNY': '0.004'}, state['search']['costs'])
        self.assertEqual(0, state['search']['pending_billed'])
        rows = [json.loads(line) for line in
                (self.root / 'mind/rice-usage/2026-11-07.jsonl').read_text().splitlines()]
        self.assertEqual({'currency': 'CNY', 'unit_price': '0.004', 'price_status': 'configured',
                          'price_url': 'https://example.test/pricing', 'checked_at': '2026-11-10',
                          'calls': 1, 'billed_calls': 1, 'cost': '0.004', 'note': None},
                         {key: rows[0][key] for key in ('currency', 'unit_price', 'price_status',
                                                        'price_url', 'checked_at', 'calls',
                                                        'billed_calls', 'cost', 'note')})
        # 配不全的覆盖（缺来源页）不抛也不静默：降级回 published 那一版，并标出来没核准。
        (self.root / 'mind/rice.json').write_text(json.dumps({
            'enabled': True, 'search_price': {'currency': 'USD', 'unit_price': '0.001'}}))
        self.assertEqual((rice.SEARCH_QUOTES['USD'], 'configured_invalid'), rice.search_price())
        rice.record_search({'calls': 1, 'http': 200, 'results_count': 1}, now=self.now)
        last = [json.loads(line) for line in
                (self.root / 'mind/rice-usage/2026-11-07.jsonl').read_text().splitlines()][-1]
        self.assertEqual(('configured_invalid', 'USD', '0.00075'),
                         (last['price_status'], last['currency'], last['unit_price']))
