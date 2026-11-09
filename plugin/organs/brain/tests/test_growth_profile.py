import unittest
from organs.brain.tests.helpers import fresh_store


class GrowthProfileTest(unittest.TestCase):
    def setUp(self):
        self.brain, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)

    def test_patch_clear_and_text_versions(self):
        self.assertTrue(hasattr(self.brain, 'update_profile'), '缺少具名成长入口')
        self.brain.update_profile({'likes': '薄荷', 'views': '慢慢试'}, note='我的选择')
        self.brain.record_selfnarration('今天有点累')
        self.brain.update_profile({'likes': ''}, note='暂时没有特别喜欢的')
        self.assertEqual(self.brain.current_profile(), {'likes': '', 'views': '慢慢试'})
        self.assertEqual(self.brain.selfnarration_versions()[0]['profile']['likes'], '薄荷')
        count = len(self.brain.selfnarration_versions())
        self.assertFalse(self.brain.update_profile({'likes': ''}, note='保持')['changed'])
        self.assertEqual(len(self.brain.selfnarration_versions()), count)

    def test_rejects_permission_fields_and_invalid_values_without_writing(self):
        self.assertTrue(hasattr(self.brain, 'update_profile'), '缺少具名成长入口')
        for patch in ({'owner': 'operator'}, {'likes': []}, {'likes': 'x' * 601}):
            with self.subTest(patch=str(patch)[:30]), self.assertRaises(ValueError):
                self.brain.update_profile(patch, note='测试')
        self.assertEqual(self.brain.selfnarration_versions(), [])
