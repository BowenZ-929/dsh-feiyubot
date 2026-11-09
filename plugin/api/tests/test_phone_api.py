import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api import api


class PhoneApiTest(unittest.TestCase):
    def test_douyin_video_and_comment_regions_exclude_fixed_input(self):
        from organs.hand import phone
        from organs.mind import expression_learning
        xml = '''<hierarchy>
        <node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/viewpager" content-desc="视频" bounds="[0,0][1080,2135]"/>
        <node resource-id="com.ss.android.ugc.aweme:id/x9g" scrollable="true">
          <node scrollable="true"><node package="com.ss.android.ugc.aweme" text="孩子：我去 隐藏款" bounds="[10,900][900,1000]"/></node>
          <node package="com.ss.android.ugc.aweme" text="发条评论" bounds="[0,2100][1080,2200]"/>
        </node></hierarchy>'''.encode()
        with patch('adapters.adb.hierarchy', return_value=xml):
            snapshot = phone.inspect()
        self.assertEqual([0,0,1080,2135], snapshot['video_bounds'])
        self.assertEqual([False,True,False], [n['in_comment_list'] for n in snapshot['nodes']])
        calls=[]
        expression_learning.learn(snapshot, context='抖音公开评论', root=self.root,
            complete=lambda messages: calls.append(messages) or '[]')
        self.assertEqual(['孩子：我去 隐藏款'], [n['text'] for n in json.loads(calls[0][1]['content'])['nodes']])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.dict('os.environ', {'QQAGENT_HOME': str(self.root)})
        patcher.start(); self.addCleanup(patcher.stop)
        (self.root / 'hand').mkdir()
        (self.root / 'hand/phone.json').write_text(json.dumps({'serial': '192.168.1.2:34567', 'adb_path': '/test/adb'}))

    def call(self, method, path, body=None):
        return api.handle(method, '/phone/' + path, body, None)

    def test_phone_status_route_exists(self):
        # 未配置的环境不应启动硬件连接。
        (self.root / 'hand/phone.json').unlink()
        status, data = self.call('GET', 'status')
        self.assertEqual(status, 200)
        self.assertFalse(data['connected'])
        self.assertEqual(data['state'], 'unconfigured')

    def test_internal_clipboard_uses_bound_device_and_does_not_add_an_api_action(self):
        from organs.hand import phone
        with patch('adapters.adb.run', return_value='【猫-哔哩哔哩】 https://b23.tv/X6f5iyP'.encode()) as run:
            phone.clipboard('')
            self.assertEqual(['192.168.1.2:34567',''],run.call_args.args[0][-2:])
            self.assertIn('set_clipboard',run.call_args.args[0][1])
            self.assertIn('https://b23.tv/X6f5iyP',phone.clipboard())
            self.assertEqual('192.168.1.2:34567',run.call_args.args[0][-1])
            count=run.call_count
            self.assertEqual(400,self.call('POST','action',{'action':'clipboard'})[0])
            self.assertEqual(count,run.call_count)

    def test_browse_routes_read_settings_stop_and_block_manual_actions(self):
        from organs.day import phone_browse
        self.assertFalse(self.call('GET','browse')[1]['settings']['enabled'])
        self.assertEqual(self.call('POST','browse/settings',{'enabled':'yes'})[0],400)
        self.assertEqual(self.call('POST','browse/start',{})[0],400,'缺魂时不启动')
        self.assertEqual(self.call('POST','browse/stop',{})[1]['status'],'idle')
        with patch.object(phone_browse,'_ACTIVE','running'), patch('adapters.adb.run') as run:
            self.assertEqual(self.call('POST','action',{'action':'home'})[0],400)
            self.assertEqual(self.call('POST','connect',{'serial':'x'})[0],400)
            run.assert_not_called()
        self.assertEqual(self.call('POST','action',{'action':'home','actor':'bot'})[0],400)

    def runner(self, args, **kw):
        if args == ['devices', '-l']:
            return b'List of devices attached\n192.168.1.2:34567 device product:fuxi model:2211133C\n'
        if args[-2:] == ['getprop', 'ro.product.model']: return b'2211133C\n'
        if args[-2:] == ['getprop', 'ro.build.version.release']: return b'15\n'
        return b''

    def test_connect_verifies_device_not_just_success_text_and_saves_selection(self):
        with patch('adapters.adb.run', side_effect=self.runner) as run:
            status, data = self.call('POST', 'connect', {'serial':'192.168.1.2:34567'})
        self.assertEqual(status, 200)
        self.assertTrue(data['connected'])
        self.assertEqual(data['model'], '2211133C')
        self.assertEqual(run.call_args_list[0].args[0], ['connect','192.168.1.2:34567'])
        with patch('adapters.adb.run', return_value=b'List of devices attached\nother device model:Other\n'):
            status, data = self.call('GET', 'status')
        self.assertFalse(data['connected'])
        self.assertEqual(data['state'], 'disconnected')

    def test_bad_targets_and_unknown_actions_never_execute(self):
        with patch('adapters.adb.run') as run:
            for serial in ('--help','host;touch /tmp/x','192.168.1.2:99999',''):
                self.assertEqual(self.call('POST','connect',{'serial':serial})[0],400)
            self.assertEqual(self.call('POST','action',{'action':'shell','command':'id'})[0],400)
            run.assert_not_called()

    def test_screenshot_returns_image_but_does_not_publish_or_write_memory(self):
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5p0AAAAASUVORK5CYII=')
        with patch('adapters.adb.run', return_value=png) as run:
            status, data = self.call('POST','screen',{})
        self.assertEqual(status,200)
        self.assertEqual(base64.b64decode(data['image'].split(',',1)[1]), png)
        self.assertEqual((data['width'], data['height']), (1,1))
        self.assertEqual(run.call_args.args[0], ['-s','192.168.1.2:34567','exec-out','screencap','-p'])
        self.assertFalse((self.root/'brain').exists())

    def test_action_uses_bound_device_and_records_receipt(self):
        with patch('adapters.adb.run', return_value=b'') as run:
            status, data = self.call('POST','action',{'action':'home'})
        self.assertEqual(status,200)
        self.assertEqual(data['state'],'command_accepted')
        self.assertEqual(run.call_args.args[0], ['-s','192.168.1.2:34567','shell','input','keyevent','KEYCODE_HOME'])
        rows=[json.loads(x) for x in (self.root/'hand/ledger.jsonl').read_text().splitlines()]
        self.assertEqual([r['state'] for r in rows], ['reserved','done'])
        self.assertEqual(rows[0]['ledger_id'],rows[1]['ledger_id'])

    def test_timeout_is_reported_and_not_retried(self):
        from adapters.adb import AdbError
        with patch('adapters.adb.run', side_effect=AdbError('操作超时，未确认')) as run:
            status, data=self.call('POST','action',{'action':'home'})
        self.assertEqual(status,503)
        self.assertIn('未确认',data['message'])
        self.assertEqual(run.call_count,1)
        rows=[json.loads(x) for x in (self.root/'hand/ledger.jsonl').read_text().splitlines()]
        self.assertEqual(rows[-1]['state'],'failed')

    def test_unauthorized_target_is_not_replaced_by_other_online_device(self):
        with patch('adapters.adb.run', return_value=b'List of devices attached\n192.168.1.2:34567 unauthorized\nother device model:Other\n'):
            _, data = self.call('GET','status')
        self.assertEqual(data['state'], 'unauthorized')
        self.assertFalse(data['connected'])

    def test_invalid_screenshot_and_unresolved_app_are_errors(self):
        with patch('adapters.adb.run', return_value=b'not a screenshot'):
            self.assertEqual(self.call('POST','screen',{})[0],503)
        with patch('adapters.adb.run', return_value=b'Error: Activity not started, unable to resolve Intent'):
            self.assertEqual(self.call('POST','action',{'action':'bilibili'})[0],503)

    def test_reading_does_not_execute_actions_and_write_method_is_required(self):
        with patch('adapters.adb.run') as run:
            for path in ('action','connect','screen'):
                self.assertEqual(self.call('GET',path)[0],405)
            run.assert_not_called()

    def test_adapter_timeout_has_explicit_uncertain_result(self):
        import subprocess
        from adapters import adb
        with patch('adapters.adb.subprocess.run', side_effect=subprocess.TimeoutExpired('adb',8)) as run:
            with self.assertRaisesRegex(adb.AdbError,'未确认'):
                adb.run(['devices','-l'],executable='/test/adb')
        run.assert_called_once_with(['/test/adb','devices','-l'],capture_output=True,timeout=8)

    def test_missing_lock_state_is_not_treated_as_unlocked(self):
        from organs.hand import phone
        from adapters.adb import AdbError
        with patch('adapters.adb.run',return_value=b'mAwake=true'):
            with self.assertRaises(AdbError): phone.locked()
        with patch('adapters.adb.run',return_value=b'isKeyguardShowing=true'):
            self.assertTrue(phone.locked())
        with patch('adapters.adb.run',return_value=b'isKeyguardShowing=false'):
            self.assertFalse(phone.locked())

    def test_inspect_route_exists(self):
        (self.root / 'hand/phone.json').unlink()
        status, data = self.call('POST', 'inspect', {})
        self.assertEqual(status,400)
        self.assertIn('连接',data['message'])

    def test_inspection_keeps_app_content_and_decodes_entities(self):
        xml = b'<hierarchy><node package="tv.danmaku.bili" text="A &amp; B" bounds="[10,20][50,80]" clickable="true"/><node package="com.android.systemui" text="private notification" bounds="[0,0][100,20]"/><node package="tv.danmaku.bili" text="hidden" visible-to-user="false" bounds="[0,0][100,20]"/><node package="tv.danmaku.bili" text="secret" password="true" bounds="[0,0][100,20]"/><node package="tv.danmaku.bili" content-desc="play" bounds="[1,2][3,4]"/></hierarchy>'
        with patch('adapters.adb.hierarchy', return_value=xml):
            status,data=self.call('POST','inspect',{})
        self.assertEqual(status,200)
        self.assertEqual([n['text'] or n['description'] for n in data['nodes']],['A & B','play'])
        self.assertEqual(data['nodes'][0]['bounds'],[10,20,50,80])
        self.assertEqual(data['packages'],['tv.danmaku.bili'])

    def test_video_bounds_use_actual_player_layer_not_page_or_ad(self):
        xml=b'<hierarchy><node package="tv.danmaku.bili" resource-id="tv.danmaku.bili:id/video_container" bounds="[0,104][1080,2270]"><node package="tv.danmaku.bili" resource-id="tv.danmaku.bili:id/video_area_float_layer_container" bounds="[0,104][1080,712]"/><node package="tv.danmaku.bili" resource-id="tv.danmaku.bili:id/ad_tint_frame" text="advert" bounds="[0,800][1080,1000]"/></node></hierarchy>'
        with patch('adapters.adb.hierarchy',return_value=xml):
            _,data=self.call('POST','inspect',{})
        self.assertEqual([0,104,1080,712],data.get('video_bounds'))

    def test_tap_uses_actual_screen_dimensions_and_swipe_is_directional(self):
        with patch('organs.hand.phone.screen', return_value={'width':1080,'height':2400}), patch('adapters.adb.run',return_value=b'') as run:
            self.assertEqual(self.call('POST','action',{'action':'tap','x':0.5,'y':0.25})[0],200)
            self.assertEqual(run.call_args.args[0][-4:],['input','tap','540','600'])
            self.assertEqual(self.call('POST','action',{'action':'swipe','direction':'up'})[0],200)
            self.assertEqual(run.call_args.args[0][-7:],['input','swipe','540','1728','540','672','350'])

    def test_comment_list_is_distinguished_from_fixed_footer(self):
        from organs.hand import phone
        xml=b'<hierarchy><node scrollable="true" class="ViewPager"><node resource-id="tv.danmaku.bili:id/comment_container"><node scrollable="true"><node package="tv.danmaku.bili" content-desc="comment" bounds="[0,10][100,20]"/></node><node package="tv.danmaku.bili" text="input hint" class="android.widget.TextView" bounds="[0,90][100,100]"/></node></node></hierarchy>'
        with patch('adapters.adb.hierarchy',return_value=xml):
            rows=phone.inspect()['nodes']
        self.assertTrue(rows[0]['in_scrollable'])
        self.assertFalse(rows[1]['in_scrollable'])
        self.assertTrue(rows[0]['in_comment_list'])
        self.assertFalse(rows[1]['in_comment_list'])

    def test_scrollable_recommendations_are_not_comment_list(self):
        from organs.hand import phone
        xml=b'<hierarchy><node scrollable="true"><node package="tv.danmaku.bili" resource-id="tv.danmaku.bili:id/title" text="video title" bounds="[0,10][100,20]"/></node></hierarchy>'
        with patch('adapters.adb.hierarchy',return_value=xml):
            row=phone.inspect()['nodes'][0]
        self.assertTrue(row['in_scrollable'])
        self.assertFalse(row['in_comment_list'])

    def test_hierarchy_to_learning_keeps_only_comment_list_with_original_index(self):
        from organs.hand import phone
        from organs.mind import expression_learning
        xml=b'<hierarchy><node scrollable="true"><node package="tv.danmaku.bili" text="recommendation" bounds="[0,0][100,10]"/><node resource-id="tv.danmaku.bili:id/comment_container"><node scrollable="true"><node package="tv.danmaku.bili" text="a real comment" bounds="[0,20][100,30]"/><node package="other.app" text="foreign" bounds="[0,30][100,40]"/><node package="tv.danmaku.bili" text="editor" class="android.widget.EditText" bounds="[0,40][100,50]"/></node><node package="tv.danmaku.bili" text="footer hint" bounds="[0,90][100,100]"/></node></node></hierarchy>'
        with patch('adapters.adb.hierarchy',return_value=xml):
            snapshot=phone.inspect()
        self.assertFalse(snapshot['nodes'][2]['in_comment_list'])
        sent=[]
        card={'node':1,'quote':'a real comment','phrase':'real comment',
              'meaning':'test','when':'test','avoid':'test'}
        result=expression_learning.learn(snapshot,context='public comments',root=self.root/'house',
            complete=lambda messages:sent.append(messages) or json.dumps([card]))
        nodes=json.loads(sent[0][1]['content'])['nodes']
        self.assertEqual([1],[n['node'] for n in nodes])
        self.assertEqual('a real comment',result['cards'][0]['quote'])

    def test_invalid_gestures_do_not_read_screen_or_move_phone(self):
        with patch('adapters.adb.run') as run:
            for body in ({'action':'tap','x':-0.1,'y':0.2},{'action':'tap','x':True,'y':0.2},
                         {'action':'tap','x':0.2,'y':float('nan')},{'action':'tap','x':1.1,'y':0.2},
                         {'action':'swipe','direction':'left'},{'action':'home','x':0.2}):
                self.assertEqual(self.call('POST','action',body)[0],400)
            run.assert_not_called()
