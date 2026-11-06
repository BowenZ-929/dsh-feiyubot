"""出厂默认 vs 实盘：三份默认文件走同一条通则，各四条一样的断言。

通则（docs/modules/home.md §3 第 13 步）：随包发的 `plugin/organs/home/data/` 是 seed 料（只读参数），
实盘是唯一真源；读一律经 `store.home_root()` 解析，不许硬编码包内路径、不许留回退兜底。

三份默认文件：`access.json`（白名单）、`rooms.json`（房名表镜像）、`仓库/anchors.jsonl`（台词库）。
每份四条：① 默认读经实盘根；② 改实盘读跟着变；③ **改出厂读不变**；④ 源码级不许出现
`seed_path`／`read_seed`／写死的 `"data"`。

第 ③ 条是**真动手改再字节级还原**——出厂那份就在代码树里，脏了会连累别人，所以一律 try/finally
并断言还原成功。台词卡那四条已经写在 test_anchors.py，这里只补 access 与 rooms。
"""
from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.home import access, anchors, rooms, store

FIVE = ("玄关", "名片盒", "工作台", "仓库", "信箱")


def _no_env_access():
    """把 HOME_ACCESS_FILE 钉成空串：空值不算设过，读路径才会落到 root 那一级。"""
    return mock.patch.dict(os.environ, {access.ENV_ACCESS: ""}, clear=False)


class FactoryVsLiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.live = Path(self._tmp.name) / "live"
        self.live.mkdir()

    # —— access.json ————————————————————————————————

    def _write_live_access(self, writable) -> Path:
        path = self.live / access.ACCESS_NAME
        path.write_text(json.dumps({"version": 1, "writable": list(writable)}), encoding="utf-8")
        return path

    def test_access_default_read_goes_through_the_home_root(self):
        self.assertEqual(self.live / access.ACCESS_NAME,
                         store.home_root(self.live) / access.ACCESS_NAME)
        self.assertNotEqual(store.home_root(self.live) / access.ACCESS_NAME, access.seed_path(),
                            "实盘与出厂默认必须是两条不同的路")

    def test_access_live_change_is_what_reads(self):
        self._write_live_access(["工作台"])
        seed_before = access.seed_path().read_bytes()
        with _no_env_access():
            self.assertEqual({"工作台"}, set(access.load_whitelist(self.live).writable))
            self._write_live_access(["仓库"])
            self.assertEqual({"仓库"}, set(access.load_whitelist(self.live).writable),
                             "改了实盘，读出来的必须是新的那份")
        self.assertEqual(seed_before, access.seed_path().read_bytes(), "改实盘不许改到出厂那份")

    def _broken(self, name: str, text: str) -> Path:
        path = Path(self._tmp.name) / ("broken-" + name)
        path.write_text(text, encoding="utf-8")
        return path

    def test_access_seed_corruption_does_not_affect_reads(self):
        """把出厂那份改坏 → 读实盘不受影响（真源只有一个）。

        **不动代码树里那个文件**：它被 git 跟踪，改它等于让并发的另一轮测试读到残局
        （这条踩过：一次被打断的运行把仓里的出厂 access.json 留成了空表）。
        这里改的是「出厂默认指向哪儿」，要证明的是同一件事——读路径根本不问 seed_path。
        """
        self._write_live_access(["工作台"])
        broken = self._broken("access.json", json.dumps({"version": 1, "writable": []}))
        with mock.patch.object(access, "seed_path", return_value=broken), _no_env_access():
            self.assertEqual({"工作台"}, set(access.load_whitelist(self.live).writable),
                             "把出厂那份的 writable 改掉，读出来不许变")
            self.assertEqual(set(), set(access.read_seed().writable),
                             "sanity: 被改坏的是出厂那份，它自己也读得出被改过")

    def test_access_read_paths_never_touch_the_seed(self):
        source = inspect.getsource(access.load_whitelist)
        for banned in ("seed_path", "read_seed", '"data"', "'data'"):
            self.assertNotIn(banned, source, "load_whitelist 里出现了 %s：读路径不许碰出厂那份" % banned)

    # —— rooms.json ————————————————————————————————

    def _write_live_rooms(self, text: str) -> Path:
        path = self.live / rooms.ROOMS_JSON_NAME
        path.write_text(text, encoding="utf-8")
        return path

    def test_rooms_default_read_goes_through_the_home_root(self):
        self.assertNotEqual(store.home_root(self.live) / rooms.ROOMS_JSON_NAME, rooms.seed_path(),
                            "实盘与出厂默认必须是两条不同的路")

    def test_rooms_live_change_is_what_reads(self):
        self._write_live_rooms(rooms.rooms_json_text())
        seed_before = rooms.seed_path().read_bytes()
        self.assertEqual(len(rooms.ROOMS), len(rooms.check_rooms_json(self.live)["rooms"]))
        self._write_live_rooms('{"version": 1, "rooms": []}')
        with self.assertRaises(rooms.RoomsJsonError) as caught:
            rooms.check_rooms_json(self.live)
        self.assertEqual("ROOMS_JSON_DRIFT", caught.exception.code, "改实盘，读必须跟着变")
        self.assertEqual(seed_before, rooms.seed_path().read_bytes(), "改实盘不许改到出厂那份")

    def test_rooms_seed_corruption_does_not_affect_reads(self):
        self._write_live_rooms(rooms.rooms_json_text())
        broken = self._broken("rooms.json", '{"version": 1, "rooms": [{"name": "厨房"}]}')
        with mock.patch.object(rooms, "seed_path", return_value=broken):
            self.assertEqual(len(rooms.ROOMS),
                             len(rooms.check_rooms_json(self.live)["rooms"]),
                             "把出厂那份镜像改坏，实盘读出来不许受影响")
            self.assertEqual([{"name": "厨房"}], rooms.read_seed()["rooms"],
                             "sanity: 被改坏的是出厂那份")

    def test_rooms_read_paths_never_touch_the_seed(self):
        source = inspect.getsource(rooms.check_rooms_json)
        for banned in ("seed_path", "read_seed", '"data"', "'data'"):
            self.assertNotIn(banned, source, "check_rooms_json 里出现了 %s" % banned)

    # —— limits.json（两本账的额度）——————————————————

    def test_limits_default_read_goes_through_the_home_root(self) -> None:
        self.assertNotEqual(store.home_root(self.live) / access.LIMITS_NAME,
                            access.limits_seed_path(),
                            "实盘与出厂默认必须是两条不同的路")

    def test_limits_live_change_is_what_reads(self) -> None:
        path = self.live / access.LIMITS_NAME
        path.write_text(json.dumps({"version": 1, "writes_per_room_per_day": 7,
                                    "consumes_per_room_per_day": 8}), encoding="utf-8")
        seed_before = access.limits_seed_path().read_bytes()
        self.assertEqual((7, 8), (access.load_limits(self.live).writes_per_room_per_day,
                                  access.load_limits(self.live).consumes_per_room_per_day))
        self.assertEqual(seed_before, access.limits_seed_path().read_bytes(),
                         "改实盘不许改到出厂那份")

    def test_limits_seed_corruption_does_not_affect_reads(self) -> None:
        self.live.mkdir(exist_ok=True)
        path = self.live / access.LIMITS_NAME
        path.write_text(json.dumps({"version": 1, "writes_per_room_per_day": 7,
                                    "consumes_per_room_per_day": 8}), encoding="utf-8")
        broken = self._broken("limits.json",
                              '{"version": 1, "writes_per_room_per_day": 999,'
                              ' "consumes_per_room_per_day": 999}')
        with mock.patch.object(access, "limits_seed_path", return_value=broken):
            loaded = access.load_limits(self.live)
            self.assertEqual((7, 8), (loaded.writes_per_room_per_day,
                                      loaded.consumes_per_room_per_day),
                             "把出厂那份额度改掉，读出来不许变")
            self.assertEqual(999, access.read_limits_seed().writes_per_room_per_day,
                             "sanity: 被改坏的是出厂那份")

    def test_limits_read_path_never_touches_the_seed(self) -> None:
        source = inspect.getsource(access.load_limits)
        for banned in ("limits_seed_path", "read_limits_seed"):
            self.assertNotIn(banned, source, "load_limits 里出现了 %s" % banned)

    # —— 三份的一致口径 ————————————————————————————

    def test_three_seed_paths_live_under_the_factory_dir_and_off_the_live_root(self):
        factory = store.factory_path("")
        for name, path in (("access", access.seed_path()), ("rooms", rooms.seed_path()),
                           ("anchors", anchors.seed_path()),
                           ("limits", access.limits_seed_path())):
            with self.subTest(seed=name):
                self.assertTrue(str(path).startswith(str(factory)),
                                "%s 的出厂默认要落在 %s 下面" % (name, factory))
                self.assertFalse(str(path).startswith(str(store.home_root())),
                                 "%s 的出厂默认不许落在实盘根下面" % name)


if __name__ == "__main__":
    unittest.main()
