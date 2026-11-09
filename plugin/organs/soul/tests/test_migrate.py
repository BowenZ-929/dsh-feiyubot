"""migrate 是形状变化的唯一入口：宽容读旧卡、新值必须过新 model、digest 不符即拒。"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from unittest import mock

from organs.soul import checks, model
from organs.soul import store as store_module
from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture, read_bytes, read_json, write_json

# 旧形状的 manner：tone + 四档矩阵。新 model 只认 stance，所以这张卡是坏卡。
OLD_MANNER = {"tone": "她对人有分寸地热络", "matrix": {"熟人": "热络一点", "生人": "收着点"}}
NEW_MANNER = {"stance": "她对人有分寸地热络，不摆架子"}


def digest(path) -> str:
    return "sha256:" + hashlib.sha256(read_bytes(path)).hexdigest()


def codes(issues) -> list:
    return [issue.code for issue in issues]


class MigrateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.manner = self.root / "manner.json"

    def _break_manner(self) -> str:
        """把 manner 换成旧形状：新 model 下这就是坏卡，load() 会拒载。"""
        write_json(self.manner, OLD_MANNER)
        with self.assertRaises(model.SoulLoadError):
            load(self.root)
        return digest(self.manner)

    def _migrate(self, block: str = "manner", value=NEW_MANNER, expect: str = "",
                 **kwargs):
        params = {"actor": "bowen", "reason": "manner 从 {tone,matrix} 收成 {stance}"}
        params.update(kwargs)
        return store_module.migrate(self.root, block=block, new_value=value,
                                   expect_old_digest=expect, **params)

    def test_bad_card_migrates_and_logs_the_migrate_kind(self):
        """旧形状是坏卡：不走 load() 也能换形状，账行 kind=migrate。"""
        expect = self._break_manner()
        result = self._migrate(expect=expect)
        self.assertTrue(result["ok"], result)
        self.assertEqual(NEW_MANNER, read_json(self.manner))
        rows = store_module.read_history(self.root / "history.jsonl")
        self.assertEqual(1, len(rows))
        self.assertEqual("migrate", rows[0]["kind"])
        self.assertEqual("manner", rows[0]["block"])
        self.assertEqual("", rows[0]["path"])          # 整块换：没有块内 path
        self.assertEqual(expect, rows[0]["old_digest"])
        self.assertTrue(rows[0]["ts"])

    def test_check_passes_after_a_migration(self):
        expect = self._break_manner()
        self.assertTrue(self._migrate(expect=expect)["ok"])
        issues = checks.collect(self.root, cross_check=False)
        self.assertEqual([], [item for item in issues if item.level == model.ERROR], codes(issues))
        self.assertNotIn("HISTORY_KIND_UNKNOWN", codes(issues))
        self.assertNotIn("MANUAL_EDIT_IN_LEDGER", codes(issues))
        self.assertEqual(NEW_MANNER["stance"], load(self.root).snapshot().manner.stance)

    def test_digest_mismatch_is_rejected_and_nothing_written(self):
        before = read_bytes(self.manner)
        result = self._migrate(expect="sha256:" + "0" * 64)
        self.assertFalse(result["ok"])
        self.assertEqual("MIGRATE_DIGEST_MISMATCH", result["issues"][0]["code"])
        self.assertEqual(before, read_bytes(self.manner))
        self.assertEqual([], store_module.read_history(self.root / "history.jsonl"))

    def test_digest_prefix_is_optional(self):
        expect = digest(self.manner)
        self.assertTrue(self._migrate(expect=expect[len("sha256:"):])["ok"])

    def test_new_value_must_pass_the_new_model(self):
        expect = self._break_manner()
        before = read_bytes(self.manner)
        result = self._migrate(value=OLD_MANNER, expect=expect)
        self.assertFalse(result["ok"])
        self.assertIn("过不了新 model", result["message"])
        self.assertEqual(before, read_bytes(self.manner))
        self.assertEqual([], store_module.read_history(self.root / "history.jsonl"))

    def test_append_failure_rolls_back_and_leaves_no_row(self):
        expect = self._break_manner()
        before = read_bytes(self.manner)
        with mock.patch.object(store_module, "append_history", side_effect=OSError("磁盘满了")):
            result = self._migrate(expect=expect)
        self.assertFalse(result["ok"])
        self.assertIn("回滚", result["message"])
        self.assertEqual(before, read_bytes(self.manner))
        self.assertEqual([], store_module.read_history(self.root / "history.jsonl"))

    def test_budget_overflow_is_rejected_before_writing(self):
        expect = digest(self.manner)
        before = read_bytes(self.manner)
        result = self._migrate(value={"stance": "热" * 1200}, expect=expect)
        self.assertFalse(result["ok"])
        self.assertEqual("BUDGET_OVER", result["issues"][0]["code"])
        self.assertEqual(before, read_bytes(self.manner))
        self.assertEqual([], store_module.read_history(self.root / "history.jsonl"))

    def test_another_broken_block_stops_the_migration(self):
        """宽容只给被迁移的那一块：另外两张卡坏了，换完也载不进去，就先拒。"""
        expect = self._break_manner()
        write_json(self.root / "identity.json", {"display": "孤零零一个字段"})
        result = self._migrate(expect=expect)
        self.assertFalse(result["ok"])
        self.assertIn("先修它们", result["message"])
        self.assertEqual(OLD_MANNER, read_json(self.manner))

    def test_baseline_is_not_a_soul_block(self):
        result = self._migrate(block="baseline", value={"axes": []}, expect="whatever")
        self.assertFalse(result["ok"])
        self.assertIn("block 只能是 identity/manner/bounds", result["message"])

    def test_blank_actor_or_reason_is_rejected(self):
        expect = digest(self.manner)
        self.assertFalse(self._migrate(expect=expect, actor="  ")["ok"])
        self.assertFalse(self._migrate(expect=expect, reason="")["ok"])
        self.assertEqual([], store_module.read_history(self.root / "history.jsonl"))

    def test_result_is_json_safe(self):
        import json

        json.dumps(self._migrate(block="baseline", value=1, expect="x"), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
