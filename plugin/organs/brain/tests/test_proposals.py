"""每晚 0–2 条改基线提案，各附三条依据记忆；没材料就 0 条不硬凑。"""
from __future__ import annotations

import unittest

from organs.brain.tests.helpers import fresh_store, ingest


class ProposalsTest(unittest.TestCase):
    def test_three_evidence_entries_produce_one_proposal(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "你今天有点不理人", "2026-11-03T10:00:00+00:00")
        ingest(store, "567890123", "怎么又冷淡了", "2026-11-03T10:02:00+00:00")
        ingest(store, "3425635473", "对生人也别不理", "2026-11-03T10:04:00+00:00")
        night = store.run_night("2026-11-04T05:00:00+00:00")
        self.assertTrue(night["ok"])
        self.assertEqual(1, len(night["proposals"]))
        proposal = night["proposals"][0]
        self.assertEqual("manner.stance", proposal["target"])
        self.assertTrue(proposal["value"])
        self.assertEqual(3, len(proposal["evidence"]))
        self.assertTrue(all(isinstance(item, str) and item for item in proposal["evidence"]))

    def test_no_evidence_produces_zero_proposals(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "今天天气不错", "2026-11-03T10:00:00+00:00")
        night = store.run_night("2026-11-04T05:00:00+00:00")
        self.assertEqual(0, len(night["proposals"]))

    def test_night_is_idempotent_and_logs_once(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "你今天有点不理人", "2026-11-03T10:00:00+00:00")
        ingest(store, "567890123", "怎么又冷淡了", "2026-11-03T10:02:00+00:00")
        ingest(store, "3425635473", "对生人也别不理", "2026-11-03T10:04:00+00:00")
        first = store.run_night("2026-11-04T05:00:00+00:00")
        second = store.run_night("2026-11-04T05:01:00+00:00")
        self.assertTrue(second["rerun"])
        self.assertEqual(first["proposals"], second["proposals"])
        self.assertEqual(1, len(store.proposal_log()))

    def test_proposals_fit_soul_accept_shape(self):
        """提案要能被魂的 accept_proposal 直接吃：id/target/value/evidence/reason 齐。"""
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "你今天有点不理人", "2026-11-03T10:00:00+00:00")
        ingest(store, "567890123", "怎么又冷淡了", "2026-11-03T10:02:00+00:00")
        ingest(store, "3425635473", "对生人也别不理", "2026-11-03T10:04:00+00:00")
        proposal = store.run_night("2026-11-04T05:00:00+00:00")["proposals"][0]
        for key in ("id", "target", "value", "evidence", "reason"):
            self.assertIn(key, proposal)


if __name__ == "__main__":
    unittest.main()
