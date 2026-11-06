"""取图的范围由家的房表与格子表决定：仓库的卡 → 抽屉的卡 → 格子名取最新 → 抽屉目录里的文件名。

这组用例**用临时家根 + 家的真写口造夹具**（`cards.append_cards` 造抽屉卡、
`residents.register_asset` 造素材卡），实盘 home/ 一个字节都不碰。

判据（手线第四批）：
- 真图不在仓库、在抽屉里：给**文件名**要能找到那张**原文件**（不复制本体）；
- 给**格子名**（`SHELF_KINDS` 里的数据值）→ 取该格里最新一张有真图的；
- 「名字不在家」与「卡在而文件不在」是两件事，`reason` 不许并成一句
  （`NAME_NOT_IN_HOME` / `FILE_MISSING`）；
- 每一笔命中都说清是**哪一步**找到的（`picked_reason`）与命中的是**哪一间**（`room`，值从家来）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from organs.home import cards as home_cards
from organs.home import residents as home_residents

from organs.hand import assets

from . import helpers


def snapshot(directory: Path) -> Dict[str, Any]:
    """目录的「几个文件、共几字节」：不复制本体的证据。"""
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    return {"names": [path.name for path in files],
            "bytes": sum(path.stat().st_size for path in files)}


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)
        self.home = helpers.make_home(self.root)
        #: 夹具里那张图的落点（抽屉/自画像/江迟-人设卡.jpg，照实盘的名字）。
        self.drawer = helpers.shelf_dir(self.root, home_residents.ARTWORK_KIND)


class DrawerCardTest(BaseCase):
    """① 名字就是抽屉卡上那个文件名。"""

    def test_drawer_card_is_found_by_name_and_points_at_the_original_file(self):
        image = helpers.register_portrait(self.root, "江迟-人设卡.jpg")
        # 家把同一张图也登记成人设卡（名字带后缀说明）——两份卡同名不同名都不许认错。
        home_cards.append_cards(
            root=helpers.home_root(self.root), role="human", by="人",
            cards=[{"room": home_cards.SHELF_ROOM, "kind": "人设卡",
                    "name": "江迟-人设卡.jpg 的文字面",
                    "params": {"image_path": str(image), "名称": "江迟"}}])

        out = assets.find_asset(name="江迟-人设卡.jpg", root=self.root)

        self.assertTrue(out["found"], out)
        self.assertTrue(out["exists"])
        self.assertIsNone(out["reason"])
        self.assertEqual("drawer_card", out["picked_reason"])
        self.assertEqual(home_cards.SHELF_ROOM, out["room"], "命中的是哪一间，值从家来")
        self.assertEqual(str(image), out["path"], "path 必须是原文件路径")
        self.assertEqual(helpers.sha256_of(helpers.IMAGE_BYTES), out["sha256"])
        self.assertEqual(len(helpers.IMAGE_BYTES), out["bytes"])
        self.assertEqual("江迟-人设卡.jpg", out["name"])
        self.assertEqual({"found", "name", "kind", "card_id", "path", "sha256", "bytes",
                          "exists", "reason", "picked_reason", "room"}, set(out),
                         "旧键一个都不许少、新键就这两个")

    def test_name_match_is_verbatim_so_the_near_miss_card_is_not_picked(self):
        helpers.register_portrait(self.root, "江迟-人设卡.jpg")
        home_cards.append_cards(
            root=helpers.home_root(self.root), role="human", by="人",
            cards=[{"room": home_cards.SHELF_ROOM, "kind": "人设卡",
                    "name": "江迟-人设卡.jpg 的文字面",
                    "params": {"image_path": "（不在盘上）"}}])
        out = assets.find_asset(name="江迟-人设卡", root=self.root)     # 少了后缀
        self.assertFalse(out["found"])
        self.assertEqual("NAME_NOT_IN_HOME", out["reason"])


class ShelfLatestTest(BaseCase):
    """② 名字是**格子名** → 该格最新一张有真图的（卡序最后）。"""

    def test_shelf_name_returns_the_latest_portrait_with_a_real_file(self):
        self.assertIn(home_residents.ARTWORK_KIND, home_cards.SHELF_KINDS,
                      "那个 kind 得真的是抽屉的一格，否则这条用例就是自说自话")
        older = helpers.register_portrait(self.root, "旧的自画像.png", b"old" * 32)
        newer = helpers.register_portrait(self.root, "江迟-人设卡.jpg")

        out = assets.find_asset(name=home_residents.ARTWORK_KIND, root=self.root)

        self.assertTrue(out["found"], out)
        self.assertEqual("shelf_latest", out["picked_reason"])
        self.assertEqual(home_cards.SHELF_ROOM, out["room"])
        self.assertEqual(str(newer), out["path"], "卡序最后的那张才算「最新」")
        self.assertNotEqual(str(older), out["path"])
        self.assertEqual(helpers.sha256_of(helpers.IMAGE_BYTES), out["sha256"])

    def test_shelf_name_falls_back_to_the_files_in_the_shelf_directory(self):
        """该格**没有卡**、文件却在：看目录里实际存在的图片文件。"""
        image = helpers.make_image(self.drawer, "谁也没登记.jpg")
        out = assets.find_asset(name=home_residents.ARTWORK_KIND, root=self.root)
        self.assertTrue(out["found"], out)
        self.assertEqual("shelf_latest", out["picked_reason"])
        self.assertEqual(str(image), out["path"])
        self.assertIsNone(out["card_id"])

    def test_shelf_name_whose_cards_all_point_nowhere_is_file_missing(self):
        gone = helpers.register_portrait(self.root, "会走的.jpg")
        gone.unlink()
        out = assets.find_asset(name=home_residents.ARTWORK_KIND, root=self.root)
        self.assertFalse(out["found"])
        self.assertEqual("FILE_MISSING", out["reason"])


class ReasonTest(BaseCase):
    """④⑤ 两种「没有」不许并成一句。"""

    def test_unknown_name_says_the_name_is_not_in_home(self):
        out = assets.find_asset(name="不存在的图", root=self.root)
        self.assertFalse(out["found"])
        self.assertEqual("NAME_NOT_IN_HOME", out["reason"])
        self.assertIsNone(out["path"])
        self.assertIsNone(out["picked_reason"])
        self.assertIsNone(out["room"])

    def test_card_whose_file_was_moved_away_is_file_missing_not_name_not_in_home(self):
        image = helpers.register_portrait(self.root, "会消失.jpg")
        image.unlink()
        out = assets.find_asset(name="会消失.jpg", root=self.root)
        self.assertTrue(out["found"], "卡还在：这是「卡在而文件不在」，不是「没这个名字」")
        self.assertFalse(out["exists"])
        self.assertEqual("FILE_MISSING", out["reason"])
        self.assertEqual("drawer_card", out["picked_reason"])
        self.assertEqual(str(image), out["path"], "卡上那个路径照原样报出来")
        self.assertEqual(helpers.sha256_of(helpers.IMAGE_BYTES), out["sha256"],
                         "文件没了就如实报卡上那份指纹，不装作算过")


class WarehouseStillWorksTest(BaseCase):
    """⑥ 仓库那条路一个字都不许回退（素材卡优先于借阅条、名字原样比）。"""

    def test_asset_card_in_the_warehouse_is_still_hit(self):
        image = helpers.make_image(Path(self._tmp.name) / "assets", "参考图.png")
        helpers.register_asset(self.root, image)

        out = assets.find_asset(name="参考图.png", root=self.root)

        self.assertTrue(out["found"], out)
        self.assertEqual("warehouse_asset", out["picked_reason"])
        self.assertEqual(home_residents.WAREHOUSE_ROOM, out["room"])
        self.assertEqual(str(image), out["path"])

    def test_warehouse_wins_over_the_drawer_when_both_have_the_name(self):
        image = helpers.make_image(Path(self._tmp.name) / "assets", "同名.png")
        helpers.register_asset(self.root, image)
        helpers.register_portrait(self.root, "同名.png")

        out = assets.find_asset(name="同名.png", root=self.root)

        self.assertEqual("warehouse_asset", out["picked_reason"], "顺序是仓库→抽屉，不许倒过来")
        self.assertEqual(str(image), out["path"])


class DrawerFileTest(BaseCase):
    """④ 没登记成卡、文件却在：按抽屉各格目录里的文件名原样比。"""

    def test_bare_file_in_a_shelf_is_found_by_its_file_name(self):
        image = helpers.make_image(helpers.shelf_dir(self.root, "人设卡"), "没登记的.jpg")
        out = assets.find_asset(name="没登记的.jpg", root=self.root)
        self.assertTrue(out["found"], out)
        self.assertEqual("drawer_file", out["picked_reason"])
        self.assertEqual(home_cards.SHELF_ROOM, out["room"])
        self.assertEqual(str(image), out["path"])

    def test_a_name_with_a_separator_is_never_glued_into_a_directory(self):
        out = assets.find_asset(name="../人设卡/没登记的.jpg", root=self.root)
        self.assertFalse(out["found"])
        self.assertEqual("NAME_NOT_IN_HOME", out["reason"])

    def test_non_image_suffixes_are_not_offered_as_pictures(self):
        helpers.write_raw(self.drawer / "笔记.md", "这不是图")
        out = assets.find_asset(name="笔记.md", root=self.root)
        self.assertFalse(out["found"])
        self.assertEqual("NAME_NOT_IN_HOME", out["reason"])


class NoCopyTest(BaseCase):
    """⑦ 只取指针：跑完抽屉的文件数与字节数一个都不变，path 就在抽屉里。"""

    def test_looking_up_an_image_never_copies_it(self):
        helpers.register_portrait(self.root, "江迟-人设卡.jpg")
        before = snapshot(self.drawer)
        listing = sorted(path.name for path in self.home.rglob("*") if path.is_file())

        for wanted in ("江迟-人设卡.jpg", home_residents.ARTWORK_KIND):
            out = assets.find_asset(name=wanted, root=self.root)
            self.assertTrue(out["found"], out)
            self.assertEqual(self.drawer, Path(out["path"]).parent, "path 就在抽屉那一格里")

        self.assertEqual(before, snapshot(self.drawer), "文件的个数与字节数一个都不许变")
        self.assertEqual(listing, sorted(path.name for path in self.home.rglob("*")
                                         if path.is_file()),
                         "家里不许凭空多出临时件")

    def test_the_hand_directory_stays_empty(self):
        """手的盘上什么都不落：这不是「落一份副本再指过去」。"""
        helpers.register_portrait(self.root, "江迟-人设卡.jpg")
        assets.find_asset(name="江迟-人设卡.jpg", root=self.root)
        self.assertEqual([], sorted(path.name for path in self.root.iterdir()))


class OrderTest(BaseCase):
    """五步的顺序就是证据：picked_reason 说得出是哪一步命中的。"""

    def test_the_same_name_in_two_kinds_of_drawer_card_picks_the_first_shelf(self):
        """②按 `SHELF_KINDS` 的顺序走：自画像那一格排在前面，就先命中它。"""
        first = helpers.register_portrait(self.root, "同一个名字.jpg")
        # 人设卡那一格放同名的一张：它排在 `SHELF_KINDS` 的后面，不许抢
        helpers.make_image(helpers.shelf_dir(self.root, "人设卡"), "同一个名字.jpg", b"other" * 8)
        out = assets.find_asset(name="同一个名字.jpg", root=self.root)
        self.assertEqual("drawer_card", out["picked_reason"])
        self.assertEqual(str(first), out["path"])

    def test_every_hit_names_a_reason_from_the_frozen_list(self):
        self.assertEqual({"warehouse_asset", "warehouse_pointer", "drawer_card",
                          "shelf_latest", "drawer_file"},
                         {assets.WAREHOUSE_ASSET, assets.WAREHOUSE_POINTER,
                          assets.DRAWER_CARD, assets.SHELF_LATEST, assets.DRAWER_FILE})


if __name__ == "__main__":
    unittest.main()
