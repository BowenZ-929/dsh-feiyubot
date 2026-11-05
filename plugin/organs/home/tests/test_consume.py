"""消费：她拿走一件东西——过两道门、追加一行、记一行审计，盘上一行都不删。

判据：家「只增不改」。所以消费不是删行，是**追加一条消费事件**（`kind="消费"`，`params.ref`
指向被拿走那张卡的 card_id），读时 fold 把目标标成 `consumed=True / left=0`。
「橱柜少一样」是读出来的结论，不是擦掉一行——擦掉就再也追不回「谁什么时候拿走的」。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.home import access, audit, cards, residents, rooms, store

FIVE = ("玄关", "名片盒", "工作台", "仓库", "信箱")


def make_home(base: Path, writable=FIVE) -> Path:
    root = base / "home"
    for name in rooms.ROOM_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "access.json").write_text(
        json.dumps({"version": 1, "writable": list(writable)}), encoding="utf-8")
    return root


def put(root: Path, name: str, room: str = "工作台", kind: str = "物件") -> dict:
    out = cards.append_cards(root=root, role="organ", by="probe",
                             cards=[{"room": room, "kind": kind, "name": name,
                                     "note": "放的"}] )
    return out["cards"][0]


class ConsumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_home(Path(self._tmp.name))

    def _rows(self, room: str = "工作台"):
        return store.read_lines(self.root / room / cards.INDEX_NAME)[0]

    def test_consumed_item_leaves_the_room_list(self) -> None:
        card = put(self.root, "一张画.md")
        cards.consume(root=self.root, role="organ", by="day", room="工作台",
                      card_id=card["card_id"], reason="画完了，收起来")
        got = cards.list_room(self.root, room="工作台")
        self.assertEqual(0, got["total"], "拿走了就不在清单里")
        self.assertEqual(1, got["consumed"], "但如实报「拿走了一件」")
        hidden = cards.list_room(self.root, room="工作台", include_consumed=True)
        self.assertEqual(1, hidden["total"])
        self.assertTrue(hidden["items"][0]["consumed"])
        self.assertEqual("day", hidden["items"][0]["consumed_by"])
        self.assertEqual("画完了，收起来", hidden["items"][0]["consume_reason"])

    def test_consumption_is_append_only_on_disk(self) -> None:
        card = put(self.root, "一张画.md")
        before_rows = self._rows()
        before_first = dict(before_rows[0])
        cards.consume(root=self.root, role="organ", by="day", room="工作台",
                      card_id=card["card_id"])
        after_rows = self._rows()
        self.assertEqual(len(before_rows) + 1, len(after_rows), "只追加一行，不删不改")
        self.assertEqual(before_first, after_rows[0], "原来那一行一个字节都不许动")
        self.assertEqual(cards.CONSUME_KIND, after_rows[-1]["kind"])
        self.assertEqual(card["card_id"], after_rows[-1]["params"]["ref"])

    def test_consumption_writes_one_audit_line(self) -> None:
        card = put(self.root, "一张画.md")
        before = len(audit.read_tail(self.root, limit=50))
        out = cards.consume(root=self.root, role="organ", by="day", room="工作台",
                            card_id=card["card_id"], reason="收起来了")
        entries = audit.read_tail(self.root, limit=50)
        self.assertEqual(before + 1, len(entries), "消费也要记一行审计")
        self.assertEqual("工作台", entries[-1].room)
        self.assertEqual("day", entries[-1].by)
        self.assertEqual("消费:一张画.md", entries[-1].name, "账里要能一眼看出这是拿走，不是放下")
        self.assertEqual(len("收起来了"), entries[-1].chars)
        self.assertEqual(out["audit"].seq, entries[-1].seq)
        self.assertTrue(audit.verify(root=self.root)["ok"])

    def test_consume_goes_through_the_whitelist_door(self) -> None:
        card = put(self.root, "一张画.md")
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": []}), encoding="utf-8")
        with self.assertRaises(access.AccessDenied) as caught:
            cards.consume(root=self.root, role="agent", by="她", room="工作台",
                          card_id=card["card_id"])
        self.assertEqual("ROOM_READONLY", caught.exception.code)
        self.assertEqual("whitelist", caught.exception.detail.get("door"))
        self.assertEqual(1, cards.list_room(self.root, room="工作台")["total"],
                         "门挡下来就一件都不许拿")
        cards.consume(root=self.root, role="organ", by="day", room="工作台",
                      card_id=card["card_id"])
        self.assertEqual(0, cards.list_room(self.root, room="工作台")["total"],
                         "organ 不过门二：她今天不能写，不等于这间拿不出来")

    def test_consume_refuses_the_structural_readonly_rooms(self) -> None:
        """已知缺口（2026-11-06 人拍 A）：橱柜／台历是结构只读，三个角色一律拿不了。

        这条钉住现状，不是认同它——等房名表的读写权定案，改这里要连带改判据。
        """
        for room in ("橱柜", "台历", "客厅", "卧室", "抽屉"):
            for role in ("agent", "organ", "human"):
                with self.subTest(room=room, role=role):
                    with self.assertRaises(access.AccessDenied) as caught:
                        access.check_consume(role=role, room=room, name="一件", root=self.root)
                    self.assertEqual("ROOM_READONLY", caught.exception.code)
                    self.assertEqual("structure", caught.exception.detail.get("door"))

    def test_unknown_item_and_second_take_are_different_errors(self) -> None:
        card = put(self.root, "一张画.md")
        with self.assertRaises(cards.CardError) as caught:
            cards.consume(root=self.root, role="organ", by="day", room="工作台",
                          card_id="工作台-99")
        self.assertEqual("CARD_NOT_FOUND", caught.exception.code)
        cards.consume(root=self.root, role="organ", by="day", room="工作台",
                      card_id=card["card_id"])
        with self.assertRaises(cards.CardError) as caught:
            cards.consume(root=self.root, role="organ", by="day", room="工作台",
                          card_id=card["card_id"])
        self.assertEqual("CARD_ALREADY_CONSUMED", caught.exception.code,
                         "「没有这一件」与「这件已经被拿走了」要分开报")
        self.assertIn("day", caught.exception.message)

    def test_same_name_is_ambiguous_instead_of_a_guess(self) -> None:
        put(self.root, "一张画.md")
        put(self.root, "一张画.md")
        with self.assertRaises(cards.CardError) as caught:
            cards.consume(root=self.root, role="organ", by="day", room="工作台",
                          name="一张画.md")
        self.assertEqual("CARD_AMBIGUOUS", caught.exception.code)
        self.assertEqual(2, len(caught.exception.detail["candidates"]), "要把候选给出来")

    def test_orphan_consume_is_reported_not_swallowed(self) -> None:
        put(self.root, "一张画.md")
        store.append_line(self.root / "工作台" / cards.INDEX_NAME,
                          {"card_id": "工作台-99", "room": "工作台", "kind": cards.CONSUME_KIND,
                           "name": "不存在的一件", "params": {"ref": "工作台-404"},
                           "by": "手工", "role": "human", "at": "2026-11-06T00:00:00+09:00"})
        got = cards.list_room(self.root, room="工作台")
        self.assertEqual(1, got["total"], "折不上的消费不许把别的卡也吃掉")
        self.assertEqual(1, got["orphan_consumes"], "孤儿消费要如实报出来")

    def test_consumed_pointer_card_drops_out_of_the_warehouse_check(self) -> None:
        target = Path(self._tmp.name) / "policy.json"
        target.write_text('{"v": 1}', encoding="utf-8")
        card = cards.append_cards(root=self.root, role="organ", by="home", cards=[{
            "room": "仓库", "kind": residents.POINTER_KIND, "name": target.name,
            "path": str(target), "sha256": store.sha256_file(target),
            "bytes": target.stat().st_size, "params": {"pointer": True}}])["cards"][0]
        self.assertEqual(1, residents.warehouse_pointers(self.root)["total"])
        cards.consume(root=self.root, role="organ", by="home", room="仓库",
                      card_id=card["card_id"], reason="这件不再借了")
        self.assertEqual(0, residents.warehouse_pointers(self.root)["total"],
                         "拿走的指针卡不再参与对齐判定，也不该冒出一条假 stale")


if __name__ == "__main__":
    unittest.main()
