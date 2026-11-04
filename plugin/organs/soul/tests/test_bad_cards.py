"""坏卡拒载、不补默认值；六轴卡已迁心，残留的 baseline.json 一律不读。"""
from __future__ import annotations

import json
import tempfile
import unittest

from organs.soul import model
from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture, read_bytes, read_json


def codes(issues) -> list:
    return [issue.code for issue in issues]


class BadCardsTest(unittest.TestCase):
    def _root(self, name: str = "valid"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return copy_fixture(name, temp.name)

    def _load(self, name: str):
        root = self._root(name)
        with self.assertRaises(model.SoulLoadError) as caught:
            load(root)
        return root, [issue.code for issue in caught.exception.issues]

    def test_block_names_are_three_no_baseline(self):
        self.assertEqual(("identity", "manner", "bounds"), model.BLOCK_NAMES)
        self.assertNotIn("baseline", model.ALLOWED_KEYS)

    def test_snapshot_carries_no_baseline_and_no_axis_types(self):
        snapshot = load(self._root()).snapshot()
        self.assertFalse(hasattr(snapshot, "baseline"))
        for name in ("Axis", "Baseline", "AXIS_IDS"):
            self.assertFalse(hasattr(model, name), name)

    def test_stray_baseline_card_is_ignored(self):
        """六轴卡迁心后，目录里残留的 baseline.json（哪怕本身是坏卡）不再被读。"""
        root = self._root("stray_baseline")
        self.assertTrue((root / "baseline.json").is_file())
        store = load(root)
        self.assertEqual([], codes(store.snapshot().issues))
        for code in ("PENDING_AXIS", "AXIS_COUNT_NOT_SIX", "AXIS_PAIR_INCOMPLETE",
                     "AXIS_OUT_OF_RANGE", "SOUL_BLOCK_MISSING"):
            self.assertNotIn(code, codes(store.check()), code)

    def test_stray_baseline_card_is_never_repaired_or_touched(self):
        root = self._root("stray_baseline")
        before = read_bytes(root / "baseline.json")
        raw = read_json(root / "baseline.json")
        load(root).check()
        self.assertEqual(before, read_bytes(root / "baseline.json"))
        self.assertNotIn("shown", raw["axes"][0])          # 没有补 0，也没有补配对
        self.assertEqual({"status", "scale", "axes"}, set(raw))

    def test_missing_baseline_card_does_not_break_the_soul(self):
        root = self._root()
        self.assertFalse((root / "baseline.json").exists())
        load(root)                                          # 不抛就是过

    def test_field_outside_the_four_is_rejected(self):
        """六件之外的字段一律拒收：她写的自述归脑，来历归家·抽屉。"""
        for extra in ({"self_written": ["我最近像变了个人"]}, {"her_notes": "昨晚做的梦"}):
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            root = copy_fixture("valid", temp.name)
            data = read_json(root / "identity.json")
            data.update(extra)
            (root / "identity.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self.assertRaises(model.SoulLoadError) as caught:
                load(root)
            messages = [issue.message for issue in caught.exception.issues]
            self.assertTrue(any("六件之外" in message for message in messages), messages)

    def test_manner_and_bounds_also_have_a_closed_key_set(self):
        for block, key in (("manner", "her_voice"), ("bounds", "soften")):
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            root = copy_fixture("valid", temp.name)
            data = read_json(root / ("%s.json" % block))
            data[key] = "x"
            (root / ("%s.json" % block)).write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self.assertRaises(model.SoulLoadError):
                load(root)


if __name__ == "__main__":
    unittest.main()
