"""改完下一回合生效、不重启：读方每次取快照都要拿到最新值。"""
from __future__ import annotations

import tempfile
import unittest

from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture, read_json, write_json


class HotReloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)

    def test_next_turn_store_reads_the_new_tone(self):
        first = load(self.root)
        result = first.edit(actor="bowen", reason="面板上改一句待人倾向", block="manner",
                            path="stance", value="说话松一点，别绷着")
        self.assertTrue(result["ok"], result)
        next_turn = load(self.root)          # 模拟下一回合：新建一个 store，不重启
        self.assertEqual("说话松一点，别绷着", next_turn.snapshot().manner.stance)
        self.assertIn("说话松一点，别绷着", next_turn.render("熟人"))

    def test_same_store_reloads_after_edit(self):
        store = load(self.root)
        old = store.snapshot().manner.stance
        store.edit(actor="bowen", reason="改基调", block="manner", path="stance", value="新基调")
        self.assertNotEqual(old, store.snapshot().manner.stance)
        self.assertEqual("新基调", store.snapshot().manner.stance)

    def test_manual_file_change_is_seen_without_restart(self):
        store = load(self.root)
        self.assertEqual("【待定稿】", store.snapshot().manner.stance)
        data = read_json(self.root / "manner.json")
        data["stance"] = "手改的基调"
        write_json(self.root / "manner.json", data)   # 人手在盘上改，不是程序写路径
        self.assertEqual("手改的基调", store.snapshot().manner.stance)

    def test_digest_follows_the_reload(self):
        store = load(self.root)
        before = store.snapshot().digest
        store.edit(actor="bowen", reason="改基调", block="manner", path="stance", value="新基调")
        self.assertNotEqual(before, store.snapshot().digest)


if __name__ == "__main__":
    unittest.main()

