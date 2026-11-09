"""同一份非默认睡眠账，应在聊天、面板、主动和独处得到相同作息判断。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api import api
from organs.brain import BrainStore
from organs.day import proactive, solitude
from organs.heart import energy, model, sleep
from organs.mind import conversation


class SleepConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.heart=self.root/'heart'; self.heart.mkdir()
        self.brain=BrainStore(self.root/'brain')
        self.addCleanup(patch.stopall)
        patch.dict('os.environ',{'HEART_ROOT':str(self.heart),'QQAGENT_HOME':str(self.root)}).start()
        for day in (4,5):
            sleep.record('asleep',root=self.heart,now=f'2026-11-{day:02d}T01:00:00+09:00')
            sleep.record('awake',root=self.heart,now=f'2026-11-{day:02d}T05:00:00+09:00')
        self.assertEqual((0,9),sleep.effective_window(self.heart))
        self.before=(self.heart/'sleep.json').read_bytes()

    def test_chat_panel_proactive_and_solitude_share_shifted_rest_window(self):
        for at,night in [('2026-11-05T23:30:00+09:00',False),('2026-11-06T08:30:00+09:00',True)]:
            with self.subTest(at=at):
                _,chat=conversation._engage(self.heart,at,{'mood':{'now':60}},
                                           {'value':80,'tier':'充沛'},self.brain,'42')
                status,panel=api.handle('GET','/heart/engage?now='+at.replace('+','%2B'),None,None,brain=self.brain)
                self.assertEqual(200,status)
                active=proactive.readings(at,root=self.root,heart_root=self.heart,brain=self.brain,who='42')
                idle=solitude.readings(at,root=self.root,heart_root=self.heart,brain=self.brain)
                for name,result in [('chat',chat),('panel',panel),('proactive',active['engage'])]:
                    with self.subTest(source=name):
                        self.assertEqual(night,model.REASON_NIGHT in result['hits'])
                self.assertEqual(night,idle['night'])
        self.assertEqual(self.before,(self.heart/'sleep.json').read_bytes(),'只读判断不得补造睡眠事件')

    def test_activity_energy_matches_sleep_adjusted_energy_including_short_sleep_cap(self):
        at='2026-11-06T08:30:00+09:00'
        seed={'version':1,'value':80,'updated_at':'2026-11-05T22:00:00+09:00',
              'day':'2026-11-05','deducted':0,'last_burst_at':None,'tier':None}
        path=self.heart/'energy.json'
        path.write_text(json.dumps(seed))
        expected=energy.read(self.heart,at,sleep_window=sleep.effective_window(self.heart,at),
                             wake_cap=sleep.wake_cap(self.heart,at))
        self.assertEqual(95,expected['value'])
        for reader in (proactive.readings,solitude.readings):
            path.write_text(json.dumps(seed))
            read=reader(at,root=self.root,heart_root=self.heart,brain=self.brain)
            actual=read['energy']['value'] if 'energy' in read else read['energy_value']
            self.assertEqual(expected['value'],actual)
