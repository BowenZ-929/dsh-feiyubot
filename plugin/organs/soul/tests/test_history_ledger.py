"""变更史只增不改：账对不上块就报错，写不成一律回滚。"""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock

from organs.soul import checks, store as store_module
from organs.soul.store import load
from organs.soul.tests.helpers import (append_row, copy_fixture, edit_card, proposal,
                                       read_bytes, read_json)


def codes(issues) -> list:
    return [issue.code for issue in issues]


class HistoryLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def _edit_tone(self, value: str = "改完的基调"):
        result = self.store.edit(actor="bowen", reason="改基调", block="manner",
                                 path="stance", value=value)
        self.assertTrue(result["ok"], result)
        return result

    def test_manual_card_change_without_ledger_is_flagged(self):
        self._edit_tone()
        edit_card(self.root, "manner", lambda data: data.update({"stance": "我手改的，没记账"}))
        self.assertIn("HISTORY_UNLOGGED_CHANGE", codes(checks.collect(self.root)))

    def test_logged_edit_is_not_flagged(self):
        self._edit_tone()
        self.assertNotIn("HISTORY_UNLOGGED_CHANGE", codes(checks.collect(self.root)))

    def test_ledger_only_appends(self):
        self._edit_tone("第一次")
        first = self.store.history()
        self._edit_tone("第二次")
        rows = self.store.history()
        self.assertEqual(2, len(rows))
        self.assertEqual(first[0], rows[0])                       # 旧行一字不改
        self.assertEqual(rows[0]["new_digest"], rows[1]["old_digest"])   # 前后接得上
        lines = (self.root / "history.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, len([line for line in lines if line.strip()]))

    def test_proposal_row_without_evidence_is_flagged(self):
        append_row(self.root, {"ts": store_module.now_iso(), "kind": "proposal",
                               "proposal_id": "p-x", "target": "manner.stance",
                               "block": "manner", "path": "stance", "reason": "没写依据"})
        self.assertIn("PROPOSAL_EVIDENCE_MISSING", codes(checks.collect(self.root)))

    def test_proposal_row_without_id_is_flagged(self):
        append_row(self.root, {"ts": store_module.now_iso(), "kind": "proposal",
                               "evidence": ["mem-1", "mem-2", "mem-3"],
                               "target": "manner.stance", "block": "manner", "path": "stance",
                               "reason": "没写提案 id"})
        self.assertIn("PROPOSAL_EVIDENCE_MISSING", codes(checks.collect(self.root)))

    def test_proposal_rows_over_monthly_limit_are_flagged(self):
        for index in range(3):
            append_row(self.root, {"ts": "2026-03-0%dT09:00:00+00:00" % (index + 1),
                                   "kind": "proposal", "proposal_id": "p-%d" % index,
                                   "evidence": ["mem-1", "mem-2", "mem-3"],
                                   "target": "manner.stance", "block": "manner", "path": "stance",
                                   "reason": "绕过限额塞进来的"})
        self.assertIn("PROPOSAL_MONTHLY_LIMIT_EXCEEDED", codes(checks.collect(self.root)))

    def test_proposal_row_touching_bounds_is_flagged(self):
        append_row(self.root, {"ts": store_module.now_iso(), "kind": "proposal",
                               "proposal_id": "p-b", "evidence": ["mem-1", "mem-2", "mem-3"],
                               "target": "bounds.items[0].rule", "block": "bounds",
                               "path": "items[0].rule", "reason": "偷偷软化红线"})
        self.assertIn("PROPOSAL_FROZEN_TARGET", codes(checks.collect(self.root)))

    def test_manual_row_is_a_warn_with_its_location(self):
        append_row(self.root, {"ts": store_module.now_iso(), "kind": "manual", "actor": "bowen",
                               "block": "manner", "path": "matrix", "reason": "手工改盘补记"})
        issues = checks.collect(self.root)
        manual = [issue for issue in issues if issue.code == "MANUAL_EDIT_IN_LEDGER"]
        self.assertEqual(1, len(manual), codes(issues))
        self.assertEqual("warn", manual[0].level)
        self.assertIn("manner.matrix", manual[0].message)
        self.assertIn("需人工复核", manual[0].message)

    def test_unknown_kind_is_an_error(self):
        append_row(self.root, {"ts": store_module.now_iso(), "kind": "handmade", "actor": "bowen",
                               "block": "manner", "path": "stance", "reason": "自己编的 kind"})
        issues = checks.collect(self.root)
        self.assertIn("HISTORY_KIND_UNKNOWN", codes(issues))
        self.assertEqual("error", [item for item in issues
                                   if item.code == "HISTORY_KIND_UNKNOWN"][0].level)

    def test_legacy_rows_without_kind_are_tolerated(self):
        """kind 是后加的字段：旧账只增不改，缺字段不是"未知字符串"，也不该被当成手工改盘。"""
        append_row(self.root, {"ts": store_module.now_iso(), "actor": "bowen", "block": "manner",
                               "path": "stance", "old_summary": "旧账号", "new_summary": "新账号",
                               "reason": "早于 kind 字段的账行"})
        found = codes(checks.collect(self.root))
        self.assertNotIn("HISTORY_KIND_UNKNOWN", found)
        self.assertNotIn("MANUAL_EDIT_IN_LEDGER", found)

    def test_kind_vocabulary_is_exactly_four(self):
        """edit 真改 / migrate 换形状 / probe 探测 / manual 手工补账（只许事故用）。"""
        self.assertEqual(("edit", "migrate", "probe", "manual"), store_module.KINDS)
        # proposal 是第五种账行：由 accept_proposal 写，check 单列校验，不混进这四类
        self.assertNotIn("proposal", store_module.KINDS)
        self.assertIn("proposal", store_module.KNOWN_KINDS)

    def test_structural_edit_logs_migrate_not_edit(self):
        result = self.store.edit(actor="bowen", reason="换形状", block="manner",
                                 path="", value={"stance": "整块换掉"}, structural=True)
        self.assertTrue(result["ok"], result)
        line = self.store.history()[-1]
        self.assertEqual("migrate", line["kind"])
        self.assertTrue(line["structural"])

    def test_probe_edit_logs_probe_and_keeps_structural(self):
        result = self.store.edit(actor="壳线-验收", reason="验证写路径", block="manner",
                                 path="stance", value="探一针", probe=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual("probe", self.store.history()[-1]["kind"])
        self.assertNotIn("structural", self.store.history()[-1])
        both = self.store.edit(actor="壳线-验收", reason="验证写路径", block="manner",
                               path="", value={"stance": "探两针"}, probe=True,
                               structural=True)
        self.assertTrue(both["ok"], both)
        line = self.store.history()[-1]
        self.assertEqual("probe", line["kind"])       # probe 优先
        self.assertTrue(line["structural"])           # 但 structural 留在账行里

    def test_probe_and_migrate_rows_are_not_flagged(self):
        self.store.edit(actor="壳线", reason="验证", block="manner", path="stance",
                        value="探一针", probe=True)
        self.store.edit(actor="bowen", reason="换形状", block="manner", path="",
                        value={"stance": "整块换掉"}, structural=True)
        found = codes(checks.collect(self.root))
        self.assertNotIn("HISTORY_KIND_UNKNOWN", found)
        self.assertNotIn("MANUAL_EDIT_IN_LEDGER", found)

    def test_no_store_path_writes_the_manual_kind(self):
        """manual 只由人补记：程序侧的写入口写不出它（写形声明只给 edit/migrate/probe/proposal）。

        改的是 identity.fears 而不是 manner.stance：M6 的 168 小时冷却按字段算，
        同一格刚 edit 过就接受提案会被 GATE_COOLDOWN 拦住（那是另一条测试的事）。
        """
        edited = self.store.edit(actor="bowen", reason="改怕什么", block="identity",
                                 path="fears", value="她怕被丢下")
        self.assertTrue(edited["ok"], edited)
        result = self.store.accept_proposal(actor="bowen", proposal=proposal())
        self.assertTrue(result["ok"], result)
        kinds = [row.get("kind") for row in self.store.history()]
        self.assertEqual(["edit", "proposal"], kinds)
        self.assertNotIn(store_module.KIND_MANUAL, kinds)

    def test_append_failure_rolls_back_the_whole_write(self):
        before = read_bytes(self.root / "manner.json")
        with mock.patch.object(store_module, "append_history", side_effect=OSError("磁盘满了")):
            result = self.store.edit(actor="bowen", reason="改基调", block="manner",
                                     path="stance", value="不该落盘")
        self.assertFalse(result["ok"])
        self.assertIn("回滚", result["message"])
        self.assertEqual(before, read_bytes(self.root / "manner.json"))
        self.assertEqual([], self.store.history())

    def test_proposal_append_failure_rolls_back_too(self):
        before = read_bytes(self.root / "manner.json")
        with mock.patch.object(store_module, "append_history", side_effect=OSError("磁盘满了")):
            result = self.store.accept_proposal(actor="bowen", proposal=proposal(
                target="manner.stance", value="不该落盘的基调"))
        self.assertFalse(result["ok"])
        self.assertEqual(before, read_bytes(self.root / "manner.json"))
        self.assertEqual([], self.store.history())

    def test_history_is_not_written_by_reading(self):
        self._edit_tone()
        before = read_bytes(self.root / "history.jsonl")
        checks.collect(self.root)
        self.root.joinpath("manner.json").read_text(encoding="utf-8")
        self.store.snapshot()
        self.store.render("熟人")
        self.store.match_bounds("今天天气不错")
        self.assertEqual(before, read_bytes(self.root / "history.jsonl"))


if __name__ == "__main__":
    unittest.main()

