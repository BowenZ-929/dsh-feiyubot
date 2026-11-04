"""十间房名表：名字与顺序、find 与最近一间、镜像校验只报不改、家的文件里不许出现魂的那句话。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.home import rooms
from organs.home.rooms import DEFAULT_WRITABLE, ROOMS, ROOM_NAMES, RoomMiss, RoomsJsonError, find, nearest, room_dir
from organs.home.tests import helpers

EXPECTED = ("玄关", "客厅", "名片盒", "工作台", "仓库", "信箱", "卧室", "橱柜", "抽屉", "台历")
HOME_DIR = Path(rooms.__file__).resolve().parent
# 魂里那句定性描述不许被复制进家；拆成码点写，免得这条测试自己把原文带进家
SOUL_APPEARANCE = "\u84dd\u53d1\u5782\u8170"


class RoomsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)

    def test_ten_rooms_in_fixed_order(self):
        self.assertEqual(EXPECTED, ROOM_NAMES)
        self.assertEqual(10, len(ROOMS))
        self.assertEqual(10, len({room.slug for room in ROOMS}))
        for room in ROOMS:
            self.assertTrue(room.purpose.strip(), room.name)
            self.assertTrue(room.slug.isascii(), room.slug)

    def test_default_writable_is_the_five(self):
        self.assertEqual({"玄关", "名片盒", "工作台", "仓库", "信箱"}, set(DEFAULT_WRITABLE))
        self.assertEqual(5, len(DEFAULT_WRITABLE))

    def test_find_accepts_name_and_slug(self):
        self.assertEqual(find("抽屉"), find("drawer"))
        self.assertEqual("抽屉", find("drawer").name)
        self.assertEqual("抽屉", find("DRAWER").name)
        self.assertEqual("工作台", find("desk").name)

    def test_find_miss_carries_nearest_real_room(self):
        with self.assertRaises(RoomMiss) as caught:
            find("厨房")
        miss = caught.exception
        self.assertEqual("厨房", miss.name)
        self.assertIn(miss.nearest, ROOM_NAMES)
        self.assertEqual(miss.nearest, nearest("厨房"))
        self.assertIn(miss.nearest, str(miss))

    def test_room_dir_uses_chinese_name(self):
        self.assertEqual(self.root / "抽屉", room_dir("抽屉", self.root))
        self.assertEqual(self.root / "抽屉", room_dir("drawer", self.root))

    def test_shipped_data_has_ten_dirs_and_consistent_mirror(self):
        data = HOME_DIR / "data"
        for name in ROOM_NAMES:
            self.assertTrue((data / name).is_dir(), name)
        self.assertTrue((data / "rooms.json").is_file())
        data_mirror = rooms.check_rooms_json()
        self.assertEqual([room.name for room in ROOMS], [row["name"] for row in data_mirror["rooms"]])

    def test_mirror_drift_is_reported_and_not_repaired(self):
        path = rooms.write_rooms_json(self.root)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["rooms"][0]["purpose"] = "手改过的说明"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(RoomsJsonError) as caught:
            rooms.check_rooms_json(self.root)
        self.assertEqual("ROOMS_JSON_DRIFT", caught.exception.code)
        self.assertEqual(before, path.read_text(encoding="utf-8"))

    def test_missing_or_bad_mirror_is_loud(self):
        with self.assertRaises(RoomsJsonError) as caught:
            rooms.check_rooms_json(self.root)
        self.assertEqual("ROOMS_JSON_MISSING", caught.exception.code)
        (self.root / "rooms.json").write_text("{不是 json", encoding="utf-8")
        with self.assertRaises(RoomsJsonError) as caught:
            rooms.check_rooms_json(self.root)
        self.assertEqual("ROOMS_JSON_BAD", caught.exception.code)

    def test_home_files_do_not_copy_the_soul_appearance(self):
        """盘上的东西与代码里都不许有那句定性描述；tests/ 里那一行是拿它当针的检索式，不是家放的东西。"""
        offenders = []
        for path in HOME_DIR.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or "tests" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if SOUL_APPEARANCE in text:
                offenders.append(str(path))
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
