"""嘴·借阅读数：全是纯函数——不读盘、不取时钟，0 是真实读数不是缺失。

判据：
- usage() 只认调用方给的名单（值为 None 的当作没读），一份一份给出「整份几条 / 本轮用掉几条」；
- readout() 产给家的读数行：learned 取不到写 0（0 是真读数），borrow_id 取不到写 None，
  不拿别的数顶上；ids_of() 把家那本借阅条账翻成 {文件名: borrow_id}，一 source 认第一条；
- 措辞只在 data/borrow.json：label / note_of 从那份数据里取，缺这一句当场抛；
- 本模块没有写盘、没有时钟：整份源码里不出现 open / read_text / write / now()。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from organs.mouth import borrow

DATA = Path(__file__).resolve().parents[1] / "data" / "borrow.json"
TABLE = json.loads(DATA.read_text(encoding="utf-8"))


class EntriesTest(unittest.TestCase):
    """「一份数据里有多少条」：清单优先，没清单才数键。"""

    def test_a_list_counts_its_items(self):
        self.assertEqual(3, borrow.entries([1, 2, 3]))

    def test_a_table_counts_its_list_first(self):
        """顶层是一份键值表时不数键，数那条清单：clauses.json 八条条款不是两个键。"""
        self.assertEqual(2, borrow.entries({"note": "说明", "clauses": [{"id": "a"}, {"id": "b"}]}))

    def test_a_table_without_a_list_counts_its_keys(self):
        """policy.json 那种按段读的：没有清单，条目就是段。"""
        self.assertEqual(2, borrow.entries({"split": {}, "pace": {}}))

    def test_a_shape_that_cannot_be_counted_is_zero(self):
        self.assertEqual(0, borrow.entries("一段字"))


class UsageTest(unittest.TestCase):
    """这一轮真读了哪几份、各用掉几条。"""

    def test_only_the_files_it_read_are_listed(self):
        """值为 None 的当作没读：名单里不许出现一份没读过的（报没读过的，读数就是编的）。"""
        event = borrow.usage({"clauses.json": {"clauses": [1, 2, 3]}, "policy.json": None})
        self.assertEqual(["clauses.json"], event["sources"])
        self.assertEqual(1, event["count"])

    def test_counts_tell_how_many_were_really_used(self):
        event = borrow.usage({"clauses.json": {"clauses": [1, 2, 3]}}, counts={"clauses.json": 1})
        self.assertEqual([{"source": "clauses.json", "entries": 3, "used": 1}], event["files"])

    def test_without_counts_the_whole_book_counts(self):
        """没注入条数就整份算上：整份读过就是整份用上。"""
        event = borrow.usage({"policy.json": {"split": {}, "pace": {}}})
        self.assertEqual(2, event["files"][0]["entries"])
        self.assertEqual(2, event["files"][0]["used"])

    def test_a_count_that_is_not_a_number_is_a_real_zero(self):
        """注入的条数不合形状时记 0：0 是一次真实读数，不是「没量过」。"""
        for bad in (True, None, "3", -2, 1.5):
            event = borrow.usage({"clauses.json": {"clauses": [1, 2]}},
                                 counts={"clauses.json": bad})
            self.assertEqual(0, event["files"][0]["used"], bad)

    def test_the_order_is_the_caller_s(self):
        event = borrow.usage({"policy.json": {}, "clauses.json": {}})
        self.assertEqual(["policy.json", "clauses.json"], event["sources"])

    def test_no_books_is_empty_not_an_error(self):
        event = borrow.usage({})
        self.assertEqual({"files": [], "sources": [], "count": 0}, event)


class IdsOfTest(unittest.TestCase):
    """家那本借阅条账 → {文件名: borrow_id}。"""

    def test_one_borrow_id_per_source_first_wins(self):
        ids = borrow.ids_of([
            {"borrow_id": "借-1", "source": "clauses.json"},
            {"borrow_id": "借-2", "source": "policy.json"},
            {"borrow_id": "借-3", "source": "clauses.json"},
        ])
        self.assertEqual({"clauses.json": "借-1", "policy.json": "借-2"}, ids)

    def test_rows_it_cannot_read_are_skipped(self):
        """缺 source 或缺 borrow_id 的行跳过：凑一个 id 上去就是编的。"""
        ids = borrow.ids_of([{"borrow_id": "借-1"}, {"source": "clauses.json"},
                             {"borrow_id": "", "source": "policy.json"}, None, "不是一行"])
        self.assertEqual({}, ids)

    def test_no_ledger_is_no_ids(self):
        self.assertEqual({}, borrow.ids_of(None))


class ReadoutTest(unittest.TestCase):
    """给家的读数行：learned 取不到写 0，borrow_id 取不到写 None。"""

    def _event(self, **over) -> dict:
        base = {"files": [{"source": "clauses.json", "entries": 8, "used": 2},
                          {"source": "policy.json", "entries": 4, "used": 1}], "count": 2}
        base.update(over)
        return base

    def test_one_row_per_file_with_the_used_count(self):
        got = borrow.readout(self._event(), {"clauses.json": "借-1", "policy.json": "借-2"})
        self.assertEqual([{"source": "clauses.json", "borrow_id": "借-1", "learned": 2},
                          {"source": "policy.json", "borrow_id": "借-2", "learned": 1}],
                         got["rows"])
        self.assertEqual([], got["missing"])

    def test_a_zero_is_a_real_readout_not_a_gap(self):
        """这一轮一条都没用上：照写 0（家那边读到 0 = 已接入），不许干脆不写这一行。"""
        got = borrow.readout(self._event(files=[{"source": "clauses.json", "entries": 8,
                                                 "used": 0}]), {"clauses.json": "借-1"})
        self.assertEqual(0, got["rows"][0]["learned"])
        self.assertIn("borrow_id", got["rows"][0])

    def test_no_number_at_all_becomes_zero(self):
        """取不到就写 0：缺这一格、或者是 bool / 负数 / 字符串，一律 0——但这一行照样出。"""
        for item in ({"source": "clauses.json"},
                     {"source": "clauses.json", "used": True},
                     {"source": "clauses.json", "used": -3},
                     {"source": "clauses.json", "used": "2"},
                     {"source": "clauses.json", "used": None}):
            got = borrow.readout({"files": [item]}, {"clauses.json": "借-1"})
            self.assertEqual(0, got["rows"][0]["learned"], item)
            self.assertEqual(1, got["count"])

    def test_a_file_without_a_borrow_id_says_none_and_is_named(self):
        """家里还没有这张借阅条：borrow_id 写 None 并点名那一份，不拿别人的 id 顶。"""
        got = borrow.readout(self._event(), {"clauses.json": "借-1"})
        self.assertIsNone(got["rows"][1]["borrow_id"])
        self.assertEqual(["policy.json"], got["missing"])

    def test_a_lookup_function_works_too(self):
        got = borrow.readout(self._event(), lambda source: "借-9" if "clauses" in source else None)
        self.assertEqual("借-9", got["rows"][0]["borrow_id"])
        self.assertIsNone(got["rows"][1]["borrow_id"])

    def test_no_borrow_id_of_at_all_is_all_none(self):
        got = borrow.readout(self._event())
        self.assertEqual([None, None], [row["borrow_id"] for row in got["rows"]])
        self.assertEqual(["clauses.json", "policy.json"], got["missing"])

    def test_rows_without_a_source_are_skipped(self):
        got = borrow.readout({"files": [{"used": 2}, "不是一行"]}, None)
        self.assertEqual([], got["rows"])


class WordsTest(unittest.TestCase):
    """措辞只在 data/borrow.json：这份数据得齐、缺了当场抛。"""

    def test_the_data_has_sources_and_notes(self):
        self.assertTrue(TABLE["sources"])
        for key in ("ok", "register", "record", "failed", "empty"):
            self.assertIsInstance(TABLE["note"][key], str)
            self.assertTrue(TABLE["note"][key])

    def test_label_comes_from_the_data(self):
        self.assertEqual(TABLE["sources"]["clauses.json"], borrow.label(TABLE, "clauses.json"))

    def test_an_unregistered_source_is_its_own_name(self):
        self.assertEqual("别的东西.json", borrow.label(TABLE, "别的东西.json"))

    def test_note_is_filled_from_the_data(self):
        got = borrow.note_of(TABLE, "ok", count=2, sources="甲、乙")
        self.assertIn("2", got)
        self.assertIn("甲、乙", got)

    def test_a_missing_note_raises_instead_of_inventing_one(self):
        for table in ({}, {"note": {}}, {"note": {"ok": ""}}):
            with self.assertRaises(ValueError):
                borrow.note_of(table, "ok", count=1, sources="x")

    def test_status_words_are_the_ones_the_home_side_reads(self):
        self.assertEqual("ok", borrow.STATUS_OK)
        self.assertEqual("failed", borrow.STATUS_FAILED)


class PurityTest(unittest.TestCase):
    """纯函数：不读盘、不联网、不取时钟。"""

    def test_the_module_has_no_disk_or_clock_path(self):
        source = Path(borrow.__file__).read_text(encoding="utf-8")
        for word in ("open(", "read_text", "write_text", "atomic_write", "append_line",
                     "datetime.now", "time.time", "urllib", "requests."):
            self.assertNotIn(word, source, word)


if __name__ == "__main__":
    unittest.main()
