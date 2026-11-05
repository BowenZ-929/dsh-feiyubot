"""仓库·台词卡：五张逐字读回、改一句只追加、body 只许写她自己、审计跟着长一行。

覆盖 organs/home/anchors.py。五张卡的 `say` 在这里是**验收基准**：这张测试就是「一个字别改」
那句话的可执行形式，所以原文只在本文件与 `仓库/anchors.jsonl` 两处出现，别处一律不许有
（test_verbatim_is_not_copied_elsewhere 把这条扫成断言）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.home import access, anchors, audit, rooms, store

FIVE = (
    {"scene": "被夸", "say": "哼，才知道啊。", "body": "尾巴翘起来晃了两下"},
    {"scene": "被关心", "say": "行了行了，我又不是玻璃做的。", "body": "别开头、脸微微发红"},
    {"scene": "不想聊", "say": "这个啊……以后再说吧。", "body": ""},
    {"scene": "认错", "say": "……是我记岔了。", "body": "低头、尾巴垂下去不敢动"},
    {"scene": "你要走", "say": "你忙你的，我在这儿。", "body": "尾巴慢慢摆一下，乖巧的笑了笑"},
)
PACKAGE = Path(anchors.__file__).resolve().parent
PLUGIN = PACKAGE.parent.parent.parent


def make_home(base: Path) -> Path:
    root = base / "home"
    for name in rooms.ROOM_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def five() -> list:
    return [dict(card) for card in FIVE]


class AnchorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = make_home(self.base)

    def test_five_cards_read_back_verbatim(self) -> None:
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        got = anchors.read_all(root=self.root)
        self.assertEqual(5, got["total"])
        by_scene = {card["scene"]: card for card in got["anchors"]}
        for want in FIVE:
            have = by_scene[want["scene"]]
            self.assertEqual(want["say"], have["say"], "say 一个字都不许改：%s" % want["scene"])
            self.assertEqual(want["body"], have["body"], "body 一个字都不许改：%s" % want["scene"])
            self.assertEqual("主人", have["who"])
            self.assertTrue(have["ts"])

    def test_change_appends_and_latest_wins(self) -> None:
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=[
            {"scene": "被夸", "say": "哼，才知道啊？", "body": "尾巴翘起来晃了两下"}])
        got = anchors.read_all(root=self.root)
        self.assertEqual(6, got["lines"], "改一句是追加一行，不是覆盖")
        self.assertEqual(5, got["total"], "现行台词仍是五条")
        latest = {card["scene"]: card for card in got["anchors"]}["被夸"]
        self.assertEqual("哼，才知道啊？", latest["say"])
        rows, _bad = store.read_lines(anchors.anchors_path(self.root))
        self.assertEqual("哼，才知道啊。", rows[0]["say"], "旧的那句原样留着，不许被改写")

    def test_body_with_another_person_is_refused(self) -> None:
        for body in ("我给你倒了水", "看了主人一眼", "朝他点头", "脸红了"):
            with self.subTest(body=body):
                if body == "脸红了":
                    anchors.check_body(body)          # 只写自己：放行
                    continue
                with self.assertRaises(anchors.AnchorError) as caught:
                    anchors.append_anchors(root=self.root, role="human", by="主人",
                                           anchors=[{"scene": "被夸", "say": "嗯。", "body": body}])
                self.assertEqual(anchors.CODE_NO_SELF, caught.exception.code)
        self.assertFalse(anchors.anchors_path(self.root).exists(), "被拒的整批不许落盘")

    def test_missing_say_and_unknown_field_are_loud(self) -> None:
        with self.assertRaises(anchors.AnchorError) as caught:
            anchors.append_anchors(root=self.root, role="human", by="主人",
                                   anchors=[{"scene": "被夸", "body": "尾巴晃"}])
        self.assertEqual(anchors.CODE_BAD, caught.exception.code)
        with self.assertRaises(anchors.AnchorError) as caught:
            anchors.append_anchors(root=self.root, role="human", by="主人",
                                   anchors=[{"scene": "被夸", "say": "嗯。", "tone": "撒娇"}])
        self.assertEqual(anchors.CODE_BAD, caught.exception.code)
        self.assertIn("tone", caught.exception.message)

    def test_audit_grows_one_line_per_card(self) -> None:
        outcome = anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        self.assertEqual(5, len(outcome["audit"]))
        entries = audit.read_tail(root=self.root, limit=50)
        self.assertEqual(5, len(entries))
        self.assertEqual([1, 2, 3, 4, 5], [entry.seq for entry in entries])
        for entry in entries:
            self.assertEqual("仓库", entry.room)
            self.assertEqual("主人", entry.by)
            self.assertTrue(entry.name.startswith("anchors.jsonl#"), entry.name)
            self.assertTrue(entry.at, "谁、哪间、何时、多大——何时不许空")
        self.assertTrue(audit.verify(root=self.root)["ok"])

    def test_whitelist_door_holds_her_back_but_not_organ(self) -> None:
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": []}), encoding="utf-8")
        with self.assertRaises(access.AccessDenied) as caught:
            anchors.append_anchors(root=self.root, role="agent", by="她", anchors=five())
        self.assertEqual("ROOM_READONLY", caught.exception.code)
        self.assertEqual("whitelist", caught.exception.detail.get("door"))
        self.assertFalse(anchors.anchors_path(self.root).exists(), "门挡下来就一行都不许落")
        outcome = anchors.append_anchors(root=self.root, role="organ", by="程序", anchors=five())
        self.assertEqual(5, outcome["count"], "organ 不过门二：她今天不能写，不等于这间放不了东西")

    def test_appending_never_rewrites_existing_lines(self) -> None:
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five()[:2])
        first = anchors.anchors_path(self.root).read_text(encoding="utf-8")
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five()[2:])
        second = anchors.anchors_path(self.root).read_text(encoding="utf-8")
        self.assertTrue(second.startswith(first), "只追加：后面那次写不许动前面一个字节")

    def test_no_new_room_is_created(self) -> None:
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        self.assertEqual(set(rooms.ROOM_NAMES), {p.name for p in self.root.iterdir() if p.is_dir()},
                         "台词卡不开新房间，房名表一字不改")
        self.assertTrue((self.root / "仓库" / "anchors.jsonl").exists())

    def test_count_only_answers_for_warehouse(self) -> None:
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        self.assertEqual(5, anchors.count(self.root, "仓库"))
        self.assertEqual(0, anchors.count(self.root, "工作台"), "别间不答，也不建空文件")

    def test_missing_file_is_not_an_error(self) -> None:
        got = anchors.read_all(root=self.root)
        self.assertFalse(got["file_exists"])
        self.assertEqual(0, got["lines"])
        self.assertEqual(0, got["total"])

    def test_committed_anchors_match_the_five(self) -> None:
        """真 data/ 里那份（入库的）若在，必须就是这五句——防有人手改跑偏。"""
        path = PACKAGE / "data" / "仓库" / "anchors.jsonl"
        if not path.exists():
            self.skipTest("真 data/ 里还没有台词库（干净检出时不入库之外的机器上才会没有）")
        got = anchors.read_all(PACKAGE / "data")
        by_scene = {card["scene"]: card for card in got["anchors"]}
        self.assertEqual({card["scene"] for card in FIVE}, set(by_scene))
        for want in FIVE:
            self.assertEqual(want["say"], by_scene[want["scene"]]["say"])

    def test_verbatim_is_not_copied_elsewhere(self) -> None:
        """「不复制原文到别处」：代码、模板、文档里都不许出现这五句，只许留在台词库。"""
        needles = [card["say"] for card in FIVE]
        targets = [p for p in PACKAGE.glob("*.py") if p.name != "anchors.py"]
        targets += list((PACKAGE / "templates").glob("*")) if (PACKAGE / "templates").is_dir() else []
        targets += list(PLUGIN.glob("api/*.py"))
        pages = PLUGIN / "pages"
        targets += list(pages.glob("*.html")) if pages.is_dir() else []
        docs = PLUGIN.parent / "docs"
        targets += list(docs.rglob("*.md")) if docs.is_dir() else []
        for path in targets:
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in needles:
                self.assertNotIn(needle, text, "台词原文被复制到了 %s" % path)


if __name__ == "__main__":
    unittest.main()
