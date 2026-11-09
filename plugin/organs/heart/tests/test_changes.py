import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from organs.heart import energy, mood, offset, model, store, sleep
from api import api


class ChangesTest(unittest.TestCase):
    def test_personality_baseline_change_has_its_own_readable_history(self):
        from organs.heart import changes
        changes.record(self.root, at=self.now, group='baseline', reason='重养采用中性起点',
                       before={'服气·心里':92,'服气·面上':18}, after={'服气·心里':50,'服气·面上':50})
        code,data=api.handle('GET','/heart/changes?date=2026-11-07&group=baseline',None,None)
        self.assertEqual(200,code)
        self.assertEqual(1,data['total'])
        self.assertEqual('性格基线',data['groups']['baseline'])
        self.assertEqual({'before':18,'after':50},data['rows'][0]['changes']['服气·面上'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = '2026-11-07T10:00:00+09:00'
        p = patch.dict(os.environ, {'HEART_ROOT': str(self.root)})
        p.start(); self.addCleanup(p.stop)

    def rows(self):
        return [json.loads(line) for line in (self.root / 'changes.jsonl').read_text().splitlines()]

    def test_proactive_feedback_history_records_actual_writes_not_time_or_duplicates(self):
        from organs.heart import proactive
        import warnings
        context={'who':'10000011','session_id':'qq:private:10000011','delivery_id':'delivery-test'}
        store.write_json(self.root/proactive.PROACTIVE_FILE,{'config':{'mute_after':2,'mute_hours':1}})
        proactive.sent(root=self.root,now=self.now,context=context)
        first=self.rows()[-1]
        self.assertEqual('proactive',first['group'])
        self.assertEqual({'before':False,'after':True},first['changes']['等待本次回应'])
        self.assertEqual({'before':None,'after':self.now},first['changes']['最近主动开口'])
        proactive.sent(root=self.root,now=self.now,context=context)
        self.assertEqual(1,len(self.rows()))
        proactive.outcome('ignored',root=self.root,now='2026-11-07T10:05:00+09:00')
        self.assertEqual({'before':0,'after':1},self.rows()[-1]['changes']['连续未获回应次数'])
        proactive.sent(root=self.root,now='2026-11-07T10:10:00+09:00',context=context)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            proactive.outcome('ignored',root=self.root,now='2026-11-07T10:15:00+09:00')
        self.assertEqual('2026-11-07T10:15:00+09:00',self.rows()[-1]['at'])
        self.assertEqual({'before':None,'after':'2026-11-07T11:15:00+09:00'},self.rows()[-1]['changes']['暂停主动开口至'])
        before={p.name:p.read_bytes() for p in self.root.iterdir()}
        self.assertFalse(proactive.is_muted(root=self.root,now='2026-11-07T11:16:00+09:00')['muted'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})
        proactive.the_other_side_spoke(root=self.root,now='2026-11-07T10:20:00+09:00')
        row=self.rows()[-1]
        self.assertEqual({'before':2,'after':0},row['changes']['连续未获回应次数'])
        self.assertEqual({'before':'2026-11-07T11:15:00+09:00','after':None},row['changes']['暂停主动开口至'])
        proactive.the_other_side_spoke(root=self.root,now='2026-11-07T10:21:00+09:00')
        proactive.outcome('replied',root=self.root,now='2026-11-07T10:22:00+09:00')
        self.assertEqual(5,len(self.rows()))
        proactive.sent(root=self.root,now='2026-11-07T10:25:00+09:00',context=context)
        proactive.receive_reply('10000012',context['session_id'],root=self.root,now='2026-11-07T10:26:00+09:00')
        self.assertEqual(6,len(self.rows()))
        proactive.receive_reply(context['who'],context['session_id'],root=self.root,now='2026-11-07T10:26:00+09:00')
        self.assertEqual({'before':True,'after':False},self.rows()[-1]['changes']['等待本次回应'])
        status,page=api.handle('GET','/heart/changes?date=2026-11-07&group=proactive',None,None)
        self.assertEqual(200,status);self.assertEqual(7,page['total'])
        self.assertEqual('主动交流反馈',page['groups']['proactive'])
        from api import activity
        status,history=activity.dispatch({'date':'2026-11-07','source':'heart'})
        self.assertEqual(200,status);self.assertEqual(7,history['total'])
        self.assertTrue(all(r['group_label']=='主动交流反馈' for r in history['rows']))

    def test_mood_event_experience_and_daily_baseline_have_actual_before_after(self):
        store.write_json(self.root / mood.MOOD_FILE, {'now':60,'baseline':60,'updated_at':self.now})
        result = mood.apply_event('praise', root=self.root, now=self.now, who='test')
        row = self.rows()[-1]
        self.assertEqual('mood', row['group'])
        self.assertEqual({'before':60,'after':result['after']}, row['changes']['心情'])
        self.assertEqual('praise', row['source'])
        mood.apply_experience('negative', source_id='phone:fixture', note='测试经历', root=self.root, now=self.now)
        self.assertEqual('phone:fixture', self.rows()[-1]['source'])
        count = len(self.rows())
        mood.apply_experience('negative', source_id='phone:fixture', note='重复', root=self.root, now=self.now)
        self.assertEqual(count, len(self.rows()))
        result = mood.settle_day(root=self.root, now=self.now)
        self.assertEqual(result['baseline_after'], self.rows()[-1]['changes']['心情基线']['after'])

    def test_energy_time_and_burst_are_distinct_and_duplicate_has_no_fake_change(self):
        store.write_json(self.root / energy.ENERGY_FILE, {'value':80,'updated_at':self.now,'tier':'一般'})
        later = '2026-11-07T11:00:00+09:00'
        energy.read(root=self.root, now=later)
        first = self.rows()[-1]
        self.assertEqual('时间结算', first['reason'])
        self.assertLess(first['changes']['精力']['after'], 80)
        energy.apply_burst(root=self.root, now=later, event_id='changes-test-burst')
        row = self.rows()[-1]
        self.assertEqual('changes-test-burst', row['source'])
        self.assertEqual(first['changes']['精力']['after'], row['changes']['精力']['before'])
        count = len(self.rows())
        energy.apply_burst(root=self.root, now=later, event_id='changes-test-burst')
        self.assertEqual(count, len(self.rows()))

    def test_daily_and_weekly_axis_values_are_recorded_without_guessing_initial_value(self):
        axis = model.AXES[0]
        offset.apply_daily(axis, .5, root=self.root, now=self.now, source='fixture')
        row = self.rows()[-1]
        self.assertIsNone(row['changes'][axis]['before'])
        self.assertEqual(.5, row['changes'][axis]['after'])
        offset.settle_week({axis:-1}, root=self.root, now=self.now)
        self.assertEqual({'before':.5,'after':-1}, self.rows()[-1]['changes'][axis])

    def test_changes_api_filters_and_is_readonly(self):
        mood.apply_experience('positive', source_id='phone:fixture', note='测试', root=self.root, now=self.now)
        path = self.root / 'changes.jsonl'
        path.write_text(path.read_text() + '\nnot-json\n')
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        status, page = api.handle('GET', '/heart/changes?date=2026-11-07&group=mood&limit=1', None, None)
        self.assertEqual(200,status)
        self.assertEqual(1,page['total'])
        self.assertEqual(1,page['invalid_records'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})
        self.assertEqual(405,api.handle('POST','/heart/changes',{},None)[0])
        for query in ('date=2026-02-30','group=secret','limit=0','offset=-1'):
            self.assertEqual(400,api.handle('GET','/heart/changes?'+query,None,None)[0])

    def test_short_gap_cost_records_actual_event_time(self):
        energy.read(root=self.root, now=self.now)
        at = '2026-11-07T10:00:01+09:00'
        energy.apply_long_reply(1000, root=self.root, now=at)
        self.assertEqual(at, self.rows()[-1]['at'])
        self.assertEqual('时间结算与长回复消耗', self.rows()[-1]['reason'])

    def test_sleep_events_are_visible_with_actual_changes_and_readonly_filter(self):
        onset = '2026-11-07T01:00:00+09:00'
        awake = '2026-11-07T05:00:00+09:00'
        sleep.record('asleep', root=self.root, now=onset)
        status, page = api.handle('GET', '/heart/changes?date=2026-11-07&group=sleep', None, None)
        self.assertEqual(200, status)
        self.assertEqual({'before':None, 'after':onset}, page['rows'][0]['changes']['上次入睡'])
        sleep.record('awake', root=self.root, now=awake)
        row = self.rows()[-1]
        self.assertEqual('醒来', row['reason'])
        self.assertEqual('awake', row['source'])
        self.assertEqual({'before':None, 'after':240}, row['changes']['上一觉时长（分钟）'])
        self.assertEqual({'before':0, 'after':1}, row['changes']['连续未睡够（夜）'])
        self.assertEqual({'before':0, 'after':1}, row['changes']['连续晚睡（夜）'])
        count = len(self.rows())
        sleep.record('awake', root=self.root, now=awake)
        self.assertEqual(count, len(self.rows()))
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        status, page = api.handle('GET', '/heart/changes?date=2026-11-07&group=sleep', None, None)
        self.assertEqual(200, status)
        self.assertEqual(2, page['total'])
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.root.iterdir()})
        sleep.record('asleep', root=self.root, now='2026-11-07T23:00:00+09:00')
        sleep.record('awake', root=self.root, now='2026-11-08T08:00:00+09:00')
        row = self.rows()[-1]
        self.assertEqual({'before':1, 'after':0}, row['changes']['连续未睡够（夜）'])
        self.assertEqual({'before':240, 'after':540}, row['changes']['上一觉时长（分钟）'])

    def test_empty_history_is_not_created_and_pagination_uses_local_day(self):
        from datetime import timezone, timedelta
        from organs.heart import changes
        self.assertEqual(0,changes.view({})['total'])
        self.assertEqual([],list(self.root.iterdir()))
        for index, at in enumerate(('2026-11-06T14:59:00Z','2026-11-06T15:01:00Z','2026-11-06T15:02:00Z')):
            changes.record(self.root, at=at, group='mood', reason='fixture', before={'心情':index},after={'心情':index+1})
        with patch('organs.day.model.local_tz', return_value=timezone(timedelta(hours=9))):
            page = changes.view({'date':'2026-11-07','limit':'1','offset':'1'})
        self.assertEqual(2,page['total'])
        self.assertEqual(2,page['rows'][0]['source_line'])
        self.assertFalse(page['has_older'])
