import json
from datetime import timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api import api


class ActivityApiTest(unittest.TestCase):
    def test_preferences_timeline_links_real_evidence_and_cross_day_replacement(self):
        from organs.brain import BrainStore, preferences
        from urllib.parse import parse_qs, urlsplit, quote
        brain=BrainStore(self.root/'brain')
        who='10000011';session='qq:private:'+who;turn='a'*32
        first_at='2026-11-06T12:00:00+09:00';second_at='2026-11-07T12:00:00+09:00'
        old_entry=brain.ingest(who,'以后别总给建议',ts=first_at,session_id=session,scene='private')['entry']
        old=preferences.record(brain.root,who=who,session_id=session,at=first_at,turn_id=turn,
            entry_ids=[old_entry['id']],change={'text':'少主动给建议','evidence':['以后别总给建议'],'replaces':[]})
        entry=brain.ingest(who,'改成我问怎么办时再建议',ts=second_at,session_id=session,scene='private')['entry']
        changed=preferences.record(brain.root,who=who,session_id=session,at=second_at,turn_id='b'*32,
            entry_ids=[entry['id']],change={'text':'明确问怎么办时给建议','evidence':['改成我问怎么办时再建议'],'replaces':[old['id']]})
        preferences.record(brain.root,who=who,session_id=session,at='2026-11-07T13:00:00+09:00',actor='panel',
            change={'text':'','evidence':[],'replaces':[changed['id']]})
        before={p:p.read_bytes() for p in brain.root.rglob('*') if p.is_file()}
        code,data=self.get('date=2026-11-07&source=preference&limit=1&offset=1')
        self.assertEqual(200,code);self.assertEqual(2,data['total']);row=data['rows'][0]
        self.assertEqual('updated',row['status'])
        self.assertEqual({'before':['少主动给建议'],'after':['明确问怎么办时给建议']},row['changes']['interaction_preference'])
        self.assertEqual(['改成我问怎么办时再建议'],row['evidence'])
        self.assertEqual('b'*32,row['model_turn_id'])
        self.assertEqual(who,parse_qs(urlsplit(row['link']).query)['who'][0])
        memory=parse_qs(urlsplit(row['memory_links'][0]['link']).query)['memory'][0]
        self.assertEqual('event:'+entry['id'],memory)
        self.assertEqual(200,api.handle('GET','/api/memory/'+quote(memory,safe='')+'/why',None,None,brain=brain)[0])
        code,data=self.get('date=2026-11-07&source=preference&limit=1')
        row=data['rows'][0];self.assertEqual('withdrawn',row['status']);self.assertNotIn('model_turn_id',row)
        self.assertEqual([],row['changes']['interaction_preference']['after'])
        self.assertEqual('panel',row['actor'])
        code,data=self.get('date=2026-11-06&source=preference')
        self.assertEqual('learned',data['rows'][0]['status'])
        self.assertEqual(before,{p:p.read_bytes() for p in brain.root.rglob('*') if p.is_file()})

    def test_missing_replaced_preference_is_reported_without_guessing(self):
        from organs.brain import BrainStore, preferences
        brain=BrainStore(self.root/'brain')
        preferences.record(brain.root,who='10000011',session_id='qq:private:10000011',at='2026-11-07T12:00:00+09:00',
            change={'text':'','evidence':[],'replaces':['missing']},actor='panel')
        code,data=self.get('date=2026-11-07&source=preference')
        self.assertEqual(200,code)
        self.assertEqual(['missing'],data['rows'][0]['missing_previous'])
        self.assertEqual([],data['rows'][0]['changes']['interaction_preference']['before'])

    def test_real_people_and_memory_edits_keep_complete_values_and_uncertainty(self):
        from organs.brain import BrainStore
        brain=BrainStore(self.root/'brain')
        at='2026-11-07T12:00:00+09:00'
        entry=brain.ingest('10000011','我今天把表填完了',ts=at,note='旧理解')['entry']
        note='长备注'+('甲乙丙丁'*30)+'结尾不能丢'
        with patch('organs.brain.store.now_iso',return_value=at):
            result=brain.patch_card('10000011','panel',{'备注':note,'雷点':'第一行\n第二行'})
            edited=brain.edit_note(entry['id'],'panel','新理解',unclear=True)
        self.assertTrue(result['ok']);self.assertTrue(edited['ok'])
        self.assertEqual([note],result['history_lines'][1]['changes']['备注']['after'])
        before={p:p.read_bytes() for p in (self.root/'brain').rglob('*') if p.is_file()}
        code,data=self.get('date=2026-11-07&source=people&limit=1')
        self.assertEqual(200,code);self.assertEqual(2,data['total']);self.assertTrue(data['has_older'])
        self.assertEqual([note],data['rows'][0]['changes']['备注']['after'])
        code,second=self.get('date=2026-11-07&source=people&limit=1&offset=1')
        self.assertNotEqual(data['rows'][0]['id'],second['rows'][0]['id'])
        self.assertEqual(['第一行','第二行'],second['rows'][0]['changes']['雷点']['after'])
        code,data=self.get('date=2026-11-07&source=memory_edit')
        self.assertEqual(200,code)
        row=data['rows'][0]
        self.assertEqual({'before':'旧理解','after':'新理解'},row['changes']['note'])
        self.assertEqual({'before':False,'after':True},row['changes']['unclear'])
        from urllib.parse import parse_qs, urlsplit, quote
        target=parse_qs(urlsplit(row['link']).query)['memory'][0]
        missing,_=api.handle('GET','/api/memory/'+entry['id']+'/why',None,None,brain=brain)
        self.assertEqual(404,missing)
        code,detail=api.handle('GET','/api/memory/'+quote(target,safe='')+'/why',None,None,brain=brain)
        self.assertEqual(200,code)
        self.assertEqual('event:'+entry['id'],detail['node']['id'])
        self.assertNotIn('我今天把表填完了',json.dumps(row,ensure_ascii=False))
        self.assertEqual(before,{p:p.read_bytes() for p in (self.root/'brain').rglob('*') if p.is_file()})

    def test_old_memory_and_person_history_stays_explicitly_partial(self):
        self.write('brain/people_card_history.jsonl',[{'ts':'2026-11-06T16:00:00Z','actor':'旧面板',
            'person_id':'10000011','field':'备注','old_summary':'旧摘要…','new_summary':'新摘要…'}],True)
        self.write('brain/note_history.jsonl',[{'ts':'2026-11-07T12:00:00+09:00','actor':'panel',
            'entry_id':'t-old','old_note':'旧理解','new_note':''}],True)
        code,data=self.get('date=2026-11-07&source=people')
        self.assertEqual(200,code)
        row=data['rows'][0]
        self.assertTrue(row['summary_only']);self.assertEqual('旧摘要…',row['before_summary'])
        self.assertIsNone(row['changes'])
        code,data=self.get('date=2026-11-07&source=memory_edit')
        self.assertEqual(200,code)
        self.assertEqual({'before':'旧理解','after':''},data['rows'][0]['changes']['note'])
        self.assertNotIn('unclear',data['rows'][0]['changes'])

    def test_longterm_note_history_preserves_original_and_flag_only_edit(self):
        from organs.brain import BrainStore
        brain=BrainStore(self.root/'brain')
        entry=brain.ingest('10000011','纸船停在水边',ts='2026-11-06T12:00:00+09:00',note='同一理解',unclear=True)['entry']
        brain.today_entries('2026-11-07T12:00:00+09:00')
        with patch('organs.brain.store.now_iso',return_value='2026-11-07T12:01:00+09:00'):
            result=brain.edit_note(entry['id'],'panel','同一理解',unclear=False)
        self.assertEqual('longterm',result['where'])
        self.assertEqual('纸船停在水边',result['quote'])
        code,data=self.get('date=2026-11-07&source=memory_edit')
        self.assertEqual(200,code)
        self.assertEqual({'before':True,'after':False},data['rows'][0]['changes']['unclear'])
        self.assertEqual({'before':'同一理解','after':'同一理解'},data['rows'][0]['changes']['note'])

    def test_settled_failure_detail_is_visible_and_old_jobs_are_not_backfilled(self):
        from organs.day import autonomy, phone_browse, solitude
        at='2026-11-07T12:00:00+09:00'
        root=self.root/'day'
        for ident,extra in [('new',{'error_stage':'画面理解','error_kind':'timeout','error_detail':'画面理解：请求超时'}),('old',{})]:
            phone_browse.save({'id':ident,'created_at':at,'status':'failed','origin':'autonomy',
                'note':'旧失败原因','observations':[],**extra},root)
            solitude.append_log(root,{'at':at,'event':'autonomy_result','model_turn_id':ident,
                'action':'browse','status':'queued','share':False,'receipt':{'id':ident}})
        finished=autonomy.settle(at,root=root,home_root=Path(self.house.name))
        old=next(r for r in finished if r['model_turn_id']=='old')
        self.assertNotIn('error_detail',old)
        before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
        for source in ('autonomy','phone'):
            code,data=self.get('date=2026-11-07&source='+source)
            self.assertEqual(200,code)
            self.assertTrue(any(r['detail']=='画面理解：请求超时' for r in data['rows']))
        self.assertEqual(before,{p:p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_soul_edit_writer_is_visible_in_timeline_without_mutating_state(self):
        from organs.soul import load
        from organs.soul.tests.helpers import copy_fixture
        soul=load(copy_fixture('valid',str(self.root)))
        result=soul.edit(actor='人',reason='隔离验证',block='manner',path='stance',
                         value='慢慢聊',now='2026-11-07T12:00:00+09:00')
        self.assertTrue(result['ok'],result)
        before={p:p.read_bytes() for p in (self.root/'soul').rglob('*') if p.is_file()}
        code,data=self.get('date=2026-11-07&source=soul')
        self.assertEqual(200,code)
        row=next(r for r in data['rows'] if r['detail']=='隔离验证')
        self.assertEqual(result['history_line']['old_summary'],row['before_summary'])
        self.assertEqual('慢慢聊',row['after_summary'])
        self.assertEqual(result['history_line']['revision'],row['revision'])
        self.assertEqual(before,{p:p.read_bytes() for p in (self.root/'soul').rglob('*') if p.is_file()})

    def test_soul_history_keeps_original_summaries_versions_and_probe_filter(self):
        path=self.write('soul/history.jsonl',[
            {'ts':'2026-11-06T15:01:00Z','kind':'edit','actor':'人','block':'manner','path':'stance',
             'old_summary':'硬一点','new_summary':'慢慢形成','reason':'调整起点','revision':2,'old_digest':'不需要的摘要哈希'},
            {'ts':'2026-11-07T10:00:00+09:00','kind':'probe','actor':'验收','old_summary':'前','new_summary':'后'},
            {'ts':'2026-11-07T11:00:00+09:00','kind':'rollback','actor':'人','target_revision':1,'revision':4,
             'old_summary':'回滚前','new_summary':'回到第1版','reason':'恢复'},
            {'ts':'2026-11-07T12:00:00+09:00','reason':'旧记录'}],True)
        before=path.read_bytes()
        code,data=self.get('date=2026-11-07&source=soul&limit=2')
        self.assertEqual(200,code)
        self.assertEqual(3,data['total'])
        self.assertEqual('recorded',data['rows'][0]['status'])
        self.assertIsNone(data['rows'][0]['before_summary'])
        self.assertEqual(1,data['rows'][1]['target_revision'])
        self.assertEqual(4,data['rows'][1]['revision'])
        _,older=self.get('date=2026-11-07&source=soul&offset=2')
        row=older['rows'][0]
        self.assertEqual('硬一点',row['before_summary'])
        self.assertEqual('慢慢形成',row['after_summary'])
        self.assertEqual('manner.stance',row['field'])
        self.assertEqual('人',row['actor'])
        self.assertNotIn('digest',json.dumps(data)+json.dumps(older))
        _,tests=self.get('date=2026-11-07&source=soul&include_tests=1')
        self.assertEqual(4,tests['total'])
        self.assertEqual(1,sum(r['is_test'] for r in tests['rows']))
        self.assertEqual([],self.get('date=2026-11-08&source=soul')[1]['rows'])
        self.assertEqual(before,path.read_bytes())

    def test_meme_history_uses_saved_changes_without_exposing_paths_or_inventing_events(self):
        path = Path(self.house.name) / '仓库/表情包/index.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'items': [], 'chat_enabled': True}))
        code, empty = self.get('source=meme&date=2026-11-07')
        self.assertEqual(code, 200)
        self.assertEqual(empty['rows'], [])
        path.write_text(json.dumps({'items': [], 'changes': [
            {'at': '2026-11-06T15:01:00Z', 'kind': 'import', 'source': '/private/original.json', 'imported': 276},
            {'at': '2026-11-07T12:00:00+09:00', 'kind': 'settings',
             'before': {'chat_enabled': False}, 'after': {'chat_enabled': True}},
            {'at': '2026-11-07T13:00:00+09:00', 'kind': 'edit',
             'before': {'id': 'abc', 'name': '小鲸鱼', 'description': '旧描述', 'enabled': True, 'file': '/private/image'},
             'after': {'id': 'abc', 'name': '小鲸鱼', 'description': '新描述', 'enabled': False, 'file': '/private/image'}}]}))
        before = path.read_bytes()
        code, page = self.get('source=meme&date=2026-11-07&limit=2')
        self.assertEqual(code, 200)
        self.assertEqual(page['total'], 3)
        self.assertTrue(page['has_older'])
        edit, toggle = page['rows']
        self.assertEqual(edit['changes'], {'description': {'before': '旧描述', 'after': '新描述'},
                                           'enabled': {'before': True, 'after': False}})
        self.assertEqual(toggle['changes'], {'chat_enabled': {'before': False, 'after': True}})
        _, older = self.get('source=meme&date=2026-11-07&offset=2')
        self.assertEqual(older['rows'][0]['imported'], 276)
        self.assertEqual(len({r['id'] for r in page['rows'] + older['rows']}), 3)
        self.assertNotIn('/private/', json.dumps(page) + json.dumps(older))
        self.assertEqual(self.get('source=meme&date=2026-11-08')[1]['rows'], [])
        self.assertEqual(path.read_bytes(), before)

    def test_nonverbal_sound_and_classifier_failure_remain_visible_without_writing(self):
        sound={'status':'ok','source':'local_yamnet','processed_seconds':8,
               'events':[{'label':'音乐','score':.73}]}
        path=self.write('ear/observations.jsonl',[
            {'at':'2026-11-07T09:00:00+09:00','transcription':{'status':'empty','sound_events':sound}},
            {'at':'2026-11-07T10:00:00+09:00','transcription':{'status':'ok','transcript':'下午见',
             'sound_events':{'status':'failed','source':'local_yamnet','events':[], 'processed_seconds':2}}}],True)
        before=path.read_bytes()
        status,page=self.get('date=2026-11-07&source=ear')
        self.assertEqual(status,200)
        self.assertEqual(page['rows'][0]['sound_events']['status'],'failed')
        self.assertEqual(page['rows'][0]['transcript'],'下午见')
        self.assertEqual(page['rows'][1]['sound_events'],sound)
        self.assertEqual(page['rows'][1]['transcript'],'')
        self.assertEqual(path.read_bytes(),before)

    def test_proactive_setting_history_keeps_actual_changes_and_old_rows_unknown(self):
        changes={'enabled':{'before':False,'after':True},'routes':{'before':[],
            'after':[{'who':'123456','scene':'private','target':123456}]}}
        path=self.write('day/proactive_log.jsonl',[
            {'at':'2026-11-07T09:00:00+09:00','stage':'config','action':'enabled','changes':changes},
            {'at':'2026-11-07T08:00:00+09:00','stage':'config','action':'disabled'}],True)
        before=path.read_bytes()
        status,page=self.get('date=2026-11-07&source=proactive')
        self.assertEqual(status,200)
        self.assertEqual(page['rows'][0]['changes'],changes)
        self.assertEqual(page['rows'][1].get('changes',{}),{})
        self.assertEqual(path.read_bytes(),before)

    def test_failed_autonomy_shows_specific_error_without_rewriting_history(self):
        path=self.write('day/proactive_idle_log.jsonl',[{'at':'2026-11-08T12:00:00+09:00',
            'event':'autonomy_result','action':'write','status':'failed','error':'ValueError',
            'error_kind':'invalid_output','error_detail':'完成需要指定自己的打算'}],lines=True)
        before=path.read_bytes()
        status,page=self.get('date=2026-11-08&source=autonomy')
        self.assertEqual(status,200)
        self.assertEqual(page['rows'][0]['detail'],'完成需要指定自己的打算')
        self.assertEqual(page['rows'][0]['title'],'写自己的作品')
        self.assertEqual(path.read_bytes(),before)

    def test_growth_limits_are_shown_from_recorded_evidence(self):
        change = {'interval_minutes':{'before':120,'after':30}, 'daily_cap':{'before':2,'after':24}}
        path = self.write('day/growth.json', {'enabled':True,'changes':[
            {'at':'2026-11-07T11:00:00+09:00','before':False,'after':True},
            {'at':'2026-11-07T12:00:00+09:00','before':True,'after':True,'changes':change}]})
        before = path.read_bytes()
        status, page = self.get('date=2026-11-07&source=growth_settings')
        self.assertEqual(status, 200)
        self.assertEqual(page['rows'][0]['changes'], change)
        self.assertEqual(page['rows'][0]['title'], '修改自主活动设置')
        self.assertEqual(page['rows'][1]['changes'], {'enabled':{'before':False,'after':True}})
        self.assertEqual(path.read_bytes(), before)

    def test_autonomous_painting_title_uses_recorded_mode(self):
        self.write('day/proactive_idle_log.jsonl', [
            {'at':'2026-11-07T13:00:00+09:00','event':'autonomy_result','status':'queued','action':'paint','mode':'image'},
            {'at':'2026-11-07T13:01:00+09:00','event':'autonomy_finished','status':'done','action':'paint','mode':'image'},
            {'at':'2026-11-07T13:02:00+09:00','event':'autonomy_result','status':'queued','action':'paint'}], lines=True)
        status, page = self.get('date=2026-11-07&source=autonomy')
        self.assertEqual(status, 200)
        self.assertEqual([r['title'] for r in page['rows']], ['逐笔绘画','本地模型绘画','本地模型绘画'])

    def test_autonomy_result_uses_recorded_time_across_midnight_without_rewriting_old_rows(self):
        start = '2026-11-07T23:59:57+09:00'
        finish = '2026-11-08T00:00:09.600000+09:00'
        self.write('mind/trace/2026-11-08.jsonl', [
            {'at':'2026-11-08T00:00:09.500000+09:00','status':200,'reply':'已选好',
             'purpose':'选择自己的活动','turn_id':'a'*32}], lines=True)
        path = self.write('day/proactive_idle_log.jsonl', [
            {'at':start,'event':'autonomy_attempt','model_turn_id':'a'*32},
            {'at':start,'recorded_at':finish,'event':'autonomy_result','model_turn_id':'a'*32,
             'status':'done','action':'write'},
            {'at':'2026-11-07T12:00:00+09:00','event':'autonomy_result','model_turn_id':'b'*32,
             'status':'failed'}], lines=True)
        before = path.read_bytes()
        _, today = self.get('date=2026-11-08&source=autonomy')
        self.assertEqual(today['total'], 1)
        self.assertEqual(today['rows'][0]['at'], finish)
        self.assertEqual(today['rows'][0]['started_at'], start)
        self.assertEqual(today['rows'][0]['recorded_at'], finish)
        _, yesterday = self.get('date=2026-11-07&source=autonomy')
        self.assertEqual(yesterday['total'], 2)
        old = next(r for r in yesterday['rows'] if r['event']=='autonomy_result')
        self.assertIsNone(old.get('recorded_at'))
        self.assertEqual(old['started_at'], '2026-11-07T12:00:00+09:00')
        _, combined = self.get('date=2026-11-08')
        self.assertEqual([r['source'] for r in combined['rows'][:2]], ['autonomy', 'model'])
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_optional_reaction_keeps_work_receipt_and_explanation(self):
        error = '活动感想没有本轮可核对的实际作品或结果依据'
        path = self.write('day/proactive_idle_log.jsonl', [{
            'at':'2026-11-08T12:00:00+09:00','event':'autonomy_result','model_turn_id':'c'*32,
            'status':'done','action':'write','reaction_error':error,
            'receipt':{'card_id':'新版','text':'纸上的折痕还在。'}}], lines=True)
        before = path.read_bytes()
        status, data = self.get('date=2026-11-08&source=autonomy')
        self.assertEqual(200, status)
        row = data['rows'][0]
        self.assertEqual('done', row['status'])
        self.assertEqual('新版', row['receipt']['card_id'])
        self.assertEqual(error, row.get('reaction_error'))
        self.assertIsNone(row.get('reaction'))
        self.assertEqual(before, path.read_bytes())

    def test_proactive_ack_exposes_confirmed_body_and_exact_model_turn(self):
        self.write('day/proactive_log.jsonl', [{'at':'2026-11-07T12:00:00+09:00',
            'stage':'ack','action':'partial','turn_id':'a'*32,'plan_id':'p-one',
            'confirmed_text':'真正送出的第一句','receipt':{'sent':1,'stopped':'send_failed'}}],lines=True)
        status,data=self.get('date=2026-11-07&source=proactive')
        self.assertEqual(200,status)
        row=data['rows'][0]
        self.assertEqual('a'*32,row.get('turn_id'))
        self.assertEqual('真正送出的第一句',row.get('confirmed_text'))
        self.assertEqual('partial',row['status'])

    def test_generation_discard_is_distinct_from_before_generation_decline(self):
        path = self.write('day/proactive_log.jsonl', [
            {'at': '2026-11-07T12:00:00+09:00', 'stage': 'due', 'action': 'declined',
             'reason_code': 'thought', 'reason': 'source-resolved', 'plan_id': 'old'},
            {'at': '2026-11-07T12:01:00+09:00', 'stage': 'due', 'action': 'declined',
             'reason_code': 'thought', 'reason': 'source-resolved', 'plan_id': 'new', 'after_generation': True}], lines=True)
        before = path.read_bytes()
        status, data = self.get('date=2026-11-07&source=proactive')
        self.assertEqual(200, status)
        rows = {row['plan_id']: row for row in data['rows']}
        self.assertTrue(rows['new'].get('after_generation'))
        self.assertFalse(rows['old'].get('after_generation'))
        self.assertEqual('declined', rows['new']['status'])
        self.assertEqual(before, path.read_bytes())

    def test_life_settings_and_edits_use_change_date_without_claiming_experience(self):
        from organs.day import life
        at='2026-11-07T12:30:00+09:00'
        life.configure(True,now=at)
        life.configure(True,now=at)
        self.write('day/life-days/2026-11-08.json',{'status':'ready','revision':1,'changes':[],
            'items':[{'start':'2026-11-08T08:00:00+09:00','end':'2026-11-08T12:00:00+09:00',
                      'activity':'读书','location':'家','detail':'慢慢读','outfit':'蓝色外套','source':'model'}]})
        life.adjust({'day':'2026-11-08','revision':1,'start':'2026-11-08T08:00:00+09:00',
            'activity':'看画册','location':'家','detail':'慢慢翻','outfit':'白色外套'},now=at)
        before={p:p.read_bytes() for p in (self.root/'day').rglob('*.json')}
        code,page=self.get('source=life&date=2026-11-07&limit=1')
        self.assertEqual(200,code)
        self.assertEqual(2,page['total'])
        _,second=self.get('source=life&date=2026-11-07&offset=1')
        rows=page['rows']+second['rows'];self.assertEqual(2,len({r['id'] for r in rows}))
        setting=next(r for r in rows if r['status']=='setting')
        edited=next(r for r in rows if r['status']=='edited')
        self.assertEqual({'before':False,'after':True},setting['changes']['enabled'])
        self.assertEqual({'before':'蓝色外套','after':'白色外套'},edited['changes']['outfit'])
        self.assertEqual('2026-11-08',edited['day'])
        self.assertEqual(2,edited['revision'])
        self.assertIn('不代表实际经历',edited['detail'])
        self.assertEqual([],self.get('source=life&date=2026-11-08')[1]['rows'])
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_asset_writer_history_preserves_changes_without_paths_or_current_backfill(self):
        from organs.home import art
        from api.tests.test_studio_api import picture
        initial=art.save_asset({'name':'测试衣服','category':'outfits','description':'蓝色',
                                'images':[picture()]})
        changed=art.save_asset({'id':initial['id'],'revision':initial['revision'],
            'name':'测试衣服','category':'outfits','description':'白色','images':[]})
        day=changed['updated_at'][:10]
        path=art.library_root()/'assets.json';before=path.read_bytes()
        code,page=self.get('source=art_assets&date='+day)
        self.assertEqual(200,code)
        self.assertEqual(2,page['total'])
        edit=next(r for r in page['rows'] if r['status']=='edited')
        self.assertEqual({'before':'蓝色','after':'白色'},edit['changes']['description'])
        self.assertEqual(initial['images'][0]['sha256'],edit['changes']['images']['before'][0]['sha256'])
        self.assertEqual([],edit['changes']['images']['after'])
        self.assertNotIn('thumbnail',json.dumps(page))
        self.assertNotIn(initial['images'][0]['file'],json.dumps(page))
        self.assertEqual(before,path.read_bytes())
        library=json.loads(before);library.pop('changes');path.write_text(json.dumps(library))
        self.assertEqual([],self.get('source=art_assets&date='+day)[1]['rows'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.house = tempfile.TemporaryDirectory()
        self.addCleanup(self.house.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict('os.environ', {'QQAGENT_HOME': str(self.root), 'QQAGENT_HOME_ROOT': str(self.root),
                                 'DAY_ROOT': str(self.root / 'day'), 'HEART_ROOT': str(self.root / 'heart'),
                                 'HOME_ROOT': self.house.name}).start()
        patch('organs.paths.REPO_ROOT', self.root).start()
        patch('organs.day.model.local_tz', return_value=timezone(timedelta(hours=9))).start()
        patch('organs.day.phone_browse._ACTIVE', None).start()
        patch('organs.home.art.painting_jobs', return_value=[]).start()
        patch('organs.mind.llm.complete', side_effect=AssertionError('只读页面不调用模型')).start()
        patch('organs.hand.phone.act', side_effect=AssertionError('只读页面不操作手机')).start()

    def write(self, relative, data, lines=False):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(x) for x in data) if lines else json.dumps(data))
        return path

    def get(self, query='date=2026-11-07'):
        return api.handle('GET', '/observe/activity?' + query, None, None)

    def test_decisions_show_merged_segment_reason_without_claiming_delivery(self):
        batch = {'id':'b144','msgs':3,'waited_ms':2000,'tier':'quietOpen'}
        path = self.write('channels/onebot/.state/decisions.jsonl', [
            {'ts':'2026-11-07T23:09:10Z','kind':'decision','human':'没回：闲聊，没抽到',
             'machine':{'decided':'silent','reason_code':'chitchat_skipped','why':'没有抽中',
                        'target':'group:10000071','batch':batch}},
            {'ts':'2026-11-07T23:09:11Z','kind':'decision','human':'判断记录：等待回执',
             'machine':{'decided':'pending','target':'group:10000071','batch':batch}},
            {'ts':'2026-11-07T23:09:12Z','kind':'decision','human':'验收','origin':'acceptance',
             'machine':{'decided':'silent'}},
            {'ts':'2026-11-07T23:09:13Z','kind':'decision','human':'损坏','machine':[]}
        ], lines=True)
        before = path.read_bytes()
        status, data = self.get('date=2026-11-08&source=decision')
        self.assertEqual(200, status)
        self.assertEqual(2, data['total'])
        self.assertEqual(1, data['hidden_tests'])
        self.assertEqual(1, data['invalid_records'])
        self.assertEqual(2, len({r['id'] for r in data['rows']}))
        pending, silent = data['rows']
        self.assertEqual('recorded', pending['status'])
        self.assertNotIn('delivery_status', pending)
        self.assertEqual('没回：闲聊，没抽到', silent['title'])
        self.assertEqual('chitchat_skipped', silent['reason_code'])
        self.assertEqual('group:10000071', silent['target'])
        self.assertEqual(batch, silent['batch'])
        self.assertEqual('2026-11-08T08:09:10+09:00', silent['at'])
        self.assertEqual('observe?source=decision&date=2026-11-08', silent['link'])
        self.assertEqual(0, self.get('date=2026-11-07&source=decision')[1]['total'])
        self.assertEqual(3, self.get('date=2026-11-08&source=decision&include_tests=1')[1]['total'])
        self.assertEqual(before, path.read_bytes())

    def test_phone_settings_history_is_read_only_and_old_state_is_not_an_event(self):
        path=self.write('day/phone-browse.json',{'enabled':True})
        before=path.read_bytes()
        code,page=self.get('date=2026-11-07&source=phone_settings')
        self.assertEqual(code,200);self.assertEqual(page['rows'],[])
        self.assertEqual(before,path.read_bytes())
        path=self.write('day/phone-browse.json',{'enabled':False,'changes':[
            {'at':'2026-11-06T15:01:00Z','before':False,'after':True},
            {'at':'2026-11-07T12:00:00+09:00','before':True,'after':False}]})
        before=path.read_bytes()
        code,page=self.get('date=2026-11-07&source=phone_settings&limit=1')
        self.assertEqual(code,200);self.assertEqual(page['total'],2);self.assertTrue(page['has_older'])
        row=page['rows'][0]
        self.assertEqual(row['status'],'setting')
        self.assertEqual(row['changes'],{'enabled':{'before':True,'after':False}})
        self.assertEqual(row['link'],'phone')
        self.assertNotIn('model_turn_id',row)
        self.assertEqual(self.get('date=2026-11-08&source=phone_settings')[1]['rows'],[])
        self.assertEqual(before,path.read_bytes())

    def test_chat_details_link_exact_turn_or_selected_day_without_guessing_legacy(self):
        self.write('mind/recent_turns.jsonl',[
            {'ts':'2026-11-07T10:00:00+09:00','said':'旧记录','turn_id':'not-a-turn'},
            {'ts':'2026-11-07T10:01:00+09:00','said':'有编号','turn_id':'a'*32}],True)
        rows=self.get('date=2026-11-07&source=chat')[1]['rows']
        self.assertEqual(rows[0]['link'],'observe?source=chat&turn_id='+'a'*32)
        self.assertEqual(rows[1]['link'],'observe?source=chat&date=2026-11-07')
        self.write('channels/onebot/.state/ledger.jsonl',[
            {'ts':'2026-11-07T10:00:00+09:00','kind':'sent'}],True)
        self.assertEqual(self.get('date=2026-11-07&source=delivery')[1]['rows'][0]['link'],
                         'observe?source=delivery&date=2026-11-07')

    def test_chat_emotion_origin_does_not_break_state_timeline_or_hide_later_rows(self):
        from organs.heart import changes, report
        origin = {'who': '群友甲', 'scene': 'group', 'turn_id': 'a' * 32,
                  'evidence': '<img src=x onerror=alert(1)>', 'note': '对bot的赞赏'}
        report.report_event('praise', root=self.root/'heart', who='群友甲',
                            now='2026-11-07T12:00:00+09:00', origin=origin)
        changes.record(self.root/'heart', at='2026-11-07T12:01:00+09:00', group='energy',
                       reason='时间结算', before={'精力':60}, after={'精力':59})
        path = self.root/'heart/changes.jsonl'
        before = path.read_bytes()
        for source in ('heart', 'all'):
            status, page = self.get('date=2026-11-07&source='+source)
            self.assertEqual(200, status)
            self.assertEqual({}, page['errors'])
            self.assertEqual(2, page['total'])
            self.assertEqual(0, page['hidden_tests'])
            row = next(r for r in page['rows'] if r['group']=='mood')
            self.assertEqual(origin, row['emotion_origin'])
            self.assertEqual('unknown', row['origin'])
            self.assertFalse(row['is_test'])
            self.assertNotIn('emotion_origin', next(r for r in page['rows'] if r['group']=='energy'))
        self.assertEqual(before, path.read_bytes())

    def test_phone_observation_failure_is_visible_without_guessing_from_summary(self):
        self.write('day/phone-sessions/failed.json',{'id':'failed','status':'partial',
                   'created_at':'2026-11-07T10:00:00+09:00','events':[
                       {'at':'2026-11-07T10:01:00+09:00','kind':'observation',
                        'status':'failed','scope':'三帧画面；未观看完整视频'}]})
        status,data=self.get('date=2026-11-07&source=phone_event')
        self.assertEqual(200,status)
        self.assertEqual('failed',data['rows'][0].get('event_status'))

    def test_painting_steps_use_event_time_and_do_not_invent_old_history(self):
        job = {'id': 'paint', 'created_at': '2026-11-06T23:59:00+09:00', 'status': 'done',
               'request': {'prompt': '小鸟'}, 'events': [
                   {'at': '2026-11-06T23:59:01+09:00', 'kind': 'planning', 'status': 'running'},
                   {'at': '2026-11-06T15:00:01Z', 'kind': 'review', 'status': 'ok', 'action': 'revise', 'reason': '<img src=x>改构图'},
                   {'at': '2026-11-07T00:00:01+09:00', 'kind': 'delivery', 'status': 'unconfirmed', 'delivery_kind': 'image', 'receipt': 'PRIVATE_RECEIPT'},
                   {'at': '2026-11-07T00:00:02+09:00', 'kind': 'new_phase', 'status': 'unknown'},
                   {'kind': 'draft', 'status': 'ok'}], 'image_path': 'PRIVATE_PATH'}
        old = dict(job, id='old')
        old.pop('events')
        test = dict(job, id='test', origin='acceptance', events=[job['events'][2]])
        before = json.dumps([job, old, test], sort_keys=True)
        with patch('organs.home.art.painting_jobs', return_value=[job, old, test]):
            status, data = self.get('date=2026-11-07&source=painting_event')
            self.assertEqual(200, status)
            self.assertEqual((3,1,1), (data['total'], data['hidden_tests'], data['invalid_records']))
            self.assertEqual(['painting_event:paint:3','painting_event:paint:2','painting_event:paint:1'], [r['id'] for r in data['rows']])
            self.assertEqual('unconfirmed', data['rows'][1]['status'])
            self.assertEqual('image', data['rows'][1]['delivery_kind'])
            self.assertEqual('revise', data['rows'][2]['action'])
            self.assertEqual('<img src=x>改构图', data['rows'][2]['detail'])
            self.assertEqual('new_phase', data['rows'][0]['title'])
            self.assertNotIn('PRIVATE_', json.dumps(data))
            self.assertEqual(1, self.get('date=2026-11-06&source=painting_event')[1]['total'])
            self.assertEqual(4, self.get('date=2026-11-07&source=painting_event&include_tests=1')[1]['total'])
            self.assertEqual(data['rows'][1:], self.get('date=2026-11-07&source=painting_event&offset=1&limit=2')[1]['rows'])
        self.assertEqual(before, json.dumps([job, old, test], sort_keys=True))

    def test_phone_summary_and_steps_share_only_recorded_model_round(self):
        for ident, extra in [('new', {'model_turn_id':'a'*32}), ('old', {})]:
            self.write('day/phone-sessions/'+ident+'.json', {
                'id':ident,'created_at':'2026-11-07T10:00:00+09:00','status':'done','origin':'automatic',
                'events':[{'at':'2026-11-07T10:01:00+09:00','kind':'reflection'}], **extra})
        for source in ['phone','phone_event']:
            status,data=self.get('date=2026-11-07&source='+source)
            self.assertEqual(status,200)
            new=next(r for r in data['rows'] if r['id'].startswith(source+':new'))
            old=next(r for r in data['rows'] if r['id'].startswith(source+':old'))
            self.assertEqual(new['model_turn_id'],'a'*32)
            self.assertNotIn('model_turn_id',old)

    def test_phone_steps_use_own_times_and_original_order_without_rewriting_jobs(self):
        job = {'id':'a'*32, 'created_at':'2026-11-06T23:59:00+09:00',
               'status':'done', 'origin':'automatic', 'title':'当前视频',
               'events':[
                   {'at':'2026-11-06T23:59:01+09:00','kind':'intention','reason':'想看一会'},
                   {'at':'2026-11-06T15:00:01Z','kind':'selection','reason':'这个标题吸引我',
                    'node':2,'options':[{'node':2,'title':'<img src=x>小猫','bounds':[1,2,3,4]}]},
                   {'at':'2026-11-07T00:00:01+09:00','kind':'action','action':'pause',
                    'receipt':{'private':'PRIVATE_RECEIPT'}},
                   {'at':'2026-11-07T00:00:02+09:00','kind':'reflection','scope':'本次没有形成可用感想'},
                   {'at':'2026-11-07T00:00:03+09:00','kind':'future_event','scope':'原有范围'},
                   {'kind':'frame','index':2}], 'frame_file':'PRIVATE_PATH'}
        path=self.write('day/phone-sessions/'+job['id']+'.json',job)
        old=self.write('day/phone-sessions/old.json',{'id':'old','status':'done',
                       'created_at':'2026-11-07T01:00:00+09:00'})
        test=self.write('day/phone-sessions/test.json',dict(job,id='test',origin='acceptance',
                        events=[{'at':'2026-11-07T00:01:00+09:00','kind':'frame','index':0}]))
        before={p:p.read_bytes() for p in (path,old,test)}
        status,data=self.get('date=2026-11-07&source=phone_event')
        self.assertEqual(200,status)
        self.assertEqual((4,1,1),(data['total'],data['hidden_tests'],data['invalid_records']))
        self.assertEqual(['future_event','reflection','action','selection'],[r['event_kind'] for r in data['rows']])
        self.assertEqual(['phone_event:'+job['id']+':'+str(i) for i in (4,3,2,1)], [r['id'] for r in data['rows']])
        self.assertEqual('本次没有形成可用感想',data['rows'][1]['detail'])
        self.assertEqual('pause',data['rows'][2]['action'])
        selected=data['rows'][3]
        self.assertEqual('<img src=x>小猫',selected['selected_title'])
        self.assertEqual('这个标题吸引我',selected['detail'])
        self.assertEqual(job['id'],selected['session_id'])
        self.assertNotIn('PRIVATE_',json.dumps(data))
        self.assertNotIn('bounds',json.dumps(data))
        self.assertEqual(data['rows'][2:],self.get('date=2026-11-07&source=phone_event&offset=2&limit=2')[1]['rows'])
        yesterday=self.get('date=2026-11-06&source=phone_event')[1]
        self.assertEqual(['intention'],[r['event_kind'] for r in yesterday['rows']])
        self.assertEqual(5,self.get('date=2026-11-07&source=phone_event&include_tests=1')[1]['total'])
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_expression_stages_show_historical_decisions_not_current_switches(self):
        from organs.home import expressions
        ident='a'*32
        card_id=ident+':0'
        record={'id':ident,'created_at':'2026-11-06T15:01:00Z','enabled':False,
                'source':{'context':'虚构游戏评论'},'cards':[{'id':card_id,'phrase':'蒸蒸日上',
                'enabled':False,'selection_source':'user','review':{'adopt':True,'reason':'符合语境',
                'applied':True,'enabled_before':False,'enabled_after':True}}],
                'reviewed_at':'2026-11-07T01:00:00+09:00',
                'selection_history':[{'at':'2026-11-07T02:00:00+09:00','changes':[{'id':card_id,
                    'enabled_before':True,'enabled_after':False,'source_before':'model','source_after':'user'}]}]}
        expressions.save(record)
        library=expressions.library_root()
        usage=library/'usage.jsonl'
        usage.write_text(json.dumps({'at':'2026-11-07T01:30:00+09:00','kind':'offered_to_model',
                         'card_ids':[card_id],'session_id':'private:sample','delivery_id':'d1'})+'\n')
        before={p:p.read_bytes() for p in library.iterdir() if p.is_file()}
        status,data=self.get('date=2026-11-07&source=expression')
        self.assertEqual(200,status)
        self.assertEqual(['selection','offered_to_model','reviewed','created'],[r['status'] for r in data['rows']])
        review=data['rows'][2]['cards'][0]['review']
        self.assertTrue(review['enabled_after'], '后来人工停用不能改写复核时实际采用的结果')
        self.assertEqual('d1',data['rows'][1]['delivery_id'])
        self.assertEqual(4,len({r['id'] for r in data['rows']}))
        self.assertEqual(before,{p:p.read_bytes() for p in library.iterdir() if p.is_file()})
        self.assertEqual(0,self.get('date=2026-11-06&source=expression')[1]['total'])
        self.assertEqual('reviewed',self.get('date=2026-11-07&source=expression&limit=1&offset=2')[1]['rows'][0]['status'])
        for key in ('applied','enabled_before','enabled_after'):
            record['cards'][0]['review'].pop(key)
        record.pop('selection_history')
        expressions.save(record)
        old=self.get('date=2026-11-07&source=expression')[1]['rows']
        self.assertNotIn('selection',[r['status'] for r in old])
        self.assertNotIn('enabled_after',next(r for r in old if r['status']=='reviewed')['cards'][0]['review'])

    def test_reminder_evaluations_and_proactive_delivery_are_distinct_readonly_records(self):
        task = self.write('day/proactive_task_log.jsonl', [
            {'at':'2026-11-06T15:10:00Z','card_id':'card1','name':'带伞',
             'due':'2026-11-07T09:00:00+09:00','action':'skipped','reason':'还没到时候','count':4},
            {'at':'2026-11-06T14:59:00Z','card_id':'old','name':'昨天','action':'planned'}], True)
        log = self.write('day/proactive_log.jsonl', [
            {'at':'2026-11-07T08:55:00+09:00','action':'queued','stage':'due',
             'reason_code':'pending_task','who':'123','because':'带伞','plan_id':'p1','delivery_id':'d1'},
            {'at':'2026-11-07T08:56:00+09:00','action':'sent','stage':'ack',
             'reason_code':'pending_task','who':'123','because':'带伞','plan_id':'p1','delivery_id':'d1'},
            {'at':'2026-11-07T10:00:00+09:00','action':'error','error':'连接失败','origin':'acceptance'}], True)
        snapshots = {p:p.read_bytes() for p in (task, log)}
        status, data = self.get('date=2026-11-07&source=reminder')
        self.assertEqual(200, status)
        self.assertEqual(1, data['total'])
        self.assertEqual(('带伞', 'skipped', 4), (data['rows'][0]['title'], data['rows'][0]['status'], data['rows'][0]['evaluation_count']))
        status, data = self.get('date=2026-11-07&source=proactive')
        self.assertEqual(200, status)
        self.assertEqual(['sent', 'queued'], [r['status'] for r in data['rows']])
        self.assertEqual(['ack', 'due'], [r['stage'] for r in data['rows']])
        self.assertEqual(['d1', 'd1'], [r['delivery_id'] for r in data['rows']])
        self.assertEqual(1, data['hidden_tests'])
        self.assertEqual(snapshots, {p:p.read_bytes() for p in snapshots})

    def test_subtitle_task_snapshot_uses_last_saved_time_without_exposing_model_payload(self):
        from organs.home import knowledge
        from organs.mind import learning
        job = {'id':'a'*32, 'created_at':'2026-11-06T10:00:00+09:00',
               'updated_at':'2026-11-07T12:00:00+09:00', 'status':'running',
               'source':{'meta':{'title':'字幕示例'}, 'lines':[{'text':'PRIVATE_SUBTITLE'}]},
               'reference':'https://www.bilibili.com/video/BV1xx411c7mD',
               'next_segment':2, 'total_segments':5, 'cards':[{'claim':'PRIVATE_CARD'}],
               'rejected':[{'raw':'PRIVATE_REJECTED'}], 'outputs':[{'raw':'PRIVATE_MODEL_OUTPUT'}]}
        knowledge.save(job)
        path = knowledge.library_root() / (job['id'] + '.json')
        before = path.read_bytes()
        with patch.object(learning, '_ACTIVE', None):
            status, data = self.get('date=2026-11-07&source=learning')
        self.assertEqual(200, status)
        row = data['rows'][0]
        self.assertEqual('interrupted', row['status'])
        self.assertEqual('字幕示例', row['title'])
        self.assertEqual((2, 5, 1, 1), (row['completed_segments'], row['total_segments'], row['card_count'], row['rejected_count']))
        self.assertEqual(job['updated_at'], row['at'])
        self.assertEqual(job['created_at'], row['created_at'])
        self.assertEqual('learning', row['link'])
        self.assertNotIn('PRIVATE_', json.dumps(data))
        self.assertEqual(before, path.read_bytes())

    def test_unavailable_house_does_not_hide_other_activity_sources(self):
        from organs.home.store import StoreError
        self.write('day/proactive_log.jsonl', [
            {'at':'2026-11-07T08:55:00+09:00','action':'queued'}], True)
        with patch('organs.home.knowledge.jobs', side_effect=StoreError('家未接入')):
            status, data = self.get()
        self.assertEqual(200, status)
        self.assertIn('learning', data['errors'])
        self.assertEqual(['queued'], [row['status'] for row in data['rows']])

    def test_legacy_sent_without_ack_is_not_presented_as_confirmed_delivery(self):
        self.write('day/proactive_log.jsonl', [
            {'at':'2026-11-07T09:05:35+09:00','action':'sent','stage':'due','delivery_id':'d-old'},
            {'at':'2026-11-07T10:53:52+09:00','action':'declined','stage':'outcome','delivery_id':'d-old',
             'reason':'withdrawn-unsent-legacy','correction_of':'2026-11-07T09:05:35+09:00'}], True)
        status, data = self.get('date=2026-11-07&source=proactive')
        self.assertEqual(200, status)
        correction, old = data['rows']
        self.assertEqual('legacy_sent', old['status'])
        self.assertEqual('withdrawn-unsent-legacy', correction['correction_reason'])
        self.assertEqual(old['at'], correction['correction_of'])

    def test_subtitle_created_time_fallback_keeps_notification_unconfirmed(self):
        from organs.home import knowledge
        job = {'id':'b'*32, 'created_at':'2026-11-07T10:00:00+09:00',
               'status':'done', 'delivery':{'status':'sending'}}
        knowledge.save(job)
        with patch('organs.mind.learning._ACTIVE', None):
            status, data = self.get('date=2026-11-07&source=learning')
        self.assertEqual(200, status)
        row = data['rows'][0]
        self.assertEqual(job['created_at'], row['at'])
        self.assertEqual(('done', 'unconfirmed'), (row['status'], row['delivery_status']))
        self.assertEqual(0, row['card_count'])

    def test_model_attempts_preserve_local_date_failure_retry_and_trace_links(self):
        turn = 'a' * 32
        p = self.write('mind/trace/2026-11-06.jsonl', [
            {'at':'2026-11-06T15:01:00Z','status':503,'error':'http','reply':'',
             'call_id':'same','turn_id':turn,'call_index':2,'attempt':1,'duration_ms':12,
             'purpose':'生成对话','raw':'unavailable','messages':[{'content':'PRIVATE_PROMPT'}]},
            {'at':'2026-11-06T15:02:00Z','status':200,'reply':'PRIVATE_REPLY',
             'call_id':'same','turn_id':turn,'call_index':2,'attempt':2,'duration_ms':1500,
             'purpose':'生成对话','request':{'model':'legacy-alias'},
             'raw':json.dumps({'model':'actual-model','usage':{'total_tokens':81}})},
            {'at':'2026-11-06T14:59:00Z','status':200,'reply':'前一天'}], True)
        q = self.write('mind/trace/2026-11-07.jsonl', [
            {'at':'2026-11-07T03:00:00+09:00','status':None,'error':'timeout','reply':'',
             'call_id':'timeout','attempt':1,'purpose':'选择聊天表情'},
            {'at':'2026-11-07T04:00:00+09:00','status':200,'reply':'旧记录'},
            {'at':'2026-11-07T05:00:00+09:00','status':200,'reply':'验收','origin':'acceptance'},
            {'at':'2026-11-07T06:00:00+09:00','status':200,'reply':'','raw':'{}'}], True)
        before = [p.read_bytes(), q.read_bytes()]
        status, page = self.get('date=2026-11-07&source=model')
        self.assertEqual(200, status)
        self.assertEqual(5, page['total'])
        self.assertEqual(1, page['hidden_tests'])
        self.assertEqual(5, len({r['id'] for r in page['rows']}))
        self.assertEqual(['unknown','completed','failed','completed','failed'],
                         [r['status'] for r in page['rows']])
        success, failure = page['rows'][-2:]
        self.assertEqual([2,1], [success['attempt'], failure['attempt']])
        self.assertEqual('actual-model', success['actual_model'])
        self.assertEqual('legacy-alias', success['requested_model'])
        self.assertEqual(1500, success['duration_ms'])
        self.assertEqual('observe?source=model&turn_id=' + turn, success['link'])
        self.assertEqual('observe?source=model&date=2026-11-07', page['rows'][1]['link'])
        self.assertEqual('2026-11-07T00:01:00+09:00', failure['at'])
        self.assertEqual('2026-11-06', failure['source_date'])
        self.assertEqual(1, failure['source_line'])
        self.assertNotIn('PRIVATE_', json.dumps(page))
        self.assertNotIn('delivery_status', success)
        self.assertEqual(before, [p.read_bytes(), q.read_bytes()])
        self.assertEqual(6, self.get('date=2026-11-07&source=model&include_tests=1')[1]['total'])

    def test_chronological_local_day_and_source_filter_paginate_without_inventing_delivery(self):
        self.write('mind/recent_turns.jsonl', [
            {'ts': '2026-11-06T15:10:00Z', 'who': '访客', 'said': '消息', 'reply': '底稿'},
            {'ts': '2026-11-06T14:59:00Z', 'said': '昨天'}], True)
        self.write('channels/onebot/.state/ledger.jsonl', [
            {'ts': '2026-11-07T01:00:00Z', 'kind': 'uncertain', 'mode': 'send', 'text': '待核实'},
            {'ts': '2026-11-07T01:01:00Z', 'kind': 'sent', 'mode': 'send', 'text': '已发送'}], True)
        status, page = self.get('date=2026-11-07&limit=2')
        self.assertEqual(status, 200)
        self.assertEqual(page['total'], 3)
        self.assertEqual([r['status'] for r in page['rows']], ['sent', 'uncertain'])
        self.assertTrue(page['has_older'])
        row = self.get('date=2026-11-07&limit=2&offset=2')[1]['rows'][0]
        self.assertEqual(row['status'], 'recorded')
        self.assertEqual(row['at'], '2026-11-07T00:10:00+09:00')
        self.assertEqual(self.get('date=2026-11-07&source=chat')[1]['total'], 1)

    def test_phone_failure_test_filter_and_interruption_are_read_only(self):
        paths = []
        for ident, origin, state in [('a', 'automatic', 'failed'), ('b', 'acceptance', 'done'),
                                     ('c', 'manual', 'running')]:
            paths.append(self.write('day/phone-sessions/' + ident + '.json',
                {'id': ident, 'created_at': '2026-11-07T09:00:00+09:00', 'status': state,
                 'origin': origin, 'title': '选择过的视频', 'note': '原始记录'}))
        before = [p.read_bytes() for p in paths]
        status, page = self.get()
        self.assertEqual(status, 200)
        self.assertEqual({r['status'] for r in page['rows']}, {'failed', 'interrupted'})
        self.assertEqual(page['hidden_tests'], 1)
        self.assertEqual(len(self.get('date=2026-11-07&include_tests=1')[1]['rows']), 3)
        self.assertEqual([p.read_bytes() for p in paths], before)

    def test_painting_done_does_not_mean_sent_and_old_origin_stays_unknown(self):
        job = {'id': 'paint', 'created_at': '2026-11-07T08:00:00+09:00', 'status': 'done',
               'request': {'prompt': '鲸鱼'}, 'delivery': {'status': 'uncertain'}}
        with patch('organs.home.art.painting_jobs', return_value=[job]):
            row = self.get()[1]['rows'][0]
            self.assertEqual(row['status'], 'done')
            self.assertEqual(row['delivery_status'], 'uncertain')
            self.assertEqual(row['origin'], 'unknown')
        self.assertNotIn('origin', job)

    def test_invalid_rows_reported_and_bad_parameters_rejected(self):
        p = self.write('mind/recent_turns.jsonl', [{'ts': 'invalid'}, {'ts': '2026-11-07T09:00:00+09:00'}], True)
        p.write_text(p.read_text() + '\nnot json\n[]\n')
        page = self.get()[1]
        self.assertEqual(page['invalid_records'], 3)
        self.assertEqual(page['total'], 1)
        for q in ['date=2026-02-30', 'source=secret', 'limit=101', 'offset=-1', 'include_tests=yes']:
            self.assertEqual(self.get(q)[0], 400, q)
        self.assertEqual(api.handle('POST', '/observe/activity', {}, None)[0], 405)

    def test_all_phone_history_is_available_not_only_latest_twenty(self):
        for i in range(25):
            self.write('day/phone-sessions/%s.json' % i, {'id': str(i), 'status': 'rested',
                       'created_at': '2026-11-07T08:00:00+09:00', 'origin': 'automatic'})
        self.assertEqual(self.get()[1]['total'], 25)

    def test_media_records_keep_observation_limits_and_generation_is_not_delivery(self):
        paths = [self.write('eye/observations.jsonl', [
            {'at':'2026-11-07T09:00:00+09:00','status':'ok','cached':True,
             'observation':{'subject':'树上的鸟','uncertainty':'物种不确定','sensitive':False}},
            {'at':'2026-11-07T09:01:00+09:00','status':'sensitive',
             'observation':{'subject':'不应展示','sensitive':True}},
            {'at':'2026-11-07T09:02:00+09:00','status':'failed','reason':'TimeoutError'},
            {'at':'2026-11-06T14:59:00Z','status':'ok'}], True),
            self.write('ear/observations.jsonl', [
            {'at':'2026-11-07T09:03:00+09:00','transcription':{'status':'partial','transcript':'前半段','partial':True}},
            {'at':'2026-11-07T09:04:00+09:00','transcription':{'status':'failed','transcript':'不应当成成功转写','reason':'decode_failed'}}], True),
            self.write('mouth/voice.jsonl', [
            {'id':'same-job','kind':'attempt','at':'2026-11-07T09:05:00+09:00','characters':5},
            {'id':'same-job','kind':'result','at':'2026-11-07T09:06:00+09:00','status':'generated','duration':3.8,'file':'file:///private/audio.wav'}], True)]
        before = [p.read_bytes() for p in paths]
        status, page = self.get()
        self.assertEqual(200, status)
        self.assertEqual(7, page['total'])
        self.assertEqual(7, len({r['id'] for r in page['rows']}))
        voice = self.get('date=2026-11-07&source=voice')[1]['rows']
        self.assertEqual(['generated','attempt'], [r['status'] for r in voice])
        self.assertNotIn('delivery_status', voice[0])
        self.assertNotIn('file', voice[0])
        self.assertEqual(3.8, voice[0]['duration'])
        eye = self.get('date=2026-11-07&source=eye')[1]['rows']
        self.assertEqual('TimeoutError', eye[0]['detail'])
        self.assertEqual('observe?source=eye&date=2026-11-07', eye[0]['link'])
        self.assertEqual({}, eye[1]['observation'])
        self.assertEqual('物种不确定', eye[2]['observation']['uncertainty'])
        self.assertTrue(eye[2]['cached'])
        ear = self.get('date=2026-11-07&source=ear')[1]['rows']
        self.assertEqual('', ear[0]['transcript'])
        self.assertEqual('前半段', ear[1]['transcript'])
        self.assertEqual('partial', ear[1]['status'])
        self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_media_settings_and_test_origins_remain_distinct_from_perception(self):
        for kind, path in [('eye','eye/observations.jsonl'), ('ear','ear/observations.jsonl'), ('voice','mouth/voice.jsonl')]:
            with self.subTest(kind=kind):
                self.write(path, [
                    {'at':'2026-11-06T15:10:00Z','kind':'setting','parameter':'enabled','before':False,'after':True,'source':'panel'},
                    {'at':'2026-11-07T10:00:00+09:00','origin':'acceptance','status':'failed'},
                    {'at':'bad','status':'ok'}], True)
                status, page = self.get('date=2026-11-07&source='+kind)
                self.assertEqual(200, status)
                self.assertEqual((1,1,1), (page['total'],page['hidden_tests'],page['invalid_records']))
                row = page['rows'][0]
                self.assertEqual('setting', row['status'])
                self.assertEqual({'enabled':{'before':False,'after':True}}, row['changes'])
                self.assertEqual('panel', row['trigger_source'])
                self.assertEqual('2026-11-07T00:10:00+09:00', row['at'])

    def test_source_failure_is_visible_and_does_not_hide_other_sources(self):
        self.write('mind/recent_turns.jsonl', [{'ts': '2026-11-07T09:00:00+09:00'}], True)
        with patch('organs.day.phone_browse.view', side_effect=OSError('unreadable')):
            status, page = self.get()
        self.assertEqual(status, 200)
        self.assertIn('phone', page['errors'])
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['rows'][0]['source'], 'chat')

    def test_night_retry_keeps_execution_and_living_day_distinct(self):
        path = self.write('day/night_log.jsonl', [{
            'ts': '2026-11-06T18:35:00Z', 'day': '2026-11-06', 'attempt': 1,
            'retried_steps': ['home_diary'], 'failed': [],
            'steps': {'home_diary': {'status': 'ok', 'detail': '短日记已保存', 'model_turn_id':'a'*32},
                      'home_dream': {'status': 'ok', 'detail': '上次已经写完'},
                      'brain_decay': {'status': 'stubbed', 'detail': '未接独立结算'}}}], True)
        before = path.read_bytes()
        status, page = self.get('date=2026-11-07&source=night')
        self.assertEqual(200, status)
        self.assertEqual(1, page['total'])
        row = page['rows'][0]
        self.assertEqual('2026-11-07T03:35:00+09:00', row['at'])
        self.assertEqual('2026-11-06', row['day'])
        self.assertEqual(1, row['attempt'])
        self.assertEqual(['home_diary'], row['retried_steps'])
        self.assertEqual('a'*32, row['steps']['home_diary']['model_turn_id'])
        self.assertEqual('上次已经写完', row['steps']['home_dream']['detail'])
        self.assertEqual('stubbed', row['steps']['brain_decay']['status'])
        self.assertEqual('unknown', row['origin'])
        self.assertEqual('night:1', row['id'])
        self.assertEqual(0, self.get('date=2026-11-06&source=night')[1]['total'])
        self.assertEqual(before, path.read_bytes())

    def test_night_never_ran_audit_and_marked_acceptance(self):
        self.write('day/night_log.jsonl', [
            {'ts': '2026-11-07T03:50:00+09:00', 'day': '2026-11-06',
             'event': 'never_ran', 'failed_steps': ['home_diary'], 'attempt': 3,
             'reason': '日记重试后仍未完成'},
            {'ts': '2026-11-07T04:00:00+09:00', 'origin': 'acceptance', 'steps': {}}
        ], True)
        page = self.get('date=2026-11-07&source=night')[1]
        self.assertEqual(1, page['total'])
        row = page['rows'][0]
        self.assertEqual('never_ran', row['status'])
        self.assertEqual('日记重试后仍未完成', row['detail'])
        self.assertEqual(['home_diary'], row['failed_steps'])
        self.assertEqual(1, page['hidden_tests'])
        self.assertEqual(2, self.get('date=2026-11-07&source=night&include_tests=1')[1]['total'])

    def test_real_state_changes_join_activity_with_values_and_provenance(self):
        from organs.heart import mood, sleep, energy
        heart = self.root / 'heart'
        mood.apply_experience('positive', source_id='phone:observed', note='有意思', root=heart,
                              now='2026-11-07T10:00:00+09:00')
        energy.read(root=heart, now='2026-11-07T10:01:00+09:00')
        sleep.record('asleep', root=heart, now='2026-11-07T23:00:00+09:00')
        self.write('day/phone-sessions/observed.json', {'id':'observed', 'status':'done',
                   'created_at':'2026-11-07T09:58:00+09:00', 'origin':'automatic'})
        ledger = heart / 'changes.jsonl'
        before = ledger.read_bytes()
        original = [json.loads(line) for line in before.decode().splitlines()]
        status, page = self.get('date=2026-11-07&source=heart')
        self.assertEqual(200, status)
        self.assertEqual(len(original), page['total'])
        self.assertEqual('sleep', page['rows'][0]['group'])
        for row in page['rows']:
            raw = original[row['source_line']-1]
            self.assertEqual(raw['changes'], row['changes'])
            self.assertEqual(raw['reason'], row['title'])
            self.assertEqual(raw['source'], row['trigger_source'])
            self.assertEqual('recorded', row['status'])
        mood_row = next(r for r in page['rows'] if r['group']=='mood')
        self.assertEqual('phone:observed', mood_row['trigger_source'])
        combined = self.get()[1]['rows']
        self.assertEqual('phone', combined[-1]['source'])
        self.assertEqual(len(original)+1, len(combined))
        self.assertEqual(before, ledger.read_bytes())

    def test_state_history_is_not_capped_at_one_page_and_bad_rows_remain_visible(self):
        from organs.heart import changes
        for index in range(125):
            changes.record(self.root/'heart', at='2026-11-06T15:01:00Z', group='energy',
                           reason='时间结算', before={'精力':index}, after={'精力':index+1})
        path = self.root/'heart/changes.jsonl'
        with path.open('a') as handle:
            handle.write('\n' + json.dumps({'at':'2026-11-07T10:00:00+09:00','group':'mood','changes':[]}) + '\n')
        status, page = self.get('date=2026-11-07&source=heart&limit=100')
        self.assertEqual(200,status)
        self.assertEqual(125,page['total'])
        self.assertEqual(1,page['invalid_records'])
        self.assertEqual(125,page['rows'][0]['source_line'])
        older = self.get('date=2026-11-07&source=heart&limit=100&offset=100')[1]
        self.assertEqual(25,len(older['rows']))
        self.assertEqual(1,older['rows'][-1]['source_line'])
        self.assertEqual(125,len({r['id'] for r in page['rows']+older['rows']}))
        self.assertEqual(0,self.get('date=2026-11-06&source=heart')[1]['total'])

    def test_autonomous_activity_attempt_decision_and_receipt_are_distinct_readonly_rows(self):
        turn = 'a' * 32
        path = self.write('day/proactive_idle_log.jsonl', [
            {'at':'2026-11-07T11:59:00+09:00','action':'a','why':'legacy'},
            {'at':'2026-11-07T12:00:00+09:00','event':'autonomy_attempt','model_turn_id':turn},
            {'at':'2026-11-07T12:00:05+09:00','event':'autonomy_result','model_turn_id':turn,
             'status':'queued','action':'paint','reason':'想画月亮','intention_id':'own-plan',
             'receipt':{'id':'b'*32,'status':'running'},
             'reaction':{'id':'previous-work','effect':'neutral','impression':'','evidence':'','mood':{'delta':0}}},
            {'at':'2026-11-07T12:01:00+09:00','event':'autonomy_finished','model_turn_id':turn,
             'status':'done','action':'paint','reason':'逐笔绘画：已完成；画面要求：月亮',
             'receipt':{'card_id':'work','job_id':'b'*32}}], lines=True)
        before = path.read_bytes()
        status, page = self.get('date=2026-11-07&source=autonomy')
        self.assertEqual(status, 200)
        self.assertEqual(page['total'], 3)
        self.assertEqual(len({r['id'] for r in page['rows']}), 3)
        self.assertEqual([r['status'] for r in page['rows']], ['done','queued','started'])
        self.assertTrue(all(r['model_turn_id'] == turn for r in page['rows']))
        self.assertEqual(page['rows'][1]['intention_id'], 'own-plan')
        self.assertEqual(page['rows'][1].get('reaction', {}).get('id'), 'previous-work')
        self.assertEqual(path.read_bytes(), before)

    def test_autonomy_links_actual_execution_without_replacing_historical_decision(self):
        jobs = [dict(id=ident, status='done', request={'mode':mode}, **extra)
                for ident, mode, extra in [('paint', 'strokes', {'model_turn_id':'b'*32}),
                                          ('image', 'image', {}), ('old', 'strokes', {})]]
        rows = [dict(at='2026-11-07T12:00:00+09:00', event='autonomy_result',
                     model_turn_id='a'*32, status='queued', action=action, receipt=receipt)
                for action, receipt in [('paint', {'id':'paint'}), ('paint', {'job_id':'image'}),
                    ('paint', {'id':'old'}), ('paint', {'id':'gone'}), ('browse', {'id':'phone'})]]
        path = self.write('day/proactive_idle_log.jsonl', rows, lines=True)
        before = path.read_bytes()
        with patch('organs.home.art.painting_jobs', return_value=jobs), \
             patch('organs.day.phone_browse.view', return_value={'jobs':[
                 {'id':'phone','status':'partial','model_turn_id':'c'*32}]}):
            status, page = self.get('date=2026-11-07&source=autonomy')
        self.assertEqual(200, status)
        self.assertFalse(page['errors'])
        execution = {r['execution']['job_id']: r['execution'] for r in page['rows']}
        self.assertEqual('b'*32, execution['paint']['model_turn_id'])
        self.assertEqual('c'*32, execution['phone']['model_turn_id'])
        self.assertEqual('partial', execution['phone']['status'])
        self.assertEqual('image', execution['image']['mode'])
        self.assertEqual('missing', execution['gone']['status'])
        for ident in ('image', 'old', 'gone'):
            self.assertNotIn('model_turn_id', execution[ident])
        self.assertTrue(all(r['status']=='queued' and r['model_turn_id']=='a'*32 for r in page['rows']))
        self.assertEqual(before, path.read_bytes())

    def test_growth_versions_compare_prior_day_and_skip_plain_selfnarration(self):
        path = self.write('brain/selfnarration.jsonl', [
            {'id':'one','version':1,'ts':'2026-11-06T12:00:00+09:00','profile':{'likes':'薄荷'},'note':'起点'},
            {'id':'plain','version':2,'ts':'2026-11-07T11:00:00+09:00','text':'旧文字自述'},
            {'id':'clear','version':3,'ts':'2026-11-07T12:00:00+09:00','profile':{'likes':''},'note':'不再喜欢'},
            {'id':'restore','version':4,'ts':'2026-11-07T13:00:00+09:00','profile':{},'note':'退回初始设定'}], lines=True)
        before = path.read_bytes()
        status, page = self.get('date=2026-11-07&source=growth')
        self.assertEqual(status, 200)
        self.assertEqual(page['total'], 2)
        self.assertEqual(page['rows'][0]['changes']['likes'], {'before':'','after':None})
        self.assertEqual(page['rows'][1]['changes']['likes'], {'before':'薄荷','after':''})
        self.assertEqual(page['rows'][0]['version'], 4)
        self.assertEqual(path.read_bytes(), before)

    def test_growth_setting_and_night_attempt_do_not_claim_personality_changed(self):
        self.write('day/growth.json', {'enabled':False,'changes':[
            {'at':'2026-11-07T11:00:00+09:00','before':True,'after':False}]})
        turn = 'c'*32
        self.write('day/night_log.jsonl', [
            {'ts':'2026-11-07T12:00:00+09:00','event':'growth_attempt','model_turn_id':turn},
            {'ts':'2026-11-07T12:00:03+09:00','event':'growth_result','model_turn_id':turn,
             'result':{'status':'ok','changed':False,'profile':{'likes':'薄荷'}}}], lines=True)
        status, page = self.get('date=2026-11-07&source=growth_settings')
        self.assertEqual(status, 200)
        self.assertEqual(page['rows'][0]['changes']['enabled'], {'before':True,'after':False})
        status, page = self.get('date=2026-11-07&source=night')
        self.assertEqual([r['status'] for r in page['rows']], ['unchanged','started'])
        self.assertTrue(all(r['model_turn_id'] == turn for r in page['rows']))

    def test_painting_summary_and_events_preserve_only_recorded_round(self):
        job={'id':'paint','model_turn_id':'a'*32,'created_at':'2026-11-07T12:00:00+09:00',
             'status':'done','request':{'prompt':'小鸟'},'events':[
                {'at':'2026-11-07T12:00:01+09:00','kind':'planning','status':'running'}]}
        old=dict(job,id='old'); old.pop('model_turn_id')
        with patch('organs.home.art.painting_jobs',return_value=[job,old]):
            for source in ('painting','painting_event'):
                data=self.get('date=2026-11-07&source='+source)[1]
                linked=[r for r in data['rows'] if r.get('model_turn_id')]
                self.assertEqual(len(linked),1)
                self.assertEqual(linked[0]['model_turn_id'],'a'*32)
