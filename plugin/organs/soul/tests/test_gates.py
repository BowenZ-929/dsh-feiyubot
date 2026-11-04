"""M6 演化五闸：接在 accept_proposal 前面，是「人接受提案」之前的前置机器校验。

① 依据 ≥3 条且不重复（同源校验等脑接上记忆库再补——现在只有字符串 id，只能查重）
② 单次改动 ≤ 该字段可编辑范围的 1/10，最小 3 字
③ 同一字段冷却 168 小时
④ 护栏：不得移动红线、不得删条目
⑤ 落盘前先存整档（M5）：取不到归档就整次拒
任一不过即拒，拒因 GATE_EVIDENCE / GATE_STEP / GATE_COOLDOWN / GATE_GUARD / GATE_SNAPSHOT。
"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from organs.soul import gates, model, store as store_module
from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture, proposal, read_bytes

FIXTURE_STANCE = "【待定稿】"          # 5 字；stance 的步长是 100 的 1/10 = 10 字


def codes(issues) -> list:
    return [issue.code for issue in issues]


class EvidenceGateTest(unittest.TestCase):
    def test_gate_needs_at_least_three(self):
        self.assertEqual([gates.EVIDENCE], codes(gates.evidence_issues(["m-1"])))
        self.assertIn("同源", gates.evidence_issues(["m-1"])[0].message)

    def test_duplicated_evidence_is_rejected(self):
        self.assertEqual([gates.EVIDENCE], codes(gates.evidence_issues(["m-1", "m-1", "m-1"])))
        self.assertIn("重复", gates.evidence_issues(["m-1", "m-1", "m-3"])[0].message)

    def test_three_distinct_evidences_pass(self):
        self.assertEqual([], gates.evidence_issues(["m-1", "m-2", "m-3"]))


class StepGateTest(unittest.TestCase):
    def test_step_table_is_one_tenth_with_a_three_char_floor(self):
        self.assertEqual({
            "manner.stance": 10,
            "identity.self_names.display": 4,
            "identity.appearance": 6,
            "identity.fears": 6,
            "bounds.items[].rule": 25,
        }, gates.step_table())

    def test_a_jump_beyond_one_tenth_is_rejected(self):
        issues = gates.step_issues("manner", "stance", FIXTURE_STANCE, "对谁都先笑一笑，再把话说慢一点，别急着替人下结论")
        self.assertEqual([gates.STEP], codes(issues))
        self.assertIn("步长", issues[0].message)

    def test_a_small_step_passes(self):
        self.assertEqual([], gates.step_issues("manner", "stance", FIXTURE_STANCE, "先笑一笑"))

    def test_unknown_field_has_no_step_and_is_rejected(self):
        self.assertEqual([gates.STEP], codes(gates.step_issues("manner", "matrix", "a", "b")))


class ProposalGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def _accept(self, **kwargs):
        params = {"actor": "bowen", "proposal": proposal(target="manner.stance",
                                                         value="先笑一笑")}
        params.update(kwargs)
        return self.store.accept_proposal(**params)

    def test_duplicated_evidence_is_rejected_before_writing(self):
        before = read_bytes(self.root / "manner.json")
        result = self._accept(proposal=proposal(evidence=["m-1", "m-1", "m-3"]))
        self.assertFalse(result["ok"], result)
        self.assertEqual(gates.EVIDENCE, result["issues"][0]["code"])
        self.assertEqual(before, read_bytes(self.root / "manner.json"))
        self.assertEqual([], self.store.history())

    def test_oversized_step_is_rejected(self):
        """验收五之二：改动超过可编辑范围 1/10 → 拒，拒因含「步长」。"""
        result = self._accept(proposal=proposal(value="对谁都先笑一笑，再把话说慢一点，别急着替人下结论"))
        self.assertFalse(result["ok"], result)
        self.assertEqual(gates.STEP, result["issues"][0]["code"])
        self.assertIn("步长", result["message"] + result["issues"][0]["message"])

    def test_cooldown_blocks_a_second_change_within_an_hour(self):
        """验收五之一：同一字段 1 小时内连改两次 → 第二次拒，拒因含「冷却」。"""
        now = "2026-03-10T09:00:00+00:00"
        first = self._accept(now=now)
        self.assertTrue(first["ok"], first)
        second = self._accept(now="2026-03-10T09:40:00+00:00",
                              proposal=proposal(id="p-2", value="再笑一笑"))
        self.assertFalse(second["ok"], second)
        self.assertEqual(gates.COOLDOWN, second["issues"][0]["code"])
        self.assertIn("冷却", second["message"] + second["issues"][0]["message"])

    def test_cooldown_counts_a_hand_edit_of_the_same_field(self):
        self.assertTrue(self.store.edit(actor="bowen", reason="手改一句", block="manner",
                                        path="stance", value="先笑一笑")["ok"])
        result = self._accept()
        self.assertFalse(result["ok"], result)
        self.assertEqual(gates.COOLDOWN, result["issues"][0]["code"])

    def test_cooldown_opens_after_168_hours(self):
        first = self._accept(now="2026-03-10T09:00:00+00:00")
        self.assertTrue(first["ok"], first)
        second = self._accept(now="2026-03-17T10:00:00+00:00",
                              proposal=proposal(id="p-2", value="再笑一笑"))
        self.assertTrue(second["ok"], second)

    def test_probe_rows_do_not_lock_the_field(self):
        """probe 是「为验证链路而改」的显式声明：不该白锁 168 小时。"""
        self.assertTrue(self.store.edit(actor="壳线", reason="验证", block="manner",
                                        path="stance", value="探一针", probe=True)["ok"])
        result = self._accept()
        self.assertTrue(result["ok"], result)

    def test_snapshot_failure_rejects_the_proposal(self):
        before = read_bytes(self.root / "manner.json")
        with mock.patch.object(store_module, "archive_now", side_effect=OSError("磁盘满了")):
            result = self._accept()
        self.assertFalse(result["ok"], result)
        self.assertEqual(gates.SNAPSHOT, result["issues"][0]["code"])
        self.assertEqual(before, read_bytes(self.root / "manner.json"))
        self.assertEqual([], self.store.history())

    def test_gates_do_not_run_for_a_plain_edit(self):
        """五闸是提案的前置校验：人手直接改值走 edit，不受冷却与步长约束（判据不变）。"""
        now_first = self.store.edit(actor="bowen", reason="第一句", block="manner",
                                    path="stance", value="先笑一笑")
        self.assertTrue(now_first["ok"], now_first)
        second = self.store.edit(actor="bowen", reason="第二句", block="manner",
                                 path="stance", value="对谁都先笑一笑，再把话说慢一点")
        self.assertTrue(second["ok"], second)


class GuardGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = load(copy_fixture("valid", self._tmp.name))
        self.bounds = self.store.snapshot().bounds

    def test_unchanged_bounds_pass(self):
        self.assertEqual([], gates.guard_issues(self.bounds, self.bounds))

    def test_moving_a_red_line_is_rejected(self):
        moved = model.Bounds(items=tuple(reversed(self.bounds.items)))
        self.assertEqual([gates.GUARD], codes(gates.guard_issues(self.bounds, moved)))

    def test_deleting_a_line_is_rejected(self):
        dropped = model.Bounds(items=self.bounds.items[:1])
        self.assertEqual([gates.GUARD], codes(gates.guard_issues(self.bounds, dropped)))

    def test_rewording_a_line_is_rejected(self):
        items = list(self.bounds.items)
        items[0] = model.BoundsItem(id=items[0].id, rule="软一点的写法", match_type="literal",
                                    pattern="软一点的写法", action="block", active=True)
        self.assertEqual([gates.GUARD],
                         codes(gates.guard_issues(self.bounds, model.Bounds(items=tuple(items)))))


if __name__ == "__main__":
    unittest.main()
