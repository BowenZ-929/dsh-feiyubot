import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from organs.brain.tests.helpers import fresh_store
from organs.day import growth, phone_browse, night, rhythm, model, plan
from organs.mind import llm
from .test_rhythm import NIGHT_CONFIG

class GrowthRuntimeTest(unittest.TestCase):
    def test_full_night_retry_reopens_profile_for_next_day_chat_and_activity(self):
        import os
        from organs.brain import BrainStore
        from organs.mind import conversation, persona
        from organs.day import autonomy
        from organs.mind.tests.test_note_display import NoteDisplayTest, CONFIG, ENV, _transport
        from .helpers import make_home

        fixture = NoteDisplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        home = make_home(self)
        brain = fixture.brain
        before = '2026-11-08T18:00:00+09:00'
        brain.ingest('12345678', '想试试画圆滚滚的小鲸鱼', side='她说',
                     ts=before, scene='group', audience='公开')
        brain.ingest('87654321', '私密紫茄子', ts=before, scene='private', session_id='qq:private:87654321')
        replies = {'日记写作': '今天说了想试试画圆滚滚的小鲸鱼。',
                   '日记事实复核': json.dumps({'text':'今天说了想试试画圆滚滚的小鲸鱼。','changes':[]}),
                   '梦境写作': '梦见一只圆滚滚的小鲸鱼。',
                   '整理自我认识': json.dumps({'patch':{'likes':'想试试画圆滚滚的小鲸鱼'},'note':'自己的选择','sources':[]})}
        calls = []
        def complete(messages, **kwargs):
            purpose = kwargs['purpose']
            calls.append(purpose)
            self.assertNotIn('紫茄子', json.dumps(messages, ensure_ascii=False))
            if purpose == '整理自我认识' and calls.count(purpose) == 1:
                raise llm.LLMError('timeout', '模拟超时')
            return replies[purpose]
        cfg = dict(NIGHT_CONFIG, night={'max_retries':3,'backoff_minutes':[1,5,15]})
        with patch.dict(os.environ, {'DAY_ROOT':str(self.root), 'HOME_ROOT':str(home)}):
            growth.configure(True, root=self.root)
            with patch.object(llm, 'complete', complete), patch.object(night, 'complete', complete), \
                 patch('organs.day.proactive.consider', return_value={}), \
                 patch('organs.day.proactive.settle_due', return_value={}):
                def tick(at):
                    return rhythm.tick(at, root=self.root, brain=brain, home_root=home,
                        heart_root=fixture.tmp/'heart', config=cfg, persona=persona.render(fixture.store, brain, root=self.root))
                first = tick('2026-11-09T03:30:00+09:00')
                self.assertEqual(first['night']['retried'][0]['failed_steps'], ['growth'])
                tick('2026-11-09T03:30:30+09:00')
                self.assertEqual(calls.count('整理自我认识'), 1)
                retried = tick('2026-11-09T03:31:00+09:00')
                self.assertTrue(retried['night']['executed'][0]['ok'])
                tick('2026-11-09T03:36:00+09:00')
            self.assertEqual(calls, ['日记写作','日记事实复核','梦境写作','整理自我认识','整理自我认识'])
            brain = BrainStore(brain.root)
            self.assertEqual(len(brain.selfnarration_versions()), 1)
            profile = brain.current_profile()
            captured = []
            conversation.answer('今天想画什么？', '12345678', store=fixture.store, brain=brain,
                now='2026-11-09T08:05:00+09:00', scene='private', session_id='qq:private:12345678',
                recent_root=fixture.tmp/'recent', transport=_transport('想画小鲸鱼。', captured), config=CONFIG, env=ENV)
            self.assertIn(profile['likes'], captured[-1]['messages'][0]['content'])
            activity = []
            def choose(messages):
                activity.extend(messages)
                return '{"action":"idle","reason":"先歇一会儿","args":{}}'
            with patch('organs.day.phone_browse._feeling', return_value={'asleep':False,'energy':{'value':80}}), \
                 patch('organs.hand.phone.config', return_value={}):
                result = autonomy.tick('2026-11-09T08:05:00+09:00', root=self.root, home_root=home,
                    brain=brain, persona=persona.render(fixture.store, brain, root=self.root), complete=choose)
            self.assertEqual(result['status'], 'done')
            self.assertIn(profile['likes'], json.loads(activity[1]['content'])['persona'])

    def setUp(self):
        self.brain, temp = fresh_store()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'day'

    def test_new_limits_do_not_change_legacy_phone_and_cap_at_ten(self):
        from organs.day import autonomy
        before = phone_browse.config(self.root)
        growth.configure(True, root=self.root, interval_minutes=90, daily_cap=10)
        start = datetime.fromisoformat('2026-11-08T08:00:00+09:00')
        complete = Mock(return_value='{"action":"idle","reason":"闲着","args":{}}')
        with patch('organs.day.phone_browse._feeling', return_value={'asleep':False,'energy':{'value':80}}), \
             patch('organs.hand.phone.config', return_value={}):
            for i in range(10):
                result = autonomy.tick((start+timedelta(minutes=i*90)).isoformat(),root=self.root,complete=complete)
                self.assertEqual(result['status'],'done')
            self.assertEqual(autonomy.tick((start+timedelta(minutes=900)).isoformat(),root=self.root,complete=complete)['reason'],'daily_cap')
        self.assertEqual(complete.call_count,10)
        growth.configure(False,root=self.root)
        self.assertEqual(phone_browse.config(self.root),before)

    def test_failed_growth_retries_without_repeating_successful_night_steps(self):
        self.check_retry(always_fail=False)

    def test_failed_growth_exhausts_existing_retry_budget(self):
        self.check_retry(always_fail=True)

    def check_retry(self, always_fail):
        growth.configure(True,root=self.root)
        steps={name:Mock(return_value={'status':'ok','detail':'已完成'}) for name in night.STEPS}
        response=json.dumps({'patch':{'views':'愿意多试一试'},'note':'自己的选择','sources':[]})
        complete=Mock(side_effect=[llm.LLMError('timeout','虚构超时')]*4 if always_fail else [llm.LLMError('timeout','虚构超时'),response])
        cfg=dict(NIGHT_CONFIG,night={'max_retries':3,'backoff_minutes':[1,5,15]})
        base=datetime.fromisoformat('2026-11-08T03:30:00+09:00')
        with patch.dict(night._STEP_FUNCS,steps),patch.object(llm,'complete',complete), \
             patch('organs.day.proactive.consider',return_value={}),patch('organs.day.proactive.settle_due',return_value={}):
            def tick(minutes):
                return rhythm.tick((base+timedelta(minutes=minutes)).isoformat(),root=self.root,brain=self.brain,config=cfg,persona='虚构性情')
            first=tick(0)
            self.assertEqual(first['night']['retried'][0]['failed_steps'],['growth'])
            tick(0.5);self.assertEqual(complete.call_count,1)
            second=tick(1)
            if always_fail:
                tick(6);last=tick(21);tick(22)
                self.assertEqual(len(last['night']['never_ran']),1)
                self.assertEqual(complete.call_count,4)
                self.assertEqual(self.brain.selfnarration_versions(),[])
            else:
                self.assertEqual(len(second['night']['executed']),1)
                tick(6)
                self.assertEqual(complete.call_count,2)
                self.assertEqual(self.brain.current_profile()['views'],'愿意多试一试')
                self.assertEqual(len(self.brain.selfnarration_versions()),1)
        for step in steps.values():self.assertEqual(step.call_count,1)
