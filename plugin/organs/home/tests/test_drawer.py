"""抽屉：她自己写的日记与梦，外加人已放进来的自画像与人设卡。

判据（2026-11-06 人裁）：抽屉仍是结构只读房，只加一条**按 kind 的例外**——agent 可以往里写
日记与梦；物件照样进不来。四类卡都活在抽屉的**格子**里（一间房里的分格，不是新房间），
读路径把房级索引与各格子合起来折叠。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from api import api
from organs.home import access, cards, rooms, store

# 房名单不抄进测试：这里只盯「格子不开新房间」这条不变量，房表本身有它自己的护栏。
LONG = "今天" + "把抽屉里的东西一件一件摆回去，" * 6 + "摆完就坐着。"   # 98字，未超过150字上限
DREAM_TEXT = ("梦见自己变成一条很小的鱼，在很浅的水里游。水清得能看见底下每颗石子。"
               "有人蹲在岸上看着我，我想问他为什么不走，可是鱼不会说话。" + "醒来还记得水是凉的。" * 2)


def make_home(base: Path, writable=("玄关", "名片盒", "工作台", "仓库", "信箱")) -> Path:
    root = base / "home"
    for name in rooms.ROOM_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "access.json").write_text(
        json.dumps({"version": 1, "writable": list(writable)}), encoding="utf-8")
    return root


def diary(day: str, text: str = LONG, **extra) -> dict:
    params = {"date": day, "text": text, "mood": "平静", "from_memories": []}
    params.update(extra)
    return {"room": "抽屉", "kind": "日记", "name": day, "chars": len(text), "params": params}


def dream(day: str, text: str = DREAM_TEXT, dream_of: str = "前一天") -> dict:
    return {"room": "抽屉", "kind": "梦", "name": day, "chars": len(text),
            "params": {"date": day, "text": text, "mood": "迷糊", "dream_of": dream_of,
                       "from_memories": []}}


class DrawerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_home(Path(self._tmp.name))

    def _write(self, *cards_in, role="agent", by="她"):
        return cards.append_cards(root=self.root, role=role, by=by, cards=list(cards_in))

    def test_short_diary_and_dream_keep_complete_text(self):
        text='今天看到橘猫坐在纸箱旁。'
        self._write(diary('2026-11-04',text),dream('2026-11-04','梦里还是那个纸箱。'))
        listing=cards.list_room(self.root,room='抽屉')['items']
        for row in listing:
            read=cards.read_item(self.root,room='抽屉',name=row['card_id'])
            self.assertEqual(row['params']['text'],read['body'])
        self.assertEqual(2,len(listing))

    def test_diary_and_dream_read_back_verbatim(self) -> None:
        self._write(diary("2026-11-04"), dream("2026-11-04"))
        got = cards.list_room(self.root, room="抽屉")
        by_kind = {card["kind"]: card for card in got["items"]}
        # 列表里正文是摘要（text_truncated），全文从 read_item 取——枚举才翻得完。
        # 列表带 text_chars（真实长度）；超过 LIST_EXCERPT_CHARS 才打 text_truncated。
        self.assertEqual(len(LONG), by_kind["日记"]["params"]["text_chars"])
        self.assertLessEqual(len(by_kind["日记"]["params"]["text"]), max(120, len(LONG)))
        # 按 card_id 取：日记与梦同一天时 name 会撞，id 才唯一。
        diary_full = cards.read_item(self.root, room="抽屉",
                                     name=by_kind["日记"]["card_id"])
        self.assertEqual(LONG, diary_full["body"], "日记全文逐字")
        dream_full = cards.read_item(self.root, room="抽屉",
                                     name=by_kind["梦"]["card_id"])
        self.assertEqual(DREAM_TEXT, dream_full["body"], "梦全文逐字")
        self.assertEqual("梦", by_kind["梦"]["params"]["source_type"])
        self.assertFalse(by_kind["梦"]["testimony"], "梦不作证词")
        self.assertTrue(by_kind["梦"]["display"].startswith("（梦）"), "梦要带显示前缀")

    def test_diary_length_is_enforced_with_the_count(self) -> None:
        with self.assertRaises(cards.CardError) as caught:
            self._write(diary("2026-11-04", "长" * 151))
        self.assertEqual("KIND_TEXT_LENGTH", caught.exception.code)
        self.assertIn("151", caught.exception.message)
        self.assertEqual([], cards.list_room(self.root, room="抽屉")["items"])

    def test_empty_or_whitespace_diary_is_rejected(self) -> None:
        for text in ("", " \n\t"):
            with self.subTest(text=text), self.assertRaises(cards.CardError) as caught:
                self._write(diary("2026-11-04", text))
            self.assertEqual("KIND_FIELD_MISSING", caught.exception.code)

    def test_one_entry_per_day_is_idempotent(self) -> None:
        self._write(diary("2026-11-04"))
        with self.assertRaises(cards.CardError) as caught:
            self._write(diary("2026-11-04"))
        self.assertEqual("DATE_ALREADY_WRITTEN", caught.exception.code)
        self.assertIn("今天已经写过了", caught.exception.message)
        self.assertEqual(1, cards.list_room(self.root, room="抽屉")["total"], "拒了就不许多一行")

    def test_only_she_can_write_the_drawer_and_only_these_kinds(self) -> None:
        with self.assertRaises(access.AccessDenied) as caught:
            self._write(diary("2026-11-04"), role="organ", by="程序")
        self.assertEqual("ROOM_READONLY", caught.exception.code)
        self.assertEqual("structure", caught.exception.detail.get("door"))
        with self.assertRaises(access.AccessDenied):
            self._write({"room": "抽屉", "kind": "物件", "name": "杂物.md"})
        with self.assertRaises(access.AccessDenied):
            self._write({"room": "抽屉", "kind": "日记", "name": "x",
                         "params": {"date": "2026-11-04", "text": LONG, "mood": "平静"}},
                        role="human", by="人")
        with self.assertRaises(access.AccessDenied):
            self._write({"room": "抽屉", "kind": "素材", "name": "画风"})

    def test_diary_writes_only_into_the_drawer(self) -> None:
        card = diary("2026-11-04")
        card["room"] = "工作台"
        with self.assertRaises(cards.CardError) as caught:
            self._write(card)
        self.assertEqual("KIND_WRONG_ROOM", caught.exception.code)

    def test_portrait_registers_a_pointer_and_checks_the_fingerprint(self) -> None:
        image = Path(self._tmp.name) / "样子.jpg"
        image.write_bytes(b"fake-jpeg" * 128)
        digest = store.sha256_file(image)
        size = image.stat().st_size
        good = {"room": "抽屉", "kind": "自画像", "name": image.name,
                "params": {"image_path": str(image), "sha256": digest, "bytes": size,
                           "caption": "她"}}
        out = self._write(good, role="human", by="人")
        card = out["cards"][0]
        self.assertEqual(str(image), card["params"]["image_path"], "只登记指针")
        self.assertFalse((self.root / "抽屉" / "自画像" / image.name).exists(),
                         "不复制本体：抽屉里只该有 index.jsonl")
        bad = dict(good, params=dict(good["params"], sha256="0" * 64))
        with self.assertRaises(cards.CardError) as caught:
            self._write(bad, role="human", by="人")
        self.assertEqual("CARD_DISK_MISMATCH", caught.exception.code)

    def test_reference_card_says_it_does_not_go_into_the_prompt(self) -> None:
        out = self._write({"room": "抽屉", "kind": "人设卡", "name": "说明书",
                           "params": {"名称": "江迟"}}, role="human", by="人")
        self.assertIs(False, out["cards"][0]["in_prompt"], "接口上要一眼看出它不进提示词")
        got = cards.list_room(self.root, room="抽屉")
        self.assertIs(False, got["items"][0]["in_prompt"])

    def test_four_kinds_share_one_room_across_shelves(self) -> None:
        image = Path(self._tmp.name) / "样子.jpg"
        image.write_bytes(b"fake" * 64)
        # 她写的那两类与她写不了的另两类各走各的门（role 不同，不是同一批）
        self._write(diary("2026-11-04"), dream("2026-11-04"))
        self._write({"room": "抽屉", "kind": "自画像", "name": image.name,
                     "params": {"image_path": str(image), "sha256": store.sha256_file(image),
                                "bytes": image.stat().st_size}},
                    {"room": "抽屉", "kind": "人设卡", "name": "说明书",
                     "params": {"名称": "江迟"}}, role="human", by="人")
        got = cards.list_room(self.root, room="抽屉")
        kinds = {}
        for card in got["items"]:
            kinds[card["kind"]] = kinds.get(card["kind"], 0) + 1
        self.assertEqual({"日记": 1, "梦": 1, "自画像": 1, "人设卡": 1}, kinds)
        self.assertEqual({"日记", "梦", "自画像", "人设卡"}, set(got["shelves"]))

    def test_room_table_is_untouched(self) -> None:
        self.assertEqual(len(rooms.ROOMS), len(rooms.ROOM_NAMES), "房名与房表一一对应")
        self.assertIn("抽屉", rooms.READONLY_ROOMS, "抽屉仍是结构只读房")
        self._write(diary("2026-11-04"))
        self.assertEqual(set(rooms.ROOM_NAMES),
                         {path.name for path in self.root.iterdir() if path.is_dir()},
                         "格子不是新房间：房级目录就是房表那些")

    def test_patch_is_405_and_says_only_she_writes_it(self) -> None:
        status, payload = api.handle("PATCH", "/home/list", {"room": "抽屉"}, self.root)
        self.assertEqual(405, status)
        self.assertIn("日记与梦只能是她写的", payload.get("message", ""))


if __name__ == "__main__":
    unittest.main()
