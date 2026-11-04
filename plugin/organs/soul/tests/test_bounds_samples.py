"""红线正负样本表：字面/正则条目按样本命中，external/review 魂内不判定。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from organs.soul import model
from organs.soul.bounds import match
from organs.soul.store import load

DATA = Path(__file__).resolve().parents[1] / "data"
SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "bounds_samples.jsonl"


class BoundsSamplesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = load(DATA)
        self.items = self.store.snapshot().bounds.items

    def _samples(self):
        rows = []
        for line in SAMPLES.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def test_every_sample_matches_expected_ids(self):
        for sample in self._samples():
            hits = match(self.items, sample["text"])
            if sample["expect"]:
                self.assertIn(sample["item"], hits, sample)
            else:
                self.assertNotIn(sample["item"], hits, sample)

    def test_sample_table_covers_every_red_line(self):
        covered = {sample["item"] for sample in self._samples()}
        self.assertEqual({item.id for item in self.items}, covered)

    def test_unenforced_items_never_hit(self):
        for item in self.items:
            if item.match_type in model.UNENFORCED_MATCH_TYPES:
                self.assertEqual([], match([item], "随便什么话"), item.id)


if __name__ == "__main__":
    unittest.main()

