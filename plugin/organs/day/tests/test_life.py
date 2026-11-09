import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from organs.day import life, model


class LifeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.day = "2026-11-07"
        self.now = model.iso("2026-11-07T10:00:00")
        self.raw = {"title": "慢慢来的一天", "items": [
            {"start": 0, "end": 240, "activity": "睡觉"},
            {"start": 240, "end": 1200, "activity": "读书", "location": "家"},
            {"start": 1200, "end": 1440, "activity": "睡觉"}]}
        self.complete = Mock(return_value=json.dumps(self.raw))

    def generate(self, **kwargs):
        return life.generate(self.day, persona="爱看书", root=self.root, now=self.now,
                             complete=self.complete, **kwargs)

    def test_get_is_readonly_and_default_off(self):
        self.assertFalse(life.view(self.now, root=self.root)["enabled"])
        self.assertEqual([], list(self.root.iterdir()))
        self.assertEqual("", life.context(self.now, root=self.root))

    def test_generation_traces_survive_failure_retry_and_restore_outer_scope(self):
        from organs.mind import llm
        from organs.mind.tests.test_llm import FakeTransport, conf, ok
        transport = FakeTransport(TimeoutError('test'), ok(json.dumps(self.raw)), ok('outer'))
        with patch.dict('os.environ', {'QQAGENT_TRACE':'1','QQAGENT_HOME_ROOT':str(self.root)}), \
             patch.object(llm, 'load_config', return_value=conf(retries=0)), \
             patch.object(llm, 'read_key', return_value='test'), \
             patch.object(llm, 'default_transport', transport), llm.trace_round('outer-test', 'test'):
            failed = life.generate(self.day, persona='爱看书', root=self.root, now=self.now)
            self.assertEqual('failed', failed['status'])
            self.assertEqual(1, len(failed.get('model_runs', [])))
            ready = life.generate(self.day, persona='爱看书', root=self.root, now=self.now, retry=True)
            self.assertEqual('ready', ready['status'])
            self.assertEqual([1,2], [r['attempt'] for r in ready['model_runs']])
            self.assertEqual(failed['model_runs'][0], ready['model_runs'][0])
            self.assertNotEqual(ready['model_runs'][0]['turn_id'], ready['model_runs'][1]['turn_id'])
            self.assertEqual(ready, life.generate(self.day, persona='爱看书', root=self.root, now=self.now))
            self.assertEqual(ready, life.view(self.now, root=self.root)['plan'])
            self.assertEqual(2, len(transport.calls))
            llm.complete([{'role':'user','content':'outer'}])
        traces = [json.loads(line) for path in (self.root/'mind/trace').glob('*.jsonl')
                  for line in path.read_text().splitlines()]
        self.assertEqual([r['turn_id'] for r in ready['model_runs']] + ['outer-test'],
                         [r['turn_id'] for r in traces])
        self.assertEqual(['安排生活背景']*2, [r['purpose'] for r in traces[:2]])
        self.assertEqual(['day:life:'+self.day]*2, [r['session_id'] for r in traces[:2]])
        self.assertEqual('timeout', traces[0]['error'])

    def test_contiguous_absolute_times_and_rollover(self):
        plan = self.generate()
        self.assertEqual("ready", plan["status"])
        self.assertEqual("2026-11-08T04:00", plan["items"][-1]["end"][:16])
        before = life.view(model.iso("2026-11-08T03:59:59"), root=self.root)
        self.assertEqual(self.day, before["day"])
        self.assertEqual("睡觉", before["current"]["activity"])
        self.assertEqual("2026-11-08", life.view(model.iso("2026-11-08T04:00:00"), root=self.root)["day"])
        state = life.view(model.iso("2026-11-07T08:00:00"), root=self.root)
        self.assertEqual("读书", state["current"]["activity"])

    def test_rejects_gaps_overlaps_incomplete_and_non_integer(self):
        for changed in ({"start": 241}, {"start": 200}, {"end": 1500}, {"start": True}):
            raw = json.loads(json.dumps(self.raw))
            raw["items"][1].update(changed)
            with self.assertRaises(life.LifeError):
                life.validate(raw, self.day, self.root)
        self.raw["items"].pop()
        with self.assertRaises(life.LifeError):
            life.validate(self.raw, self.day, self.root)

    def test_failure_and_restart_do_not_repeat_calls(self):
        self.complete.return_value = "not json"
        self.assertEqual("failed", self.generate()["status"])
        self.generate()
        self.complete.assert_called_once()
        self.complete.return_value = json.dumps(self.raw)
        self.assertEqual("ready", self.generate(retry=True)["status"])
        self.generate(retry=True)
        self.assertEqual(2, self.complete.call_count)
        saved = json.loads((self.root / "life-days" / (self.day + ".json")).read_text())
        saved.update(status="pending", items=[])
        (self.root / "life-days" / (self.day + ".json")).write_text(json.dumps(saved))
        self.generate()
        self.assertEqual(2, self.complete.call_count)

    def test_adjust_current_preserves_elapsed_and_audit(self):
        plan = self.generate()
        old = plan["items"][1]
        body = {"day": self.day, "revision": 1, "start": old["start"],
                "activity": "画画", "location": "书桌", "detail": "画一只猫"}
        changed = life.adjust(body, root=self.root, now=self.now)
        self.assertEqual(old["activity"], changed["items"][1]["activity"])
        self.assertEqual(self.now, changed["items"][1]["end"])
        self.assertEqual(self.now, changed["items"][2]["start"])
        self.assertEqual("画画", changed["items"][2]["activity"])
        self.assertEqual(old, changed["changes"][0]["before"])
        with self.assertRaisesRegex(life.LifeError, "已变化"):
            life.adjust(body, root=self.root, now=self.now)
        body.update(revision=2, start=changed["items"][0]["start"])
        with self.assertRaisesRegex(life.LifeError, "已结束"):
            life.adjust(body, root=self.root, now=self.now)
        # 新功能的落盘仅有计划，不写脑、家、任务或 QQ 队列。
        self.assertEqual({"life-days"}, {p.name for p in self.root.iterdir()})

    def test_context_is_current_and_opt_in(self):
        self.generate()
        self.assertEqual("", life.context(self.now, root=self.root))
        life.configure(True, root=self.root, now=self.now)
        ctx = life.context(self.now, root=self.root)
        self.assertIn('"activity": "读书"', ctx)
        self.assertIn("不代表真实执行", ctx)
        life.configure(False, root=self.root, now=self.now)
        self.assertEqual("", life.context(self.now, root=self.root))

    def test_actual_rest_overrides_planned_activity_without_rewriting_history(self):
        from organs.heart import rest
        self.generate()
        life.configure(True, root=self.root)
        heart = self.root / 'heart'
        for now, wake, expected in (
            ('2026-11-07T23:01:00', False, '睡着'),
            ('2026-11-07T23:02:00', True, '被叫醒后硬撑'),
            ('2026-11-07T23:18:00', False, '彻底睡着'),
        ):
            with self.subTest(now=now):
                resting = rest.advance(heart, model.iso(now), can_wake=wake)
                before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                state = life.view(model.iso(now), root=self.root)
                self.assertEqual(resting, state['rest'])
                self.assertEqual('读书', state['current']['activity'])
                ctx = life.context(model.iso(now), root=self.root)
                self.assertIn(expected, ctx)
                self.assertNotIn('读书', ctx)
                self.assertNotIn('"next"', ctx)
                self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        rest.advance(heart, model.iso('2026-11-08T08:01:00'))
        # 回到醒着时允许使用仍存在的背景；不伪造翌日计划。
        self.assertEqual('awake', life.view(model.iso('2026-11-08T08:01:00'), root=self.root)['rest']['phase'])

    def test_generation_uses_effective_sleep_window_from_explicit_heart(self):
        from organs.heart import sleep, store
        heart = self.root / 'other-heart'
        store.write_json(heart/'sleep.json', {'consecutive_late_nights': 2})
        window = sleep.overview(heart, self.now)['effective_window']
        self.assertNotEqual([23, 8], list(window))
        result = life.generate(self.day, persona='爱看书', root=self.root, now=self.now,
                               heart_root=heart, complete=self.complete)
        self.assertEqual('ready', result['status'])
        prompt = self.complete.call_args.args[0][0]['content']
        self.assertIn('当前生效睡眠时段', prompt)
        self.assertIn('%02d:00–%02d:00' % tuple(window), prompt)
        self.assertFalse((self.root/'heart').exists())

    def test_evening_prepares_tomorrow_once_and_read_does_not_generate(self):
        life.configure(True, root=self.root)
        with patch.object(life.llm, "complete", self.complete):
            life.tick(persona="爱看书", root=self.root, now=model.iso("2026-11-07T22:01:00"))
            life.tick(persona="爱看书", root=self.root, now=model.iso("2026-11-07T22:02:00"))
        self.assertEqual(2, self.complete.call_count)
        self.assertTrue((self.root / "life-days/2026-11-08.json").exists())

    def test_boundary_inputs_and_busy(self):
        for day in ("../secrets", None, "2026-11-07/other"):
            with self.assertRaises(ValueError):
                life.view(self.now, day=day or "invalid", root=self.root)
        with self.assertRaises(life.LifeError):
            life.configure("true", root=self.root)
        life._WRITE.acquire()
        try:
            with self.assertRaisesRegex(life.LifeError, "正在"):
                self.generate()
        finally:
            life._WRITE.release()
