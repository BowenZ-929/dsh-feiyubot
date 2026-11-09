"""CLI regression for isolated current-context conversational evaluation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ContinuityProbeTest(unittest.TestCase):
    def test_materials_are_synthetic_visible_and_keep_private_diary_out(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'report.json'
            done=subprocess.run([sys.executable,str(ROOT/'bench/continuity_probe.py'),
                '--scenario','materials','--output',str(output)],capture_output=True,text=True)
            self.assertEqual(0,done.returncode,done.stderr[-2000:])
            report=json.loads(output.read_text())
            self.assertEqual(5,len(report['results']))
            self.assertEqual(0,report['real_calls'])
            self.assertEqual(4,len(report['fixtures']))
            requests=json.dumps([r['request'] for r in report['calls']],ensure_ascii=False)
            self.assertIn('纸船停在玻璃瓶里',requests)
            self.assertIn('灯塔',requests)
            self.assertIn('未取得最终图观察',requests)
            self.assertNotIn('私密代号紫砂壶',requests)
            self.assertEqual({},report['initial_profile'])
            self.assertTrue(report['completed'])
            first=report['results'][0]['assembly']['sections']
            third=report['results'][2]['assembly']['sections']
            self.assertIn('drawer_always',[s['key'] for s in first])
            self.assertIn('drawer',[s['key'] for s in third])
            self.assertNotIn('drawer_always',[s['key'] for s in report['results'][1]['assembly']['sections']])

    def test_naturalness_uses_one_isolated_root_and_current_neutral_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'report.json'
            done=subprocess.run([sys.executable,str(ROOT/'bench/continuity_probe.py'),
                '--scenario','naturalness','--output',str(output)],capture_output=True,text=True)
            self.assertEqual(0,done.returncode,done.stderr[-3000:])
            report=json.loads(output.read_text())
            self.assertEqual(10,len(report['results']))
            self.assertEqual(0,report['real_calls'])
            self.assertEqual('not_evaluated',report['semantic_verdict'])
            self.assertTrue(report['completed'])
            self.assertTrue(all(v['inner']==v['shown']==50 for v in report['baseline']['axes']))
            root=Path(report['isolated_root'])
            self.assertEqual(str(root/'brain'),report['brain_root'])
            self.assertTrue((root/'mind/recent_turns.jsonl').exists())
            requests=[r['request'] for r in report['calls']]
            self.assertTrue(any('河岸图书馆' in str(p) for p in requests))
            self.assertTrue(any('普通分享' in r['criterion'] for r in report['results']))
            self.assertNotIn(str(ROOT/'home/organs'),json.dumps(requests))
            self.assertEqual({},report['initial_profile'])
            self.assertEqual(20,len(requests))
            self.assertIn('离线占位。',json.dumps(requests[-1],ensure_ascii=False))
            merged=report['results'][4]
            self.assertEqual(2,len(merged['segment']))
            self.assertEqual('打错了，是河岸图书馆，饮料照旧',merged['segment'][-1]['text'])

    def test_live_transport_failure_keeps_partial_evidence(self):
        from unittest.mock import patch
        from bench import continuity_probe as probe
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'failed.json'
            with patch.object(probe.llm,'read_key',return_value='synthetic-key'), \
                 patch.object(probe.llm,'default_transport',side_effect=probe.llm.LLMError('timeout','test')), \
                 patch.object(probe.rice,'record') as record:
                with self.assertRaises(probe.llm.LLMError):
                    probe.run(live=True,scenario='naturalness',output=output)
                record.assert_not_called()
            report=json.loads(output.read_text())
            self.assertFalse(report['completed'])
            self.assertGreater(report['real_calls'],0)
            self.assertEqual([],report['results'])
            self.assertNotIn('synthetic-key',output.read_text())


if __name__=='__main__':unittest.main()
