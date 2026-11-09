import tempfile
import unittest
from organs.home import cards


class IntentionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_persist_pause_resume_abandon_without_user_task_contract(self):
        from organs.home import intentions
        made = intentions.apply({'name': '写一首小诗', 'note': '自己想试试', 'next_step': '想一个开头'}, root=self.root)
        cid = made['card_id']
        intentions.apply({'card_id': cid, 'status': 'paused'}, root=self.root)
        self.assertEqual(intentions.listing(self.root)[0]['status'], 'paused')
        intentions.apply({'card_id': cid, 'status': 'active', 'next_step': '第二天继续'}, root=self.root)
        self.assertEqual(intentions.listing(self.root)[0]['next_step'], '第二天继续')
        intentions.apply({'card_id': cid, 'status': 'abandoned'}, root=self.root)
        self.assertEqual(intentions.listing(self.root)[0]['status'], 'abandoned')
        self.assertEqual(cards.list_tasks(self.root), [])

    def test_user_card_and_forged_self_marker_cannot_be_updated(self):
        from organs.home import intentions
        user = cards.append_cards(self.root, role='human', by='用户', cards=[{
            'room': '工作台', 'kind': '物件', 'name': '用户的事', 'params': {'self': True}}])['cards'][0]
        with self.assertRaises(ValueError):
            intentions.apply({'card_id': user['card_id'], 'status': 'abandoned'}, root=self.root)
        with self.assertRaises(ValueError):
            intentions.apply({'name': '伪装', 'owner': '她自己', 'self': True}, root=self.root)
        self.assertEqual(intentions.listing(self.root), [])

    def test_model_cannot_assert_done_and_active_cap_is_three(self):
        from organs.home import intentions
        first = intentions.apply({'name': '想法0'}, root=self.root)
        with self.assertRaises(ValueError):
            intentions.apply({'card_id': first['card_id'], 'status': 'done'}, root=self.root)
        for i in (1, 2):
            intentions.apply({'name': '想法' + str(i)}, root=self.root)
        with self.assertRaises(ValueError):
            intentions.apply({'name': '第四个'}, root=self.root)
        intentions.apply({'card_id': first['card_id'], 'status': 'paused'}, root=self.root)
        intentions.apply({'name': '第四个'}, root=self.root)
        with self.assertRaises(ValueError):
            intentions.apply({'card_id': first['card_id'], 'status': 'active'}, root=self.root)
        # 编辑暂停卡不等于重新激活，也不应该消耗正在做的三项名额。
        intentions.apply({'card_id': first['card_id'], 'next_step': '以后再想'}, root=self.root)
        paused = next(p for p in intentions.listing(self.root) if p['card_id'] == first['card_id'])
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(paused['next_step'], '以后再想')
