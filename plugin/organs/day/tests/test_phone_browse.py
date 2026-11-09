import json
import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from organs.day import phone_browse as browse


class BrowseTest(unittest.TestCase):
    def douyin_probe(self, source_menu=None, after_title="小猫", return_error=None):
        self.mock_feeling()
        package='com.ss.android.ugc.aweme'
        page={'at':'2026-11-07T10:00:00+09:00','video_bounds':[0,0,100,90], 'nodes':[
            {'package':package,'resource_id':package+':id/desc','text':'小猫','description':'','bounds':[0,70,80,80]},
            {'package':package,'resource_id':'comment','text':'','description':'评论10，按钮','clickable':True,'bounds':[80,50,100,60]}]}
        if source_menu is not None:
            page['nodes'].append({'package':package,'resource_id':'share','text':'','description':'分享20，按钮','clickable':True,'bounds':[80,60,100,70]})
        after = json.loads(json.dumps(page))
        after['nodes'][0]['text'] = after_title
        comments={'at':page['at'],'nodes':[{'package':package,'text':'困得眼皮打架了','in_comment_list':True},
                                        {'package':package,'text':'发条评论','in_comment_list':False}]}
        image=io.BytesIO();Image.new('RGB',(100,100),'blue').save(image,format='PNG')
        screen={'width':100,'height':100,'at':page['at'],'image':'data:image/png;base64,'+base64.b64encode(image.getvalue()).decode()}
        with patch.object(browse._STOP,'wait',return_value=False), \
             patch.object(browse.phone,'inspect',side_effect=[page]*6+([source_menu,return_error or after] if source_menu is not None else [])+[comments]*2), \
             patch.object(browse.phone,'screen',return_value=screen), \
             patch.object(browse.phone,'act',return_value={'state':'command_accepted'}) as act, \
             patch.object(browse.vision,'load_config',return_value={'enabled':True}), \
             patch.object(browse.vision,'read_key',return_value='test'), \
             patch.object(browse.vision,'describe',return_value={'status':'ok','observation':{'subject':'小猫闭眼'}}), \
             patch.object(browse.expression_learning,'learn',return_value={'id':'draft','cards':[]}) as learn, \
             patch.object(browse.phone_reflection,'reflect',return_value={'effect':'neutral','impression':'','evidence':''}):
            receipt=browse.start(persona='喜欢猫',root=self.root,platform='douyin',origin='acceptance',
                         now=page['at'],complete=lambda _: '{"node":0,"reason":"喜欢猫"}')
            browse._FUTURE.result(timeout=3)
        job=next(j for j in browse.view(self.root)['jobs'] if j['id']==receipt['id'])
        return job, act, learn

    def test_douyin_copies_fresh_link_then_returns_before_reading_comments(self):
        menu={'nodes':[{'package':'com.ss.android.ugc.aweme','text':'分享链接',
                        'description':'','bounds':[10,10,20,20]}]}
        samples=[(['','','3.82 复制打开抖音 小猫 https://v.douyin.com/TestClip/ abc'],True),
                 (['','https://example.com/video'],False), (['']*6,False)]
        for answers,valid in samples:
            with self.subTest(answers=answers), patch.object(browse.phone,'clipboard',side_effect=answers) as clipboard:
                job,act,learn=self.douyin_probe(menu)
            self.assertEqual('done',job['status'],job.get('note'))
            self.assertEqual('ok' if valid else 'unavailable',job['video_source']['status'])
            self.assertEqual('https://v.douyin.com/TestClip/' if valid else None,job['video_source'].get('url'))
            self.assertEqual(('',),clipboard.call_args_list[0].args)
            self.assertTrue(learn.called)
            actions=[c.args[0] for c in act.call_args_list]
            self.assertEqual(['wake','douyin','pause','play','pause','tap','tap','back','tap','swipe','pause','home'],actions)

    def test_douyin_share_menu_never_selects_a_contact(self):
        menu={'nodes':[{'package':'com.ss.android.ugc.aweme','text':'测试联系人',
                        'description':'','bounds':[10,10,20,20]}]}
        with patch.object(browse.phone,'clipboard') as clipboard:
            job,act,learn=self.douyin_probe(menu)
        clipboard.assert_not_called()
        self.assertEqual('unavailable',job['video_source']['status'])
        self.assertEqual([.9,.9],[c.kwargs['x'] for c in act.call_args_list if c.args[0]=='tap'])
        self.assertTrue(learn.called)

    def test_douyin_video_changed_after_copy_drops_link_and_comments(self):
        menu={'nodes':[{'package':'com.ss.android.ugc.aweme','text':'分享链接',
                        'description':'','bounds':[10,10,20,20]}]}
        with patch.object(browse.phone,'clipboard',side_effect=['','https://v.douyin.com/TestClip/']) as clipboard:
            job,act,learn=self.douyin_probe(menu,after_title='另一条视频')
        self.assertEqual('partial',job['status'])
        self.assertIn('分享后没有确认回到同一视频',job['note'])
        self.assertEqual(2,clipboard.call_count)
        self.assertNotIn('url',job['video_source'])
        learn.assert_not_called()

    def test_douyin_return_read_failure_does_not_commit_an_unconfirmed_link(self):
        from adapters.adb import AdbError
        menu={'nodes':[{'package':'com.ss.android.ugc.aweme','text':'分享链接',
                        'description':'','bounds':[10,10,20,20]}]}
        with patch.object(browse.phone,'clipboard',side_effect=['','https://v.douyin.com/TestClip/']):
            job,act,learn=self.douyin_probe(menu,return_error=AdbError('return-inspect-failed'))
        self.assertEqual('partial',job['status'])
        self.assertEqual('return-inspect-failed',job['note'])
        self.assertEqual('unavailable',job['video_source']['status'])
        self.assertNotIn('url',job['video_source'])
        learn.assert_not_called()

    def test_douyin_reuses_capture_learning_and_cleanup_pipeline(self):
        job,act,learn=self.douyin_probe()
        self.assertEqual('done',job['status'],job)
        self.assertEqual('douyin',job['platform'])
        self.assertEqual(['困得眼皮打架了'],[n['text'] for n in learn.call_args.args[0]['nodes']])
        self.assertIn('抖音',learn.call_args.kwargs['context'])
        self.assertEqual(['wake','douyin','pause','play','pause','tap','swipe','pause','home'],[c.args[0] for c in act.call_args_list])
        self.assertEqual(3,sum(e['kind']=='frame' for e in job['events']))

    def test_douyin_selects_visible_video_and_caption_without_private_navigation(self):
        package='com.ss.android.ugc.aweme'
        snapshot={'video_bounds':[0,0,100,90], 'nodes':[
            {'package':package,'resource_id':package+':id/title','text':'@猫主人','description':'','bounds':[0,60,80,70]},
            {'package':package,'resource_id':package+':id/desc','text':'猫又睡着了','description':'','bounds':[0,70,80,80]}]}
        options=browse.choices(snapshot,platform='douyin')
        self.assertEqual('@猫主人：猫又睡着了', options[0]['title'])
        self.assertEqual(options[0]['title'],browse.video_title(snapshot,platform='douyin'))
        self.assertEqual([],browse.choices(dict(snapshot,video_bounds=None),platform='douyin'))
        with self.assertRaises(ValueError):
            browse.start(persona='她',root=self.root,platform='unknown')

    def test_settings_record_real_changes_only_and_stop_without_starting_work(self):
        with patch.object(browse.model,'now_iso',return_value='2026-11-08T10:00:00+09:00'), \
             patch.object(browse,'start',side_effect=AssertionError('设置不能发起活动')), \
             patch.object(browse,'_STOP') as stop:
            initial=browse.configure(False,self.root)
            self.assertEqual(initial.get('changes',[]),[])
            on=browse.configure(True,self.root)
            self.assertEqual(on['changes'],[{'at':'2026-11-08T10:00:00+09:00','before':False,'after':True}])
            self.assertEqual(browse.configure(True,self.root)['changes'],on['changes'])
            off=browse.configure(False,self.root)
            self.assertEqual(len(off['changes']),2)
            self.assertEqual(off['changes'][-1]['before'],True)
            self.assertEqual(off['changes'][-1]['after'],False)
            self.assertEqual(stop.set.call_count,2)
            for key in ('interval_minutes','daily_cap','min_energy','watch_seconds'):
                self.assertEqual(off[key],initial[key])

    def test_failed_or_invalid_setting_does_not_create_history(self):
        browse.configure(False,self.root)
        path=self.root/'phone-browse.json';before=path.read_bytes()
        with patch.object(browse.home_store,'atomic_write_text',side_effect=OSError('test write failure')) as write:
            with self.assertRaises(ValueError):browse.configure('true',self.root)
            write.assert_not_called()
            with self.assertRaises(OSError):browse.configure(True,self.root)
        self.assertEqual(path.read_bytes(),before)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_video_choices_require_visible_video_container_and_title(self):
        nodes = [
            {'text':'','description':'视频,猫打哈欠,时长30秒','package':'tv.danmaku.bili','resource_id':'','bounds':[0,100,500,600]},
            {'text':'猫打哈欠','description':'','package':'tv.danmaku.bili','resource_id':'tv.danmaku.bili:id/title','bounds':[10,400,400,450]},
            {'text':'私信','description':'','package':'tv.danmaku.bili','resource_id':'tv.danmaku.bili:id/title','bounds':[510,400,900,450]},
            {'text':'删除','description':'','package':'other.app','resource_id':'tv.danmaku.bili:id/title','bounds':[10,400,400,450]}]
        self.assertEqual([x['node'] for x in browse.choices({'nodes':nodes})],[1])

    def test_video_title_uses_its_expand_container_not_the_first_ad_title(self):
        # 实机 inspect-06：广告也叫 id/title，实际视频标题在自己的展开区域。
        nodes = [
            {'package':'tv.danmaku.bili','text':'别再到处找资料了','description':'','resource_id':'tv.danmaku.bili:id/title','bounds':[201,769,933,806]},
            {'package':'tv.danmaku.bili','text':'','description':'操 控 买 瓜 2.0，展开','resource_id':'','bounds':[0,1159,1080,1252]},
            {'package':'tv.danmaku.bili','text':'    操 控 买 瓜 2.0','description':'','resource_id':'tv.danmaku.bili:id/title','bounds':[33,1176,975,1235]},
            {'package':'tv.danmaku.bili','text':'合集 · 买瓜二创','description':'','resource_id':'tv.danmaku.bili:id/title','bounds':[58,1574,498,1629]}]
        self.assertEqual('操 控 买 瓜 2.0', browse.video_title({'nodes':nodes}))
        self.assertEqual('',browse.video_title({'nodes':[nodes[0],nodes[2]]}))

    def test_interactive_video_title_keeps_container_and_unique_match_checks(self):
        title = '多 动 症 3.0（完结篇）'
        container = {'package':'tv.danmaku.bili','text':'','description':'互动视频，'+title+'，展开',
                     'resource_id':'','bounds':[0,966,1080,1059]}
        label = {'package':'tv.danmaku.bili','text':title,'description':'',
                 'resource_id':'tv.danmaku.bili:id/title','bounds':[33,983,975,1042]}
        ad = dict(label,text='这部放这，夸克有想法',bounds=[407,1643,1047,1693])
        self.assertEqual(browse.video_title({'nodes':[ad,container,label]}),title)
        self.assertEqual(browse.video_title({'nodes':[container,dict(label,bounds=[33,1100,975,1160])]}),'')
        self.assertEqual(browse.video_title({'nodes':[dict(container,description='合集，'+title+'，展开'),label]}),'')
        self.assertEqual(browse.video_title({'nodes':[container,label,dict(label,text='另一个视频'),
            dict(container,description='另一个视频，展开')]}),'')

    def test_selection_cannot_invent_a_target_or_omit_reason(self):
        options=[{'node':1,'title':'猫','bounds':[0,100,500,600]}]
        for data in ({'node':2,'reason':'想看'}, {'node':True,'reason':'想看'}, {'node':1,'reason':''}):
            with self.assertRaises(ValueError):
                browse.choose('喜欢猫',{},options,complete=lambda messages: json.dumps(data))
        self.assertIsNone(browse.choose('喜欢猫',{},options,complete=lambda messages:'{"node":null,"reason":"不想看"}')['node'])

    def test_bottom_video_uses_cover_above_title_not_navigation_overlap(self):
        # 真机失败卡：标题中心2149.5落到消息底栏附近，封面仍在上方。
        nodes = [
            {'text':'','description':'视频,实机演示,','package':'tv.danmaku.bili','resource_id':'','bounds':[546,1680,1067,2270]},
            {'text':'实机演示','description':'','package':'tv.danmaku.bili','resource_id':'tv.danmaku.bili:id/title','bounds':[568,2095,1045,2204]}]
        chosen = browse.choices({'nodes':nodes})[0]
        self.assertEqual([546,1680,1067,2095], chosen['bounds'])

    def test_opening_is_a_distinct_decision_without_requiring_video_titles(self):
        requests=[]
        browse.choose('她',{},[{'node':0,'title':'打开B站'}],opening=True,
                      complete=lambda messages:requests.append(messages) or '{"node":null,"reason":"现在不想看"}')
        self.assertIn('不要求先有感兴趣的视频',requests[0][0]['content'])

    def test_disabled_tick_does_not_read_hardware_or_model(self):
        with patch.object(browse,'start') as start:
            self.assertFalse(browse.tick(persona='她',root=self.root)['enabled'])
            start.assert_not_called()

    def test_worker_calls_link_to_their_own_job_and_preserve_failure(self):
        from organs.mind import llm
        self.mock_feeling()
        config={'provider':'fake','model':'fake','base_url':'http://example.invalid/v1',
                'key_env':'FAKE_KEY','timeout_ms':1000,'retries':0,'temperature':0,'max_tokens':100}
        traces=[]
        def transport(*args):
            if len(traces)==1:
                raise TimeoutError('synthetic timeout')
            return 200,json.dumps({'choices':[{'message':{'content':'{"node":null,"reason":"先休息"}'}}]}).encode()
        with patch.dict('os.environ',{'QQAGENT_TRACE':'1','QQAGENT_HOME_ROOT':str(self.root)}), \
             patch.object(llm,'load_config',return_value=config),patch.object(llm,'read_key',return_value='fake'), \
             patch.object(llm,'default_transport',side_effect=transport),patch.object(browse.phone,'act') as act:
            with llm.trace_round('f'*32,'unrelated:caller'):
                for _ in range(2):
                    started=browse.start(persona='虚构人设',root=self.root,now='2026-11-07T10:00:00+09:00')
                    browse._FUTURE.result(timeout=3)
                    job=json.loads((self.root/'phone-sessions'/(started['id']+'.json')).read_text())
                    rows=[json.loads(line) for p in (self.root/'mind/trace').glob('*.jsonl') for line in p.read_text().splitlines()]
                    row=rows[-1];traces.append(row)
                    self.assertEqual(job['model_turn_id'],started['id'])
                    self.assertEqual(row['turn_id'],started['id'])
                    self.assertEqual(row['session_id'],'day:phone:'+started['id'])
                    self.assertEqual(row['call_index'],1)
                    self.assertEqual(row['purpose'],'决定是否浏览手机')
                self.assertEqual(llm._ROUND_TRACE.get()['turn_id'],'f'*32)
            act.assert_not_called()
        self.assertNotEqual(traces[0]['turn_id'],traces[1]['turn_id'])
        self.assertEqual(traces[1]['error'],'timeout')

    def test_rest_window_does_not_spend_the_days_browsing_opportunities(self):
        browse.configure(True, self.root)
        with patch.object(browse.phone, 'status', return_value={'connected': False}) as device:
            for hour in (4, 6):
                result = browse.tick(persona='她', root=self.root,
                                     now=f'2026-11-08T{hour:02d}:00:00+09:00')
                if browse._FUTURE is not None:
                    browse._FUTURE.result(timeout=3)
                self.assertIn('休息', result.get('reason', ''))
                self.assertEqual([], browse.view(self.root)['jobs'])
            device.assert_not_called()
            result = browse.tick(persona='她', root=self.root, now='2026-11-08T08:00:00+09:00')
            browse._FUTURE.result(timeout=3)
            self.assertIn('id', result)
            self.assertEqual('unavailable', browse.view(self.root)['jobs'][0]['status'])
            self.assertEqual(1, device.call_count)

    def test_acceptance_history_cannot_hide_daily_browsing_limit(self):
        browse.configure(True, self.root)
        for index in range(2):
            browse.save({'id':f'attempt-{index}', 'origin':'automatic', 'status':'unavailable',
                         'created_at':f'2026-11-07T{8+index*2:02d}:00:00+09:00'}, self.root)
        for index in range(21):
            browse.save({'id':f'acceptance-{index}', 'origin':'acceptance', 'status':'done',
                         'created_at':f'2026-11-07T11:{index:02d}:00+09:00'}, self.root)
        with patch.object(browse.phone, 'status', return_value={'connected': False}) as device:
            result = browse.tick(persona='她', root=self.root, now='2026-11-07T14:00:00+09:00')
            if browse._FUTURE is not None:
                browse._FUTURE.result(timeout=3)
        self.assertEqual('今天浏览机会已用完', result.get('reason'))
        self.assertEqual(23, len(browse.view(self.root, limit=None)['jobs']))
        device.assert_not_called()

    def test_low_energy_defers_without_creating_a_session(self):
        browse.configure(True, self.root)
        heart = self.root / 'heart'
        stamp = '2026-11-07T10:00:00+09:00'
        browse.energy.read(heart, stamp)
        path = heart / 'energy.json'
        state = json.loads(path.read_text())
        state['value'] = 20
        path.write_text(json.dumps(state))
        with patch.object(browse.phone, 'status') as device, patch.object(browse, 'choose') as choose:
            result = browse.tick(persona='她', root=self.root, now=stamp)
            if browse._FUTURE is not None:
                browse._FUTURE.result(timeout=3)
        self.assertEqual('精力不足，先休息', result.get('reason'))
        self.assertEqual([], browse.view(self.root)['jobs'])
        device.assert_not_called()
        choose.assert_not_called()

    def test_stop_is_observed_before_any_next_phone_operation(self):
        self.mock_feeling()
        def choose_then_stop(*args, **kwargs):
            browse.stop()
            return {'node':0,'reason':'原本想看，但已收到停止请求'}
        with patch.object(browse,'choose',side_effect=choose_then_stop), patch.object(browse.phone,'act') as act:
            browse.start(persona='她',root=self.root,now='2026-11-07T10:00:00+09:00',complete=lambda messages:'{}')
            browse._FUTURE.result(timeout=3)
        act.assert_not_called()
        self.assertEqual(browse.view(self.root)['jobs'][0]['status'],'stopped')

    def test_entering_actual_rest_stops_current_browse_before_next_operation(self):
        from organs.heart import rest
        self.mock_feeling()
        browse.configure(True, self.root)
        def choose_as_sleep_begins(*args, **kwargs):
            rest.advance(self.root/'heart', '2026-11-07T23:00:00+09:00')
            browse.tick(persona='她', root=self.root, now='2026-11-07T23:00:00+09:00')
            return {'node':0,'reason':'入睡前发起的选择'}
        with patch.object(browse, 'choose', side_effect=choose_as_sleep_begins), patch.object(browse.phone, 'act', side_effect=AssertionError('休息后仍操作手机')) as act:
            browse.start(persona='她', root=self.root, now='2026-11-07T22:59:00+09:00', complete=lambda messages:'{}')
            browse._FUTURE.result(timeout=3)
        act.assert_not_called()
        self.assertEqual('stopped', browse.view(self.root)['jobs'][0]['status'])

    def test_chat_triggered_rest_is_observed_without_waiting_for_next_tick(self):
        from organs.heart import rest
        self.mock_feeling()
        def choose_as_sleep_begins(*args, **kwargs):
            rest.advance(self.root/'heart', '2026-11-07T23:00:00+09:00')
            return {'node':0,'reason':'选择期间由聊天入口进入休息'}
        with patch.object(browse, 'choose', side_effect=choose_as_sleep_begins), patch.object(browse.phone, 'act', side_effect=AssertionError('休息后仍操作手机')) as act:
            browse.start(persona='她', root=self.root, now='2026-11-07T22:59:00+09:00', complete=lambda messages:'{}')
            browse._FUTURE.result(timeout=3)
        act.assert_not_called()
        self.assertEqual('stopped', browse.view(self.root)['jobs'][0]['status'])

    def test_stop_during_device_read_does_not_start_a_new_model_call(self):
        self.mock_feeling()
        def device_read():
            browse.stop()
            return {'connected':True}
        browse.phone.status.side_effect=device_read
        with patch.object(browse,'choose') as choose:
            browse.start(persona='她',root=self.root,now='2026-11-07T10:00:00+09:00',complete=lambda messages:'{}')
            browse._FUTURE.result(timeout=3)
        choose.assert_not_called()

    def mock_feeling(self, value=80):
        for owner,name,result in ((browse.sleep,'overview',{'effective_window':[23,7],'wake_cap':100}),
                                  (browse.energy,'read',{'value':value}), (browse.mood,'read',{'now':.6}),
                                  (browse.phone,'status',{'connected':True}), (browse.phone,'locked',False)):
            p=patch.object(owner,name,return_value=result);p.start();self.addCleanup(p.stop)
        p=patch.object(browse.phone_audio,'start',return_value=None);p.start();self.addCleanup(p.stop)

    def comment_probe(self, pages, *, stop_on_swipe=False, source_menu=None, source_title='猫', vision_error=None):
        self.mock_feeling()
        feed={'nodes':[
            {'package':'tv.danmaku.bili','text':'','description':'视频,猫','resource_id':'','bounds':[0,10,100,65]},
            {'package':'tv.danmaku.bili','text':'猫','description':'','resource_id':'tv.danmaku.bili:id/title','bounds':[10,40,90,50]}]}
        video={'nodes':[{'package':'tv.danmaku.bili','text':'评论','description':'','bounds':[0,70,20,80]}],
               'video_bounds':[0,10,100,65],'at':'2026-11-07T10:00:00+09:00'}
        if source_menu is not None:
            video['nodes'] += [
                {'package':'tv.danmaku.bili','text':'','description':source_title+'，展开','resource_id':'','bounds':[0,65,100,70]},
                {'package':'tv.danmaku.bili','text':source_title,'description':'','resource_id':'tv.danmaku.bili:id/title','bounds':[0,65,80,70]},
                {'package':'tv.danmaku.bili','text':'','description':'分享','resource_id':'tv.danmaku.bili:id/frame_share','bounds':[80,70,100,80]}]
        image=io.BytesIO();Image.new('RGB',(100,100),'blue').save(image,format='PNG')
        screen={'width':100,'height':100,'at':video['at'],'image':'data:image/png;base64,'+base64.b64encode(image.getvalue()).decode()}
        actions=[]
        def act(action,**params):
            actions.append(action)
            if action=='swipe' and stop_on_swipe: browse._STOP.set()
            return {'state':'command_accepted'}
        decisions=iter(['{"node":0,"reason":"想看猫"}','{"node":1,"reason":"猫有意思"}'])
        with patch.object(browse._STOP,'wait',return_value=False), \
             patch.object(browse.phone,'inspect',side_effect=[feed,feed]+[video]*4+([source_menu,video] if source_menu is not None else [])+pages) as inspect, \
             patch.object(browse.phone,'act',side_effect=act),patch.object(browse.phone,'screen',return_value=screen), \
             patch.object(browse.vision,'load_config',return_value={'enabled':True}),patch.object(browse.vision,'read_key',return_value='test'), \
             patch.object(browse.vision,'describe',return_value={'status':'ok','observation':{'subject':'猫'}}, side_effect=vision_error), \
             patch.object(browse.phone_reflection,'reflect',return_value={'effect':'neutral','impression':'猫','evidence':'猫'}), \
             patch.object(browse.expression_learning,'learn',return_value={'id':'draft','cards':[]}) as learn:
            started=browse.start(persona='她',root=self.root,now=video['at'],origin='acceptance',complete=lambda _:next(decisions))
            browse._FUTURE.result(timeout=3)
        return next(j for j in browse.view(self.root)['jobs'] if j['id']==started['id']),actions,inspect.call_count,learn

    def test_visual_timeout_records_safe_stage_without_learning_or_losing_cleanup(self):
        from urllib.error import URLError
        for error in (TimeoutError('/private/test-secret'),URLError(TimeoutError('/private/test-secret'))):
            with self.subTest(error=type(error).__name__):
                job,actions,_,learn=self.comment_probe([],vision_error=error)
                self.assertEqual('failed',job['status'])
                self.assertEqual('画面理解',job['error_stage'])
                self.assertEqual('timeout',job['error_kind'])
                self.assertEqual('画面理解：请求超时',job['error_detail'])
                self.assertEqual(3,sum(e['kind']=='frame' for e in job['events']))
                self.assertEqual(['pause','home'],actions[-2:])
                self.assertEqual([],job['observations'])
                learn.assert_not_called()

    def test_video_source_uses_only_fresh_copy_from_current_video_share_menu(self):
        menu={'at':'share-menu','nodes':[{'package':'tv.danmaku.bili','text':'','description':label,'bounds':[10,10,20,20]}
                       for label in ('消息','QQ','复制链接')]}
        comments={'at':'first','nodes':[{'package':'tv.danmaku.bili','text':'猫猫可爱','in_comment_list':True}]}
        for copied, valid in [('【猫-哔哩哔哩】 https://b23.tv/X6f5iyP',True),
                              ('【别的视频-哔哩哔哩】 https://b23.tv/other12',False),
                              ('【猫-哔哩哔哩】 https://evil.invalid',False), ('',False)]:
            with self.subTest(copied=copied), patch.object(browse.phone,'clipboard',create=True,side_effect=['',copied]) as clipboard:
                job,actions,_,learn=self.comment_probe([comments,comments],source_menu=menu)
                self.assertEqual('done',job['status'],job.get('note'))
                self.assertEqual('ok' if valid else 'unavailable',job['video_source']['status'])
                self.assertEqual('https://b23.tv/X6f5iyP' if valid else None,job['video_source'].get('url'))
                self.assertEqual([('',),()], [call.args for call in clipboard.call_args_list])
                self.assertEqual(4,actions.count('tap'))  # 打开视频、分享、复制、评论。
                self.assertTrue(learn.called)

    def test_share_menu_without_copy_returns_without_reading_clipboard_or_sending(self):
        menu={'at':'share-menu','nodes':[{'package':'tv.danmaku.bili','text':'','description':'消息','bounds':[10,10,20,20]}]}
        comments={'at':'first','nodes':[{'package':'tv.danmaku.bili','text':'猫猫可爱','in_comment_list':True}]}
        with patch.object(browse.phone,'clipboard',create=True) as clipboard:
            job,actions,_,learn=self.comment_probe([comments,comments],source_menu=menu)
        self.assertEqual('done',job['status'],job.get('note'))
        self.assertEqual('unavailable',job['video_source']['status'])
        clipboard.assert_not_called()
        self.assertEqual(3,actions.count('tap'))
        self.assertIn('back',actions)
        self.assertTrue(learn.called)

    def test_changed_video_does_not_attach_its_comments_to_selected_title(self):
        with patch.object(browse.phone,'clipboard') as clipboard:
            job,actions,_,learn=self.comment_probe([],source_menu={'nodes':[]},source_title='另一条视频')
        self.assertEqual('partial',job['status'])
        self.assertIn('标题',job['note'])
        self.assertNotIn('url',job['video_source'])
        clipboard.assert_not_called()
        learn.assert_not_called()

    def test_copy_failure_is_reported_and_stops_before_comments(self):
        from adapters.adb import AdbError
        menu={'at':'share','nodes':[{'package':'tv.danmaku.bili','text':'复制链接','bounds':[10,10,20,20]}]}
        for answers in ([AdbError('test')],['',AdbError('test')]):
            with patch.object(browse.phone,'clipboard',side_effect=answers):
                job,actions,_,learn=self.comment_probe([],source_menu=menu)
            self.assertEqual('partial',job['status'])
            self.assertEqual('视频链接复制未完成',job['video_source']['reason'])
            self.assertEqual(['pause','home'],actions[-2:])
            learn.assert_not_called()

    def test_comment_capture_stops_at_budget_without_truncating_quote(self):
        nodes=[{'package':'tv.danmaku.bili','text':text,'in_comment_list':True} for text in ('字'*11990,'尾'*20)]
        job,actions,_,learn=self.comment_probe([{'at':'first','nodes':nodes}])
        self.assertEqual('done',job['status'],job.get('note'))
        self.assertNotIn('swipe',actions)
        self.assertEqual(['字'*11990],[n['text'] for n in learn.call_args.args[0]['nodes']])

    def test_leaving_comment_list_does_not_scroll_other_page_or_include_it(self):
        comment={'package':'tv.danmaku.bili','text':'普通公开评论','in_comment_list':True}
        job,actions,_,learn=self.comment_probe([{'at':'first','nodes':[comment]},
            {'at':'second','nodes':[{'package':'other.app','text':'别的页面'}]}])
        self.assertEqual('done',job['status'])
        self.assertEqual(1,actions.count('swipe'))
        self.assertEqual([comment],learn.call_args.args[0]['nodes'])

    def test_stop_during_comment_scroll_prevents_next_capture_and_learning(self):
        comment={'package':'tv.danmaku.bili','text':'普通公开评论','in_comment_list':True}
        job,actions,inspected,learn=self.comment_probe([{'at':'first','nodes':[comment]}],stop_on_swipe=True)
        self.assertEqual('stopped',job['status'])
        self.assertEqual(7,inspected)
        learn.assert_not_called()
        self.assertEqual(['pause','home'],actions[-2:])

    def test_low_energy_and_sleep_prevent_phone_or_model_calls(self):
        self.mock_feeling(value=20)
        with patch.object(browse.phone,'act') as act, patch.object(browse,'choose') as choose:
            browse.start(persona='她',root=self.root,now='2026-11-07T10:00:00+09:00')
            browse._FUTURE.result(timeout=3)
            browse.energy.read.return_value={'value':80}
            browse.start(persona='她',root=self.root,now='2026-11-08T01:00:00+09:00')
            browse._FUTURE.result(timeout=3)
        act.assert_not_called();choose.assert_not_called()
        self.assertTrue(all(j['status']=='rested' for j in browse.view(self.root)['jobs']))

    def test_session_pauses_after_frames_before_audio_vision_and_comment_analysis(self):
        self.mock_feeling()
        feed={'nodes':[
            {'package':'tv.danmaku.bili','text':'','description':'视频,猫','resource_id':'','bounds':[0,10,100,65]},
            {'package':'tv.danmaku.bili','text':'猫打哈欠','description':'','resource_id':'tv.danmaku.bili:id/title','bounds':[10,40,90,50]}]}
        page={'nodes':[{'package':'tv.danmaku.bili','text':'评论','description':'','resource_id':'','bounds':[0,70,20,80]}], 'video_bounds':[0,10,100,65], 'at':'2026-11-07T10:00:00+09:00'}
        first_comments=dict(page,nodes=[{'package':'tv.danmaku.bili','text':'置顶：邀请码领福利','in_comment_list':True}])
        next_comments=dict(page,at='2026-11-07T10:00:02+09:00',nodes=first_comments['nodes']+[
            {'package':'tv.danmaku.bili','text':'哈欠这东西，还带隔空传染的','in_comment_list':True}])
        moved=json.loads(json.dumps(feed))
        moved['nodes'].insert(0,{'package':'tv.danmaku.bili','text':'推荐','description':'','resource_id':'','bounds':[0,0,10,10]})
        moved['nodes'][2]['bounds']=[10,42,90,52]
        other_tab={'nodes':[{'package':'tv.danmaku.bili','text':'首页','description':'','resource_id':'','bounds':[0,90,20,100]}]}
        image=io.BytesIO();Image.new('RGB',(100,100),'blue').save(image,format='PNG')
        screen={'width':100,'height':100,'at':page['at'],'image':'data:image/png;base64,'+base64.b64encode(image.getvalue()).decode()}
        for audio_status, origin in ((a, o) for a in ('ok','unreliable','unavailable') for o in ('automatic','autonomy')):
            run_root=self.root/(audio_status + '-' + origin)
            def assert_paused_before_analysis():
                events=browse.view(run_root)['jobs'][0]['events']
                self.assertEqual(3,sum(e['kind']=='frame' for e in events))
                self.assertTrue(any(e.get('action')=='pause' for e in events),
                                '采样后必须先暂停，不能让转写/看图/评论分析延长播放')
            def transcribe(*args):
                assert_paused_before_analysis()
                return {'status':audio_status,'transcript':'喵，醒啦','processed_seconds':30}
            def describe(*args, **kwargs):
                assert_paused_before_analysis()
                self.assertIs(kwargs.get('video_samples'), True)
                return {'status':'ok','observation':{'subject':'猫张嘴'}}
            def learn_comments(*args, **kwargs):
                assert_paused_before_analysis()
                self.assertEqual([n['text'] for n in args[0]['nodes']], ['置顶：邀请码领福利','哈欠这东西，还带隔空传染的'])
                self.assertEqual([p['at'] for p in args[0]['captures']], [page['at'],next_comments['at']])
                kwargs["complete"]([{"role":"user","content":"虚构提炼"}])
                return {'id':'draft','cards':[{'id':'one'}]}
            def review_comments(*args, **kwargs):
                kwargs["complete"]([{"role":"user","content":"虚构复核"}])
                return {'cards':[{'enabled':True}]}
            def fake_send(*args):
                return 200,json.dumps({'choices':[{'message':{'content':next(decisions)}}]}).encode()
            fake_config={'provider':'fake','model':'fake','base_url':'http://example.invalid/v1','key_env':'FAKE_KEY',
                         'timeout_ms':1000,'retries':0,'temperature':0,'max_tokens':100}
            with patch.dict('os.environ',{'QQAGENT_TRACE':'1','QQAGENT_HOME_ROOT':str(run_root)}), \
                 patch.object(browse.llm,'load_config',return_value=fake_config),patch.object(browse.llm,'read_key',return_value='fake'), \
                 patch.object(browse.llm,'default_transport',side_effect=fake_send), \
                 patch.object(browse._STOP,'wait',return_value=False), patch.object(browse.phone,'inspect',side_effect=[other_tab,feed,moved]+[page]*4+[first_comments,next_comments,next_comments]), \
                 patch.object(browse.phone,'screen',return_value=screen), patch.object(browse.phone,'act',return_value={'state':'command_accepted'}) as act, \
                 patch.object(browse.vision,'load_config',return_value={'enabled':True}), patch.object(browse.vision,'read_key',return_value='test'), \
                 patch.object(browse.vision,'describe',side_effect=describe), \
                 patch.object(browse.phone_audio,'start',return_value=None if audio_status=='unavailable' else 'recorder'), \
                 patch.object(browse.phone_audio,'finish',side_effect=transcribe) as finish, \
                 patch.object(browse.phone_audio,'stop') as audio_stop, \
                 patch.object(browse.expression_learning,'learn',side_effect=learn_comments) as learn, \
                 patch.object(browse.expression_learning,'review',side_effect=review_comments) as review:
                decisions=iter(['{"node":0,"reason":"想看猫"}','{"node":1,"reason":"猫的动作有意思"}','{}',
                                '{"effect":"positive","impression":"这个哈欠有点可爱","evidence":"猫张嘴"}','{}'])
                browse.start(persona='喜欢猫',root=run_root,now='2026-11-07T10:00:00+09:00',origin=origin)
                browse._FUTURE.result(timeout=3)
            job=browse.view(run_root)['jobs'][0]
            self.assertEqual(job['status'],'done',job)
            traces=[json.loads(line) for p in (run_root/'mind/trace').glob('*.jsonl') for line in p.read_text().splitlines()]
            self.assertEqual([r['call_index'] for r in traces],list(range(1,6)))
            self.assertEqual({r['turn_id'] for r in traces},{job['model_turn_id']})
            self.assertEqual([r['purpose'] for r in traces],['决定是否浏览手机','选择推荐视频','提炼评论表达','形成片段感想','复核评论表达'])
            self.assertEqual(len([e for e in job['events'] if e['kind']=='frame']),3)
            with Image.open(job['frame_file']) as frames:
                self.assertEqual((100,55),frames.size,'只截播放器，不能把下方广告作为剧情')
            self.assertEqual([c.args[0] for c in act.call_args_list],['wake','bilibili','tap','tap','pause','tap','swipe','swipe','pause','home'])
            self.assertEqual(act.call_args_list[2].kwargs['y'],.95,'打开B站可能停在上次标签，先回已观察的首页')
            self.assertEqual(act.call_args_list[3].kwargs['y'],.26,'用重新定位后的封面区域，不点击可能被底栏遮住的标题')
            self.assertIn('猫打哈欠',learn.call_args.kwargs['context'])
            self.assertIn('猫张嘴',learn.call_args.kwargs['viewed_clip'])
            self.assertEqual('喵，醒啦' in learn.call_args.kwargs['viewed_clip'],audio_status=='ok')
            self.assertIn('猫张嘴',browse.context('2026-11-07T12:00:00+09:00',root=run_root))
            self.assertEqual('喵，醒啦' in browse.context('2026-11-07T12:00:00+09:00',root=run_root),audio_status=='ok')
            self.assertEqual(audio_status,job['audio']['status'])
            self.assertTrue(job['reflection']['mood']['ok'])
            self.assertIn('这个哈欠有点可爱',browse.context('2026-11-07T12:00:00+09:00',root=run_root))
            if audio_status=='unavailable':
                audio_stop.assert_not_called()
                finish.assert_not_called()
            else:
                audio_stop.assert_called_once_with('recorder')
            review.assert_called_once()
            self.assertEqual(job['expression_review']['adopted'],1)
            event_kinds=[e.get('action') or e['kind'] for e in job['events']]
            self.assertLess(event_kinds.index('home'),event_kinds.index('expression_review'))

    def test_failed_screen_read_does_not_create_viewed_experience(self):
        self.mock_feeling()
        with patch.object(browse._STOP,'wait',return_value=False), patch.object(browse.phone,'act',return_value={}), \
             patch.object(browse.phone,'inspect',side_effect=ValueError('离线')):
            browse.start(persona='她',root=self.root,now='2026-11-07T10:00:00+09:00',complete=lambda messages:'{"node":0,"reason":"想看"}')
            browse._FUTURE.result(timeout=3)
        self.assertEqual(browse.view(self.root)['jobs'][0]['status'],'failed')
        self.assertEqual(browse.context('2026-11-07T12:00:00+09:00',root=self.root),'')

    def test_stop_after_audio_start_closes_recorder_without_transcribing(self):
        self.mock_feeling()
        feed={'nodes':[{'package':'tv.danmaku.bili','text':'','description':'视频,猫','resource_id':'','bounds':[0,10,100,65]},
                       {'package':'tv.danmaku.bili','text':'猫','description':'','resource_id':'tv.danmaku.bili:id/title','bounds':[10,40,90,50]}]}
        page={'nodes':[{'package':'tv.danmaku.bili','text':'简介','bounds':[0,70,20,80]}], 'video_bounds':[0,10,100,65]}
        def start_audio(*args):
            browse.stop()
            return 'recorder'
        with patch.object(browse._STOP,'wait',return_value=False),patch.object(browse.phone,'inspect',side_effect=[feed,feed,page]), \
             patch.object(browse.phone,'screen',return_value={'width':100,'height':100}),patch.object(browse.phone,'act',return_value={}), \
             patch.object(browse.phone_audio,'start',side_effect=start_audio),patch.object(browse.phone_audio,'finish') as finish, \
             patch.object(browse.phone_audio,'stop') as stop:
            decisions=iter(['{"node":0,"reason":"想看"}','{"node":1,"reason":"猫"}'])
            browse.start(persona='她',root=self.root,now='2026-11-07T10:00:00+09:00',complete=lambda _:next(decisions))
            browse._FUTURE.result(timeout=3)
        finish.assert_not_called();stop.assert_called_once_with('recorder')
        self.assertEqual('stopped',browse.view(self.root)['jobs'][0]['audio']['status'])

    def test_interrupted_jobs_do_not_claim_they_are_still_running(self):
        job={'id':'a'*32,'created_at':'2026-11-07T10:00:00+09:00','status':'running','events':[]}
        browse.save(job,self.root)
        self.assertEqual(browse.view(self.root)['jobs'][0]['status'],'interrupted')

    def test_current_day_context_excludes_failed_and_unobserved_titles(self):
        for i,status,frames in ((1,'done',[{'status':'ok','observation':{'subject':'猫打哈欠'}}]),(2,'failed',[]),(3,'done',[]),
                               (4,'partial',[{'status':'ok','observation':{'subject':'猫伸懒腰'}}])):
            browse.save({'id':str(i)*32,'created_at':'2026-11-07T10:00:00+09:00','status':status,'events':[],
                         'title':'title'+str(i),'observations':frames},self.root)
        context=browse.context('2026-11-07T12:00:00+09:00',root=self.root)
        self.assertIn('title1',context)
        self.assertNotIn('title2',context)
        self.assertNotIn('title3',context)
        self.assertIn('title4',context,'评论学习失败不能抹掉已经观察到的画面')
        self.assertIn('没有听取音轨',context)

    def test_verified_video_source_reaches_chat_and_growth_without_guessing(self):
        from organs.day import growth
        growth.configure(True,root=self.root)
        job={'id':'source-test','created_at':'2026-11-07T10:00:00+09:00','status':'done',
             'origin':'autonomy','scope':'public','title':'小猫与纸箱','events':[],
             'observations':[{'status':'ok','observation':{'subject':'小猫在纸箱旁'}}]}
        for platform,url in (('bilibili','https://b23.tv/TestClip'),('douyin','https://v.douyin.com/TestClip/')):
            for source in ({'status':'ok','url':url},{'status':'unavailable','url':url},{}):
                with self.subTest(platform=platform,source=source):
                    browse.save(dict(job,platform=platform,video_source=source),self.root)
                    before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                    expected=url if source.get('status')=='ok' else None
                    row=browse.experiences('2026-11-07T12:00:00+09:00',root=self.root)[0]
                    self.assertEqual(expected,row.get('source_url'))
                    context=browse.context('2026-11-07T12:00:00+09:00',root=self.root)
                    self.assertEqual(bool(expected),url in context)
                    materials=growth.materials(None,'2026-11-07T12:00:00+09:00',root=self.root)
                    self.assertEqual(expected,materials[0].get('source_url'))
                    self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        for excluded in ({'scope':'private'},{'origin':'acceptance'}):
            browse.save(dict(job,video_source={'status':'ok','url':url},**excluded),self.root)
            self.assertEqual('',browse.context('2026-11-07T12:00:00+09:00',root=self.root))
            self.assertEqual([],growth.materials(None,'2026-11-07T12:00:00+09:00',root=self.root))
        growth.configure(False,root=self.root)
        for scope in ('private',None):
            browse.save(dict(job,scope=scope,video_source={'status':'ok','url':url}),self.root)
            self.assertNotIn('source_url',browse.experiences('2026-11-07T12:00:00+09:00',root=self.root)[0])
            self.assertNotIn(url,browse.context('2026-11-07T12:00:00+09:00',root=self.root))

    def test_unclear_audio_and_acceptance_never_become_heard_dialogue(self):
        job={'id':'a'*32,'created_at':'2026-11-07T10:00:00+09:00','status':'done','title':'测试视频','events':[],
             'observations':[{'status':'ok','observation':{'subject':'角色张嘴'}}],
             'audio':{'status':'unclear','transcript':'不可靠原句','raw_transcript':'字幕by某人','reason':'未取得可用对白'}}
        browse.save(job,self.root)
        context=browse.context('2026-11-07T12:00:00+09:00',root=self.root)
        self.assertIn('未取得可用对白',context)
        self.assertNotIn('不可靠原句',context);self.assertNotIn('字幕by',context)
        job['origin']='acceptance';browse.save(job,self.root)
        self.assertEqual('',browse.context('2026-11-07T12:00:00+09:00',root=self.root))

    def test_recent_experience_survives_day_cutover_without_writing(self):
        browse.save({'id':'yesterday','created_at':'2026-11-07T23:00:00+09:00',
                     'status':'partial','origin':'automatic','title':'猫与纸箱',
                     'observations':[{'status':'ok','observation':'猫坐在纸箱旁，没有看到结尾'}]},self.root)
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        context=browse.context('2026-11-08T08:00:00+09:00',root=self.root)
        self.assertIn('猫与纸箱',context)
        self.assertIn('2026-11-07T23:00:00+09:00',context)
        self.assertIn('2026-11-08T08:00:00+09:00',context)
        self.assertIn('partial',context)
        self.assertNotIn('[今天实际手机浏览]',context)
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_nonverbal_sound_survives_into_experiences_without_failed_transcript(self):
        sounds={'status':'ok','source':'local_yamnet','processed_seconds':4,
                'events':[{'label':'音乐','score':.8}]}
        browse.save({'id':'music-clip','created_at':'2026-11-07T10:00:00+09:00','status':'done',
                     'title':'窗边','observations':[{'status':'ok','observation':{'subject':'一扇窗'}}],
                     'audio':{'status':'unclear','transcript':'未听清的猜测','sound_events':sounds}},self.root)
        row=browse.experiences('2026-11-07T12:00:00+09:00',root=self.root)[0]
        self.assertEqual(row['audio']['sound_events'],sounds)
        text=browse.context('2026-11-07T12:00:00+09:00',root=self.root)
        self.assertIn('音乐',text)
        self.assertNotIn('未听清的猜测',text)
        self.assertIn('可能线索',text)

    def test_recent_context_filters_before_limiting_and_orders_actual_time(self):
        # 首页列表只展示20条；验收/失败记录不应挤走仍可用的三条经历。
        for index in range(22):
            browse.save({'id':f'test-{index}','created_at':'2026-11-08T11:00:00+09:00',
                         'status':'done','origin':'acceptance','title':'验收素材',
                         'observations':[{'status':'ok','observation':'测试'}]},self.root)
        for name,at,status in (
            ('旧片','2026-11-06T23:00:00+09:00','done'),
            ('昨晚','2026-11-07T23:00:00+09:00','done'),
            ('早晨','2026-11-07T23:30:00Z','stopped'),
            ('上午','2026-11-08T10:00:00+09:00','partial'),
            ('未来','2026-11-08T13:00:00+09:00','done'),
            ('失败','2026-11-08T11:00:00+09:00','failed')):
            browse.save({'id':name,'created_at':at,'status':status,'origin':'automatic',
                         'title':name,'observations':[{'status':'ok','observation':name}]},self.root)
        context=browse.context('2026-11-08T12:00:00+09:00',root=self.root)
        for name in ('上午','早晨','昨晚'):
            self.assertIn(name,context)
        for name in ('旧片','未来','失败','验收素材'):
            self.assertNotIn(name,context)
        self.assertLess(context.index('上午'),context.index('早晨'))
        self.assertLess(context.index('早晨'),context.index('昨晚'))

    def test_topic_recall_uses_content_and_preserves_filters_and_original_records(self):
        for index, subject in enumerate(('旧片细节', '海獭抱着贝壳', '蓝色圆圈', '沿街散步'), 1):
            browse.save({'id': str(index), 'created_at': f'2026-11-07T{index+9}:00:00+09:00',
                         'status': 'done', 'origin': 'automatic', 'title': '视频',
                         'observations': [{'status': 'ok', 'observation': {'subject': subject}}],
                         'audio': {'status': 'unclear', 'transcript': '不可靠海獭抱着贝壳', 'reason': '机器转写失败'}}, self.root)
        # 较老的内容分别只存在于有效感想、可靠音轨、标题中。
        first = browse.view(self.root, limit=None)['jobs'][-1]
        first.update(title='灯塔纪录片',
                     audio={'status': 'partial', 'transcript': '一只鲸鱼游过来了'},
                     reflection={'status': 'ok', 'at': first['created_at'], 'effect': 'positive',
                                 'impression': '小船摇晃得很有趣', 'evidence': '旧片细节'})
        browse.save(first, self.root)
        for ident, status, origin, at, observations in (
            ('acceptance', 'done', 'acceptance', '2026-11-07T14:00:00+09:00', [{'status':'ok','observation':'灯塔纪录片'}]),
            ('failed', 'failed', 'automatic', '2026-11-07T14:00:00+09:00', [{'status':'ok','observation':'灯塔纪录片'}]),
            ('future', 'done', 'automatic', '2026-11-09T14:00:00+09:00', [{'status':'ok','observation':'灯塔纪录片'}]),
            ('unseen', 'done', 'automatic', '2026-11-07T14:00:00+09:00', [])):
            browse.save({'id': ident, 'status': status, 'origin': origin, 'created_at': at,
                         'title': '灯塔纪录片', 'observations': observations}, self.root)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for query in ('海獭抱着什么', '那部灯塔纪录片', '一只鲸鱼', '小船摇晃', '不相关问候', '机器转写失败', 'subject'):
            context = browse.context('2026-11-08T12:00:00+09:00', root=self.root, query=query)
            rows = json.loads(context.split('\n')[-1])
            expected = ['4', '3', '1'] if query in ('那部灯塔纪录片', '一只鲸鱼', '小船摇晃') else ['4', '3', '2']
            self.assertEqual(expected, [row['id'] for row in rows], query)
            self.assertNotIn('不可靠海獭', context)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_reflection_failure_stop_and_acceptance_do_not_change_mood(self):
        self.mock_feeling()
        feed={'nodes':[{'package':'tv.danmaku.bili','text':'','description':'视频,猫',
                        'resource_id':'','bounds':[0,10,100,65]},
                       {'package':'tv.danmaku.bili','text':'猫','description':'',
                        'resource_id':'tv.danmaku.bili:id/title','bounds':[10,40,90,50]}]}
        page={'nodes':[{'package':'tv.danmaku.bili','text':'简介'}],
              'video_bounds':[0,10,100,65],'at':'2026-11-07T10:00:00+09:00'}
        image=io.BytesIO();Image.new('RGB',(100,100),'blue').save(image,format='PNG')
        screen={'width':100,'height':100,'at':page['at'],
                'image':'data:image/png;base64,'+base64.b64encode(image.getvalue()).decode()}
        def reflect(*args,**kwargs):
            if mode=='failed':raise ValueError('感想没有观察依据')
            if mode=='stopped':browse.stop()
            return {'effect':'positive','impression':'挺好玩','evidence':'猫坐着'}
        def act(action, **kwargs):
            if mode == 'rest_cleanup' and action == 'home':
                browse.rest.advance(self.root/'heart', '2026-11-07T23:00:00+09:00')
            return {}
        for mode in ('failed','stopped','acceptance','rest_cleanup'):
            with self.subTest(mode=mode),patch.object(browse._STOP,'wait',return_value=False), \
                 patch.object(browse.phone,'inspect',side_effect=[feed,feed]+[page]*5), \
                 patch.object(browse.phone,'screen',return_value=screen),patch.object(browse.phone,'act',side_effect=act), \
                 patch.object(browse.vision,'load_config',return_value={'enabled':True}),patch.object(browse.vision,'read_key',return_value='test'), \
                 patch.object(browse.vision,'describe',return_value={'status':'ok','observation':'猫坐着'}), \
                 patch.object(browse.phone_reflection,'reflect',side_effect=reflect) as reflection, \
                 patch.object(browse.mood,'apply_experience') as apply:
                decisions=iter(['{"node":0,"reason":"想看看"}','{"node":1,"reason":"想看猫"}'])
                created=browse.start(persona='喜欢猫',root=self.root,now=page['at'],
                    origin='acceptance' if mode=='acceptance' else 'automatic',complete=lambda _:next(decisions))
                browse._FUTURE.result(timeout=3)
                apply.assert_not_called()
                job=next(j for j in browse.view(self.root)['jobs'] if j['id']==created['id'])
                self.assertEqual('stopped' if mode in ('stopped','rest_cleanup') else 'done',job['status'],job)
                if mode == 'rest_cleanup': reflection.assert_not_called()
                self.assertEqual('猫坐着',job['observations'][0]['observation'])
                if mode=='failed':self.assertEqual('failed',job['reflection']['status'])
                elif mode=='acceptance':self.assertEqual('acceptance',job['reflection']['mood']['skipped'])
                else:self.assertNotIn('reflection',job)
