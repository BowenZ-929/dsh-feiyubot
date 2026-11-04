"""提案只许改 manner.stance：三条依据、每月限额、红线冻结、六轴不再是魂的 target。"""
from __future__ import annotations

import tempfile
import unittest

from organs.soul.store import load
from organs.soul.tests.helpers import (append_row, copy_fixture, proposal, read_json,
                                       write_json)


class ProposalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def _manner(self):
        return read_json(self.root / "manner.json")

    def test_accept_proposal_changes_tone_and_logs_one_line(self):
        result = self.store.accept_proposal(actor="bowen", proposal=proposal(
            target="manner.stance", value="改完的基调"))
        self.assertTrue(result["ok"], result)
        data = self._manner()
        self.assertEqual("改完的基调", data["stance"])       # 只改提案指的那一格
        self.assertEqual(["stance"], list(data))           # 矩阵已归脑：没有别的格可动
        rows = self.store.history()
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("proposal", row["kind"])
        self.assertEqual("p-0001", row["proposal_id"])
        self.assertEqual(["mem-1", "mem-2", "mem-3"], row["evidence"])
        self.assertEqual("manner.stance", row["target"])
        self.assertEqual("manner", row["block"])
        self.assertEqual("stance", row["path"])

    def test_accept_proposal_manner_tone_reaches_render(self):
        result = self.store.accept_proposal(
            actor="bowen", proposal=proposal(target="manner.stance", value="说话松一点，别绷着"))
        self.assertTrue(result["ok"], result)
        self.assertEqual("说话松一点，别绷着", self._manner()["stance"])
        self.assertIn("说话松一点，别绷着", self.store.render("熟人"))

    def test_direct_baseline_edit_is_an_unknown_block(self):
        """baseline 已经不是魂的块：edit 按未知 block 拒，不再有「不能直改」那条错误码。"""
        before = self._manner()
        result = self.store.edit(actor="bowen", reason="手滑", block="baseline",
                                 path="axes[0].inner", value=50)
        self.assertFalse(result["ok"])
        self.assertIn("block 只能是 identity/manner/bounds", result["message"])
        self.assertNotIn("BASELINE_DIRECT_EDIT", result["message"])
        self.assertEqual([], result["issues"])
        self.assertEqual(before, self._manner())
        self.assertEqual([], self.store.history())

    def test_accept_proposal_rejects_baseline_targets_as_unknown(self):
        """六轴卡归心：baseline.* 一律按未知 target 拒，不写盘、不记账。"""
        for target in ("baseline.axes[0].inner", "baseline.axes[0].shown", "baseline.status",
                       "baseline.axes[0]", "baseline"):
            result = self.store.accept_proposal(actor="bowen", proposal=proposal(
                target=target, value=55))
            self.assertFalse(result["ok"], target)
            self.assertIn("target 只许 manner.stance", result["message"])
            self.assertIn(str(target), result["message"])
        self.assertEqual([], self.store.history())

    def test_six_axis_card_is_gone_so_pending_axis_has_no_target(self):
        result = self.store.accept_proposal(actor="bowen", proposal=proposal(
            target="baseline.axes[4].inner", value=50))
        self.assertFalse(result["ok"])
        self.assertIn("六轴卡归心", result["message"])
        self.assertFalse((self.root / "baseline.json").exists())
        self.assertEqual([], self.store.history())

    def test_accept_proposal_requires_exactly_three_evidence(self):
        before = self._manner()
        two = self.store.accept_proposal(actor="bowen", proposal=proposal(
            evidence=["mem-1", "mem-2"]))
        self.assertFalse(two["ok"])
        self.assertIn("3 条", two["message"])
        blank = self.store.accept_proposal(actor="bowen", proposal=proposal(
            evidence=["mem-1", "", "mem-3"]))
        self.assertFalse(blank["ok"])
        self.assertEqual(before, self._manner())
        self.assertEqual([], self.store.history())

    def test_accept_proposal_rejects_redline_target(self):
        bounds_before = read_json(self.root / "bounds.json")
        for target in ("bounds.items[0].rule", "bounds.no-profanity.rule"):
            result = self.store.accept_proposal(actor="bowen", proposal=proposal(
                target=target, value="这条红线可以松一点"))
            self.assertFalse(result["ok"], target)
            self.assertIn("红线", result["message"])
        self.assertEqual(bounds_before, read_json(self.root / "bounds.json"))
        self.assertEqual([], self.store.history())

    def test_accept_proposal_rejects_target_outside_whitelist(self):
        for target in ("identity.self_names.display", "manner.matrix[0].note", "history"):
            result = self.store.accept_proposal(actor="bowen", proposal=proposal(target=target))
            self.assertFalse(result["ok"], target)
        self.assertEqual([], self.store.history())

    def test_accept_proposal_rejects_a_tone_that_is_not_text(self):
        before = self._manner()
        result = self.store.accept_proposal(actor="bowen", proposal=proposal(
            target="manner.stance", value=42))
        self.assertFalse(result["ok"])
        self.assertEqual(before, self._manner())
        self.assertEqual([], self.store.history())

    def test_monthly_limit_rejects_third_proposal_in_same_month(self):
        """月限额与冷却并存：同一格一周只许动一次，所以三份提案要隔 ≥168 小时。"""
        first_now = "2026-03-01T09:00:00+00:00"
        second_now = "2026-03-10T09:00:00+00:00"   # 距上一次 9 天，冷却已满
        third_now = "2026-03-20T09:00:00+00:00"    # 仍在三月：月限额已经用完
        first = self.store.accept_proposal(actor="bowen", now=first_now, proposal=proposal(
            id="p-1", target="manner.stance", value="第一次的基调"))
        second = self.store.accept_proposal(actor="bowen", now=second_now, proposal=proposal(
            id="p-2", target="manner.stance", value="第二次的基调"))
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        third = self.store.accept_proposal(actor="bowen", now=third_now, proposal=proposal(
            id="p-3", target="manner.stance", value="第三次的基调"))
        self.assertFalse(third["ok"])
        self.assertEqual("PROPOSAL_MONTHLY_LIMIT", third["issues"][0]["code"])
        self.assertEqual("第二次的基调", self._manner()["stance"])
        self.assertEqual(2, len(self.store.history()))

    def test_monthly_limit_counts_ledger_rows_already_in_this_month(self):
        """不靠等时间：往账里塞一行本月提案，再用注入的 now 构造「第二次」。"""
        old_now = "2026-03-01T09:00:00+00:00"     # 账里已有的那一份（同月）
        first_now = "2026-03-10T09:00:00+00:00"   # 距上一次 9 天，冷却已满
        second_now = "2026-03-25T09:00:00+00:00"  # 同月、冷却已满：只剩月限额能拦
        append_row(self.root, {"ts": old_now, "kind": "proposal", "proposal_id": "p-old",
                               "evidence": ["mem-1", "mem-2", "mem-3"],
                               "target": "manner.stance", "block": "manner", "path": "stance",
                               "reason": "上月留下的账"})
        first = self.store.accept_proposal(actor="bowen", now=first_now, monthly_limit=2,
                                           proposal=proposal(id="p-2", target="manner.stance",
                                                             value="松一点"))
        self.assertTrue(first["ok"], first)
        second = self.store.accept_proposal(actor="bowen", now=second_now, monthly_limit=2,
                                            proposal=proposal(id="p-3", target="manner.stance",
                                                              value="再松一点"))
        self.assertFalse(second["ok"])
        self.assertEqual("PROPOSAL_MONTHLY_LIMIT", second["issues"][0]["code"])

    def test_quota_free_but_cooldown_still_rejects(self):
        """两道闸是 AND：本月限额没满，但同一格 7 天内动过 → 照样拒（GATE_COOLDOWN）。"""
        first = self.store.accept_proposal(actor="bowen", now="2026-03-01T09:00:00+00:00",
                                          proposal=proposal(id="p-1", value="第一次的基调"))
        self.assertTrue(first["ok"], first)
        again = self.store.accept_proposal(actor="bowen", now="2026-03-02T09:00:00+00:00",
                                           monthly_limit=9,
                                           proposal=proposal(id="p-2", value="隔天又改"))
        self.assertFalse(again["ok"])
        self.assertEqual("GATE_COOLDOWN", again["issues"][0]["code"])
        self.assertEqual("第一次的基调", self._manner()["stance"])
        self.assertEqual(1, len(self.store.history()))

    def test_cooldown_free_but_monthly_quota_still_rejects(self):
        """反过来：冷却已满（9 天前改过），但本月配额用完 → 照样拒（PROPOSAL_MONTHLY_LIMIT）。"""
        append_row(self.root, {"ts": "2026-03-01T09:00:00+00:00", "kind": "proposal",
                               "proposal_id": "p-old", "evidence": ["mem-1", "mem-2", "mem-3"],
                               "target": "manner.stance", "block": "manner", "path": "stance",
                               "reason": "本月已经用过的那一份"})
        result = self.store.accept_proposal(actor="bowen", now="2026-03-10T09:00:00+00:00",
                                            monthly_limit=1,
                                            proposal=proposal(id="p-2", value="松一点"))
        self.assertFalse(result["ok"])
        self.assertEqual("PROPOSAL_MONTHLY_LIMIT", result["issues"][0]["code"])
        self.assertNotEqual("松一点", self._manner()["stance"])

    def test_next_month_is_a_fresh_quota(self):
        """月限额按月清零；冷却按格算，所以两月的第一次都落在 +9/+22 天处。"""
        first_march = "2026-03-01T09:00:00+00:00"
        second_march = "2026-03-10T09:00:00+00:00"
        third_march = "2026-03-20T09:00:00+00:00"    # 冷却已满，只剩月限额
        april = "2026-04-01T09:00:00+00:00"
        for index, now in enumerate((first_march, second_march)):
            result = self.store.accept_proposal(actor="bowen", now=now, proposal=proposal(
                id="p-%d" % index, target="manner.stance", value="第%d次" % index))
            self.assertTrue(result["ok"], result)
        blocked = self.store.accept_proposal(actor="bowen", now=third_march, proposal=proposal(
            id="p-x", target="manner.stance", value="超了"))
        self.assertFalse(blocked["ok"])
        fresh = self.store.accept_proposal(actor="bowen", now=april, proposal=proposal(
            id="p-april", target="manner.stance", value="下个月"))
        self.assertTrue(fresh["ok"], fresh)

    def test_accept_proposal_rejects_blank_actor_reason_or_id(self):
        self.assertFalse(self.store.accept_proposal(actor="  ", proposal=proposal())["ok"])
        self.assertFalse(self.store.accept_proposal(actor="bowen", proposal=proposal(reason=" "))["ok"])
        self.assertFalse(self.store.accept_proposal(actor="bowen", proposal=proposal(id=""))["ok"])
        self.assertFalse(self.store.accept_proposal(actor="bowen", proposal={"id": "x"})["ok"])

    def test_accept_proposal_result_is_json_safe(self):
        import json
        ok = self.store.accept_proposal(actor="bowen", proposal=proposal())
        bad = self.store.accept_proposal(actor="bowen", proposal=proposal(evidence=[]))
        missing = self.store.accept_proposal(actor="bowen", proposal=proposal(target="baseline.axes[0].inner"))
        json.dumps(ok, ensure_ascii=False)
        json.dumps(bad, ensure_ascii=False)
        json.dumps(missing, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
