"""M5 整档快照 + revision + 环形 20：写前存一份整档，回滚读它。

分工写进注释与响应：**history 解释「谁为什么改」，快照负责「回到哪一版」**。
history 只留摘要与指纹，复原不了原文；previous.json 是整份原文，但答不出「为什么」。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from organs.soul import store as store_module
from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture, read_bytes, read_json


class ArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def _archive(self):
        return json.loads((self.root / store_module.ARCHIVE_FILE).read_text(encoding="utf-8"))

    def _edit_stance(self, value: str, **kwargs):
        return self.store.edit(actor="bowen", reason="改一句待人倾向", block="manner",
                               path="stance", value=value, **kwargs)

    def test_archive_appears_before_the_first_write(self):
        self.assertFalse((self.root / store_module.ARCHIVE_FILE).exists())
        self.assertEqual(0, self.store.revision())
        self.assertTrue(self._edit_stance("改完的姿态")["ok"])
        self.assertTrue((self.root / store_module.ARCHIVE_FILE).is_file())
        archive = self._archive()
        self.assertEqual(0, archive["revision"])              # 存下的是「被覆盖的那一版」
        self.assertEqual({"identity", "manner", "bounds"}, set(archive["blocks"]))
        self.assertEqual("【待定稿】", json.loads(archive["blocks"]["manner"])["stance"])

    def test_archive_file_is_0600(self):
        self.assertTrue(self._edit_stance("改完的姿态")["ok"])
        mode = os.stat(str(self.root / store_module.ARCHIVE_FILE)).st_mode & 0o777
        self.assertEqual(0o600, mode)

    def test_revision_increments_on_every_write(self):
        self.assertEqual(0, self.store.revision())
        first = self._edit_stance("第一次")
        self.assertEqual(1, first["revision"])
        second = self._edit_stance("第二次")
        self.assertEqual(2, second["revision"])
        self.assertEqual(2, self.store.revision())
        # 账行记的是「写完之后的活版本号」：第一次 1、第二次 2（归档里存的是被覆盖的那一版）
        self.assertEqual([1, 2], [row["revision"] for row in
                                  [first["history_line"], second["history_line"]]])
        self.assertEqual(2, len(self.store.revisions()))

    def test_ring_keeps_the_last_twenty_versions(self):
        for index in range(21):
            result = self.store.edit(actor="bowen", reason="第 %d 次" % index, block="identity",
                                     path="fears", value="她怕第 %d 样东西" % index)
            self.assertTrue(result["ok"], result)
        revisions = [row["revision"] for row in self.store.revisions()]
        self.assertEqual(20, len(revisions))
        self.assertEqual(list(range(20, 0, -1)), revisions)   # 0 已被第 21 版挤掉
        self.assertEqual(21, self.store.revision())

    def test_rollback_goes_back_to_the_archived_version(self):
        before = read_bytes(self.root / "manner.json")
        self.assertTrue(self._edit_stance("改完的姿态")["ok"])
        result = self.store.rollback(actor="bowen", reason="改坏了，退回去", revision=0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(before, read_bytes(self.root / "manner.json"))
        self.assertEqual(0, result["restored_from"])
        self.assertIn("回到哪一版", result["note"])
        self.assertIn("history", result["note"])

    def test_rollback_is_logged_and_snapshotted(self):
        self.assertTrue(self._edit_stance("改完的姿态")["ok"])
        self.store.rollback(actor="bowen", reason="退回去", revision=0)
        row = self.store.history()[-1]
        self.assertEqual("rollback", row["kind"])
        self.assertEqual(0, row["target_revision"])
        self.assertEqual("bowen", row["actor"])
        self.assertEqual("退回去", row["reason"])
        # 回滚前那一版也被存下来了：所以回滚可以再回滚回去
        latest = self.store.revisions()[0]
        self.assertEqual(1, latest["revision"])
        again = self.store.rollback(actor="bowen", reason="再回滚一次", revision=1)
        self.assertTrue(again["ok"], again)
        self.assertEqual("改完的姿态", read_json(self.root / "manner.json")["stance"])

    def test_rollback_refuses_an_unknown_revision(self):
        self.assertTrue(self._edit_stance("改完的姿态")["ok"])
        result = self.store.rollback(actor="bowen", reason="乱点", revision=99)
        self.assertFalse(result["ok"])
        self.assertIn("没有 revision", result["message"])

    def test_rollback_requires_actor_and_reason(self):
        self.assertTrue(self._edit_stance("改完的姿态")["ok"])
        self.assertFalse(self.store.rollback(actor="  ", reason="x", revision=0)["ok"])
        self.assertFalse(self.store.rollback(actor="bowen", reason=" ", revision=0)["ok"])

    def test_rollback_refuses_a_version_whose_redline_breaks_the_assertion(self):
        """回滚目标本身也要过 M1：软话词那一版回不去。"""
        self.assertTrue(self._edit_stance("改完的姿态")["ok"])
        archive = self._archive()
        blocks = archive["blocks"]
        bounds = json.loads(blocks["bounds"])
        bounds["items"][0]["rule"] = "尽量避免嘲讽"
        blocks["bounds"] = json.dumps(bounds, ensure_ascii=False, indent=2)
        dirty = {"revision": 0, "ts": archive["ts"], "blocks": blocks}
        for name in (store_module.ARCHIVE_FILE, store_module.RING_FILE % 0):
            (self.root / name).write_text(json.dumps(dirty, ensure_ascii=False), encoding="utf-8")
        before = read_bytes(self.root / "bounds.json")
        result = self.store.rollback(actor="bowen", reason="回到那一版", revision=0)
        self.assertFalse(result["ok"], result)
        self.assertIn("REDLINE_SOFT_WORD", [item["code"] for item in result["issues"]])
        self.assertEqual(before, read_bytes(self.root / "bounds.json"))

    def test_revisions_endpoint_shape_is_plain_json(self):
        self._edit_stance("改完的姿态")
        rows = self.store.revisions()
        self.assertEqual({"revision", "ts", "bytes", "blocks", "file"}, set(rows[0]))
        json.dumps(rows, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
