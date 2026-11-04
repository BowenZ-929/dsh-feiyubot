"""状态枚举 → 写者断言：每个状态值必须有生产者，未接线的要能查出来。"""
from __future__ import annotations

import unittest

from organs.brain.model import STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_STALE, status_writer_report


class StatusWritersTest(unittest.TestCase):
    def test_every_status_has_a_writer_or_explicit_none(self):
        rows = {row["status"]: row for row in status_writer_report()}
        self.assertEqual({STATUS_ACTIVE, STATUS_STALE, STATUS_ARCHIVED}, set(rows))
        self.assertTrue(rows[STATUS_ACTIVE]["wired"])
        self.assertTrue(rows[STATUS_STALE]["wired"])
        self.assertTrue(rows[STATUS_ARCHIVED]["wired"],
                        "第三轮已接线：读时结算的遗忘曲线写 archived")
        self.assertIn("settle_retention", rows[STATUS_ARCHIVED]["writer"])

    def test_writer_names_point_to_real_functions(self):
        from organs.brain import BrainStore
        rows = {row["status"]: row for row in status_writer_report()}
        self.assertIn("retire", rows[STATUS_STALE]["writer"])
        self.assertIn("ingest", rows[STATUS_ACTIVE]["writer"])
        self.assertTrue(hasattr(BrainStore, "retire"))
        self.assertTrue(hasattr(BrainStore, "_settle_retention"))


if __name__ == "__main__":
    unittest.main()
