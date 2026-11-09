"""三层闸：白名单 → 单次授权 → 额度。顺序、码、以及「say 要说清缺哪一条」。"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from organs.hand import permit, whitelist

from . import helpers


class WhitelistTest(unittest.TestCase):
    def test_exactly_six_named_actions_in_the_frozen_order(self):
        self.assertEqual(whitelist.ACTIONS,
                         ("add_task", "complete_task", "send_image", "draw", "set_style",
                          "lookup"))

    def test_generic_capabilities_are_not_on_the_list(self):
        for name in ("write_file", "read_file", "run", "shell", "exec", "delete", "register"):
            self.assertFalse(whitelist.allowed(name), name)

    def test_allowed_does_not_normalise_names(self):
        self.assertTrue(whitelist.allowed("add_task"))
        self.assertFalse(whitelist.allowed("ADD_TASK"))
        self.assertFalse(whitelist.allowed(" add_task"))


class PermitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)

    def test_every_action_is_reversible_and_needs_no_grant(self):
        # 新增的三个也走「可撤销」：发图可撤回、画只落本地的盘、改口吻只改一张偏好卡；
        # 本轮不另立一次性授权。
        self.assertEqual(permit.ACTION_RISK,
                         {"add_task": "可撤销", "complete_task": "可撤销",
                          "send_image": "可撤销", "draw": "可撤销",
                          "set_style": "可撤销", "lookup": "可撤销"})
        for name in whitelist.ACTIONS:
            self.assertFalse(permit.needs_grant(name), name)

    def test_unknown_action_is_riskiest_tier(self):
        self.assertEqual(permit.risk_of("nuke"), "不可逆")
        self.assertTrue(permit.needs_grant("nuke"))

    def test_action_outside_whitelist_is_refused_at_gate_one(self):
        got = permit.check("write_file", who="1000xxxx", now=helpers.NOW, root=self.root)
        self.assertFalse(got["allowed"])
        self.assertEqual(got["code"], "ACTION_NOT_ALLOWED")
        self.assertIn("缺第 1 条", got["say"])
        self.assertIn("白名单", got["say"])

    def test_allowed_action_reports_how_many_left(self):
        got = permit.check("add_task", who="1000xxxx", now=helpers.NOW, root=self.root)
        self.assertTrue(got["allowed"])
        self.assertIsNone(got["code"])
        self.assertIn("还能", got["say"])
        self.assertEqual(got["detail"]["passed"], ["whitelist", "grant", "quota"])

    def test_injected_irreversible_action_is_refused_without_a_grant(self):
        # 注入一个假的不可逆动作：名单里加它、风险档标成不可逆。
        with mock.patch.object(whitelist, "ACTIONS", whitelist.ACTIONS + ("wipe_disk",)), \
             mock.patch.dict(permit.ACTION_RISK, {"wipe_disk": "不可逆"}):
            self.assertTrue(permit.needs_grant("wipe_disk"))
            got = permit.check("wipe_disk", who="1000xxxx", now=helpers.NOW, root=self.root)
        self.assertFalse(got["allowed"])
        self.assertEqual(got["code"], "GRANT_REQUIRED")
        self.assertIn("缺第 2 条", got["say"])
        self.assertIn("单次授权", got["say"])
        self.assertEqual(got["detail"]["gate"], "grant")

    def test_no_quota_left_is_refused_at_gate_three(self):
        helpers.write_limits(self.root, 0)
        got = permit.check("add_task", who="1000xxxx", now=helpers.NOW, root=self.root)
        self.assertFalse(got["allowed"])
        self.assertEqual(got["code"], "QUOTA_EXCEEDED")
        self.assertIn("还能", got["say"])
        self.assertIn("缺第 3 条", got["say"])

    def test_gate_order_whitelist_before_quota(self):
        # 名单外的动作连额度都不该数：拒绝码必须是白名单那条，不是额度那条。
        helpers.write_limits(self.root, 0)
        got = permit.check("write_file", who="1000xxxx", now=helpers.NOW, root=self.root)
        self.assertEqual(got["code"], "ACTION_NOT_ALLOWED")


if __name__ == "__main__":
    unittest.main()
