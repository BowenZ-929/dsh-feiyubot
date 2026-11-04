"""索引卡：一行一张只追加、重存只加 mentions、读时重新验盘、整批超限不部分落。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from organs.home import access, audit, cards, rooms

ORGAN_DATA = Path(__file__).resolve().parents[1] / "data"
INDEX = "index.jsonl"


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CardsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "home"
        self.root.mkdir()
        # 临时家也要有一份白名单：agent 那一支不看默认值，缺件就拒（access 的判据）。
        (self.root / "access.json").write_text(
            (ORGAN_DATA / "access.json").read_text(encoding="utf-8"), encoding="utf-8")

    def index_path(self, room: str) -> Path:
        return self.root / room / INDEX

    def rows(self, room: str) -> list:
        path = self.index_path(room)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def put_room_file(self, room: str, name: str, text: str):
        path = self.root / room / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        path.write_bytes(data)
        return path, sha_of(data), len(data)

    def test_one_card_lands_one_line_audits_one_line_and_reads_back(self):
        """放一张卡：index.jsonl 多一行、审计多一行、list_room 读得到。"""
        outcome = cards.append_cards(root=self.root, role="agent", by="眼", cards=[
            {"room": "工作台", "kind": "物件", "name": "扫地的清单.md",
             "note": "眼扫到的一张纸", "chars": 8}])
        self.assertEqual(len(outcome["cards"]), 1)
        self.assertEqual(outcome["cards"][0]["card_id"], "工作台-1")
        self.assertEqual(len(self.rows("工作台")), 1)

        tail = audit.read_tail(root=self.root, limit=10)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0].room, "工作台")
        self.assertEqual(tail[0].seq, 1)
        self.assertTrue(tail[0].ok)
        self.assertTrue(audit.verify(root=self.root)["ok"])

        listed = cards.list_room(root=self.root, room="工作台")
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["name"], "扫地的清单.md")
        self.assertEqual(listed["items"][0]["mentions"], 0)
        self.assertFalse(listed["truncated"])

    def test_second_time_same_thing_only_bumps_mentions(self):
        """同一件东西存第二次：卡数不变、mentions 从 0 变 1（fold 的证据）。"""
        card = {"room": "工作台", "kind": "物件", "name": "a.md", "path": "/外面/a.md",
                "sha256": "a" * 64, "bytes": 12, "chars": 5}
        cards.append_cards(root=self.root, role="agent", by="agent", cards=[card])
        before = cards.list_room(root=self.root, room="工作台")
        self.assertEqual(before["items"][0]["mentions"], 0)

        cards.append_cards(root=self.root, role="agent", by="agent", cards=[dict(card)])
        after = cards.list_room(root=self.root, room="工作台")
        self.assertEqual(after["total"], 1)                     # 卡数不变
        self.assertEqual(after["items"][0]["mentions"], 1)       # 只加一句「又被提到一次」
        self.assertEqual(len(self.rows("工作台")), 2)            # 流水仍然只追加

    def test_explicit_mention_row_does_not_create_a_card(self):
        """显式 kind="提到" 行指向旧 card_id：只加 mentions，不新建卡。"""
        cards.append_cards(root=self.root, role="agent", by="agent", cards=[
            {"room": "工作台", "kind": "物件", "name": "b.md", "sha256": "b" * 64,
             "bytes": 3, "chars": 3}])
        cards.append_cards(root=self.root, role="agent", by="agent", cards=[
            {"room": "工作台", "kind": "提到", "name": "又被提到一次",
             "params": {"ref": "工作台-1"}}])
        listed = cards.list_room(root=self.root, room="工作台")
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["mentions"], 1)

    def test_read_item_verifies_disk_and_catches_tampered_sha256(self):
        """读时再验一遍：卡上的 sha256 被改掉 → CARD_DISK_MISMATCH，消息说「卡与盘对不上」。"""
        path, digest, size = self.put_room_file("工作台", "她写的一张纸.md", "第一行\n第二行\n")
        cards.append_cards(root=self.root, role="agent", by="agent", cards=[
            {"room": "工作台", "kind": "物件", "name": "她写的一张纸.md", "path": str(path),
             "sha256": digest, "bytes": size, "chars": size, "note": "她自己写的"}])
        got = cards.read_item(root=self.root, room="工作台", name="她写的一张纸.md")
        self.assertIn("第二行", got["body"])
        self.assertFalse(got["external"])
        self.assertTrue(got["verified"])

        rows = self.rows("工作台")
        rows[0]["sha256"] = "0" * 64
        self.index_path("工作台").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8")
        with self.assertRaises(cards.CardError) as caught:
            cards.read_item(root=self.root, room="工作台", name="她写的一张纸.md")
        self.assertEqual(caught.exception.code, "CARD_DISK_MISMATCH")
        self.assertIn("卡与盘对不上", str(caught.exception))

    def test_read_item_rejects_symlink(self):
        """符号链接不认：指出去的路径读回来也是 CARD_DISK_MISMATCH。"""
        real = Path(self._tmp.name) / "real.md"
        real.write_text("真的正文\n", encoding="utf-8")
        data = real.read_bytes()
        link = self.root / "工作台" / "链.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(real), str(link))
        cards.append_cards(root=self.root, role="agent", by="agent", cards=[
            {"room": "工作台", "kind": "物件", "name": "链.md", "path": str(link),
             "sha256": sha_of(data), "bytes": len(data), "chars": 5}])
        with self.assertRaises(cards.CardError) as caught:
            cards.read_item(root=self.root, room="工作台", name="链.md")
        self.assertEqual(caught.exception.code, "CARD_DISK_MISMATCH")

    def test_external_original_returns_null_body(self):
        """记的是外面那张图：如实回 body=None + external=True，不编内容。"""
        original = Path(self._tmp.name) / "她.png"
        data = b"\x89PNG\r\n\x1a\n" + "假的".encode("utf-8")
        original.write_bytes(data)
        cards.append_cards(root=self.root, role="agent", by="眼", cards=[
            {"room": "仓库", "kind": "素材", "name": "她.png", "path": str(original),
             "sha256": sha_of(data), "bytes": len(data), "chars": 4, "note": "参考图"}])
        got = cards.read_item(root=self.root, room="仓库", name="她.png")
        self.assertIsNone(got["body"])
        self.assertTrue(got["external"])
        self.assertTrue(got["verified"])

    def test_card_without_path_is_a_pure_note(self):
        """纯说明卡（path 为空）：body=None、external=True，不落任何本体。"""
        cards.append_cards(root=self.root, role="agent", by="嘴", cards=[
            {"room": "工作台", "kind": "痕迹", "name": "今天没什么", "chars": 5,
             "note": "她今天只写了这一句"}])
        got = cards.read_item(root=self.root, room="工作台", name="今天没什么")
        self.assertIsNone(got["body"])
        self.assertTrue(got["external"])
        self.assertFalse(got["verified"])

    def test_batch_over_limit_is_rejected_whole(self):
        """一次 21 张：整批拒（FILES_OVER），盘上一行都不落。"""
        batch = [{"room": "工作台", "kind": "物件", "name": "第%d张.md" % index, "chars": 3}
                 for index in range(access.MAX_FILES_PER_WRITE + 1)]
        with self.assertRaises(access.AccessDenied) as caught:
            cards.append_cards(root=self.root, role="agent", by="agent", cards=batch)
        self.assertEqual(caught.exception.code, "FILES_OVER")
        self.assertFalse(self.index_path("工作台").exists())
        self.assertEqual(audit.read_tail(root=self.root, limit=10), [])

    def test_one_bad_name_rejects_the_whole_batch(self):
        """批里第二张名字带 ..：先全检后落，所以第一张也不许落。"""
        batch = [{"room": "工作台", "kind": "物件", "name": "好的.md", "chars": 3},
                 {"room": "工作台", "kind": "物件", "name": "../跑出去.md", "chars": 3},
                 {"room": "工作台", "kind": "物件", "name": "也好的.md", "chars": 3}]
        with self.assertRaises(access.AccessDenied) as caught:
            cards.append_cards(root=self.root, role="agent", by="agent", cards=batch)
        self.assertEqual(caught.exception.code, "PATH_TRAVERSAL")
        self.assertFalse(self.index_path("工作台").exists())

    def test_unknown_kind_and_unknown_room_are_refused(self):
        """kind 只七种；房名不在十间时回执带最接近的一间。"""
        with self.assertRaises(cards.CardError) as caught:
            cards.append_cards(root=self.root, role="agent", by="agent", cards=[
                {"room": "工作台", "kind": "心情", "name": "现在很烦", "chars": 4}])
        self.assertEqual(caught.exception.code, "BAD_KIND")

        with self.assertRaises(cards.CardError) as caught:
            cards.append_cards(root=self.root, role="agent", by="agent", cards=[
                {"room": "车棚", "kind": "物件", "name": "自行车", "chars": 3}])
        self.assertEqual(caught.exception.code, "ROOM_UNKNOWN")
        self.assertTrue(caught.exception.detail.get("nearest"))

    def test_list_room_pages_and_reports_truncation(self):
        """分页：limit 掐住就 truncated=True，total 仍然是全量。"""
        batch = [{"room": "工作台", "kind": "物件", "name": "第%d件.md" % index,
                  "sha256": ("%d" % index) * 64, "chars": 3} for index in range(5)]
        cards.append_cards(root=self.root, role="agent", by="agent", cards=batch)
        page = cards.list_room(root=self.root, room="工作台", offset=1, limit=2)
        self.assertEqual(page["total"], 5)
        self.assertEqual(page["lines"], 2)
        self.assertTrue(page["truncated"])
        self.assertEqual([item["name"] for item in page["items"]], ["第1件.md", "第2件.md"])

    def test_slug_and_chinese_name_both_work(self):
        """房名与 slug 都认：同一间房不会因为写法不同变成两间。"""
        cards.append_cards(root=self.root, role="agent", by="agent", cards=[
            {"room": "desk", "kind": "物件", "name": "x.md", "chars": 3}])
        listed = cards.list_room(root=self.root, room="工作台")
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["slug"], rooms.find("工作台").slug)


if __name__ == "__main__":
    unittest.main()
