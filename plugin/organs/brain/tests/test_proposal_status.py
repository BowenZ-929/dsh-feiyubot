"""提案被处理之后要落状态：mark_proposal 只原地打终态，别的一个字不动。

背景（真 bug）：accept_proposal 只写魂的账，proposals.json 里没有终态，于是面板刷新
照旧显示「等人点头」，再点一次被演化五闸的 GATE_COOLDOWN 拦下——人看到的是「点了没反应」。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.brain import BrainStore
from organs.brain.tests.helpers import fresh_store, ingest


def _proposal(pid: str, value: str = "不熟就先接话") -> dict:
    return {"id": pid, "target": "manner.stance", "value": value,
            "evidence": ["m-1", "m-2", "m-3"], "reason": "最近几条都指同一个方向"}


def _write_batch(root: Path, day: str, rows) -> None:
    path = Path(root) / "proposals.json"
    path.write_text(json.dumps({"night_of": day, "proposals": rows},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class MarkProposalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = BrainStore(Path(self._tmp.name) / "brain")
        self.path = self.store.root / "proposals.json"
        self.first = _proposal("bp-2026-11-04-1")
        self.second = _proposal("bp-2026-11-04-2", value="说话先软一句")
        _write_batch(self.store.root, "2026-11-04", [self.first, self.second])

    def test_accept_writes_status_in_place_and_keeps_other_fields(self):
        result = self.store.mark_proposal("bp-2026-11-04-1", "accepted", "panel",
                                         "2026-11-05T09:00:00+00:00")
        self.assertTrue(result["ok"])
        batch = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual("2026-11-04", batch["night_of"], "整份文件要写回，night_of 不许丢")
        rows = {row["id"]: row for row in batch["proposals"]}
        accepted = rows["bp-2026-11-04-1"]
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual("panel", accepted["actor"])
        self.assertEqual("2026-11-05T09:00:00+00:00", accepted["ts"])
        for key, value in self.first.items():
            self.assertEqual(value, accepted[key], "提案正文不许被回写改掉：%s" % key)
        self.assertEqual(self.second, rows["bp-2026-11-04-2"], "没选中的那条一个字都不许动")

    def test_reject_keeps_her_reason_and_writes_decision_reason(self):
        result = self.store.mark_proposal("bp-2026-11-04-2", "rejected", "panel",
                                         "2026-11-05T09:00:00+00:00", "这一条不太对")
        self.assertTrue(result["ok"])
        rows = {row["id"]: row for row in
                json.loads(self.path.read_text(encoding="utf-8"))["proposals"]}
        rejected = rows["bp-2026-11-04-2"]
        self.assertEqual("rejected", rejected["status"])
        # 她「为什么提」那句话一个字不许被人的处理结果盖掉；人的理由另记一栏
        self.assertEqual(self.second["reason"], rejected["reason"])
        self.assertEqual("这一条不太对", rejected["decision_reason"])
        self.assertEqual("panel", rejected["actor"])
        self.assertEqual(self.first, rows["bp-2026-11-04-1"])

    def test_accept_carries_no_decision_reason(self):
        self.store.mark_proposal("bp-2026-11-04-1", "accepted", "panel",
                                 "2026-11-05T09:00:00+00:00")
        rows = {row["id"]: row for row in
                json.loads(self.path.read_text(encoding="utf-8"))["proposals"]}
        accepted = rows["bp-2026-11-04-1"]
        self.assertEqual("accepted", accepted["status"])
        self.assertNotIn("decision_reason", accepted,
                         "接受没有「拒绝理由」：不许凭空多出一个键")
        self.assertEqual(self.first["reason"], accepted["reason"])

    def test_unknown_id_reports_and_writes_nothing(self):
        before = self.path.read_bytes()
        result = self.store.mark_proposal("没有这条", "accepted", "panel",
                                         "2026-11-05T09:00:00+00:00")
        self.assertFalse(result["ok"])
        self.assertIn("没有这条", result["message"])
        self.assertIn("没有这条提案", result["message"])
        self.assertEqual(before, self.path.read_bytes(), "找不到 id 就不许写盘")

    def test_only_two_terminal_statuses(self):
        for bad in ("pending", "APPROVED", "", None):
            with self.assertRaises(ValueError):
                self.store.mark_proposal("bp-2026-11-04-1", bad, "panel", "2026-11-05T09:00:00+00:00")

    def test_status_survives_a_restart(self):
        self.store.mark_proposal("bp-2026-11-04-1", "accepted", "panel",
                                 "2026-11-05T09:00:00+00:00")
        reopened = BrainStore(self.store.root)
        rows = {row["id"]: row for row in reopened.proposals()["proposals"]}
        self.assertEqual("accepted", rows["bp-2026-11-04-1"]["status"])
        self.assertEqual("panel", rows["bp-2026-11-04-1"]["actor"])

    def test_night_batch_shape_is_unchanged(self):
        """新提案写入时 status 缺省不带：run_night 的产物形状不动。"""
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "你今天有点不理人", "2026-11-03T10:00:00+00:00")
        ingest(store, "567890123", "怎么又冷淡了", "2026-11-03T10:02:00+00:00")
        ingest(store, "3425635473", "对生人也别不理", "2026-11-03T10:04:00+00:00")
        night = store.run_night("2026-11-04T05:00:00+00:00")
        self.assertTrue(night["proposals"])
        for proposal in night["proposals"]:
            self.assertNotIn("status", proposal, "新提案不带 status：缺省按 pending 读")
            self.assertNotIn("actor", proposal)
            self.assertNotIn("ts", proposal)


if __name__ == "__main__":
    unittest.main()
