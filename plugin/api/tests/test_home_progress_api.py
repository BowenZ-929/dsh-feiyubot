"""家（/home/progress）的 HTTP 层：设/清进度、越界拒绝、审计留痕。

每个用例自己造临时家，真 data/ 不碰。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from api import api
from organs.home import audit, cards, rooms

FIVE = ("玄关", "名片盒", "工作台", "仓库", "信箱")


class HomeProgressApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "home"
        for name in rooms.ROOM_NAMES:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": list(FIVE)}), encoding="utf-8")
        self.card = cards.append_cards(root=self.root, role="organ", by="day", cards=[{
            "room": "工作台", "kind": "物件", "name": "一张画.md",
            "note": "画的"}])["cards"][0]

    def _post(self, body):
        return api.handle("POST", "/home/progress", body, self.root)

    def _item(self):
        _status, listed = api.handle("GET", "/home/list", None, self.root,
                                     query={"room": "工作台"})
        return listed["items"][0]

    def test_set_to_40_reads_back_40_and_audits(self) -> None:
        before = len(audit.read_tail(self.root, limit=50))
        status, got = self._post({"role": "organ", "by": "day", "room": "工作台",
                                  "card_id": self.card["card_id"], "progress": 40,
                                  "reason": "画到一半"})
        self.assertEqual(200, status, got)
        self.assertTrue(got["ok"])
        self.assertEqual(40, got["progress"])
        self.assertEqual(40, self._item()["progress"], "读回来是 40%")
        self.assertEqual(before + 1, len(audit.read_tail(self.root, limit=50)), "审计多一行")
        self.assertEqual("进度:一张画.md", audit.read_tail(self.root, limit=50)[-1].name)

    def test_clear_reads_as_absent(self) -> None:
        self._post({"role": "organ", "by": "day", "room": "工作台",
                    "card_id": self.card["card_id"], "progress": 40})
        status, got = self._post({"role": "organ", "by": "day", "room": "工作台",
                                  "card_id": self.card["card_id"], "progress": None})
        self.assertEqual(200, status, got)
        self.assertIsNone(got["progress"])
        self.assertNotIn("progress", self._item(), "删掉就是没有这一栏，不补 0")

    def test_range_and_type_are_400(self) -> None:
        for bad in (101, -1, "40", 40.5):
            with self.subTest(progress=bad):
                status, got = self._post({"role": "organ", "by": "day", "room": "工作台",
                                          "card_id": self.card["card_id"], "progress": bad})
                self.assertEqual(400, status)
                self.assertIn(got["code"], ("BAD_PROGRESS", "PROGRESS_OUT_OF_RANGE"))

    def test_missing_progress_key_is_400_because_null_is_a_decision(self) -> None:
        """`progress` 缺键 ≠ `progress: null`：前者是「没说改什么」，后者是「改成没有」。"""
        status, got = self._post({"role": "organ", "by": "day", "room": "工作台",
                                  "card_id": self.card["card_id"]})
        self.assertEqual(400, status)
        self.assertEqual("BAD_ARGUMENT", got["code"])

    def test_gate_and_method_and_unknown_item(self) -> None:
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": []}), encoding="utf-8")
        status, got = self._post({"role": "agent", "by": "她", "room": "工作台",
                                  "card_id": self.card["card_id"], "progress": 40})
        self.assertEqual(403, status)
        self.assertEqual("whitelist", got["door"])
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": list(FIVE)}), encoding="utf-8")
        status, got = self._post({"role": "organ", "by": "day", "room": "工作台",
                                  "card_id": "工作台-99", "progress": 40})
        self.assertEqual(404, status)
        self.assertEqual("CARD_NOT_FOUND", got["code"])
        status, _got = api.handle("GET", "/home/progress", None, self.root)
        self.assertEqual(405, status)


if __name__ == "__main__":
    unittest.main()
