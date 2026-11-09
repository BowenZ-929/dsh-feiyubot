"""后台和HTTP共用tick的角色/状态接线；模型替身，不发消息。"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api import day as day_api
from organs.day import delivery, proactive, proactive_ask
from .test_proactive import ProactiveCase, open_thought
from .helpers import at


class ProactivePersonaTest(ProactiveCase):
    def test_shared_tick_sends_current_persona_and_heart_state_to_proactive_model(self):
        self.heard()
        open_thought(self.heart)
        self.assertIsNotNone(self.consider()['plan'])
        stamp = at('2026-11-05T10:36:00')
        state = proactive.readings(stamp, root=self.root, heart_root=self.heart, brain=self.brain)
        persona = '【虚构角色验收】她是小岚，喜欢海边的旧书店。'
        complete = Mock(return_value='{"send":true,"chunks":["我想起那家书店了"]}')
        with patch.object(proactive_ask, 'mind_complete', complete), \
             patch('organs.day.life.tick', return_value={}), \
             patch('organs.day.phone_browse.tick', return_value={}):
            result = day_api.run_tick(stamp, root=self.root, brain=self.brain, heart_root=self.heart,
                                      store=SimpleNamespace(render=lambda: persona))
        self.assertEqual({'queued': 1}, result['proactive']['actions'])
        self.assertEqual(1, complete.call_count)
        messages = complete.call_args.args[0]
        self.assertEqual(1, messages[0]['content'].count(persona))
        self.assertIn(state['mood']['tier'], messages[1]['content'])
        self.assertIn(state['energy']['tier'], messages[1]['content'])
        self.assertIn(stamp, messages[1]['content'])
        self.assertNotIn(str(self.root), str(messages))
        self.assertEqual(1, len(delivery.load(self.root)['items']))
        self.assertEqual('expect', delivery.load(self.root)['items'][0]['state'])
