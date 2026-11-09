import json
import unittest
from unittest.mock import patch, MagicMock

from api import api


class ChannelStatusTest(unittest.TestCase):
    def get(self):
        return api.handle('GET','/observe/channel',None,None)

    def test_health_is_local_read_only_and_returns_only_connection_fields(self):
        data={'channel':'nonebot','bots':[{'self_id':'12345','business_connected':True,'running':True,'extra':'private-value'}]}
        connection=MagicMock();connection.getresponse.return_value.status=200
        connection.getresponse.return_value.read.return_value=json.dumps(data).encode()
        with patch('api.observe.HTTPConnection',return_value=connection) as factory, \
             patch.dict('os.environ',{'QQAGENT_NONEBOT_PORT':'6298'}):
            status,result=self.get()
            self.assertEqual(status,200);self.assertTrue(result['ok'])
            self.assertEqual(result['bots'],[{'self_id':'12345','business_connected':True,'running':True}])
            self.assertNotIn('private-value',json.dumps(result))
            factory.assert_called_once_with('127.0.0.1',6298,timeout=2)
            connection.request.assert_called_once_with('GET','/healthz')
            connection.close.assert_called_once()
            self.assertEqual(api.handle('POST','/observe/channel',{},None)[0],405)
            self.assertEqual(factory.call_count,1)

    def test_empty_connections_and_failure_are_distinct(self):
        connection=MagicMock();connection.getresponse.return_value.status=200
        with patch('api.observe.HTTPConnection',return_value=connection):
            connection.getresponse.return_value.read.return_value=b'{"channel":"nonebot","bots":[]}'
            self.assertEqual(self.get()[1]['bots'],[])
            for body in [b'not json',b'{"channel":"other","bots":[]}',
                         b'{"channel":"nonebot","bots":[{"self_id":"123","running":true,"business_connected":"false"}]}']:
                connection.getresponse.return_value.read.return_value=body
                code,result=self.get();self.assertEqual(code,502);self.assertFalse(result['ok'])
                self.assertNotIn('bots',result)
            connection.getresponse.return_value.status=503
            self.assertEqual(self.get()[0],502)
            connection.request.side_effect=OSError('test unavailable')
            self.assertEqual(self.get()[0],502)

    def test_bad_port_does_not_open_a_connection(self):
        with patch.dict('os.environ',{'QQAGENT_NONEBOT_PORT':'https://other.example'}), \
             patch('api.observe.HTTPConnection') as factory:
            self.assertEqual(self.get()[0],500)
            factory.assert_not_called()


class BusinessRuntimeTest(unittest.TestCase):
    def get(self):
        return api.handle('GET','/observe/channel-runtime',None,None)

    def test_runtime_proxy_uses_local_configured_channel_and_excludes_extra_fields(self):
        data={'ok':True,'channel':'onebot-business','started_at':'2026-11-09T01:00:00Z',
              'mode':'send','core_enabled':True,'core_timeout_ms':45000,'policy':{'batch':{}},
              'gate_words':{'chitchatSampleMod':10},'disk_matches':False,'config_error':None,
              'environment':{'secret':'do-not-return'}}
        connection=MagicMock();connection.getresponse.return_value.status=200
        connection.getresponse.return_value.read.return_value=json.dumps(data).encode()
        with patch('api.observe.HTTPConnection',return_value=connection) as factory, \
             patch.dict('os.environ',{'HAND_CHANNEL_URL':'http://localhost:6208/'}):
            code,result=self.get();self.assertEqual(code,200)
            self.assertFalse(result['disk_matches']);self.assertEqual(result['gate_words'],data['gate_words'])
            self.assertNotIn('do-not-return',json.dumps(result))
            factory.assert_called_once_with('localhost',6208,timeout=2)
            connection.request.assert_called_once_with('GET','/runtime')
            connection.close.assert_called_once()
            self.assertEqual(api.handle('POST','/observe/channel-runtime',{},None)[0],405)

    def test_nonlocal_or_ambiguous_address_does_not_make_request(self):
        for address in ('https://example.com:6200','http://example.com:6200',
                        'http://user:secret@localhost:6200','http://127.0.0.1:6200/path',
                        'http://localhost:6200?foo=bar','http://localhost:99999'):
            with self.subTest(address=address), patch.dict('os.environ',{'HAND_CHANNEL_URL':address}), \
                 patch('api.observe.HTTPConnection') as connection:
                self.assertEqual(self.get()[0],500);connection.assert_not_called()

    def test_offline_or_old_channel_does_not_invent_loaded_policy(self):
        connection=MagicMock();connection.getresponse.return_value.status=200
        with patch('api.observe.HTTPConnection',return_value=connection), \
             patch.dict('os.environ',{'HAND_CHANNEL_URL':'http://127.0.0.1:6200'}):
            for body in (b'not json', b'{}', b'{"ok":true,"channel":"other"}'):
                connection.getresponse.return_value.read.return_value=body
                code,data=self.get();self.assertEqual(code,502);self.assertNotIn('policy',data)
            connection.request.side_effect=OSError('not running')
            self.assertEqual(self.get()[0],502)
