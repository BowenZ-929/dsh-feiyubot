import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from organs.day import autonomy, growth, phone_browse, proactive, solitude
from organs.home import art, cards, intentions
from organs.hand.tests.test_strokes import PROGRAM
from organs.mind import stroke_painting

AT = '2026-11-08T12:00:00+09:00'


class ActivityReceiptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'day'
        self.home = self.root / 'home'
        growth.configure(True, root=self.root)
        self.addCleanup(patch.stopall)
        patch('organs.day.phone_browse._feeling', return_value={
            'asleep': False, 'energy': {'value': 80}, 'mood': {'tier': '平常'}}).start()
        patch('organs.hand.phone.config', return_value={'serial': 'test', 'isolated_for_bot': True}).start()
        self.plan = intentions.apply({'name': '画一张三角涂鸦'}, root=self.home)

    def choose(self, args, action='paint', share=False):
        return autonomy.tick(AT, root=self.root, complete=lambda _: json.dumps({
            'action': action, 'reason': '继续自己的打算', 'args': args, 'share': share}))

    def test_result_records_later_time_without_moving_opportunity_start(self):
        finish = '2026-11-08T12:00:09.600000+09:00'
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromisoformat(finish)
        for raw, status in [('{"action":"idle","reason":"先闲着","args":{}}', 'done'), ('{bad', 'failed')]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / 'day'
                growth.configure(True, root=root)
                with patch('organs.day.model.datetime', Clock), patch('organs.day.autonomy.datetime', Clock, create=True):
                    result = autonomy.tick(AT, root=root, complete=lambda _: raw)
                self.assertEqual(result['status'], status)
                rows = solitude.log_lines(root)
                attempt = next(r for r in rows if r['event']=='autonomy_attempt')
                completed = next(r for r in rows if r['event']=='autonomy_result')
                self.assertEqual(attempt['at'], AT)
                self.assertEqual(completed['at'], AT)
                self.assertEqual(completed['recorded_at'], finish)
                self.assertEqual(autonomy.opportunities(root), [attempt])

    def test_real_painting_finishes_plan_only_after_persisted_result(self):
        # 阻住工作线程，先验证真实排队；释放后用虚构笔触经真实渲染与落盘。
        import threading
        entered, release = threading.Event(), threading.Event()
        def draw(_, **kwargs):
            self.assertEqual(kwargs.get('purpose'), '规划绘画笔触')
            entered.set()
            release.wait(3)
            return json.dumps(PROGRAM)
        with patch('organs.mind.llm.complete', side_effect=draw):
            try:
                result = self.choose({'prompt': '三角涂鸦', 'intention_id': self.plan['card_id'], 'finish': True}, share=True)
                self.assertEqual(result['status'], 'queued')
                self.assertIsNone(proactive.open_own_doing(self.root, AT))
                self.assertTrue(entered.wait(2))
                self.assertEqual(intentions.listing(self.home)[0]['status'], 'active')
            finally:
                release.set()
                if entered.is_set():
                    stroke_painting._FUTURE.result(timeout=5)
        finished = art.painting_job(result['receipt']['id'], root=self.home)
        self.assertEqual(finished['status'], 'done')
        self.assertTrue(Path(finished['image_path']).is_file())
        # 再次 tick 被间隔挡住，也必须收回上一件事的结果，不调用模型。
        with patch('organs.mind.llm.complete', side_effect=AssertionError('不应新增调用')):
            autonomy.tick(AT, root=self.root)
            autonomy.tick(AT, root=self.root)
        plan = intentions.listing(self.home)[0]
        self.assertEqual(plan['status'], 'done')
        self.assertEqual(len(plan['sources']), 1)
        receipts = [r for r in solitude.log_lines(self.root) if r.get('event') == 'autonomy_finished']
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['status'], 'done')
        self.assertIn('三角涂鸦', autonomy.context(root=self.root))
        candidates = proactive.own_doing_lines(self.root)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['job_id'], finished['id'])
        self.assertEqual(candidates[0]['card_id'], receipts[0]['receipt']['card_id'])
        self.assertIn('画面要求', candidates[0]['because'])
        self.assertIn('不是看图结论', candidates[0]['because'])
        self.assertNotIn(str(self.root), candidates[0]['because'])
        self.assertTrue(next(r for r in solitude.log_lines(self.root)
                             if r.get('event') == 'autonomy_result')['share'])
        self.assertNotIn(str(self.root), autonomy.context(root=self.root))
        materials = growth.materials(None, AT, root=self.root)
        self.assertTrue(any('三角涂鸦' in r.get('text', '') for r in materials))

    def test_forged_or_foreign_intention_cannot_start_painting(self):
        result = self.choose({'prompt': '三角', 'intention_id': 'not-mine', 'finish': True})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(art.painting_jobs(self.home), [])

    def queued_result(self, action, status, *, ident='a' * 32, scope='public', share=None):
        if action == 'paint':
            art.save_painting_job({'id': ident, 'status': status, 'created_at': AT,
                'request': {'prompt': '三角涂鸦'}, 'error': '原始错误中的私人信息'}, root=self.home)
        else:
            phone_browse.save({'id': ident, 'status': status, 'created_at': AT, 'origin': 'autonomy',
                'scope': scope, 'title': '仅本地标题' if scope != 'public' else '月亮片段',
                'observations': [], 'note': '原始错误中的私人信息'}, self.root)
        solitude.append_log(self.root, {'at': AT, 'event': 'autonomy_result', 'model_turn_id': ident,
            'action': action, 'status': 'queued', 'receipt': {'id': ident},
            'intention_id': self.plan['card_id'], 'finish': action == 'paint',
            **({'share': share} if share is not None else {})})

    def test_model_image_receipt_does_not_claim_stroke_work(self):
        self.queued_result('paint','done')
        job = art.painting_job('a'*32,root=self.home)
        job['request']['mode']='image'
        art.save_painting_job(job,root=self.home)
        rows = autonomy.settle(AT,root=self.root)
        self.assertIn('本地模型绘画：已完成',rows[0]['reason'])
        self.assertNotIn('逐笔',autonomy.context(root=self.root))
        self.assertEqual(rows[0]['mode'],'image')

    def test_final_canvas_observation_reaches_activity_and_growth_only_with_image_match(self):
        for index, case in enumerate(('observed', 'revised', 'failed', 'missing_path')):
            with self.subTest(case=case):
                ident = str(index) * 32
                self.queued_result('paint', 'done', ident=ident)
                job = art.painting_job(ident, root=self.home)
                job.update(image_path='/private/final.png', draft_image_path='/private/final.png',
                    review={'status':'ok', 'reason':'对初稿的检查结果', 'observation':{
                        'status':'ok', 'observation':{'subject':'蓝鲸鱼抱白碗', 'scene':'白背景',
                        'uncertainty':'碗内物体无法确定', 'private_debug':'不应复制'}}})
                if case == 'revised':
                    job['image_path'] = '/private/revised.png'
                elif case == 'failed':
                    job['review']['observation']['status'] = 'failed'
                    job['review']['status'] = 'failed'
                elif case == 'missing_path':
                    del job['image_path']; del job['draft_image_path']
                art.save_painting_job(job, root=self.home)
                result = autonomy.settle(AT, root=self.root)[0]
                text = result['reason']
                self.assertEqual(case == 'observed', '蓝鲸鱼抱白碗' in text)
                if case == 'observed':
                    self.assertIn('最终图观察', text)
                    self.assertIn('碗内物体无法确定', text)
                else:
                    self.assertIn('没有可用的最终图观察', text)
                self.assertNotIn('/private/', text)
                self.assertNotIn('不应复制', text)
                material = next(r for r in growth.materials(None, AT, root=self.root)
                                if r['id'] == result['receipt']['card_id'])
                self.assertEqual(text, material['text'])
                self.assertIn(text, json.loads(autonomy.context(root=self.root).split('\n')[-1])['activities'][-1]['text'])
                self.assertEqual([], autonomy.settle(AT, root=self.root))

    def test_failure_cancel_and_interruption_never_finish_or_export_raw_errors(self):
        for index, status in enumerate(('failed', 'cancelled', 'running')):
            with self.subTest(status=status):
                self.queued_result('paint', status, ident=str(index) * 32)
                autonomy.settle(AT, root=self.root)
                self.assertEqual(intentions.listing(self.home)[0]['status'], 'active')
        self.assertNotIn('原始错误中的私人信息', autonomy.context(root=self.root))
        self.assertIn('已中断', autonomy.context(root=self.root))
        self.assertEqual(intentions.listing(self.home)[0]['sources'], [])

    def test_late_painting_cannot_overwrite_paused_or_abandoned_plan(self):
        for index, status in enumerate(('paused', 'abandoned')):
            intentions.apply({'card_id': self.plan['card_id'], 'status': status}, root=self.home)
            self.queued_result('paint', 'done', ident=str(index) * 32)
            autonomy.settle(AT, root=self.root)
            self.assertEqual(intentions.listing(self.home)[0]['status'], status)

    def test_phone_terminal_states_are_progress_not_goal_completion(self):
        for index, status in enumerate(('done', 'partial', 'stopped', 'failed', 'rested', 'unavailable')):
            with self.subTest(status=status):
                self.queued_result('browse', status, ident=str(index) * 32,
                                   scope='private' if index == 2 else 'public')
                autonomy.settle(AT, root=self.root)
                self.assertEqual(intentions.listing(self.home)[0]['status'], 'active')
        text = autonomy.context(root=self.root)
        self.assertNotIn('仅本地标题', text)
        self.assertNotIn('原始错误中的私人信息', text)
        self.assertEqual(len([r for r in solitude.log_lines(self.root)
                              if r.get('event') == 'autonomy_finished']), 6)

    def test_phone_done_waits_for_reflection_worker_to_finish(self):
        self.queued_result('browse', 'done')
        with patch('organs.day.phone_browse._ACTIVE', 'a' * 32):
            self.assertEqual(autonomy.settle(AT, root=self.root), [])
        self.assertEqual(len(autonomy.settle(AT, root=self.root)), 1)

    def test_disabled_mode_records_existing_receipt_without_model_or_new_activity(self):
        from api import day
        self.queued_result('paint', 'done')
        growth.configure(False, root=self.root)
        with patch('organs.day.rhythm.tick', return_value={}), \
             patch('organs.mind.llm.complete', side_effect=AssertionError('不得调用模型')):
            result = day.run_tick(AT, root=self.root)
        self.assertEqual(result['activity_receipts'][0]['status'], 'done')
        self.assertEqual(intentions.listing(self.home)[0]['status'], 'done')
        self.assertNotIn('autonomy', result)

    def test_browse_start_carries_own_plan_and_collects_real_job_record(self):
        phone_browse.configure(True, self.root)
        personas = []
        def worker(job, persona, root, heart_root, complete):
            try:
                personas.append(persona)
                job.update(status='done', finished_at=AT, title='月亮片段')
                phone_browse.save(job, root)
            finally:
                phone_browse._ACTIVE = None
                phone_browse._LOCK.release()
        # 替换手机连接和实际工作；保留真实启动、任务存储和异步执行入口。
        with patch('organs.hand.phone.status', return_value={'connected': True, 'state': 'device'}), \
             patch('organs.day.phone_browse._run', side_effect=worker):
            result = self.choose({'intention_id': self.plan['card_id']}, action='browse')
            self.assertEqual(result['status'], 'queued')
            phone_browse._FUTURE.result(timeout=3)
        job = phone_browse.view(self.root)['jobs'][0]
        self.assertEqual(job['origin'], 'autonomy')
        self.assertIn('画一张三角涂鸦', personas[0])
        autonomy.settle(AT, root=self.root)
        self.assertEqual(intentions.listing(self.home)[0]['status'], 'active')
        self.assertIn('月亮片段', autonomy.context(root=self.root))
        rows, _ = cards._read_room_rows(self.home, '工作台')
        receipt = next(c for c in rows if c['by'] == '日子线·自主活动')
        self.assertEqual(receipt['params']['intention_id'], self.plan['card_id'])

    def test_nonsharing_old_failed_or_disabled_painting_never_raises_candidate(self):
        for index, (status, share, enabled) in enumerate([
                ('done', False, True), ('done', None, True), ('failed', True, True),
                ('cancelled', True, True), ('running', True, True), ('done', True, False)]):
            with self.subTest(status=status, share=share, enabled=enabled):
                growth.configure(enabled, root=self.root)
                self.queued_result('paint', status, ident=str(index) * 32, share=share)
                autonomy.settle(AT, root=self.root)
                self.assertEqual(proactive.own_doing_lines(self.root), [])

    def test_completed_painting_uses_completion_day_and_keeps_existing_candidate(self):
        tomorrow = '2026-11-09T12:00:00+09:00'
        self.queued_result('paint', 'done', share=True)
        autonomy.settle(tomorrow, root=self.root)
        candidate = proactive.open_own_doing(self.root, tomorrow)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate['day'], '2026-11-09')
        self.assertIsNone(proactive.open_own_doing(self.root, AT))
        self.queued_result('paint', 'done', ident='b' * 32, share=True)
        autonomy.settle(tomorrow, root=self.root)
        self.assertEqual(proactive.own_doing_lines(self.root), [candidate])
