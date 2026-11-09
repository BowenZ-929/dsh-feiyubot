import tempfile
import unittest
from pathlib import Path
from organs.brain.tests.helpers import fresh_store
from organs.soul import load


class GrowthPersonaTest(unittest.TestCase):
    def test_server_root_mapping_connects_completed_cross_day_work_to_chat(self):
        import json
        from unittest.mock import patch
        from api import roots
        from organs.day import autonomy, growth, solitude
        from organs.home import intentions, store as home_store
        from organs.mind import conversation
        from organs.mind.tests.test_note_display import NoteDisplayTest, CONFIG, ENV, _transport
        fixture=NoteDisplayTest(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        mapping=roots.export_env()
        Path(mapping['HOME_ROOT']).mkdir(parents=True)
        self.assertEqual(solitude._home_root(),home_store.home_root())
        growth.configure(True)
        plan=intentions.apply({'name':'两行纸船小诗','next_step':'把第二行写完'})
        with patch('organs.day.phone_browse._feeling',return_value={'asleep':False,'energy':{'value':80}}), \
             patch('organs.day.phone_browse.availability',return_value={'available':False}), \
             patch('organs.hand.comfy.config',return_value={'enabled':False}):
            first=autonomy.tick('2026-11-08T12:00:00+09:00',brain=fixture.brain,
                complete=lambda _:json.dumps({'action':'write','reason':'先写第一行','args':{
                    'text':'纸船在窗边停了一夜。','intention_id':plan['card_id']}}))
            complete_text='纸船在窗边停了一夜，\n晨光替它把水面铺开。'
            def finish(messages):
                packet=json.loads(messages[1]['content'])
                self.assertEqual(first['receipt']['text'],packet['writings'][0]['text'])
                return json.dumps({'action':'write','reason':'把小诗接完','args':{
                    'text':complete_text,'intention_id':plan['card_id'],
                    'based_on':first['receipt']['card_id'],'finish':True}})
            second=autonomy.tick('2026-11-09T08:05:00+09:00',brain=fixture.brain,complete=finish)
        self.assertEqual('done',second['status'])
        finished=intentions.listing()[0]
        self.assertEqual('done',finished['status'])
        self.assertEqual([second['receipt']['card_id']],finished['sources'])
        captured=[]
        conversation.answer('昨天那首纸船小诗后来怎么样了？','12345678',store=fixture.store,brain=fixture.brain,
            now='2026-11-09T08:10:00+09:00',scene='private',session_id='qq:private:12345678',
            recent_root=fixture.tmp/'recent',transport=_transport('写完了。',captured),config=CONFIG,env=ENV)
        prompt=captured[-1]['messages'][0]['content']
        self.assertIn('晨光替它把水面铺开',prompt)
        self.assertIn('"status": "done"',prompt)
        self.assertIn(first['receipt']['card_id'],prompt)
        self.assertIn(second['receipt']['card_id'],prompt)

    def test_same_running_brain_uses_growth_edit_clear_and_rollback_in_next_chat(self):
        import json
        import os
        from unittest.mock import patch
        from api import day
        from organs.day import growth
        from organs.mind import conversation
        from organs.mind.tests.test_note_display import NoteDisplayTest, CONFIG, ENV, _transport
        fixture = NoteDisplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root, home = fixture.tmp/'day', fixture.tmp/'home'/'home'
        baseline = fixture.store.render()
        original_files = {p:p.read_bytes() for p in (fixture.tmp/'soul').rglob('*') if p.is_file()}
        first = {'likes':'想试试种薄荷', 'dislikes':'不喜欢闹钟刺耳', 'views':'慢慢试也算向前',
                 'self_description':'喜欢安静地观察', 'stance':'先听对方说完再接话',
                 'sleep_notice':'我先睡啦，明天见'}
        with patch.dict(os.environ, {'DAY_ROOT':str(root), 'HOME_ROOT':str(home)}):
            growth.configure(True, root=root)
            reflected = growth.reflect('2026-11-09T03:30:00+09:00', root=root,
                home_root=home, brain=fixture.brain, persona=baseline,
                complete=lambda _: json.dumps({'patch':first,'note':'虚构的自主选择','sources':[]}))
            self.assertEqual('ok', reflected['status'])
            version = reflected['record']['version']
            states = [('grown', first), ('edited', dict(first, likes='', stance='愿意先分享一点自己的想法')),
                      ('rolled_back', first), ('disabled', None)]
            for index, (name, expected) in enumerate(states):
                with self.subTest(state=name):
                    if name in ('edited', 'rolled_back'):
                        body = ({'patch':{'likes':'','stance':expected['stance']},'note':'虚构编辑'}
                                if name == 'edited' else {'version':version,'note':'恢复之前的选择'})
                        code, result = day.dispatch('/day/growth/profile', {'root':str(root)},
                            body, fixture.store, fixture.brain)
                        self.assertEqual(200, code)
                        self.assertEqual(expected, result['profile'])
                    elif name == 'disabled':
                        growth.configure(False, root=root)
                    capture = []
                    conversation.answer('早上好', '12345678', store=fixture.store, brain=fixture.brain,
                        now='2026-11-09T08:%02d:00+09:00' % index, scene='private',
                        session_id='qq:private:12345678', recent_root=fixture.tmp/'recent',
                        transport=_transport('早呀。', capture), config=CONFIG, env=ENV)
                    prompt = capture[-1]['messages'][0]['content']
                    if expected is None:
                        self.assertIn(baseline, prompt)
                        self.assertNotIn('【当前兴趣】', prompt)
                    else:
                        for title, key in [('当前兴趣','likes'),('不喜欢什么','dislikes'),
                                           ('当前看法','views'),('性格底色','self_description'),('待人姿态','stance')]:
                            self.assertIn('【'+title+'】'+expected[key]+'\n', prompt)
                        if name == 'edited':
                            self.assertNotIn(first['likes'], prompt)
                            self.assertNotIn(first['stance'], prompt)
                        elif name == 'rolled_back':
                            self.assertNotIn(states[1][1]['stance'], prompt)
                    self.assertIn(fixture.store.snapshot().identity.appearance, prompt)
                    self.assertIn(fixture.store.snapshot().identity.likes, prompt)
                    self.assertIn('【红线】', prompt)
            self.assertEqual(first, fixture.brain.current_profile())
            self.assertEqual(3, len(fixture.brain.selfnarration_versions()))
        self.assertEqual(original_files, {p:p.read_bytes() for p in original_files})

    def test_chat_recalls_older_public_work_by_topic_after_recent_window_fills(self):
        import os
        import json
        from unittest.mock import patch
        from organs.home import cards
        from organs.day import autonomy, growth
        from organs.mind import conversation
        from organs.mind.tests.test_note_display import NoteDisplayTest, CONFIG, ENV, _transport
        fixture = NoteDisplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        day, home = fixture.tmp/'day', fixture.tmp/'home'/'home'
        story = '纸船飞到月亮上，把银色的潮水装进了口袋。'
        old_activity = '逐笔绘画：失败；画面要求：纸船飞到月亮。没有实际成图。'
        with patch.dict(os.environ, {'DAY_ROOT':str(day), 'HOME_ROOT':str(home)}):
            growth.configure(True, root=day)
            for by, text, scope in [('日子线·自主写作',story,'public'),
                                    ('日子线·自主活动',old_activity,'public'),
                                    ('日子线·自主写作','纸船飞到月亮：私人故事','private'),
                                    ('其他作者','纸船飞到月亮：别人的故事','public')]:
                with patch.object(cards, '_now', return_value='2026-11-07T12:00:00+09:00'):
                    cards.append_cards(home, role='organ', by=by, cards=[{
                        'room':'工作台','kind':'物件','name':by+scope,
                        'params':{'text':text,'scope':scope,'action':'paint','status':'failed'}}])
            for i in range(13):
                for by in ('日子线·自主写作','日子线·自主活动'):
                    cards.append_cards(home, role='organ', by=by, cards=[{
                        'room':'工作台','kind':'物件','name':by+str(i),
                        'params':{'text':'煮面日常'+str(i),'scope':'public','action':'paint','status':'done'}}])
            before = {p:p.read_bytes() for p in home.rglob('*') if p.is_file()}
            capture = []
            conversation.answer('你还记得自己写的纸船飞到月亮那个故事吗？', '12345678',
                store=fixture.store, brain=fixture.brain, now='2026-11-09T12:00:00+09:00',
                scene='private', session_id='qq:private:12345678', recent_root=fixture.tmp/'recent',
                transport=_transport('写过这样的童话。',capture),config=CONFIG,env=ENV)
            prompt = capture[-1]['messages'][0]['content']
            self.assertIn(story, prompt)
            self.assertIn(old_activity, prompt)
            self.assertIn('创作内容不代表实际经历', prompt)
            self.assertNotIn('私人故事', prompt)
            self.assertNotIn('别人的故事', prompt)
            current = autonomy.context(root=day, home_root=home)
            self.assertNotIn(story, current)
            self.assertIn('煮面日常12', current)
            self.assertEqual(current, autonomy.context(root=day, home_root=home, query='今天什么天气'))
            found = autonomy.context(root=day, home_root=home, query='纸船飞到月亮')
            payload = json.loads(found[found.index('{'):])
            self.assertEqual(3, len(payload['writings']))
            self.assertEqual(3, len(payload['activities']))
            old = next(row for row in payload['writings'] if row['text'] == story)
            self.assertEqual('2026-11-07T12:00:00+09:00', old['recorded_at'])
            self.assertIn('煮面日常12', found)
            self.assertEqual(before,{p:p.read_bytes() for p in home.rglob('*') if p.is_file()})

    def test_growth_replaces_only_mutable_sections_and_clear_stays_empty(self):
        from organs.mind import persona
        from organs.day import growth
        brain, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / 'day'
        soul = load(Path(__file__).resolve().parents[2] / 'soul' / 'data')
        original = soul.render()
        brain.update_profile({'likes': '薄荷', 'self_description': '慢慢来'}, note='自己的选择')
        self.assertEqual(persona.render(soul, brain, root=root), original)

        growth.configure(True, root=root)
        text = persona.render(soul, brain, root=root)
        self.assertIn('【当前兴趣】薄荷', text)
        self.assertIn(soul.snapshot().identity.likes, text)
        self.assertIn(soul.snapshot().identity.appearance, text)
        self.assertIn('【红线】', text)
        brain.update_profile({'likes': ''}, note='清空')
        text = persona.render(soul, brain, root=root)
        self.assertNotIn('薄荷', text)
        self.assertIn(soul.snapshot().identity.likes, text)
        growth.configure(False, root=root)
        self.assertEqual(persona.render(soul, brain, root=root), original)

    def test_real_chat_prompt_omits_unscoped_private_context(self):
        import os
        import json
        from unittest.mock import patch
        from organs.mind import conversation, recent
        from organs.day import growth
        from organs.mind.tests.test_note_display import NoteDisplayTest, CONFIG, ENV, _transport
        fixture = NoteDisplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        day = fixture.tmp / 'day'
        day.mkdir()
        at = '2026-11-08T12:00:00+09:00'
        with patch.dict(os.environ, {'DAY_ROOT': str(day)}), patch('organs.brain.store.now_iso', return_value=at):
            growth.configure(True, root=day)
            fixture.brain.update_profile({'likes': 'L' * 600, 'dislikes': 'D' * 600, 'views': 'V' * 600}, note='测试')
            fixture.brain.ingest('12345678', '紫茄子秘密', note='私下的理解', ts=at,
                scene='private', session_id='qq:private:12345678')
            history = fixture.tmp / 'recent'
            recent.append({'ts':at, 'who':'12345678', 'said':'紫茄子旧私聊', 'reply':'记住了',
                           'session_id':'qq:private:12345678'}, root=history)
            capture = []
            with patch.object(conversation, '_person_text', return_value='紫茄子无来源人物备注'):
                conversation.answer('紫茄子是什么', '87654321', store=fixture.store, brain=fixture.brain,
                    now=at, scene='group', session_id='qq:group:123', recent_root=history,
                    transport=_transport('不知道。', capture), config=CONFIG, env=ENV)
            packed = json.dumps(capture, ensure_ascii=False)
            self.assertNotIn('紫茄子秘密', packed)
            self.assertNotIn('紫茄子旧私聊', packed)
            self.assertNotIn('紫茄子无来源人物备注', packed)
            self.assertIn('V' * 600, packed)
            self.assertIn('【红线】', packed)

    def test_own_fiction_keeps_its_provenance_in_chat_activity_and_growth(self):
        import os
        import json
        from unittest.mock import patch
        from organs.day import autonomy, growth
        from organs.mind import conversation
        from organs.mind.tests.test_note_display import NoteDisplayTest, CONFIG, ENV, _transport
        fixture = NoteDisplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        day, home = fixture.tmp / 'home' / 'day', fixture.tmp / 'home' / 'home'
        at = '2026-11-08T12:00:00+09:00'
        story = '我乘着纸船飞到月亮上，把整片海装进了口袋。'
        calls = {}
        with patch.dict(os.environ, {'DAY_ROOT': str(day)}), \
             patch('organs.day.phone_browse._feeling', return_value={'asleep': False, 'energy': {'value': 80}}), \
             patch('organs.day.phone_browse._ACTIVE', None), \
             patch('organs.mind.stroke_painting._ACTIVE', None), \
             patch('organs.hand.phone.config', return_value={}):
            growth.configure(True, root=day)
            written = autonomy.tick(at, root=day, home_root=home, complete=lambda _: json.dumps({
                'action': 'write', 'reason': '想写童话', 'args': {'text': story}}))
            self.assertEqual(written['status'], 'done')
            capture = []
            conversation.answer('你刚才写了什么？', '12345678', store=fixture.store, brain=fixture.brain,
                now=at, scene='private', session_id='qq:private:12345678', recent_root=fixture.tmp/'recent',
                transport=_transport('写了一个纸船的故事。', capture), config=CONFIG, env=ENV)
            calls['chat'] = capture[-1]['messages']
            def choose(messages):
                calls['activity'] = messages
                return json.dumps({'action': 'idle', 'reason': '先歇会儿', 'args': {}})
            result = autonomy.tick('2026-11-08T15:00:00+09:00', root=day, home_root=home, complete=choose)
            self.assertEqual(result['status'], 'done')
            def reflect(messages):
                calls['growth'] = messages
                return json.dumps({'patch': {}, 'note': '先保持', 'sources': [written['receipt']['card_id']]})
            result = growth.reflect('2026-11-09T01:00:00+09:00', root=day, home_root=home,
                                    brain=fixture.brain, complete=reflect)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(autonomy.own_writings(home)[0]['text'], story)
            for source, messages in calls.items():
                with self.subTest(source=source):
                    packed = json.dumps(messages, ensure_ascii=False)
                    self.assertIn(story, packed)
                    self.assertIn(written['receipt']['card_id'], packed)
                    self.assertIn('创作内容不代表实际经历', messages[0]['content'])
                    self.assertIn('画面要求不等于实际画面', messages[0]['content'])
