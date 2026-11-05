"""家（/home/anchors*）的 HTTP 层：台词卡读得回来、只追加、body 写别人被 400 挡下。

覆盖 api/home.py 新增的两条路由与 api/core.py 的接线。每个用例自己造临时家，真 data/ 不碰。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from api import api
from api import core as api_core
from api import home as home_api
from organs.home import access, anchors, rooms

# 五句原文只有一份权威副本（organs/home/tests/test_anchors.py）——「不复制原文到别处」也管测试：
# 这里要验逐字一致，但不必再抄一遍，抄两份就是两处真源。
from organs.home.tests.test_anchors import FIVE  # noqa: E402
FIVE_ROOMS = ("玄关", "名片盒", "工作台", "仓库", "信箱")


def make_home(base: Path) -> Path:
    root = base / "home"
    for name in rooms.ROOM_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "access.json").write_text(
        json.dumps({"version": 1, "writable": list(FIVE_ROOMS)}), encoding="utf-8")
    return root


class HomeAnchorsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_home(Path(self._tmp.name))

    def _call(self, method, path, body=None, query=None):
        return api.handle(method, path, body, self.root, query=query)

    def _append(self, cards, role="human", by="主人"):
        return self._call("POST", "/home/anchors/append",
                          {"role": role, "by": by, "anchors": cards})

    def test_two_routes_are_registered(self) -> None:
        family = {path: method for path, method in api_core.ROUTES.items()
                  if path.startswith("/home/anchors")}
        self.assertEqual({"/home/anchors": "GET", "/home/anchors/append": "POST"}, family)

    def test_append_then_read_back_verbatim(self) -> None:
        status, payload = self._append([dict(card) for card in FIVE])
        self.assertEqual(200, status, payload)
        self.assertEqual(5, payload["count"])
        self.assertFalse(payload["readonly"])
        status, got = self._call("GET", "/home/anchors")
        self.assertEqual(200, status)
        self.assertTrue(got["ok"] and got["readonly"])
        self.assertEqual(5, got["total"])
        self.assertEqual(5, got["lines"])
        by_scene = {card["scene"]: card for card in got["anchors"]}
        for want in FIVE:
            self.assertEqual(want["say"], by_scene[want["scene"]]["say"])

    def test_changing_one_line_lands_a_new_row_and_reads_latest(self) -> None:
        self._append([dict(card) for card in FIVE])
        changed = FIVE[0]["say"][:-1] + "？"      # 由原文派生，不另抄一句
        status, payload = self._append([{"scene": "被夸", "say": changed,
                                         "body": FIVE[0]["body"]}])
        self.assertEqual(200, status, payload)
        _status, got = self._call("GET", "/home/anchors")
        self.assertEqual(6, got["lines"], "改一句是追加，不是覆盖")
        self.assertEqual(5, got["total"])
        latest = {card["scene"]: card for card in got["anchors"]}["被夸"]
        self.assertEqual(changed, latest["say"])

    def test_body_written_on_someone_else_is_refused_with_400(self) -> None:
        status, payload = self._append([{"scene": "被夸", "say": "嗯。", "body": "我给你倒了水"}])
        self.assertEqual(400, status)
        self.assertEqual("BODY_NOT_SELF", payload["code"])
        self.assertEqual("body", payload["detail"].get("field", "body"))
        _status, got = self._call("GET", "/home/anchors")
        self.assertEqual(0, got["total"], "被拒的整批一行都不许落")

    def test_wrong_method_is_405(self) -> None:
        status, payload = self._call("POST", "/home/anchors", {"role": "human"})
        self.assertEqual(405, status)
        status, payload = self._call("GET", "/home/anchors/append")
        self.assertEqual(405, status)

    def test_warehouse_row_carries_the_anchor_count(self) -> None:
        self._append([dict(card) for card in FIVE])
        _status, table = self._call("GET", "/home/rooms")
        row = {item["name"]: item for item in table["rooms"]}["仓库"]
        self.assertEqual(5, row["anchors"], "仓库的台词条数要能被房表看见")
        self.assertEqual(0, row["count"], "台词不占 home-objects 的单件身份：index 仍是 0 件")
        others = {item["name"]: item for item in table["rooms"]}
        self.assertEqual(0, others["工作台"]["anchors"], "别间不答台词")

    def test_whitelist_door_turns_her_back_with_403(self) -> None:
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": []}), encoding="utf-8")
        status, payload = self._append([dict(card) for card in FIVE], role="agent", by="她")
        self.assertEqual(403, status)
        self.assertEqual("ROOM_READONLY", payload["code"])
        self.assertEqual("whitelist", payload["door"])
        status, _payload = self._append([dict(card) for card in FIVE], role="organ", by="程序")
        self.assertEqual(200, status, "她今天不能写，不等于这间放不了东西")

    def test_unconnected_home_is_not_a_500(self) -> None:
        status, payload = api.handle("GET", "/home/anchors", None, None)
        self.assertEqual(200, status)
        self.assertEqual("未接入", payload["status"])
        self.assertTrue(payload["readonly"])


if __name__ == "__main__":
    unittest.main()
