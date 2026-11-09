import tempfile
import unittest
from pathlib import Path
from organs.brain.tests.helpers import fresh_store
from organs.soul import load
from api import day


class GrowthApiTest(unittest.TestCase):
    def setUp(self):
        self.brain, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        self.root = str(Path(tmp.name) / 'day')
        Path(self.root).mkdir()
        self.soul = load(Path(__file__).resolve().parents[2] / 'organs' / 'soul' / 'data')

    def call(self, path, body=None):
        return day.dispatch('/day/growth' + path, {'root': self.root}, body, self.soul, self.brain)

    def test_fresh_start_accounting_keeps_usage_without_becoming_activity(self):
        from unittest.mock import patch
        from organs.day import growth, solitude
        growth.configure(True, root=self.root, daily_cap=4)
        for at, accounting in [('2026-11-06T12:00:00+09:00', True),
                               ('2026-11-07T10:00:00+09:00', True),
                               ('2026-11-07T11:00:00+09:00', False),
                               ('2026-11-07T13:00:00+09:00', True)]:
            solitude.append_log(self.root, {'at':at, 'event':'autonomy_attempt',
                'model_turn_id':'old-usage' if accounting else 'a'*32, 'accounting_only':accounting})
        before = solitude.log_lines(self.root)
        with patch('organs.day.model.now_iso', return_value='2026-11-07T12:00:00+09:00'):
            code, data = self.call('')
        self.assertEqual(200, code)
        self.assertEqual({'day':'2026-11-07','used':2,'remaining':2,'accounting_only':1}, data['usage'])
        self.assertEqual(['a'*32], [r['model_turn_id'] for r in data['log']])
        self.assertEqual(4, len(data['opportunities']))
        self.assertEqual(before, solitude.log_lines(self.root))

    def test_usage_counts_opportunities_across_life_day_without_counting_results(self):
        from unittest.mock import patch
        from organs.day import growth, solitude
        growth.configure(True, root=self.root, daily_cap=2)
        for at, event in [
            ('2026-11-07T04:00:00+09:00', 'autonomy_attempt'),
            ('2026-11-08T03:00:00+09:00', 'autonomy_attempt'),
            ('2026-11-08T03:01:00+09:00', 'autonomy_result'),
            ('2026-11-08T04:00:00+09:00', 'autonomy_attempt'),
            ('2026-11-08T08:00:00+09:00', 'autonomy_attempt'),
        ]:
            solitude.append_log(self.root, {'at':at, 'event':event})
        before = solitude.log_lines(self.root)
        jobs = [{'created_at':'2026-11-07T12:00:00+09:00', 'origin':origin}
                for origin in ('automatic', 'manual', 'autonomy')]
        with patch('organs.day.phone_browse.view', return_value={'jobs':jobs}):
            for now, expected in [
                ('2026-11-08T03:59:00+09:00', {'day':'2026-11-07','used':3,'remaining':0,'accounting_only':0}),
                ('2026-11-08T04:00:00+09:00', {'day':'2026-11-08','used':1,'remaining':1,'accounting_only':0}),
            ]:
                with self.subTest(now=now), patch('organs.day.model.now_iso', return_value=now):
                    code, data = self.call('')
                    self.assertEqual(code, 200)
                    self.assertEqual(data['usage'], expected)
        self.assertEqual(solitude.log_lines(self.root), before)

    def test_phone_available_reflects_live_connection(self):
        from unittest.mock import patch
        from organs.day import phone_browse
        phone_browse.configure(True, self.root)
        with patch('organs.hand.phone.config', return_value={'serial':'test','isolated_for_bot':True}), \
             patch('organs.hand.phone.status', return_value={'connected':False,'state':'disconnected'}) as status:
            code, payload = self.call('')
            self.assertEqual(200, code)
            self.assertFalse(payload['phone_available'])
            self.assertEqual('disconnected', payload['phone']['state'])
            status.return_value = {'connected':True,'state':'device'}
            _, payload = self.call('')
            self.assertTrue(payload['phone_available'])
            self.assertEqual('device', payload['phone']['state'])

    def test_read_and_correction_do_not_enable_or_call_model(self):
        code, payload = self.call('')
        self.assertEqual(code, 200)
        self.assertFalse(payload['settings']['enabled'])
        code, _ = self.call('/profile', {'patch': {'likes': '薄荷'}, 'note': '人工纠正'})
        self.assertEqual(code, 200)
        _, payload = self.call('')
        self.assertEqual(payload['profile']['likes'], '薄荷')
        self.assertFalse(payload['settings']['enabled'])

    def test_permission_fields_and_arbitrary_versions_rejected(self):
        self.assertEqual(self.call('/config', {'enabled': True, 'actor': 'operator'})[0], 400)
        self.assertEqual(self.call('/profile', {'patch': {'permissions': 'all'}, 'note': '修改'})[0], 400)
        self.assertEqual(self.call('/profile', {'version': 999, 'note': '回退'})[0], 400)

    def test_rollback_keeps_history_and_restores_exact_snapshot(self):
        self.call('/profile', {'patch': {'likes': 'A'}, 'note': '初版'})
        self.call('/profile', {'patch': {'likes': 'B', 'stance': '慢一点'}, 'note': '第二版'})
        code, _ = self.call('/profile', {'version': 1, 'note': '回到初版'})
        self.assertEqual(code, 200)
        self.assertEqual(self.brain.current_profile(), {'likes': 'A'})
        self.assertEqual(len(self.brain.selfnarration_versions()), 3)

    def test_version_links_follow_exact_successful_receipts_without_mutating_history(self):
        from organs.day import night
        first = self.brain.update_profile({'likes':'薄荷'}, note='有依据的新喜好')['record']
        self.call('/profile', {'patch':{'likes':'茶'},'note':'人工纠正'})
        self.call('/profile', {'version':1,'note':'回退'})
        versions_before = self.brain.selfnarration_versions()
        for row in [
            {'event':'growth_result','model_turn_id':'a'*32,'result':{'status':'ok','changed':True,'record':first}},
            {'event':'growth_result','model_turn_id':'b'*32,'result':{'status':'ok','changed':True,
                'record':dict(first,id='other-id')}},
            {'event':'growth_result','model_turn_id':'c'*32,'result':{'status':'failed','changed':True,'record':first}},
            {'event':'growth_result','model_turn_id':'d'*32,'result':{'status':'ok','changed':False,'record':first}}]:
            night._append_log(row, self.root)
        before = night.log_path(self.root).read_bytes()
        code, data = self.call('')
        self.assertEqual(code, 200)
        self.assertEqual(data['versions'][0].get('model_turn_id'), 'a'*32)
        self.assertTrue(all(not v.get('model_turn_id') for v in data['versions'][1:]))
        self.assertEqual(self.brain.selfnarration_versions(), versions_before)
        self.assertEqual(night.log_path(self.root).read_bytes(), before)

    def test_decisions_after_reflection_use_committed_persona_in_same_tick(self):
        import json
        from unittest.mock import patch
        from organs.day import growth
        from organs.mind import persona
        growth.configure(True, root=self.root)
        for i, value in enumerate(('新喜欢薄荷', '', None, 'invalid')):
            with self.subTest(value=value):
                self.brain.update_profile({'likes':'原来喜欢旧书'}, note='虚构起点')
                stamp = '2026-11-%02dT12:00:00+09:00' % (8+i)
                original = persona.render(self.soul, self.brain, root=self.root)
                captured = []
                def settle_night(*args, **kwargs):
                    captured.append(kwargs['persona'])
                    answer = ('{bad' if value == 'invalid' else json.dumps({
                        'patch':{} if value is None else {'likes':value},'note':'虚构选择','sources':[]}))
                    return growth.reflect(stamp, root=self.root, brain=self.brain,
                        persona=kwargs['persona'], complete=lambda _: answer)
                with patch('organs.day.rhythm._settle_night', side_effect=settle_night), \
                     patch('organs.day.proactive.settle_due', return_value={}) as proactive, \
                     patch('organs.day.proactive.consider', return_value={}), \
                     patch('organs.heart.rest.advance', return_value={'phase':'awake'}), \
                     patch('organs.heart.pending.derive'), \
                     patch('organs.day.life.tick', return_value={}) as life, \
                     patch('organs.day.autonomy.tick', return_value={}) as autonomy:
                    day.run_tick(stamp, root=self.root, brain=self.brain, store=self.soul)
                expected = persona.render(self.soul, self.brain, root=self.root)
                self.assertEqual(captured, [original])
                for sink in (proactive, life, autonomy):
                    self.assertEqual(sink.call_args.kwargs['persona'], expected)
                if value in (None, 'invalid'):
                    self.assertEqual(expected, original)
                else:
                    self.assertNotIn('原来喜欢旧书', expected)

    def test_activity_limits_roundtrip_and_invalid_input_is_not_saved(self):
        from organs.day import growth, phone_browse
        before = phone_browse.config(self.root)
        code, _ = self.call('/config', {'enabled':True, 'interval_minutes':90, 'daily_cap':10})
        self.assertEqual(code, 200)
        _, data = self.call('')
        self.assertEqual(data['interval_minutes'], 90)
        self.assertEqual(data['daily_cap'], 10)
        for invalid in (0, 25, True, '10'):
            self.assertEqual(self.call('/config', {'enabled':True, 'daily_cap':invalid})[0], 400)
        self.assertEqual(growth.limits(self.root), {'interval_minutes':90, 'daily_cap':10})
        self.assertEqual(phone_browse.config(self.root), before)
