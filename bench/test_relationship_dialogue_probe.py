"""验证评估器会把实答带入后续、区分填充，并在失败时保留证据；无真实请求。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bench import relationship_dialogue_probe as probe


class RelationshipDialogueProbeTest(unittest.TestCase):
    def test_live_boundary_calls_only_key_rounds_and_carries_actual_replies(self):
        requests = []

        def official(url, headers, payload, timeout):
            requests.append(payload)
            content = (json.dumps({'kind': None, 'evidence': '', 'reason': '模拟识别'})
                       if payload['messages'][0]['content'].startswith(probe.appraisal.PROMPT)
                       else '这是关键回合的实答标记%s。' % len(requests))
            return 200, json.dumps({'choices': [{'message': {'content': content,
                                   'reasoning_content': '模拟推理'}}], 'usage': {'total_tokens': 7}}).encode()

        with patch.object(probe.llm, 'read_key', return_value='mock-only'), \
                patch.object(probe.llm, 'default_transport', side_effect=official), \
                contextlib.redirect_stdout(io.StringIO()):
            report = probe.run(live=True)
        self.assertTrue(report['completed'])
        self.assertEqual(26, report['real_calls'])
        self.assertEqual(26, len(requests))
        self.assertEqual(52, sum(call['filler'] for call in report['calls']))
        self.assertFalse(any(call['live'] for call in report['calls'] if call['filler']))
        second = report['calls'][3]['request']['messages']
        self.assertIn('这是关键回合的实答标记2。', '\n'.join(m['content'] for m in second))
        self.assertEqual('模拟推理', report['calls'][1]['response']['choices'][0]['message']['reasoning_content'])
        self.assertEqual(7, report['calls'][1]['response']['usage']['total_tokens'])

    def test_failure_retains_partial_requests_without_claiming_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'failed.json'
            with patch.object(probe.llm, 'read_key', return_value='mock-only'), \
                    patch.object(probe.llm, 'default_transport', side_effect=RuntimeError('模拟传输中断')), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(probe.llm.LLMError, '模拟传输中断'):
                probe.run(live=True, output=output)
            report = json.loads(output.read_text())
        self.assertFalse(report['completed'])
        # 理解阶段按现有规则降级后，主回复也失败；这是两个阶段，不是重试。
        self.assertEqual(2, report['real_calls'])
        self.assertEqual(2, len(report['calls']))
        self.assertNotIn('mock-only', json.dumps(report))

    def test_budget_stops_before_an_extra_official_request(self):
        response = json.dumps({'choices': [{'message': {'content': '{"kind": null}'}}]}).encode()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'budget.json'
            with patch.object(probe, 'LIMIT', 1), \
                    patch.object(probe.llm, 'read_key', return_value='mock-only'), \
                    patch.object(probe.llm, 'default_transport', return_value=(200, response)) as official, \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(probe.llm.LLMError):
                probe.run(live=True, output=output)
            report = json.loads(output.read_text())
        self.assertEqual(1, official.call_count)
        self.assertEqual(1, report['real_calls'])
        self.assertFalse(report['completed'])

    def test_non_json_http_failure_keeps_status_and_original_body(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'http-error.json'
            with patch.object(probe.llm, 'read_key', return_value='mock-only'), \
                    patch.object(probe.llm, 'default_transport', return_value=(502, b'<html>Bad Gateway</html>')), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(probe.llm.LLMError):
                probe.run(live=True, output=output)
            report = json.loads(output.read_text())
        for call in report['calls']:
            self.assertEqual(502, call['status'])
            self.assertEqual('<html>Bad Gateway</html>', call['response_text'])

    def test_empty_gated_replies_are_reported_as_missing_samples(self):
        def official(url, headers, payload, timeout):
            answer = ('{"kind": null}' if payload['messages'][0]['content'].startswith(probe.appraisal.PROMPT)
                      else '感谢您的理解。')
            return 200, json.dumps({'choices': [{'message': {'content': answer}}]}).encode()
        with patch.object(probe.llm, 'read_key', return_value='mock-only'), \
                patch.object(probe.llm, 'default_transport', side_effect=official), \
                contextlib.redirect_stdout(io.StringIO()):
            report = probe.run(live=True)
        self.assertTrue(report['completed'], '只表示执行完毕')
        self.assertEqual('not_evaluated', report['semantic_verdict'])
        self.assertEqual(0, report['samples']['nonempty'])
        self.assertEqual(13, len(report['samples']['empty_replies']))
        self.assertEqual(0, report['samples']['unexecuted'])
        first = report['cases'][0]['rounds'][0]
        self.assertEqual('dropped', first['gate']['decision'])
        self.assertIn('audit', first)


if __name__ == '__main__':
    unittest.main()
