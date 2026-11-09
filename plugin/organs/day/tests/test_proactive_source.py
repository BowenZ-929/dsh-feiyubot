"""未完话题属于原作者；临时脑、队列和模型替身，不调用外部服务。"""
from unittest import mock

from organs.day import delivery, plan, proactive
from .test_proactive import ProactiveCase, PERSON, OTHER, NOW, at, caps, open_thought


class SourceOwnershipTest(ProactiveCase):
    def seed(self, scene='group', session_id=''):
        self.heard('熟人之前的话', now='2026-11-05T10:00:00', who=PERSON)
        result = self.brain.ingest(who=OTHER, text='我刚才说的那本书', ts=at(NOW),
                                   scene=scene, turn_id='b' * 32,
                                   **({'session_id': session_id} if session_id else {}))
        self.assertTrue(result['ok'])
        people = [dict(id=PERSON, familiarity=90, last_interaction=at(NOW)),
                  dict(id=OTHER, familiarity=2, last_interaction=at(NOW))]
        self.patch_people = mock.patch.object(self.brain, 'people_list', return_value=people)
        self.patch_people.start()
        self.addCleanup(self.patch_people.stop)
        return result['entry']

    def check_source_chain(self, scene, session_id=''):
        entry = self.seed(scene, session_id)
        out = self.consider()
        self.assertEqual(OTHER, out['who'])
        self.assertEqual('source-author', out['who_source'])
        source = dict(entry_id=entry['id'], turn_id='b' * 32, who=OTHER,
                      scene=scene, at=at(NOW))
        if session_id:
            source['session_id'] = session_id
        self.assertEqual(source, out['row']['source'])
        saved = plan.load(self.root)['pending'][0]
        self.assertEqual(source, saved['source'])
        seen = []
        def complete(messages, **kwargs):
            seen.extend(messages)
            return '{"send":true,"chunks":["那本书怎么了"]}'
        queued = proactive.run_due(saved, at('2026-11-05T10:36:00'), root=self.root,
            heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual('queued', queued['action'])
        self.assertIn(OTHER, seen[0]['content'])
        self.assertNotIn(PERSON, seen[0]['content'])
        item = delivery.load(self.root)['items'][0]
        self.assertEqual(OTHER, item['to'])
        self.assertEqual(source, item['source'])
        self.assertFalse(queued['sent'])

    def test_group_source_survives_plan_and_queue(self):
        self.check_source_chain('group')

    def test_private_source_survives_plan_and_queue(self):
        self.check_source_chain('private')

    def test_unknown_source_is_not_assumed_private(self):
        self.check_source_chain('unknown')

    def test_exact_group_session_survives_plan_and_queue(self):
        self.check_source_chain('group', 'qq:group:70001')

    def test_exact_private_session_survives_plan_and_queue(self):
        self.check_source_chain('private', 'qq:private:9876543210')

    def test_original_authors_quota_is_checked(self):
        self.seed()
        plan.add([{'kind': 'proactive', 'at': at('2026-11-05T10:00:00'),
                   'who': OTHER}], root=self.root, now_ts=at('2026-11-05T09:59:00'))
        out = self.consider(limits=caps(per_who=1))
        self.assertEqual(OTHER, out['who'])
        self.assertEqual('quota', out['action'])
        self.assertIsNone(out['plan'])

    def test_unspecified_thought_still_selects_familiar_person(self):
        self.seed()
        open_thought(self.heart)
        out = self.consider()
        self.assertEqual(proactive.REASON_THOUGHT, out['reason_code'])
        self.assertEqual(PERSON, out['who'])
        self.assertEqual('recent-talk', out['who_source'])
        self.assertNotIn('source', out['plan'])

    def test_reply_in_original_session_retires_old_unfinished_plan(self):
        self.seed('group', 'qq:group:70001')
        row = self.consider()['plan']
        self.brain.ingest('她说', '刚才那本书我看过', side='她说', scene='group',
            session_id='qq:group:70001', turn_id='b' * 32, ts=at('2026-11-05T10:33:00'))
        complete = mock.Mock(return_value='{"send":true,"chunks":["那本书怎么了"]}')
        out = proactive.run_due(row, at('2026-11-05T10:36:00'), root=self.root,
            heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual(('declined', 'source-changed', False),
                         (out['action'], out.get('reason'), out['sent']))
        complete.assert_not_called()
        self.assertEqual([], delivery.load(self.root)['items'])
        self.assertEqual([], plan.load(self.root)['pending'])
        self.assertEqual('source-changed', proactive.log_tail(self.root, 1)[0]['reason'])
        again = proactive.run_due(row, at('2026-11-05T10:37:00'), root=self.root,
            heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual('not-pending', again['skipped'])

    def test_new_input_in_original_session_prevents_old_topic_generation(self):
        self.seed('group', 'qq:group:70001')
        row = self.consider()['plan']
        self.brain.ingest(OTHER, '书先不聊了，改说电影吧', scene='group',
            session_id='qq:group:70001', turn_id='c' * 32, ts=at('2026-11-05T10:33:00'))
        complete = mock.Mock(return_value='{"send":true,"chunks":["那本书怎么了"]}')
        out = proactive.run_due(row, at('2026-11-05T10:36:00'), root=self.root,
            heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual('source-changed', out.get('reason'))
        complete.assert_not_called()
        self.assertEqual([], delivery.load(self.root)['items'])

    def test_another_group_does_not_resolve_original_groups_topic(self):
        import json
        from organs.brain.store import TIERS_FILE
        entry = self.seed('group', 'qq:group:70001')
        row = self.consider()['plan']
        (self.brain.root / TIERS_FILE).write_text(json.dumps(dict(
            self.brain._tiers(), today_max=1, today_demote_count=2)))
        self.brain.ingest(OTHER, '另一个群的电影', scene='group',
            session_id='qq:group:70002', turn_id='c' * 32, ts=at('2026-11-05T10:33:00'))
        self.brain.ingest('她说', '这部电影看过', side='她说', scene='group',
            session_id='qq:group:70002', turn_id='c' * 32, ts=at('2026-11-05T10:34:00'))
        self.assertIn(entry['id'], {record['id'] for record in self.brain.longterm_active_entries()})
        complete = mock.Mock(return_value='{"send":true,"chunks":["那本书怎么了"]}')
        out = proactive.run_due(row, at('2026-11-05T10:36:00'), root=self.root,
            heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual('queued', out['action'])
        self.assertEqual(1, complete.call_count)
        self.assertEqual('qq:group:70001', delivery.load(self.root)['items'][0]['source']['session_id'])

    def test_original_session_moves_on_during_generation(self):
        self.seed('group', 'qq:group:70001')
        row = self.consider()['plan']
        def answer(*args, **kwargs):
            self.brain.ingest(OTHER, '书先不聊了', scene='group', session_id='qq:group:70001',
                              turn_id='c' * 32, ts=at('2026-11-05T10:36:00'))
            return '{"send":true,"chunks":["那本书怎么了"]}'
        complete = mock.Mock(side_effect=answer)
        out = proactive.run_due(row, at('2026-11-05T10:36:00'), root=self.root,
            heart_root=self.heart, brain=self.brain, complete=complete)
        self.assertEqual(('declined', 'source-changed'), (out['action'], out.get('reason')))
        self.assertEqual(1, complete.call_count)
        self.assertEqual([], delivery.load(self.root)['items'])
        self.assertEqual([], plan.load(self.root)['pending'])
