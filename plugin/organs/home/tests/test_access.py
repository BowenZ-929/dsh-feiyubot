"""准入：白名单三级解析（缺件响亮）、写前各道闸各一条、读侧与审计不以「她能写」为前提。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.home import access, audit, rooms
from organs.home.access import (
    AccessDenied, Whitelist, check_path_shape, check_read, check_write, load_whitelist,
)
from organs.home.tests import helpers

FIVE = ("玄关", "名片盒", "工作台", "仓库", "信箱")


class WhitelistTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)

    def test_root_file_is_the_third_level(self):
        helpers.write_access(self.root, FIVE)
        whitelist = load_whitelist(self.root)
        self.assertEqual(set(FIVE), set(whitelist.writable))
        self.assertTrue(whitelist.source.startswith("root:"))

    def test_env_beats_root_and_explicit_beats_env(self):
        helpers.write_access(self.root, FIVE)
        env_path = Path(self._tmp.name) / "env" / "access.json"
        helpers.write_raw_access(env_path, json.dumps({"version": 1, "writable": ["玄关"]}, ensure_ascii=False))
        with mock.patch.dict(os.environ, {"HOME_ACCESS_FILE": str(env_path)}, clear=False):
            self.assertEqual({"玄关"}, set(load_whitelist(self.root).writable))
            self.assertTrue(load_whitelist(self.root).source.startswith("env:"))
            explicit = load_whitelist(self.root, explicit={"writable": ["信箱"]})
            self.assertEqual({"信箱"}, set(explicit.writable))
            self.assertEqual("explicit", explicit.source)

    def test_missing_file_is_loud_not_empty(self):
        with self.assertRaises(AccessDenied) as caught:
            load_whitelist(self.root)
        self.assertEqual("ACCESS_FILE_MISSING", caught.exception.code)
        self.assertEqual({"used", "limit", "needed"}, set(caught.exception.detail) & {"used", "limit", "needed"})

    def test_broken_file_is_loud(self):
        helpers.write_raw_access(self.root / "access.json", "{不是 json")
        with self.assertRaises(AccessDenied) as caught:
            load_whitelist(self.root)
        self.assertEqual("ACCESS_FILE_BAD", caught.exception.code)

    def test_unknown_room_in_file_is_loud(self):
        helpers.write_access(self.root, ["厨房"])
        with self.assertRaises(AccessDenied) as caught:
            load_whitelist(self.root)
        error = caught.exception
        self.assertEqual("ACCESS_ROOM_UNKNOWN", error.code)
        self.assertEqual("厨房", error.detail["room"])
        self.assertIn(error.detail["nearest"], rooms.ROOM_NAMES)

    def test_naming_a_readonly_room_is_loud(self):
        """配置与结构打架：白名单只许在结构可写的五间里点名，点名抽屉就报，不静默取交集。"""
        helpers.write_access(self.root, ["抽屉"])
        with self.assertRaises(AccessDenied) as caught:
            load_whitelist(self.root)
        self.assertEqual("ACCESS_ROOM_READONLY", caught.exception.code)
        self.assertEqual("抽屉", caught.exception.detail["room"])

    def test_unknown_version_is_loud(self):
        helpers.write_access(self.root, FIVE, version=2)
        with self.assertRaises(AccessDenied) as caught:
            load_whitelist(self.root)
        self.assertEqual("ACCESS_VERSION_UNKNOWN", caught.exception.code)

    def test_whitelist_is_reread_every_call(self):
        helpers.write_access(self.root, FIVE)
        self.assertEqual(5, len(load_whitelist(self.root).writable))
        helpers.write_access(self.root, ["工作台"])
        self.assertEqual({"工作台"}, set(load_whitelist(self.root).writable))


class EmptyWhitelistTest(unittest.TestCase):
    """她能写 0 间（门二关死）时，别的器官照常进门、读侧与审计照常——「不把她的写权当前提」的完整证明。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)
        helpers.write_access(self.root, [])

    def test_door_two_closes_for_her_only(self):
        for room in ("工作台", "仓库"):
            with self.assertRaises(AccessDenied) as caught:
                check_write(role="agent", room=room, name="一张卡.md", chars=10, root=self.root)
            denied = caught.exception
            self.assertEqual("ROOM_READONLY", denied.code)
            self.assertEqual("whitelist", denied.detail["door"])
            self.assertEqual(0, denied.detail["limit"])
            self.assertEqual(1, denied.detail["needed"])
        for role in ("organ", "human"):
            check_write(role=role, room="工作台", name="一张卡.md", chars=10, root=self.root)

    def test_door_one_still_closes_for_everyone(self):
        for role in ("agent", "organ", "human"):
            with self.assertRaises(AccessDenied) as caught:
                check_write(role=role, room="抽屉", name="资料卡.md", chars=10, root=self.root)
            self.assertEqual("ROOM_READONLY", caught.exception.code)
            self.assertEqual("structure", caught.exception.detail["door"])

    def test_read_side_and_audit_keep_working(self):
        check_read(room="抽屉", name="资料卡.md", lines=200, chars=6000)
        entry = audit.append_entry(self.root, role="agent", by="test", room="工作台",
                                   name="x.md", chars=3, bytes=3)
        self.assertEqual(1, entry.seq)
        self.assertEqual([1], [item.seq for item in audit.read_tail(self.root)])
        self.assertTrue(audit.verify(self.root)["ok"])
        self.assertEqual("抽屉", rooms.find("drawer").name)


