import json
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from unittest import mock

from api import chat
from organs.mind import llm
from organs.mind.tests.test_llm import LLMTest, FakeTransport, conf, ok, MESSAGES


class ModelTraceTest(LLMTest):
    def setUp(self):
        super().setUp()
        patcher=mock.patch.dict('os.environ', {'QQAGENT_TRACE':'1','QQAGENT_HOME_ROOT':str(self.tmp)})
        patcher.start(); self.addCleanup(patcher.stop)

    def rows(self):
        return [json.loads(line) for p in sorted((self.tmp/'mind/trace').glob('*.jsonl'))
                for line in p.read_text().splitlines()]

    def test_failed_attempts_and_empty_body_are_preserved_without_credentials(self):
        transport=FakeTransport(TimeoutError('slow'), (503,b'{"error":"busy"}'), ok('成功'))
        self.assertEqual('成功',llm.complete(MESSAGES,config=conf(),env={'QQAGENT_LLM_KEY':'test-secret'},transport=transport))
        with self.assertRaises(llm.LLMError):
            llm.complete(MESSAGES,config=conf(retries=0),env={'QQAGENT_LLM_KEY':'test-secret'},transport=FakeTransport(ok('')))
        rows=self.rows()
        self.assertEqual(4,len(rows))
        self.assertEqual([1,2,3,1],[r['attempt'] for r in rows])
        self.assertEqual(['timeout','http',None,'empty'],[r.get('error') for r in rows])
        self.assertEqual([None,503,200,200],[r['status'] for r in rows])
        self.assertEqual(1,len({r['call_id'] for r in rows[:3]}))
        self.assertNotEqual(rows[2]['call_id'],rows[3]['call_id'])
        self.assertNotIn('test-secret',json.dumps(rows))
        self.assertTrue(all(r['duration_ms']>=0 for r in rows))

    def test_round_scope_reaches_rewrite_thread_and_resets_after_error(self):
        def call():
            return llm.complete(MESSAGES,config=conf(retries=0),env={'QQAGENT_LLM_KEY':'fake'},transport=FakeTransport(ok('正文')))
        with self.assertRaises(ValueError):
            with llm.trace_round('a'*32,'qq:private:test'):
                call()
                chat._bounded(call,1)
                raise ValueError('结束本轮')
        with llm.trace_round('b'*32,'qq:private:other'):
            call()
        call()
        rows=self.rows()
        self.assertEqual(['a'*32,'a'*32,'b'*32,None],[r.get('turn_id') for r in rows])
        self.assertEqual([1,2,1,None],[r.get('call_index') for r in rows])
        self.assertEqual('qq:private:test',rows[1]['session_id'])

    def test_late_rewrite_keeps_original_round_after_timeout(self):
        release,finished=threading.Event(),threading.Event()
        def late():
            try:
                release.wait(2)
                llm.complete(MESSAGES,config=conf(retries=0),env={'QQAGENT_LLM_KEY':'fake'},transport=FakeTransport(ok('晚到的稿')))
            finally:
                finished.set()
        try:
            with llm.trace_round('a'*32,'old'):
                with self.assertRaises(FutureTimeout):
                    chat._bounded(late,.01)
            with llm.trace_round('b'*32,'new'):
                llm.complete(MESSAGES,config=conf(retries=0),env={'QQAGENT_LLM_KEY':'fake'},transport=FakeTransport(ok('新一轮')))
                release.set()
                self.assertTrue(finished.wait(1))
            self.assertEqual(['b'*32,'a'*32],[r['turn_id'] for r in self.rows()])
        finally:
            release.set()
            finished.wait(2)
