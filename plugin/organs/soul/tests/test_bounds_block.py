"""M3 不可覆盖尾块：红线块只许逐条加、逐条撤，不许被整块替换或被压缩产物覆盖。

翻译口径：人本来就能加线、撤线（active=false），所以这条不是「红线不可改」——
要做的是把「整块写红线」这条捷径关掉：edit 动 bounds.items 整表、migrate 的目标块是 bounds，
一律拒（BOUNDS_BLOCK_REPLACE_FORBIDDEN）。加线走 add_redline，撤线走 retire_redline
（⑤ 的动作名专用：旧名 add_bounds_item / retire_bounds_item 已删，不留别名）。
整档回滚（rollback）是唯一能一次换三张卡的路，它是 M5 的「回到某一版」，不是「重写红线」。

另有三条同块的规矩（⑥）：加/撤都必须写 reason（REDLINE_REASON_REQUIRED）；
都不占那 2 次月限额；同一条线撤了 7 天内不许加回（REDLINE_RERELEASE_COOLDOWN）。
"""
from __future__ import annotations

import json
import tempfile
import unittest

from organs.soul import model, store as store_module
from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture, proposal, read_bytes, read_json


class BoundsBlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def _items(self):
        return read_json(self.root / "bounds.json")["items"]

    def test_whole_block_replace_is_rejected(self):
        """整块替换（path 为空 + structural）也是整块写红线：id 一条不少也照样拒。"""
        before = read_bytes(self.root / "bounds.json")
        rewritten = {"items": [dict(item, rule="整块重写过的第 %d 条" % index)
                               for index, item in enumerate(self._items())]}
        result = self.store.edit(actor="bowen", reason="一次性重写整段红线", block="bounds",
                                 path="", value=rewritten, structural=True)
        self.assertFalse(result["ok"], result)
        self.assertEqual("BOUNDS_BLOCK_REPLACE_FORBIDDEN", result["issues"][0]["code"])
        self.assertIn("逐条", result["message"])
        self.assertEqual(before, read_bytes(self.root / "bounds.json"))
        self.assertEqual([], self.store.history())

    def test_whole_table_replace_is_rejected(self):
        before = read_bytes(self.root / "bounds.json")
        rewritten = [dict(item, rule="整表重写过的第 %d 条" % index)
                     for index, item in enumerate(self._items())]
        result = self.store.edit(actor="bowen", reason="整表重写", block="bounds",
                                 path="items", value=rewritten)
        self.assertFalse(result["ok"], result)
        self.assertEqual("BOUNDS_BLOCK_REPLACE_FORBIDDEN", result["issues"][0]["code"])
        self.assertEqual(before, read_bytes(self.root / "bounds.json"))

    def test_deleting_an_item_still_reports_the_delete(self):
        """删线比「整块替换」更接近人要干的事，所以删的拒因先报（口径不变）。"""
        result = self.store.edit(actor="bowen", reason="删一条", block="bounds",
                                 path="items", value=self._items()[:1])
        self.assertFalse(result["ok"], result)
        self.assertEqual("BOUNDS_DELETE_ATTEMPT", result["issues"][0]["code"])

    def test_migrate_may_not_target_bounds(self):
        payload = {"items": self._items()}
        digest = store_module._digest_bytes(read_bytes(self.root / "bounds.json"))
        before = read_bytes(self.root / "bounds.json")
        result = store_module.migrate(self.root, actor="bowen", reason="顺手清理红线",
                                      block="bounds", new_value=payload,
                                      expect_old_digest=digest)
        self.assertFalse(result["ok"], result)
        self.assertEqual("BOUNDS_BLOCK_REPLACE_FORBIDDEN", result["issues"][0]["code"])
        self.assertEqual(before, read_bytes(self.root / "bounds.json"))

    def test_migrate_other_blocks_is_still_allowed(self):
        write = json.dumps({"stance": "换完的姿态"}, ensure_ascii=False)
        digest = store_module._digest_bytes(read_bytes(self.root / "manner.json"))
        result = store_module.migrate(self.root, actor="bowen", reason="换个形状",
                                      block="manner", new_value=json.loads(write),
                                      expect_old_digest=digest)
        self.assertTrue(result["ok"], result)

    def test_one_line_can_be_added_and_only_it_appears(self):
        before = self._items()
        result = self.store.add_redline(
            actor="bowen", reason="补一条群聊边界",
            item={"id": "b03", "rule": "不在群里念别人的账号",
                  "match": {"type": "literal", "pattern": "不在群里念别人的账号"},
                  "action": "block", "active": True, "enforced_by": "soul·字面"})
        self.assertTrue(result["ok"], result)
        items = self._items()
        self.assertEqual(3, len(items))
        self.assertEqual(before, items[:2])                  # 原有两条逐字不动
        self.assertEqual("b03", items[2]["id"])
        line = self.store.history()[-1]
        self.assertEqual("bounds", line["block"])
        self.assertEqual("items[2]", line["path"])

    def test_adding_a_duplicate_id_is_rejected(self):
        result = self.store.add_redline(
            actor="bowen", reason="撞 id",
            item={"id": "b01", "rule": "另一条",
                  "match": {"type": "literal", "pattern": "另一条"},
                  "action": "block", "active": True, "enforced_by": "soul·字面"})
        self.assertFalse(result["ok"], result)
        self.assertIn("BOUNDS_ID_DUPLICATE",
                      [item["code"] for item in result["issues"]])
        self.assertEqual(2, len(self._items()))

    def test_one_line_can_be_retired_but_stays_in_the_card(self):
        result = self.store.retire_redline(actor="bowen", reason="这条不再需要",
                                            item_id="b02")
        self.assertTrue(result["ok"], result)
        items = self._items()
        self.assertEqual(2, len(items))                       # 不物理删除
        self.assertEqual("b02", items[1]["id"])               # id 还在
        self.assertFalse(items[1]["active"])
        self.assertNotIn(items[1]["rule"], self.store.render())   # 注入里消失
        self.assertEqual([], self.store.match_bounds(items[1]["match"]["pattern"]))

    def test_retiring_twice_is_rejected(self):
        first = self.store.retire_redline(actor="bowen", reason="撤线", item_id="b02")
        self.assertTrue(first["ok"], first)
        second = self.store.retire_redline(actor="bowen", reason="再撤一次", item_id="b02")
        self.assertFalse(second["ok"], second)
        self.assertIn("已经是撤下状态", second["message"])

    def test_retiring_an_unknown_id_is_rejected(self):
        result = self.store.retire_redline(actor="bowen", reason="撤线", item_id="nope")
        self.assertFalse(result["ok"], result)
        self.assertIn("没有 id", result["message"])

    def test_acceptance_delete_blocked_then_active_false_passes(self):
        """验收二：删掉一条（id 还在的整条删除）被拒；用 active=false 撤线通过。"""
        before = read_bytes(self.root / "bounds.json")
        deleted = self.store.edit(actor="bowen", reason="删掉一条", block="bounds",
                                  path="items[0]", value=None)
        self.assertFalse(deleted["ok"], deleted)
        self.assertEqual("BOUNDS_DELETE_ATTEMPT", deleted["issues"][0]["code"])
        self.assertEqual(before, read_bytes(self.root / "bounds.json"))
        retired = self.store.edit(actor="bowen", reason="撤线", block="bounds",
                                  path="items[0].active", value=False)
        self.assertTrue(retired["ok"], retired)
        items = self._items()
        self.assertEqual(2, len(items))
        self.assertEqual("b01", items[0]["id"])
        self.assertFalse(items[0]["active"])

    def test_bounds_edit_of_a_leaf_is_still_allowed(self):
        result = self.store.edit(actor="bowen", reason="改一句禁令", block="bounds",
                                 path="items[0].rule", value="不自称 AI，也不解释设定")
        self.assertTrue(result["ok"], result)
        self.assertEqual("不自称 AI，也不解释设定", self._items()[0]["rule"])


