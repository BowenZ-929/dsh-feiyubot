import unittest


class VisibilityTest(unittest.TestCase):
    def test_private_scope_and_local_material(self):
        from organs.brain.visibility import visible
        private = {'scene': 'private', 'session_id': 'qq:private:1', 'text': '秘密'}
        self.assertTrue(visible(private, session_id='qq:private:1'))
        self.assertFalse(visible(private, session_id='qq:private:2'))
        self.assertFalse(visible(private, session_id='qq:group:1'))
        self.assertFalse(visible(private))
        self.assertFalse(visible(dict(private, audience='公开')))
        self.assertFalse(visible({'text': '旧来源不明材料'}))
        self.assertFalse(visible({'audience': '公开', 'local_only': True}))
        self.assertTrue(visible({'audience': '公开', 'scene': 'group'}))

    def test_recall_filters_before_ranking_and_source_read(self):
        from organs.brain.tests.helpers import fresh_store
        brain, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        at = '2026-11-08T12:00:00+09:00'
        brain.ingest('12345678', '薄荷私聊秘密', ts=at, scene='private', session_id='qq:private:12345678')
        own = brain.recall('薄荷', now_ts=at, session_id='qq:private:12345678')
        other = brain.recall('薄荷', now_ts=at, session_id='qq:private:87654321')
        public = brain.recall('薄荷', now_ts=at, session_id='')
        self.assertTrue(own['items'])
        self.assertEqual(other['items'], [])
        self.assertEqual(public['items'], [])

    def test_local_flag_survives_rollover_and_never_enters_recall(self):
        from organs.brain.tests.helpers import fresh_store
        brain, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        at = '2026-11-08T12:00:00+09:00'
        saved = brain.ingest('12345678', '本地密钥材料', ts=at, scene='group', audience='公开', local_only=True)
        brain.run_night('2026-11-09T12:00:00+09:00')
        found = brain.recall('本地密钥材料', now_ts='2026-11-09T12:00:00+09:00', session_id='')
        self.assertEqual(found['items'], [])
        archived = next(r for r in brain.longterm_active_entries() if r['id'] == saved['entry']['id'])
        self.assertTrue(archived['local_only'])

    def test_local_only_cannot_seed_or_continue_proactive_plan(self):
        from organs.brain.tests.helpers import fresh_store
        from organs.day import proactive
        brain, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        at = '2026-11-08T12:00:00+09:00'
        row = brain.ingest('12345678', '只留本机的事情', ts=at, local_only=True,
                           session_id='qq:private:12345678')['entry']
        self.assertIsNone(proactive._unfinished(brain, at))
        plan = {'reason_code':'unfinished','source':{'entry_id':row['id'], 'session_id':row['session_id']}}
        self.assertEqual(proactive._obsolete_reason(plan, at, brain=brain), 'source-changed')
