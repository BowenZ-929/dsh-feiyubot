"""审计只增：seq 自增、高水位随写、行数变少只报不修、读账不碰水位。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.home import audit
from organs.home.tests import helpers


class AuditAppendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)
        self.path = self.root / audit.AUDIT_NAME
        self.mark = self.root / audit.HIGH_WATER_NAME

    def test_two_appends_make_two_lines_and_seq_1_2(self):
        first = audit.append_entry(self.root, role="agent", by="手", room="工作台",
                                   name="一张卡.md", chars=12, bytes=30)
        second = audit.append_entry(self.root, role="organ", by="眼", room="仓库",
                                    name="一张.png", chars=0, bytes=1024)
        self.assertEqual(1, first.seq)
        self.assertEqual(2, second.seq)
        self.assertEqual(2, len(helpers.file_lines(self.path)))
        self.assertEqual({"lines": 2, "seq": 2}, {"lines": helpers.read_json(self.mark)["lines"],
                                                  "seq": helpers.read_json(self.mark)["seq"]})

    def test_row_carries_the_four_facts(self):
        audit.append_entry(self.root, role="agent", by="她", room="卧室",
                           name="今天.md", chars=1999, bytes=3000)
        row = json.loads(helpers.file_lines(self.path)[0])
        self.assertEqual("agent", row["role"])       # 谁
        self.assertEqual("她", row["by"])
        self.assertEqual("卧室", row["room"])         # 哪间
        self.assertTrue(row["at"])                   # 何时
        self.assertIn("T", row["at"])
        self.assertEqual(1999, row["chars"])         # 多大
        self.assertEqual(3000, row["bytes"])
        self.assertTrue(row["ok"])
        self.assertEqual("ok", row["code"])

    def test_four_facts_are_mandatory(self):
        for field in ("role", "by", "room", "name"):
            kwargs = {"role": "agent", "by": "她", "room": "工作台", "name": "x.md",
                      "chars": 1, "bytes": 1}
            kwargs[field] = "  "
            with self.assertRaises(ValueError):
                audit.append_entry(self.root, **kwargs)

    def test_negative_size_is_refused(self):
        with self.assertRaises(ValueError):
            audit.append_entry(self.root, role="agent", by="她", room="工作台",
                               name="x.md", chars=-1, bytes=1)


class AuditShrinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)
        self.path = self.root / audit.AUDIT_NAME
        self.mark = self.root / audit.HIGH_WATER_NAME

    def test_shrink_is_reported_and_not_repaired(self):
        audit.append_entry(self.root, role="agent", by="她", room="工作台", name="一.md", chars=1, bytes=1)
        audit.append_entry(self.root, role="agent", by="她", room="工作台", name="二.md", chars=1, bytes=1)
        self.assertTrue(audit.verify(self.root)["ok"])
        mark_before = self.mark.read_text(encoding="utf-8")

        # 手工截成一行：账被动过
        first_line = helpers.file_lines(self.path)[0]
        self.path.write_text(first_line + "\n", encoding="utf-8")

        result = audit.verify(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual("AUDIT_SHRUNK", result["code"])
        self.assertEqual(1, result["lines"])
        self.assertEqual(2, int(result["mark"]["lines"]))
        self.assertEqual(1, len(helpers.file_lines(self.path)))
        self.assertEqual(mark_before, self.mark.read_text(encoding="utf-8"))

    def test_missing_mark_while_ledger_exists_is_loud(self):
        audit.append_entry(self.root, role="agent", by="她", room="工作台", name="一.md", chars=1, bytes=1)
        self.mark.unlink()
        result = audit.verify(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual("AUDIT_MARK_MISSING", result["code"])

    def test_empty_home_verifies_ok(self):
        result = audit.verify(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["lines"])
        self.assertEqual(0, result["seq"])


class ReadTailTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)
        self.mark = self.root / audit.HIGH_WATER_NAME

    def test_read_tail_does_not_touch_high_water(self):
        for index in range(3):
            audit.append_entry(self.root, role="agent", by="她", room="工作台",
                               name="%d.md" % index, chars=1, bytes=1)
        before = (self.mark.read_text(encoding="utf-8"), self.mark.stat().st_mtime_ns)
        tail = audit.read_tail(self.root)
        self.assertEqual([1, 2, 3], [entry.seq for entry in tail])
        after = (self.mark.read_text(encoding="utf-8"), self.mark.stat().st_mtime_ns)
        self.assertEqual(before, after)

    def test_tail_limit_and_fields(self):
        for index in range(5):
            audit.append_entry(self.root, role="agent", by="她", room="工作台",
                               name="%d.md" % index, chars=index, bytes=index)
        tail = audit.read_tail(self.root, limit=2)
        self.assertEqual([4, 5], [entry.seq for entry in tail])
        entry = tail[-1]
        self.assertEqual(("agent", "她", "工作台", "4.md", 4, 4), (
            entry.role, entry.by, entry.room, entry.name, entry.chars, entry.bytes))

    def test_bad_lines_are_counted_and_do_not_hide_good_ones(self):
        audit.append_entry(self.root, role="agent", by="她", room="工作台", name="一.md", chars=1, bytes=1)
        path = self.root / audit.AUDIT_NAME
        with open(str(path), "a", encoding="utf-8") as handle:
            handle.write("这不是一行账\n")
        tail = audit.read_tail(self.root)
        self.assertEqual([1], [entry.seq for entry in tail])
        self.assertEqual(1, audit.verify(self.root)["bad"])


if __name__ == "__main__":
    unittest.main()