class RedlineActionRulesTest(unittest.TestCase):
    """⑥ 红线加/撤的三条规矩：必须写 reason、不占月限额、撤了 7 天内不许加回。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def _items(self):
        return read_json(self.root / "bounds.json")["items"]

    def _item(self, item_id="b09", rule="不在群里念别人的账号"):
        return {"id": item_id, "rule": rule,
                "match": {"type": "literal", "pattern": rule},
                "action": "block", "active": True, "enforced_by": "soul·字面"}

    def _shift(self, hours: float) -> str:
        """以账末那一行的时间为基准，往后推 hours 小时（自测注入时间，不真等）。"""
        epoch = store_module.iso_to_epoch(self.store.history()[-1]["ts"])
        return store_module.iso_from_epoch(epoch + hours * 3600)

    def test_add_redline_without_reason_is_rejected(self):
        before = read_bytes(self.root / "bounds.json")
        result = self.store.add_redline(actor="bowen", reason="   ", item=self._item())
        self.assertFalse(result["ok"], result)
        self.assertEqual(store_module.REDLINE_REASON_REQUIRED, result["issues"][0]["code"])
        self.assertEqual(before, read_bytes(self.root / "bounds.json"))
        self.assertEqual([], self.store.history())

    def test_retire_redline_without_reason_is_rejected(self):
        result = self.store.retire_redline(actor="bowen", reason="", item_id="b02")
        self.assertFalse(result["ok"], result)
        self.assertEqual(store_module.REDLINE_REASON_REQUIRED, result["issues"][0]["code"])
        self.assertEqual(2, len(self._items()))
        self.assertTrue(self._items()[1]["active"])
        self.assertEqual([], self.store.history())

    def test_redlines_do_not_use_the_monthly_proposal_quota(self):
        """两次红线写入之后本月提案数仍是 0：把月限压到 1 份也照样能接受提案。"""
        self.assertTrue(self.store.add_redline(actor="bowen", reason="补一条",
                                               item=self._item())["ok"])
        self.assertTrue(self.store.retire_redline(actor="bowen", reason="撤这条",
                                                  item_id="b09")["ok"])
        rows = self.store.history()
        self.assertEqual([], store_module.proposals_in_month(rows, store_module.now_iso()))
        result = self.store.accept_proposal(actor="bowen", proposal=proposal(), monthly_limit=1)
        self.assertTrue(result["ok"], result)

    def test_add_redline_back_within_seven_days_is_rejected(self):
        """撤了又加（再走一次 add_redline）：拒因是复燃冷却，不是笼统的撞 id。"""
        self.assertTrue(self.store.retire_redline(actor="bowen", reason="先撤下",
                                                  item_id="b02")["ok"])
        too_soon = self.store.add_redline(actor="bowen", reason="又想加回来",
                                          item=self._item("b02", "有人难过时，不嘲讽、不玩梗"),
                                          now=self._shift(6 * 24))
        self.assertFalse(too_soon["ok"], too_soon)
        self.assertEqual(store_module.REDLINE_RERELEASE_COOLDOWN,
                         too_soon["issues"][0]["code"])
        self.assertFalse(self._items()[1]["active"])
        self.assertEqual(2, len(self._items()))

    def test_setting_active_back_to_true_within_seven_days_is_rejected(self):
        """另一条「加回」的路（把 active 改回 true）走同一把尺子：7 天内一样拒。"""
        self.assertTrue(self.store.retire_redline(actor="bowen", reason="先撤下",
                                                  item_id="b02")["ok"])
        result = self.store.edit(actor="bowen", reason="加回来", block="bounds",
                                 path="items[1].active", value=True,
                                 now=self._shift(6 * 24))
        self.assertFalse(result["ok"], result)
        self.assertEqual(store_module.REDLINE_RERELEASE_COOLDOWN, result["issues"][0]["code"])
        self.assertFalse(self._items()[1]["active"])

    def test_redline_can_be_added_back_after_seven_days(self):
        self.assertTrue(self.store.retire_redline(actor="bowen", reason="先撤下",
                                                  item_id="b02")["ok"])
        result = self.store.edit(actor="bowen", reason="加回来", block="bounds",
                                 path="items[1].active", value=True,
                                 now=self._shift(8 * 24))
        self.assertTrue(result["ok"], result)
        self.assertTrue(self._items()[1]["active"])


if __name__ == "__main__":
    unittest.main()
