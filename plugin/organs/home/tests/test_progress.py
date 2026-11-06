"""进度：挂在卡片上的一栏，不是状态、也不是新房间。

判据（人裁 2026-11-06）：「那张画」是可指认的物件（有卡、在工作台），「画到一半」是它的一个字段。
落法照旧——只追加：改一次进度是往流水里追一行 `进度` 事件，读时 fold 把最新那行的值贴回卡上。
取值只选一套：**0-100 的整数**，或 null 表示没有进度。**null 不等于 0**。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.home import access, audit, cards, rooms, store

FIVE = ("玄关", "名片盒", "工作台", "仓库", "信箱")


def make_home(base: Path, writable=FIVE) -> Path:
    root = base / "home"
    for name in rooms.ROOM_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "access.json").write_text(
        json.dumps({"version": 1, "writable": list(writable)}), encoding="utf-8")
    return root


class ProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_home(Path(self._tmp.name))

    def _put(self, name="一张画.md", **extra):
        card = {"room": "工作台", "kind": "物件", "name": name, "note": "画的"}
        card.update(extra)
        return cards.append_cards(root=self.root, role="organ", by="day",
                                  cards=[card])["cards"][0]

    def _item(self, room="工作台"):
        items = cards.list_room(self.root, room=room)["items"]
        return items[0] if items else None

    def test_set_progress_reads_back_and_leaves_one_audit_line(self) -> None:
        card = self._put()
        before = len(audit.read_tail(self.root, limit=50))
        cards.set_progress(root=self.root, role="organ", by="day", room="工作台",
                           card_id=card["card_id"], progress=40, reason="画到一半")
        item = self._item()
        self.assertEqual(40, item.get("progress"), "读回来就是设进去的那个数")
        self.assertEqual("day", item.get("progress_by"))
        self.assertEqual(1, item.get("progress_count"))
        self.assertEqual(before + 1, len(audit.read_tail(self.root, limit=50)),
                         "改一次进度要留一行审计")
        self.assertEqual("进度:一张画.md", audit.read_tail(self.root, limit=50)[-1].name,
                         "账里要能一眼看出这是改进度，不是又放了一张卡")
        self.assertTrue(audit.verify(root=self.root)["ok"])

    def test_progress_is_append_only_and_the_card_body_is_untouched(self) -> None:
        card = self._put()
        rows_before = store.read_lines(self.root / "工作台" / cards.INDEX_NAME)[0]
        cards.set_progress(root=self.root, role="organ", by="day", room="工作台",
                           card_id=card["card_id"], progress=40)
        rows_after = store.read_lines(self.root / "工作台" / cards.INDEX_NAME)[0]
        self.assertEqual(len(rows_before) + 1, len(rows_after), "只追加一行")
        self.assertEqual(dict(rows_before[0]), rows_after[0], "卡片本体一个字节都不许动")
        self.assertEqual(cards.PROGRESS_KIND, rows_after[-1]["kind"])

    def test_clearing_progress_reads_as_absent_not_zero(self) -> None:
        """删掉 progress → 按「没有进度」读，不报错、不补 0。"""
        card = self._put()
        cards.set_progress(root=self.root, role="organ", by="day", room="工作台",
                           card_id=card["card_id"], progress=40)
        self.assertEqual(40, self._item().get("progress"))
        cards.set_progress(root=self.root, role="organ", by="day", room="工作台",
                           card_id=card["card_id"], progress=None, reason="重新来过")
        item = self._item()
        self.assertNotIn("progress", item, "删掉就是没有这一栏，不许补成 0")
        self.assertNotEqual(0, item.get("progress"), "更不许读成 0")
        self.assertEqual(2, item.get("progress_count"), "设一次 + 清一次 = 改过两次，账要记得住")

    def test_a_card_can_be_born_with_progress(self) -> None:
        """进度与卡片同生共死：建卡时就能带上，不必先建再改。"""
        self._put(progress=0)
        self.assertEqual(0, self._item().get("progress"), "0 是「在起点」，是一个真值")

    def test_out_of_range_and_wrong_type_are_loud(self) -> None:
        card = self._put()
        for bad in (-1, 101, 40.5, "40", True):
            with self.subTest(progress=bad):
                with self.assertRaises(cards.CardError) as caught:
                    cards.set_progress(root=self.root, role="organ", by="day", room="工作台",
                                       card_id=card["card_id"], progress=bad)
                self.assertIn(caught.exception.code,
                              ("BAD_PROGRESS", "PROGRESS_OUT_OF_RANGE"))

    def test_setting_progress_goes_through_the_gate(self) -> None:
        card = self._put()
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": []}), encoding="utf-8")
        with self.assertRaises(access.AccessDenied) as caught:
            cards.set_progress(root=self.root, role="agent", by="她", room="工作台",
                               card_id=card["card_id"], progress=40)
        self.assertEqual("ROOM_READONLY", caught.exception.code)
        self.assertEqual("whitelist", caught.exception.detail.get("door"))
        self.assertNotIn("progress", self._item(), "门挡下来就不许落")
        cards.set_progress(root=self.root, role="organ", by="day", room="工作台",
                           card_id=card["card_id"], progress=40)
        self.assertEqual(40, self._item().get("progress"))

    def test_unknown_item_is_loud(self) -> None:
        with self.assertRaises(cards.CardError) as caught:
            cards.set_progress(root=self.root, role="organ", by="day", room="工作台",
                               card_id="工作台-99", progress=40)
        self.assertEqual("CARD_NOT_FOUND", caught.exception.code)

    def test_a_bad_event_on_disk_does_not_break_the_read(self) -> None:
        """盘上一行写坏了，读整间房不许崩：如实计数，不降级成 0。"""
        self._put()
        store.append_line(self.root / "工作台" / cards.INDEX_NAME,
                          {"card_id": "工作台-9", "room": "工作台", "kind": cards.PROGRESS_KIND,
                           "name": "一张画.md", "params": {"ref": "工作台-1", "progress": "坏值"},
                           "by": "手工", "role": "human", "at": "2026-11-06T00:00:00+09:00"})
        got = cards.list_room(self.root, room="工作台")
        self.assertEqual(1, got["total"])
        self.assertEqual(1, got["bad_progress"])
        self.assertNotIn("progress", got["items"][0], "坏事件不许把卡写成 0")

    def test_progress_opens_no_new_room(self) -> None:
        """进度不加房间：房名表不因为「进度」多出一间，落盘也不多出目录。"""
        self.assertEqual(tuple(room.name for room in rooms.ROOMS), rooms.ROOM_NAMES,
                         "房名表就是 ROOMS，这里不写字面量")
        card = self._put()
        cards.set_progress(root=self.root, role="organ", by="day", room="工作台",
                           card_id=card["card_id"], progress=40)
        on_disk = {path.name for path in self.root.iterdir() if path.is_dir()}
        on_disk.discard("__pycache__")
        self.assertEqual(set(rooms.ROOM_NAMES) - {"__pycache__"}, on_disk)


if __name__ == "__main__":
    unittest.main()
