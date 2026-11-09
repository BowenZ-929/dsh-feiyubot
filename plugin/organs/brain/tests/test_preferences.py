import tempfile
import unittest
from pathlib import Path
from organs.brain import preferences


class PreferencesTest(unittest.TestCase):
    def test_persistent_scoped_replacements_and_retraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            one=preferences.record(root, who='100001', session_id='qq:private:100001',
                change={'text':'吐槽时陪聊，问怎么办时可以建议', 'evidence':['以后吐槽先陪聊'], 'replaces':[]},
                at='2026-11-09T08:00:00+09:00', turn_id='turn1', entry_ids=['t-one'])
            self.assertEqual([one],preferences.current(Path(tmp),'100001','qq:private:100001'))
            self.assertEqual([],preferences.current(root,'100002','qq:private:100001'))
            self.assertEqual([],preferences.current(root,'100001','qq:group:1'))
            two=preferences.record(root,who='100001',session_id='qq:private:100001',
                change={'text':'不用刻意可爱，照常说话','evidence':['之前可爱的要求作废'], 'replaces':[one['id']]},
                at='2026-11-09T09:00:00+09:00',turn_id='turn2',entry_ids=['t-two'])
            self.assertEqual([two],preferences.current(root,'100001','qq:private:100001'))
            preferences.record(root,who='100001',session_id='qq:private:100001',
                change={'text':'','evidence':[],'replaces':[two['id']]},at='2026-11-09T10:00:00+09:00',actor='panel')
            self.assertEqual([],preferences.current(root,'100001','qq:private:100001'))
            history=preferences.history(root,'100001')
            self.assertEqual(3,len(history))
            self.assertEqual(['t-one'],history[0]['entry_ids'])
            self.assertEqual('panel',history[-1]['actor'])
            self.assertEqual([],preferences.history(root,'100002'))
