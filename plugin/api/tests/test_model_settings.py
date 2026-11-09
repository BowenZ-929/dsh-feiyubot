import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import api
from organs.mind import llm


class ModelSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict('os.environ', {'QQAGENT_HOME':str(self.root)})
        env.start(); self.addCleanup(env.stop)

    def test_read_is_public_only_and_does_not_write_or_call_network(self):
        with patch.object(llm, 'read_key') as key, patch.object(llm, 'complete') as call:
            status, data = api.handle('GET','/model/status',None,None)
        self.assertEqual(200,status)
        self.assertEqual(llm.resolve_config()['model'],data['config']['model'])
        self.assertNotIn('key_env',data['config'])
        self.assertNotIn('base_url',data['config'])
        key.assert_not_called(); call.assert_not_called()
        self.assertEqual([],list(self.root.iterdir()))

    def test_save_preserves_other_fields_and_next_request_uses_value(self):
        path = self.root/'mind/llm.json'; path.parent.mkdir()
        original = dict(llm.load_config(), note='保留说明', key_env='SECRET_NAME')
        path.write_text(json.dumps(original))
        status,data = api.handle('POST','/model/config',{'max_tokens':1234,'reasoning_effort':'low'},None)
        self.assertEqual(200,status,data)
        saved = json.loads(path.read_text())
        self.assertEqual('保留说明',saved['note'])
        self.assertEqual(original['temperature_tiers'],saved['temperature_tiers'])
        self.assertEqual('SECRET_NAME',saved['key_env'])
        requests=[]
        def transport(url,headers,payload,timeout):
            requests.append(payload)
            return 200,b'{"choices":[{"message":{"content":"ok"}}]}'
        with patch.object(llm,'read_key',return_value='isolated-key'):
            llm.complete([{'role':'user','content':'test'}],transport=transport)
        self.assertEqual(1234,requests[0]['max_tokens'])
        self.assertEqual('low',requests[0]['reasoning_effort'])
        history=self.root/'mind/config_history.jsonl'
        event=json.loads(history.read_text())
        self.assertEqual({'before':original['max_tokens'],'after':1234},event['changes']['max_tokens'])
        from organs.day import model
        date=model.parse_ts(event['at']).astimezone(model.local_tz()).date().isoformat()
        _,timeline=api.handle('GET','/observe/activity?source=model_settings&date='+date,None,None)
        self.assertEqual(event['changes'],timeline['rows'][0]['changes'])
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(200,api.handle('POST','/model/config',{'max_tokens':1234},None)[0])
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        code,_=api.handle('POST','/model/config',{'thinking':None,'reasoning_effort':None},None)
        self.assertEqual(200,code)
        cleared=llm.resolve_config()
        self.assertNotIn('thinking',cleared)
        self.assertNotIn('reasoning_effort',cleared)
        self.assertEqual(1234,cleared['max_tokens'])

    def test_rejects_invalid_and_secret_fields_without_writing(self):
        for body in ({},None,{'base_url':'https://other.invalid'}, {'key_env':'other'},
                     {'temperature':float('nan')}, {'temperature':3}, {'retries':-1},
                     {'max_tokens':False}, {'thinking':{'type':'maybe'}}, {'reasoning_effort':'unknown'}):
            with self.subTest(body=body):
                self.assertEqual(400,api.handle('POST','/model/config',body,None)[0])
        self.assertEqual([],list(self.root.iterdir()))

    def test_deepseek_output_limit_is_enforced_and_reported_to_the_form(self):
        status,data=api.handle('GET','/model/status',None,None)
        self.assertEqual(393216,data.get('limits',{}).get('max_tokens'))
        status,data=api.handle('POST','/model/config',{'max_tokens':393217},None)
        self.assertEqual(400,status)
        self.assertEqual([],list(self.root.iterdir()))
        self.assertEqual(200,api.handle('POST','/model/config',{'max_tokens':393216},None)[0])
