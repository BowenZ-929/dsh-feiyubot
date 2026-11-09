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
# likes 在里面：这张卡考的是 fears 那次形状变化，不该顺带考别的件。
OLD_IDENTITY = {
    "self_names": {"display": "【待定稿】", "synonyms": []},
    "appearance": "【待定稿】",
    "self_statements": ["【待定稿】", "【待定稿】", "【待定稿】"],
    "likes": "【待定稿】",
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

    def test_missing_fears_degrades_with_the_placeholder(self):
        """缺这一格 → 降级载入：值换成占位，报 error 级但非阻塞的 Issue（码名不变）。"""
        edit_card(self.root, "identity", lambda data: data.pop("fears"))
        store = load(self.root)                       # 不抛：缺这一格不拦载入
        self.assertEqual(model.PLACEHOLDER, store.snapshot().identity.fears)
        found = [item for item in store.check() if item.code == "IDENTITY_FEARS_MISSING"]
        self.assertEqual(1, len(found), codes(store.check()))
        self.assertEqual(model.ERROR, found[0].level)
        self.assertFalse(found[0].blocking)
        self.assertNotIn("SOUL_JSON_UNREADABLE", codes(store.check()))   # 不连带报"七件之外"
        self.assertIn("【怕什么】" + model.PLACEHOLDER, store.render("熟人"))
        self.assertIn("IDENTITY_FEARS_MISSING", "\n".join(checks.summary(self.root)))

    def test_blank_fears_degrades_with_the_placeholder(self):
        edit_card(self.root, "identity", lambda data: data.update({"fears": "   "}))
        found = [item for item in store_module.read_blocks(self.root)[1]
                 if item.code == "IDENTITY_FEARS_MISSING"]
        self.assertEqual(1, len(found))
        self.assertFalse(found[0].blocking)
        self.assertEqual(model.PLACEHOLDER, load(self.root).snapshot().identity.fears)

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
        self.assertEqual(["【自称】", "【形象】", "【性格底色】", "【怕什么】", "【喜欢什么】",
                          "【待人姿态】", "【红线】"], heads)

    def test_fears_counts_toward_the_total_budget(self):
        edit_card(self.root, "identity", lambda data: data.update({"fears": "怕" * 60}))
        snapshot = load(self.root).snapshot()
        expected = (model.identity_name_chars(snapshot.identity)
                    + model.identity_statement_chars(snapshot.identity)
                    + model.char_count(snapshot.identity.appearance)
                    + 60 + model.likes_chars(snapshot.identity)
                    + model.manner_chars(snapshot.manner)
                    + model.bounds_chars(snapshot.bounds))
        self.assertEqual(expected, model.body_chars(snapshot.identity, snapshot.manner,
                                                    snapshot.bounds))
        self.assertEqual(expected, render.budget_used(snapshot))
        self.assertIn("七件·怕什么 60/60 字（硬线）", "\n".join(checks.summary(self.root)))

    def test_migrate_adds_the_field_and_check_passes(self):
        """形状变化只有 migrate 一条路：换完 check 无 error，渲染里有了这一行。"""
        write_json(self.root / "identity.json", OLD_IDENTITY)
        # 旧形状在新 model 下是「缺这一格」：降级载入、报非阻塞 error，migrate 仍是换形状的路
        self.assertEqual(model.PLACEHOLDER, load(self.root).snapshot().identity.fears)
        new_value = dict(OLD_IDENTITY, fears=SETTLED_FEARS)
        result = store_module.migrate(self.root, actor="bowen", reason="补 identity.fears",
                                      block="identity", new_value=new_value,
                                      expect_old_digest=digest(self.root / "identity.json"))
        self.assertTrue(result["ok"], result)
        self.assertEqual("migrate", store_module.read_history(
            self.root / "history.jsonl")[-1]["kind"])
        self.assertEqual([], [item for item in checks.collect(self.root, cross_check=False)
                              if item.level == model.ERROR])
        self.assertEqual(SETTLED_FEARS, read_json(self.root / "identity.json")["fears"])
        self.assertIn("【怕什么】" + SETTLED_FEARS, load(self.root).render("熟人"))
        self.assertIn("七件·怕什么 %d/60 字（硬线）" % model.char_count(SETTLED_FEARS),
                      "\n".join(checks.summary(self.root)))


class RealCardFearsTest(unittest.TestCase):
    """真卡里的怕什么是用户给的定稿：逐字写入，不改写、不加料。"""

    def test_real_identity_leaves_fears_to_experience(self):
        from pathlib import Path
        data = read_json(Path(__file__).resolve().parents[1] / "data" / "identity.json")
        self.assertEqual("随经历形成，不预设怕什么", data["fears"])
        self.assertLessEqual(model.char_count(data["fears"]), model.limits.fears)


if __name__ == "__main__":
    unittest.main()
