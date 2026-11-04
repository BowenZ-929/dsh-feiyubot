"""怕什么：identity.fears 必填、≤60 字硬线、进渲染、进合计预算。"""
from __future__ import annotations

import hashlib
import tempfile
import unittest

from organs.soul import checks, model, render
from organs.soul import store as store_module
from organs.soul.store import load
from organs.soul.tests.helpers import (copy_fixture, edit_card, read_bytes, read_json,
                                       write_json)

# 旧形状：没有 fears。migrate 是唯一能把这张卡换成新形状的路。
OLD_IDENTITY = {
    "self_names": {"display": "【待定稿】", "synonyms": []},
    "appearance": "【待定稿】",
    "self_statements": ["【待定稿】", "【待定稿】", "【待定稿】"],
}
# 用户定稿，逐字写入：心里很怕、几乎不露（六轴 inner 78／shown 15），所以写的是她绕着怕走。
SETTLED_FEARS = '她怕被丢下，但不许把这句话直接说出口'


def codes(issues) -> list:
    return [issue.code for issue in issues]


def digest(path) -> str:
    return "sha256:" + hashlib.sha256(read_bytes(path)).hexdigest()


class FearsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)

    def _rejected(self, mutate) -> list:
        edit_card(self.root, "identity", mutate)
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        return codes(caught.exception.issues)

    def test_missing_fears_is_rejected(self):
        """缺字段不许补默认值：怕什么是必填件，缺了就是坏卡。"""
        found = self._rejected(lambda data: data.pop("fears"))
        self.assertIn("IDENTITY_FEARS_MISSING", found)
        self.assertNotIn("SOUL_JSON_UNREADABLE", found)   # 缺字段不连带报"六件之外"

    def test_blank_fears_is_rejected(self):
        self.assertIn("IDENTITY_FEARS_MISSING",
                      self._rejected(lambda data: data.update({"fears": "   "})))

    def test_sixty_one_chars_is_rejected(self):
        """≤60 是硬线（与形象同口径）：超一个字就 error 拒载，不截断、不 WARN。"""
        found = self._rejected(lambda data: data.update({"fears": "怕" * 61}))
        self.assertIn("IDENTITY_FEARS_TOO_LONG", found)

    def test_sixty_chars_passes(self):
        edit_card(self.root, "identity", lambda data: data.update({"fears": "怕" * 60}))
        self.assertEqual("怕" * 60, load(self.root).snapshot().identity.fears)

    def test_render_puts_fears_between_character_and_stance(self):
        edit_card(self.root, "identity", lambda data: data.update({"fears": "怕失去"}))
        lines = load(self.root).render("熟人").splitlines()
        heads = [line.split("】")[0] + "】" for line in lines]
        self.assertEqual("【怕什么】怕失去", lines[3])
        self.assertEqual(["【自称】", "【形象】", "【性格底色】", "【怕什么】", "【待人姿态】",
                          "【红线】"], heads)

    def test_fears_counts_toward_the_total_budget(self):
        edit_card(self.root, "identity", lambda data: data.update({"fears": "怕" * 60}))
        snapshot = load(self.root).snapshot()
        expected = (model.identity_name_chars(snapshot.identity)
                    + model.identity_statement_chars(snapshot.identity)
                    + model.char_count(snapshot.identity.appearance)
                    + 60 + model.manner_chars(snapshot.manner)
                    + model.bounds_chars(snapshot.bounds))
        self.assertEqual(expected, model.body_chars(snapshot.identity, snapshot.manner,
                                                    snapshot.bounds))
        self.assertEqual(expected, render.budget_used(snapshot))
        self.assertIn("六件·怕什么 60/60 字（硬线）", "\n".join(checks.summary(self.root)))

    def test_migrate_adds_the_field_and_check_passes(self):
        """形状变化只有 migrate 一条路：换完 check 无 error，渲染里有了这一行。"""
        write_json(self.root / "identity.json", OLD_IDENTITY)
        with self.assertRaises(model.SoulLoadError):
            load(self.root)
        new_value = dict(OLD_IDENTITY, fears=SETTLED_FEARS)
        result = store_module.migrate(self.root, actor="bowen", reason="补 identity.fears",
                                      block="identity", new_value=new_value,
                                      expect_old_digest=digest(self.root / "identity.json"))
        self.assertTrue(result["ok"], result)
        self.assertEqual("migrate", store_module.read_history(
            self.root / "history.jsonl")[-1]["kind"])
        self.assertEqual([], [item for item in checks.collect(self.root)
                              if item.level == model.ERROR])
        self.assertEqual(SETTLED_FEARS, read_json(self.root / "identity.json")["fears"])
        self.assertIn("【怕什么】" + SETTLED_FEARS, load(self.root).render("熟人"))
        self.assertIn("六件·怕什么 %d/60 字（硬线）" % model.char_count(SETTLED_FEARS),
                      "\n".join(checks.summary(self.root)))


class RealCardFearsTest(unittest.TestCase):
    """真卡里的怕什么是用户给的定稿：逐字写入，不改写、不加料。"""

    def test_real_identity_carries_the_settled_fears(self):
        from pathlib import Path
        data = read_json(Path(__file__).resolve().parents[1] / "data" / "identity.json")
        self.assertEqual(SETTLED_FEARS, data["fears"])
        self.assertLessEqual(model.char_count(data["fears"]), model.limits.fears)


if __name__ == "__main__":
    unittest.main()
