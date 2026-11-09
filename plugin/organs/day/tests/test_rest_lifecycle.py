import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from api import api, day
from organs.day import phone_browse, proactive, solitude
from organs.heart import rest


class RestLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.heart = self.root/'heart'
        self.addCleanup(patch.stopall)
        patch.dict('os.environ', {'HEART_ROOT':str(self.heart), 'QQAGENT_HOME':str(self.root)}).start()
        patch('organs.mind.llm.complete', side_effect=AssertionError('此测试不调用模型')).start()

    def test_day_tick_starts_actual_sleep_once_and_panel_remains_readonly(self):
        for at in ('2026-11-07T23:00:00+09:00', '2026-11-07T23:01:00+09:00'):
            day.run_tick(at, root=self.root, heart_root=self.heart)
        self.assertEqual('asleep', rest.state(self.heart)['phase'])
        before = (self.heart/'rest.json').read_bytes()
        status, body = api.handle('GET', '/heart/sleep', None, None)
        self.assertEqual(200, status)
        self.assertEqual('asleep', body['rest']['phase'])
        self.assertEqual('2026-11-07T23:00:00+09:00', body['last_asleep_at'])
        self.assertEqual(before, (self.heart/'rest.json').read_bytes())

    def test_saved_holding_state_stops_autonomy_even_after_clock_window(self):
        rest.advance(self.heart, '2026-11-07T23:00:00+09:00')
        rest.advance(self.heart, '2026-11-08T07:55:00+09:00', can_wake=True)
        # 尚未有tick推进早晨转换；只读消费者应尊重已保存阶段，不能各自猜她已醒。
        at = '2026-11-08T08:01:00+09:00'
        self.assertTrue(phone_browse._feeling(self.heart, at)['asleep'])
        active = proactive.readings(at, root=self.root, heart_root=self.heart)
        self.assertEqual('不说', active['engage']['state'])
        self.assertTrue(solitude.readings(at, root=self.root, heart_root=self.heart)['night'])
