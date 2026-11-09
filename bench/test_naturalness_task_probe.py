"""对照准备和付费调用边界；全部使用虚构请求与模拟HTTP。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bench import naturalness_task_probe as probe


def source():
    payload={'model':'deepseek-flash','thinking':{'type':'enabled'},'reasoning_effort':'high',
             'max_tokens':8192,'temperature':.7,'messages':[
                 {'role':'system','content':'[魂六件]\n虚构人设\n[最近几轮]\n还没有更早的对话\n\n[与这个人的近期相处]\n'},
                 {'role':'user','content':'原始虚构输入'}]}
    return {'synthetic':True,'calls':[
        {'case':'A伤人后B普通分享','round':2,'request':payload},
        {'case':'A伤人后A道歉','round':1,'request':copy.deepcopy(payload)}]}


class NaturalnessTaskProbeTest(unittest.TestCase):
    def test_preparation_changes_only_target_task_note_and_keeps_source(self):
        original=source(); before=copy.deepcopy(original)
        cases=probe.prepare(original)
        self.assertEqual(len(cases),6)
        for case in cases:
            baseline,candidate=case['requests']
            note=probe.NOTES[case['focus']]
            self.assertEqual(candidate['messages'][0]['content'],baseline['messages'][0]['content']+'\n\n'+note)
            restored=copy.deepcopy(candidate);restored['messages'][0]['content']=baseline['messages'][0]['content']
            self.assertEqual(restored,baseline)
        self.assertEqual(original,before)
        self.assertIn('周日',cases[4]['requests'][0]['messages'][0]['content'])
        self.assertIn('小周',cases[5]['requests'][0]['messages'][0]['content'])

    def test_offline_does_not_read_key_or_open_network(self):
        with patch.object(probe.llm,'read_key',side_effect=AssertionError('offline key read')), \
             patch.object(probe.llm,'default_transport',side_effect=AssertionError('offline network')):
            report=probe.run(source(),live=False)
        self.assertEqual(report['real_calls'],0)
        self.assertEqual(report['planned_calls'],12)
        self.assertEqual(report['semantic_verdict'],'not_evaluated')

    def test_live_cap_no_retry_and_raw_failure_saved_without_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.json'
            replies=iter([(200,b'{"choices":[{"message":{"content":"synthetic","reasoning_content":"synthetic reasoning"}}],"usage":{"total_tokens":9}}'),(502,b'not json')])
            def send(*args):return next(replies)
            with patch.object(probe.llm,'resolve_config',return_value={'base_url':'https://api.deepseek.com/v1','timeout_ms':30000}), \
                 patch.object(probe.llm,'read_key',return_value='secret-not-in-report'), \
                 patch.object(probe.llm,'default_transport',side_effect=send) as transport:
                report=probe.run(source(),live=True,output=path)
            self.assertEqual(transport.call_count,2)
            self.assertEqual(report['real_calls'],2)
            self.assertFalse(report['completed'])
            self.assertEqual(report['calls'][-1]['response_text'],'not json')
            self.assertEqual(report['calls'][0]['response']['usage']['total_tokens'],9)
            self.assertNotIn('secret-not-in-report',path.read_text())

    def test_successful_transport_does_not_become_semantic_pass(self):
        body=json.dumps({'choices':[{'message':{'content':'synthetic'}}]}).encode()
        with patch.object(probe.llm,'resolve_config',return_value={'base_url':'https://api.deepseek.com','timeout_ms':30000}), \
             patch.object(probe.llm,'read_key',return_value='fake'), \
             patch.object(probe.llm,'default_transport',return_value=(200,body)) as transport:
            report=probe.run(source(),live=True)
        self.assertEqual(transport.call_count,12)
        self.assertEqual(report['real_calls'],12)
        self.assertTrue(report['completed'])
        self.assertEqual(report['semantic_verdict'],'not_evaluated')

    def test_expanded_batch_rejected_before_reading_key_or_sending(self):
        expanded=probe.prepare(source())
        expanded.append(copy.deepcopy(expanded[0]))
        with patch.object(probe,'prepare',return_value=expanded), \
             patch.object(probe.llm,'read_key',side_effect=AssertionError('unexpected key read')), \
             patch.object(probe.llm,'default_transport',side_effect=AssertionError('unexpected network')):
            with self.assertRaisesRegex(ValueError,'上限'):
                probe.run(source(),live=True)

    def test_empty_reply_is_not_a_completed_comparison(self):
        body=b'{"choices":[{"message":{"content":""}}]}'
        with patch.object(probe.llm,'resolve_config',return_value={'base_url':'https://api.deepseek.com','timeout_ms':30000}), \
             patch.object(probe.llm,'read_key',return_value='fake'), \
             patch.object(probe.llm,'default_transport',return_value=(200,body)) as transport:
            report=probe.run(source(),live=True)
        self.assertEqual(transport.call_count,1)
        self.assertFalse(report['completed'])
        self.assertEqual(report['calls'][0]['error'],'http_or_empty')

    def test_transport_failure_keeps_kind_and_status_without_exception_message(self):
        for kind,status in [('timeout',0),('http',503)]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'report.json'
                with patch.object(probe.llm,'resolve_config',return_value={'base_url':'https://api.deepseek.com','timeout_ms':30000}), \
                     patch.object(probe.llm,'read_key',return_value='fake-secret'), \
                     patch.object(probe.llm,'default_transport',side_effect=probe.llm.LLMError(kind,'fake-secret',status)) as transport:
                    report=probe.run(source(),live=True,output=path)
                self.assertEqual(transport.call_count,1)
                self.assertFalse(report['completed'])
                self.assertEqual(report['calls'][0]['error_kind'],kind)
                self.assertEqual(report['calls'][0]['status'],status)
                self.assertNotIn('fake-secret',path.read_text())
