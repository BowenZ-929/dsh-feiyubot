import tempfile
import unittest
from pathlib import Path
from api import api
from organs.brain import BrainStore, preferences


class PreferencesApiTest(unittest.TestCase):
    def test_read_evidence_history_and_retract_only_target_persons_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain=BrainStore(Path(tmp))
            row=preferences.record(brain.root,who='100001',session_id='qq:private:100001',
                change={'text':'说话随口一点','evidence':['以后和我说话随口一点'],'replaces':[]},
                at='2026-11-09T08:00:00+09:00',turn_id='t1',entry_ids=['m1'])
            def call(method,who='100001',body=None):
                return api.handle(method,'/brain/people/'+who+'/preferences',body,None,brain=brain)
            code,data=call('GET')
            self.assertEqual(200,code);self.assertEqual([row],data['current']);self.assertEqual([row],data['history'])
            before=(brain.root/preferences.FILE).read_bytes()
            code,data=call('POST','100002',{'id':row['id']})
            self.assertEqual(404,code)
            self.assertEqual(before,(brain.root/preferences.FILE).read_bytes())
            code,data=call('POST',body={'id':row['id']})
            self.assertEqual(200,code);self.assertTrue(data['ok'])
            code,data=call('GET')
            self.assertEqual([],data['current']);self.assertEqual(2,len(data['history']))
            self.assertEqual([row['id']],data['history'][1]['replaces'])
            self.assertEqual('panel',data['history'][1]['actor'])
            self.assertEqual(404,call('POST',body={'id':row['id']})[0])
            self.assertEqual(400,call('POST',body={})[0])
