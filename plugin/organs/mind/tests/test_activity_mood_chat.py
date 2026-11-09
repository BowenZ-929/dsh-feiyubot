"""活动感想、同一颗心、下一轮聊天与页面来源的贯通检查。"""
import json
import os
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import patch


class ActivityMoodChatTest(unittest.TestCase):
    def test_activity_feeling_reaches_next_chat_without_becoming_a_personal_conflict(self):
        from api import activity, roots
        from organs.brain import BrainStore
        from organs.day import autonomy, growth
        from organs.heart import mood, store as heart_store
        from organs.home import cards
        from organs.mind import appraisal, conversation
        from organs.mind.tests.test_note_display import NoteDisplayTest, CONFIG, ENV, _transport

        fixture = NoteDisplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        at = '2026-11-09T12:00:00+09:00'
        work = '纸船在窗边停了一夜，晨光替它把水面铺开。'
        # 靠近现有档界，足以检出聊天误读旧状态；不是调整正式心情或增益。
        for effect, impression, tier in (
            ('positive', '这一句接上了，有点开心', '平常'),
            ('negative', '这句还没有写出我想要的感觉，有点失落', '偏冷'),
            ('neutral', '先放着，没什么特别感觉', '平常'),
        ):
            with self.subTest(effect=effect), patch.dict(os.environ, {
                'QQAGENT_HOME': str(fixture.tmp/effect),
            }), patch.object(cards, '_now', return_value=at):
                mapping = roots.export_env()
                roots.seed('heart')
                brain = BrainStore(fixture.tmp/effect/'brain')
                home = Path(mapping['HOME_ROOT'])
                home.mkdir(parents=True)
                growth.configure(True)
                heart = Path(mapping['HEART_ROOT'])
                heart_store.write_json(heart/'mood.json', {
                    'version': 1, 'now': 50.2, 'baseline': 60, 'updated_at': at,
                    'day': '2026-11-09', 'conflict_used': 0,
                })
                cid = cards.append_cards(home, role='organ', by='日子线·自主写作', cards=[{
                    'room':'工作台', 'kind':'物件', 'name':'纸船小诗',
                    'params':{'text':work, 'scope':'public'},
                }])['cards'][0]['card_id']
                choice = {'action':'idle', 'reason':'回看自己的小诗', 'args':{},
                          'reaction':{'id':cid, 'effect':effect, 'impression':impression, 'evidence':work}}
                with patch('organs.day.phone_browse.availability', return_value={'available':False}), \
                     patch('organs.hand.comfy.config', return_value={'enabled':False}), \
                     patch.object(autonomy, 'datetime') as clock:
                    clock.now.return_value = datetime.fromisoformat(at)
                    result = autonomy.tick(at, brain=brain, complete=lambda _:json.dumps(choice))
                self.assertEqual('done', result['status'], result)
                feedback = result['reaction']['mood']
                state = mood.read(now=at)
                self.assertEqual(tier, state['tier'])
                self.assertEqual(feedback['after'], state['now'])
                self.assertEqual(0, heart_store.read_json(heart/'mood.json', {})['conflict_used'])
                captured = []
                def transport(url, headers, payload, timeout):
                    text = ('{"kind":null,"evidence":"","reason":"普通日常分享"}'
                            if payload['messages'][0]['content'].startswith(appraisal.PROMPT) else '填完了呀。')
                    return _transport(text, captured)(url, headers, payload, timeout)
                with patch.object(conversation, '_voice', wraps=conversation._voice) as voice:
                    reply = conversation.answer('终于填完表了，来找你聊两句。', '10000420',
                        store=fixture.store, brain=brain, now=at,
                        scene='private', session_id='qq:private:10000420',
                        recent_root=fixture.tmp/effect/'recent',
                        config=CONFIG, env=ENV, assessor=appraisal.assess, transport=transport)
                self.assertEqual(round(state['now']/100, 3), voice.call_args.args[3]['mood'])
                self.assertEqual(tier, reply['heart']['mood']['tier'])
                prompt = '\n'.join(m['content'] for m in captured[-1]['messages'])
                self.assertIn('心境：'+tier, prompt)
                self.assertIn('自己的心情低落不等于对方在催促或冒犯', prompt)
                self.assertIn(work, prompt)
                # 页面读的是同一条实际活动回执，不能把感受来源安到聊天对象上。
                code, page = activity.dispatch({'date':'2026-11-09', 'source':'autonomy'})
                self.assertEqual(200, code)
                row = next(r for r in page['rows'] if r.get('reaction'))
                self.assertEqual(cid, row['reaction']['id'])
                self.assertEqual(impression, row['reaction']['impression'])
                self.assertEqual(feedback, row['reaction']['mood'])
                self.assertEqual(result['model_turn_id'], row['model_turn_id'])
