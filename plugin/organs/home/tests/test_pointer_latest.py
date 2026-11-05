"""指针卡只追加、读时取最新：fold 不许把后追加的那一行并成「又被提到一次」。

病灶（2026-11-05）：实盘 `仓库/index.jsonl` 里 policy.json 有两行（旧 1022 字节、新 1687 字节），
fold 按 `(房, 路径, 名字)` 判同件——两条 sha 不同但路径相同——于是把新行当成 mentions 并进旧行，
「现算」永远拿旧 sha 去比，`/home/warehouse` 恒 409。文件明明改对了，卡也没错，是读法错了。

口径与台词卡（`anchors`）一致：**按追加顺序取最新，不比 ts**——ts 是人给的，可错、可重复，
拿它当权威等于让写错的时间戳盖掉后写的那一行。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.home import cards, residents, rooms, store

INDEX = cards.INDEX_NAME
POINTER = {"pointer": True, "aligned": "名字不动、.json 后缀不动、顶层仍是一份键值表"}


def make_home(base: Path) -> Path:
    root = base / "home"
    for name in rooms.ROOM_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def pointer_card(card_id: str, target: Path, sha: str, size: int) -> dict:
    return {"card_id": card_id, "room": "仓库", "kind": "借阅条", "name": target.name,
            "path": str(target), "sha256": sha, "bytes": size, "chars": 0,
            "note": "指针卡", "by": "home", "role": "organ", "at": "2026-11-05T00:00:00+00:00",
            "params": dict(POINTER)}


class FoldTakesLatestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = make_home(self.base)
        self.target = self.base / "policy.json"
        self.target.write_text('{"v": 2, "更长一点": true}', encoding="utf-8")
        self.new_sha = store.sha256_file(self.target)
        self.new_size = self.target.stat().st_size
        self.old_sha = "144a543f00000000" + "0" * 48
        self.old_size = 1022

    def _write_index(self, rows) -> None:
        path = self.root / "仓库" / INDEX
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")

    def test_fold_takes_the_later_row_for_pointer_cards(self) -> None:
        """纯函数：同 (房,路径,名字) 两行 sha 不同 → 折出来的是后那行。"""
        rows = [pointer_card("仓库-1", self.target, self.old_sha, self.old_size),
                pointer_card("仓库-2", self.target, self.new_sha, self.new_size)]
        folded = cards.fold(rows)
        self.assertEqual(1, len(folded), "同一份东西仍是一件")
        self.assertEqual("仓库-2", folded[0]["card_id"], "取追加顺序最后那一行")
        self.assertEqual(self.new_sha, folded[0]["sha256"])
        self.assertEqual(1, folded[0]["mentions"], "被覆盖的那一次仍算「又登记过一次」")

    def test_warehouse_aligns_on_the_latest_pointer(self) -> None:
        """回归主条：盘上文件已是新的，卡里有旧行也有新行 → 必须 aligned，不许报 stale。

        这条在修前是红的：fold 把新行并进旧行，现算的新 sha 跟旧卡比 → POINTER_STALE。
        """
        self._write_index([pointer_card("仓库-1", self.target, self.old_sha, self.old_size),
                           pointer_card("仓库-2", self.target, self.new_sha, self.new_size)])
        got = residents.warehouse_pointers(self.root)
        self.assertEqual([], got["stale"], "有旧行不等于对不上：读最新的那行")
        self.assertTrue(got["aligned"])
        entry = got["pointers"][0]
        self.assertEqual(self.new_sha, entry["sha256"], "读回的指纹必须是新那行")
        self.assertEqual("ok", entry["code"])

    def test_stale_message_carries_both_sha_prefixes(self) -> None:
        """真对不上时，消息里要有两个 sha 的前 12 位——别再让人为一句「对不上」多跑一轮。"""
        self._write_index([pointer_card("仓库-1", self.target, self.old_sha, self.old_size)])
        got = residents.warehouse_pointers(self.root)
        entry = got["pointers"][0]
        self.assertEqual("POINTER_STALE", entry["code"])
        self.assertEqual(self.new_sha, entry["actual_sha256"])
        self.assertEqual(self.old_sha, entry["card_sha256"])
        note = entry["note"]
        self.assertIn(self.new_sha[:12], note, "消息里要出现现算的 12 位")
        self.assertIn(self.old_sha[:12], note, "消息里要出现卡上的 12 位")
        self.assertIn(str(self.new_size), note)
        self.assertIn(str(self.old_size), note)

    def test_non_pointer_cards_still_just_count_mentions(self) -> None:
        """别把这条规矩漏到物件卡上：重存一次仍是「又被提到一次」，新行不许顶掉旧卡。"""
        shared = {"room": "工作台", "kind": "物件", "name": "一张画.md",
                  "path": str(self.target), "sha256": self.new_sha, "params": {}}
        rows = [dict(shared, card_id="工作台-1", note="第一次"),
                dict(shared, card_id="工作台-2", note="又放一次")]
        folded = cards.fold(rows)
        self.assertEqual(1, len(folded))
        self.assertEqual("工作台-1", folded[0]["card_id"], "物件卡：旧卡在，不新建")
        self.assertEqual("第一次", folded[0]["note"])
        self.assertEqual(1, folded[0]["mentions"])


if __name__ == "__main__":
    unittest.main()
