"""家（/home/consume）的 HTTP 层：拿走一件要过两道门、记一行审计，拒绝如实翻成状态码。

每个用例自己造临时家，真 data/ 不碰。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from api import api
from organs.home import access, audit, cards, rooms

FIVE = ("玄关", "名片盒", "工作台", "仓库", "信箱")


def make_home(base: Path, writable=FIVE) -> Path:
    root = base / "home"
    for name in rooms.ROOM_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "access.json").write_text(
        json.dumps({"version": 1, "writable": list(writable)}), encoding="utf-8")
    return root


class HomeConsumeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_home(Path(self._tmp.name))
        self.card = cards.append_cards(root=self.root, role="organ", by="probe", cards=[{
            "room": "工作台", "kind": "物件", "name": "一张画.md", "note": "放的"}])["cards"][0]

    def _post(self, body):
        return api.handle("POST", "/home/consume", body, self.root)

    def test_take_one_item(self) -> None:
        status, got = self._post({"role": "organ", "by": "day", "room": "工作台",
                                  "card_id": self.card["card_id"], "reason": "收起来了"})
        self.assertEqual(200, status, got)
        self.assertTrue(got["ok"])
        self.assertEqual(0, got["left"])
        self.assertEqual(cards.CONSUME_KIND, got["card"]["kind"])
        self.assertEqual("消费:一张画.md", got["audit"]["name"])
        _status, listed = api.handle("GET", "/home/list", None, self.root,
                                     query={"room": "工作台"})
        self.assertEqual(0, listed["total"])
        self.assertEqual(1, listed["consumed"])
        _status, hidden = api.handle("GET", "/home/list", None, self.root,
                                     query={"room": "工作台", "include_consumed": "1"})
        self.assertEqual(1, hidden["total"], "include_consumed=1 才看得见被拿走的")

    def test_missing_item_is_404_but_a_taken_one_says_so(self) -> None:
        status, got = self._post({"role": "organ", "by": "day", "room": "工作台",
                                  "card_id": "工作台-99"})
        self.assertEqual(404, status)
        self.assertEqual("CARD_NOT_FOUND", got["code"])
        self._post({"role": "organ", "by": "day", "room": "工作台",
                    "card_id": self.card["card_id"]})
        status, got = self._post({"role": "organ", "by": "day", "room": "工作台",
                                  "card_id": self.card["card_id"]})
        self.assertEqual(409, status)
        self.assertEqual("CARD_ALREADY_CONSUMED", got["code"])

    def test_whitelist_door_turns_her_back_with_403(self) -> None:
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": []}), encoding="utf-8")
        status, got = self._post({"role": "agent", "by": "她", "room": "工作台",
                                  "card_id": self.card["card_id"]})
        self.assertEqual(403, status)
        self.assertEqual("ROOM_READONLY", got["code"])
        self.assertEqual("whitelist", got["door"])
        status, _got = self._post({"role": "organ", "by": "day", "room": "工作台",
                                   "card_id": self.card["card_id"]})
        self.assertEqual(200, status, "她今天不能拿，不等于这间拿不出来")

    def test_structural_readonly_room_is_403_with_structure_door(self) -> None:
        for role in ("agent", "organ", "human"):
            with self.subTest(role=role):
                status, got = self._post({"role": role, "by": "x", "room": "橱柜",
                                          "name": "一件衣服"})
                self.assertEqual(403, status)
                self.assertEqual("structure", got["door"], "橱柜是结构只读，三个角色一律拒")

    def test_audit_grows_and_verify_stays_ok(self) -> None:
        before = len(audit.read_tail(self.root, limit=50))
        self._post({"role": "organ", "by": "day", "room": "工作台",
                    "card_id": self.card["card_id"]})
        self.assertEqual(before + 1, len(audit.read_tail(self.root, limit=50)))
        self.assertTrue(audit.verify(root=self.root)["ok"])

    def test_wrong_method_and_bad_body(self) -> None:
        status, _got = api.handle("GET", "/home/consume", None, self.root)
        self.assertEqual(405, status)
        status, got = api.handle("POST", "/home/consume", {}, self.root)
        self.assertEqual(400, status)
        self.assertEqual("BAD_ARGUMENT", got["code"])
        status, got = self._post({"role": "organ", "by": "day", "room": "工作台",
                                  "card_id": self.card["card_id"], "name": "一张画.md"})
        self.assertEqual(400, status, "card_id 与 name 只能给一个")
        self.assertEqual("BAD_ARGUMENT", got["code"])


if __name__ == "__main__":
    unittest.main()
