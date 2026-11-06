"""偏好卡：一人一张「她想怎么对这个人说话」，只增不改、读时折成最新。

落点在名片盒里独立的 prefs.jsonl（照 borrow.jsonl 的先例）：名片盒的 index.jsonl 仍只放人卡。
字段只做结构与类型，取值范围是手那边的判据——本文件的用例也照这条线钉住。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.home import cards, residents
from organs.home.tests import helpers

#: 占位 QQ 号：不写真号（拿 1000xxxx 这种一眼看得出是填头的字符串）
WHO = "1000xxxx"
OTHER = "1000yyyy"
INDEX = Path("名片盒") / "index.jsonl"
PREFS = Path("名片盒") / "prefs.jsonl"


class PreferenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)

    def prefs_path(self) -> Path:
        return self.root / PREFS

    def write(self, **kwargs):
        kwargs.setdefault("by", "主人")
        kwargs.setdefault("role", "human")
        return residents.add_preference(root=self.root, **kwargs)

    # ------------------------------------------------------------ ① 写进去、读得回

    def test_write_then_read_back(self):
        card = self.write(who=WHO, chunks_per_reply=3, max_chars=60)
        self.assertEqual(card["card_id"], "偏好-1")
        self.assertEqual(card["kind"], "偏好")
        self.assertEqual(card["room"], "名片盒")
        got = residents.preference_of(root=self.root, who=WHO)
        self.assertEqual(got["who"], WHO)
        self.assertEqual(got["chunks_per_reply"], 3)
        self.assertEqual(got["max_chars"], 60)
        self.assertEqual(got["card_id"], "偏好-1")
        self.assertTrue(got["at"])

    # ------------------------------------------------------------ ② 再写一次 = 再追加一行

    def test_second_write_appends_row_and_reading_takes_latest(self):
        self.write(who=WHO, chunks_per_reply=3, max_chars=60)
        first = helpers.file_lines(self.prefs_path())
        second_card = self.write(who=WHO, chunks_per_reply=1, max_chars=20)

        # 盘上两行都在（旧行一个字都没改）
        rows = helpers.file_lines(self.prefs_path())
        self.assertEqual(len(first), 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[0]), json.loads(first[0]))
        self.assertEqual([json.loads(row)["card_id"] for row in rows], ["偏好-1", "偏好-2"])
        self.assertEqual(second_card["card_id"], "偏好-2")

        # 读的时候取最新那行
        got = residents.preference_of(root=self.root, who=WHO)
        self.assertEqual(got["chunks_per_reply"], 1)
        self.assertEqual(got["max_chars"], 20)
        self.assertEqual(got["card_id"], "偏好-2")

    def test_latest_is_by_append_order_not_by_at(self):
        """人写错一个更早的时间戳，也盖不掉后写的那一行：顺序说了算。"""
        self.write(who=WHO, max_chars=60, at="2026-01-02T00:00:00+00:00")
        self.write(who=WHO, max_chars=20, at="2026-01-01T00:00:00+00:00")
        self.assertEqual(residents.preference_of(root=self.root, who=WHO)["max_chars"], 20)

    # ------------------------------------------------------------ ③ 没设的旋钮不补默认值

    def test_only_max_chars_leaves_the_other_knob_absent(self):
        self.write(who=WHO, max_chars=20)
        got = residents.preference_of(root=self.root, who=WHO)
        self.assertEqual(got["max_chars"], 20)
        self.assertNotIn("chunks_per_reply", got)
        self.assertIsNone(got.get("chunks_per_reply"))

    def test_empty_card_keeps_only_who_at_and_card_id(self):
        """两个旋钮都不给：空卡也是一行账，读回来只有 who/at/card_id，没有 0。"""
        self.write(who=WHO)
        got = residents.preference_of(root=self.root, who=WHO)
        self.assertEqual(sorted(got), ["at", "card_id", "who"])
        self.assertEqual(residents.preference_of(root=self.root, who=WHO)["who"], WHO)

    # ------------------------------------------------------------ ④ 没有这个人 → {}，不建文件

    def test_unknown_person_returns_empty_dict_and_creates_no_file(self):
        self.assertEqual(residents.preference_of(root=self.root, who=OTHER), {})
        self.assertFalse(self.prefs_path().exists())
        self.assertEqual(residents.preferences(root=self.root), [])
        self.assertEqual(residents.preference_ledger(root=self.root)["status"], "未接入")

    # ------------------------------------------------------------ ⑤ 非法键

    def test_extra_param_key_is_bad_key(self):
        with self.assertRaises(cards.CardError) as caught:
            cards.append_cards(root=self.root, role="human", by="主人", cards=[
                {"room": "名片盒", "kind": "偏好", "name": WHO,
                 "params": {"who": WHO, "tone": "甜"}}])
        self.assertEqual(caught.exception.code, "BAD_KEY")
        self.assertIn("chunks_per_reply", caught.exception.message)
        self.assertFalse(self.prefs_path().exists())

    # ------------------------------------------------------------ ⑥ 类型：int 且不是 bool

    def test_value_must_be_int_and_bool_is_refused(self):
        for value in (True, "20", 2.5, [3]):
            with self.subTest(value=value):
                with self.assertRaises(cards.CardError) as caught:
                    self.write(who=WHO, chunks_per_reply=value)
                self.assertEqual(caught.exception.code, "BAD_VALUE")
        self.assertFalse(self.prefs_path().exists())

    def test_value_type_is_checked_through_the_card_path_too(self):
        with self.assertRaises(cards.CardError) as caught:
            cards.append_cards(root=self.root, role="human", by="主人", cards=[
                {"room": "名片盒", "kind": "偏好", "name": WHO,
                 "params": {"who": WHO, "max_chars": False}}])
        self.assertEqual(caught.exception.code, "BAD_VALUE")

    def test_who_is_required(self):
        for value in ("", "   ", None):
            with self.subTest(who=value):
                with self.assertRaises(cards.CardError) as caught:
                    self.write(who=value)
                self.assertEqual(caught.exception.code, "KIND_FIELD_MISSING")

    def test_at_must_be_iso8601(self):
        with self.assertRaises(cards.CardError) as caught:
            self.write(who=WHO, at="昨天")
        self.assertEqual(caught.exception.code, "BAD_AT")
        self.write(who=WHO, at="2026-01-02T03:04:05+00:00")
        self.assertEqual(residents.preference_of(root=self.root, who=WHO)["at"],
                         "2026-01-02T03:04:05+00:00")

    # ------------------------------------------------------------ ⑦ 只落名片盒

    def test_pref_card_in_another_room_is_wrong_room(self):
        with self.assertRaises(cards.CardError) as caught:
            cards.append_cards(root=self.root, role="human", by="主人", cards=[
                {"room": "工作台", "kind": "偏好", "name": WHO,
                 "params": {"who": WHO}}])
        self.assertEqual(caught.exception.code, "KIND_WRONG_ROOM")
        self.assertEqual(caught.exception.detail["room"], "工作台")

    # ------------------------------------------------------------ ⑧ index.jsonl 行数不变

    def test_business_card_index_is_untouched(self):
        """名片盒的 index.jsonl 仍只放人卡：写偏好前后行数一样，偏好只走 prefs.jsonl。"""
        cards.append_cards(root=self.root, role="human", by="主人", cards=[
            {"room": "名片盒", "kind": "名片", "name": OTHER, "params": {}}])
        before = helpers.file_lines(self.root / INDEX)
        self.write(who=WHO, chunks_per_reply=2, max_chars=40)
        self.write(who=WHO, max_chars=10)
        self.assertEqual(helpers.file_lines(self.root / INDEX), before)
        self.assertEqual(len(helpers.file_lines(self.prefs_path())), 2)

    # ------------------------------------------------------------ 审计与坏行计数

    def test_every_write_lands_one_audit_row(self):
        from organs.home import audit
        self.write(who=WHO, max_chars=20)
        tail = audit.read_tail(root=self.root, limit=5)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[-1].room, "名片盒")
        self.assertEqual(tail[-1].name, "prefs.jsonl")
        self.assertEqual(tail[-1].role, "human")

    def test_bad_lines_are_counted_not_swallowed(self):
        from organs.home.store import append_line
        self.write(who=WHO, max_chars=20)
        with open(str(self.prefs_path()), "a", encoding="utf-8") as handle:
            handle.write("这不是一行 JSON\n")
        # 读得出来但字段立不住的那一行：另计数，不参与折叠、不拿它顶「没设过」
        append_line(self.prefs_path(), {"kind": "偏好", "card_id": "偏好-9",
                                        "params": {"who": OTHER, "max_chars": "20"}})
        ledger = residents.preference_ledger(root=self.root)
        self.assertEqual(ledger["bad_lines"], 1)
        self.assertEqual(ledger["bad_rows"], 1)
        self.assertEqual(ledger["cards"], 2)
        self.assertEqual([item["who"] for item in residents.preferences(root=self.root)], [WHO])
        self.assertEqual(residents.preference_of(root=self.root, who=OTHER), {})

    def test_preferences_lists_one_entry_per_person_latest_first_written(self):
        self.write(who=WHO, max_chars=60)
        self.write(who=OTHER, chunks_per_reply=2)
        self.write(who=WHO, max_chars=20)
        listed = residents.preferences(root=self.root)
        self.assertEqual([item["who"] for item in listed], [OTHER, WHO])
        self.assertEqual(listed[-1]["max_chars"], 20)
        self.assertEqual(len(helpers.file_lines(self.prefs_path())), 3)
