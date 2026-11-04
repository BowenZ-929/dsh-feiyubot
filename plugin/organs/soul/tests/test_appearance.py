"""形象：identity.appearance 必填、≤60 字硬线、进渲染、进合计预算。"""
from __future__ import annotations

import hashlib
import tempfile
import unittest

from organs.soul import checks, model, render
from organs.soul import store as store_module
from organs.soul.store import load
from organs.soul.tests.helpers import (copy_fixture, edit_card, read_bytes, read_json,
                                       write_json)

# 旧形状：没有 appearance。migrate 是唯一能把这张卡换成新形状的路。
# fears 在里面：这张卡考的是 appearance 那次形状变化，不该顺带考 fears。
OLD_IDENTITY = {
    "self_names": {"display": "【待定稿】", "synonyms": []},
    "self_statements": ["【待定稿】", "【待定稿】", "【待定稿】"],
    "fears": "【待定稿】",
}
NEW_APPEARANCE = "蓝发垂腰，蓝色眼睛，耳侧一对鲸鱼鳍，身后拖着小鲸鱼尾巴；18 岁。"


def codes(issues) -> list:
    return [issue.code for issue in issues]


def digest(path) -> str:
    return "sha256:" + hashlib.sha256(read_bytes(path)).hexdigest()


class AppearanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)

    def _rejected(self, mutate) -> list:
        edit_card(self.root, "identity", mutate)
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        return codes(caught.exception.issues)

    def test_missing_appearance_is_rejected(self):
        """缺字段不许补默认值：形象是必填件，缺了就是坏卡。"""
        found = self._rejected(lambda data: data.pop("appearance"))
        self.assertIn("IDENTITY_APPEARANCE_MISSING", found)
        self.assertNotIn("SOUL_JSON_UNREADABLE", found)   # 缺字段不连带报"六件之外"

    def test_blank_appearance_is_rejected(self):
        self.assertIn("IDENTITY_APPEARANCE_MISSING",
                      self._rejected(lambda data: data.update({"appearance": "   "})))

    def test_sixty_one_chars_is_rejected(self):
        """≤60 是硬线：超一个字就 error 拒载，不截断、不 WARN。"""
        found = self._rejected(lambda data: data.update({"appearance": "蓝" * 61}))
        self.assertIn("IDENTITY_APPEARANCE_TOO_LONG", found)

    def test_sixty_chars_passes(self):
        edit_card(self.root, "identity", lambda data: data.update({"appearance": "蓝" * 60}))
        self.assertEqual("蓝" * 60, load(self.root).snapshot().identity.appearance)

    def test_render_puts_appearance_right_after_self_name(self):
        edit_card(self.root, "identity", lambda data: data.update({"appearance": "蓝发垂腰"}))
        lines = load(self.root).render("熟人").splitlines()
        self.assertEqual("【形象】蓝发垂腰", lines[1])
        self.assertEqual("【自称】", lines[0].split("】")[0] + "】")

    def test_appearance_counts_toward_the_total_budget(self):
        edit_card(self.root, "identity", lambda data: data.update({"appearance": "蓝" * 60}))
        snapshot = load(self.root).snapshot()
        expected = (model.identity_name_chars(snapshot.identity)
                    + model.identity_statement_chars(snapshot.identity)
                    + 60 + model.fears_chars(snapshot.identity)
                    + model.manner_chars(snapshot.manner)
                    + model.bounds_chars(snapshot.bounds))
        self.assertEqual(expected, model.body_chars(snapshot.identity, snapshot.manner,
                                                    snapshot.bounds))
        self.assertIn("六件·形象 60/60 字（硬线）", "\n".join(checks.summary(self.root)))

    def test_migrate_adds_the_field_and_check_passes(self):
        """形状变化只有 migrate 一条路：换完 check 无 error，渲染里有了这一行。"""
        write_json(self.root / "identity.json", OLD_IDENTITY)
        with self.assertRaises(model.SoulLoadError):
            load(self.root)
        new_value = dict(OLD_IDENTITY, appearance=NEW_APPEARANCE)
        result = store_module.migrate(self.root, actor="bowen", reason="补 identity.appearance",
                                      block="identity", new_value=new_value,
                                      expect_old_digest=digest(self.root / "identity.json"))
        self.assertTrue(result["ok"], result)
        self.assertEqual("migrate", store_module.read_history(
            self.root / "history.jsonl")[-1]["kind"])
        self.assertEqual([], [item for item in checks.collect(self.root)
                              if item.level == model.ERROR])
        self.assertEqual(NEW_APPEARANCE, read_json(self.root / "identity.json")["appearance"])
        self.assertIn("【形象】" + NEW_APPEARANCE, load(self.root).render("熟人"))


class RealCardAppearanceTest(unittest.TestCase):
    """真卡里的形象是用户给的定稿：逐字写入，瞳色原文没有就不编。"""

    def test_real_identity_carries_the_settled_appearance(self):
        from pathlib import Path
        data = read_json(Path(__file__).resolve().parents[1] / "data" / "identity.json")
        self.assertEqual(NEW_APPEARANCE, data["appearance"])
        self.assertLessEqual(model.char_count(data["appearance"]), model.limits.appearance)


if __name__ == "__main__":
    unittest.main()
