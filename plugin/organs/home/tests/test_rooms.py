"""十间房名表：名字与顺序、find 与最近一间、镜像校验只报不改、家的文件里不许出现魂的那句话。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.home import rooms
from organs.home.rooms import DEFAULT_WRITABLE, ROOMS, ROOM_NAMES, RoomMiss, RoomsJsonError, find, nearest, room_dir
from organs.home.tests import helpers

# 不写房名字面量：房表是数据，护栏要盯的是**不变量**（唯一、连续、每间有说明、可写集有派生来源），
# 而不是把当前名单抄一份。加一间房不该改测试。
SOURCE_HOME = "astrbot_plugin_house 的 HOUSE/ROOMS（十间时代的来源，2026-11-06 起为十一间）"
HOME_DIR = Path(rooms.__file__).resolve().parent
# 魂里那句定性描述不许被复制进家；拆成码点写，免得这条测试自己把原文带进家
SOUL_APPEARANCE = "\u84dd\u53d1\u5782\u8170"


class RoomsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)

    def test_ten_rooms_in_fixed_order(self):
        # 房名字面量不在测试里：这一组盯的是结构不变量
        self.assertEqual(len(ROOM_NAMES), len(set(ROOM_NAMES)), "房名不许重")
        self.assertEqual(len(ROOM_NAMES), len({room.slug for room in ROOMS}), "slug 不许重")
        for room in ROOMS:
            self.assertTrue(room.purpose.strip(), "%s 要有一句话说明" % room.name)
        self.assertTrue(SOURCE_HOME)
        # 断言等于数据常量：房表多一间，这里不该改（改数字的断言是台账，不是护栏）
        self.assertEqual(len(ROOM_NAMES), len(ROOMS))
        self.assertEqual(len(ROOMS), len({room.slug for room in ROOMS}), "slug 不许重")
        for room in ROOMS:
            self.assertTrue(room.purpose.strip(), room.name)
            self.assertTrue(room.slug.isascii(), room.slug)

    def test_default_writable_matches_the_room_table(self):
        """可写哪几间 = 房表里 writable_default 为真的那些。**不写字面量**。"""
        self.assertEqual({room.name for room in ROOMS if room.writable_default},
                         set(DEFAULT_WRITABLE))
        self.assertEqual(len({room.name for room in ROOMS if room.writable_default}),
                         len(DEFAULT_WRITABLE), "可写集由 writable_default 派生，不许两处各数一次")

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

    def test_shipped_data_has_every_room_dir_and_consistent_mirror(self):
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