class StructureDoorTest(unittest.TestCase):
    """门一：只读五间对三个角色一律拒——房型不因角色而变；它与 writable_default 是同一份事实。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)
        helpers.write_access(self.root, FIVE)

    def test_fifteen_combinations_are_all_refused(self):
        self.assertEqual({"客厅", "卧室", "台历", "橱柜", "抽屉"}, set(rooms.READONLY_ROOMS))
        seen = 0
        for room in sorted(rooms.READONLY_ROOMS):
            for role in access.ROLES:
                with self.assertRaises(AccessDenied) as caught:
                    check_write(role=role, room=room, name="x.md", chars=10, root=self.root)
                denied = caught.exception
                self.assertEqual("ROOM_READONLY", denied.code)
                self.assertEqual("structure", denied.detail["door"])
                self.assertIn(room, denied.message)
                self.assertIn("只读", denied.message)
                seen += 1
        self.assertEqual(15, seen)

    def test_structure_is_one_fact_with_writable_default(self):
        self.assertEqual(set(rooms.ROOM_NAMES) - set(rooms.DEFAULT_WRITABLE),
                         set(rooms.READONLY_ROOMS))
        self.assertIs(access.ROOM_READONLY, rooms.READONLY_ROOMS)

    def test_mirror_carries_the_same_split(self):
        path = rooms.write_rooms_json(self.root)
        rows = json.loads(path.read_text(encoding="utf-8"))["rooms"]
        writable = {row["name"] for row in rows if row["writable"]}
        self.assertEqual(set(rooms.DEFAULT_WRITABLE), writable)


class WriteGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)
        helpers.write_access(self.root, FIVE)

    def _deny(self, code: str, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            check_write(root=self.root, **kwargs)
        self.assertEqual(code, caught.exception.code)
        detail = caught.exception.detail
        for key in ("used", "limit", "needed"):
            self.assertIn(key, detail)
        return caught.exception

    def test_happy_path(self):
        check_write(role="agent", room="工作台", name="一张卡.md", chars=2000, files=20, root=self.root)

    def test_room_not_in_ten(self):
        error = self._deny("ROOM_MISS", role="agent", room="厨房", name="x.md", chars=1)
        self.assertIn(error.detail["nearest"], rooms.ROOM_NAMES)

    def test_readonly_room(self):
        error = self._deny("ROOM_READONLY", role="agent", room="客厅", name="x.md", chars=1)
        self.assertEqual("structure", error.detail["door"])

    def test_absolute_path(self):
        self._deny("PATH_ABSOLUTE", role="agent", room="工作台", name="/etc/passwd", chars=1)

    def test_traversal(self):
        self._deny("PATH_TRAVERSAL", role="agent", room="工作台", name="../外面.md", chars=1)

    def test_control_char(self):
        self._deny("PATH_CONTROL_CHAR", role="agent", room="工作台", name="a\tb.md", chars=1)

    def test_segment_too_long(self):
        error = self._deny("PATH_SEGMENT_TOO_LONG", role="agent", room="工作台", name="抽" * 65, chars=1)
        self.assertEqual(65, error.detail["used"])
        self.assertEqual(64, error.detail["limit"])

    def test_too_deep(self):
        self._deny("PATH_TOO_DEEP", role="agent", room="工作台", name="a/b/c/d/e.md", chars=1)

    def test_chars_over_2000(self):
        error = self._deny("CHARS_OVER", role="agent", room="工作台", name="x.md", chars=2001)
        self.assertEqual(2001, error.detail["used"])
        self.assertEqual(2000, error.detail["limit"])
        self.assertEqual(1, error.detail["needed"])

    def test_files_over_20(self):
        error = self._deny("FILES_OVER", role="agent", room="工作台", name="x.md", chars=1, files=21)
        self.assertEqual(21, error.detail["used"])
        self.assertEqual(20, error.detail["limit"])

    def test_warehouse_cap(self):
        with mock.patch.object(access, "_room_files", return_value=16000):
            error = self._deny("ROOM_CAP_REACHED", role="agent", room="仓库", name="一张.png", chars=1)
        self.assertEqual(16000, error.detail["used"])
        self.assertEqual(16000, error.detail["limit"])
        self.assertEqual(1, error.detail["needed"])

    def test_cap_only_exists_for_three_rooms(self):
        self.assertEqual({"仓库", "卧室", "橱柜"}, set(access.ROOM_CAP))

    def test_day_quota_on_the_51st_write(self):
        helpers.seed_today(self.root, "工作台", 50)
        error = self._deny("DAY_QUOTA_REACHED", role="agent", room="工作台", name="x.md", chars=1)
        self.assertEqual(50, error.detail["used"])
        self.assertEqual(50, error.detail["limit"])

    def test_49_writes_today_still_pass(self):
        helpers.seed_today(self.root, "工作台", 49)
        check_write(role="agent", room="工作台", name="x.md", chars=1, root=self.root)

    def test_unknown_role(self):
        self._deny("ROLE_UNKNOWN", role="root", room="工作台", name="x.md", chars=1)

    def test_empty_name(self):
        self._deny("PATH_EMPTY", role="agent", room="工作台", name="   ", chars=1)


class ReadGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)

    def test_limits(self):
        check_read(room="仓库", name="一张.png", lines=200, chars=6000)
        with self.assertRaises(AccessDenied) as caught:
            check_read(room="仓库", name="一张.png", lines=201, chars=1)
        self.assertEqual("READ_LINES_OVER", caught.exception.code)
        with self.assertRaises(AccessDenied) as caught:
            check_read(room="仓库", name="一张.png", lines=1, chars=6001)
        self.assertEqual("READ_CHARS_OVER", caught.exception.code)

    def test_read_does_not_need_a_whitelist_file(self):
        check_read(room="抽屉", name="资料卡.md", lines=1, chars=1)


class PathShapeTest(unittest.TestCase):
    def test_nested_ok(self):
        check_path_shape("工作台", "a/b/c/d.md")

    def test_shape_is_room_agnostic(self):
        with self.assertRaises(AccessDenied) as caught:
            check_path_shape("客厅", "a/../b")
        self.assertEqual("PATH_TRAVERSAL", caught.exception.code)
        self.assertEqual("客厅", caught.exception.detail["room"])


if __name__ == "__main__":
    unittest.main()
