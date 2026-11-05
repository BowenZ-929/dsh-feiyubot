"""缺这一格 → 降级载入；写错了 → 仍硬拒载。

死锁那条路的回归测试：必填格缺失原本让 load() 抛、于是补它的正规入口 edit() 也进不去，
既起不来也修不了。现在「缺这一格」与「写错了」分成两条路：

- 缺这一格（字段不存在 / 空串 / 全空白）→ 值换 PLACEHOLDER、报 error 级但非阻塞的
  Issue、面板显红；人走 edit 补齐后硬门禁自动恢复。
- 写错了（类型错、超硬线、自述不是三句）→ 仍硬拒载，行为一字不变。

self_statements 特别：缺了也按「写错」硬拒——三句同样的占位会被逐字注入，把模型带偏。
"""
from __future__ import annotations

import tempfile
import unittest

from organs.soul import checks, model, render
from organs.soul import store as store_module
from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture, edit_card, read_json


def codes(issues) -> list:
    return [issue.code for issue in issues]


class DegradeTest(unittest.TestCase):
    """验收 1、2、3、5 与「blocking 的 error 仍然抛出」都在这里。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)

    def test_missing_likes_loads_render_placeholder_and_reports_non_blocking(self):
        """验收 1：删掉 identity.likes → load 成功、render 是占位、check 有一条非阻塞 error。"""
        edit_card(self.root, "identity", lambda data: data.pop("likes"))
        store = load(self.root)                                  # 不抛就是这一条的要点
        self.assertIn("【喜欢什么】" + model.PLACEHOLDER, store.render("熟人"))
        found = [item for item in store.check() if item.code == "IDENTITY_LIKES_MISSING"]
        self.assertEqual(1, len(found), codes(store.check()))
        self.assertEqual(model.ERROR, found[0].level)
        self.assertFalse(found[0].blocking)
        self.assertIn("补齐后恢复硬门禁", found[0].message)

    def test_edit_fills_the_missing_cell_and_the_issue_disappears(self):
        """验收 2：降级载入之后，edit 就是补它的正规入口——ok=True、账 +1、Issue 消失。"""
        edit_card(self.root, "identity", lambda data: data.pop("likes"))
        store = load(self.root)
        rows_before = len(store_module.read_history(self.root / "history.jsonl"))
        result = store.edit(actor="bowen", reason="补喜欢什么：人给定稿",
                            block="identity", path="likes", value="喜欢的食物：大白饭")
        self.assertTrue(result["ok"], result)
        self.assertEqual("edit", result["history_line"]["kind"])   # 补一格不是形状变化
        self.assertEqual("likes", result["history_line"]["path"])
        self.assertEqual(rows_before + 1,
                         len(store_module.read_history(self.root / "history.jsonl")))
        self.assertNotIn("IDENTITY_LIKES_MISSING", codes(store.check()))
        self.assertEqual("喜欢的食物：大白饭", read_json(self.root / "identity.json")["likes"])
        self.assertIn("【喜欢什么】喜欢的食物：大白饭", store.render("熟人"))

    def test_likes_over_the_hard_line_still_refuses_to_load(self):
        """验收 3：81 字是「写错了」，仍硬拒载（IDENTITY_LIKES_TOO_LONG，blocking）。"""
        edit_card(self.root, "identity", lambda data: data.update({"likes": "喜" * 81}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        found = [item for item in caught.exception.issues
                 if item.code == "IDENTITY_LIKES_TOO_LONG"]
        self.assertEqual(1, len(found), codes(caught.exception.issues))
        self.assertTrue(found[0].blocking)

    def test_missing_self_statements_still_refuses_to_load(self):
        """验收 4：self_statements 缺了仍按「写错」硬拒——占位三句会把模型带偏。"""
        edit_card(self.root, "identity", lambda data: data.pop("self_statements"))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        self.assertIn("self_statements",
                      "；".join(issue.message for issue in caught.exception.issues))
        self.assertTrue(all(issue.blocking for issue in caught.exception.issues
                            if issue.level == model.ERROR))

    def test_missing_fears_degrades_then_edit_restores_the_hard_gate(self):
        """验收 5：删掉 fears → 降级 + 占位 + 红；补齐后恢复。"""
        edit_card(self.root, "identity", lambda data: data.pop("fears"))
        store = load(self.root)
        self.assertEqual(model.PLACEHOLDER, store.snapshot().identity.fears)
        self.assertIn("IDENTITY_FEARS_MISSING", codes(store.check()))
        self.assertIn("【怕什么】" + model.PLACEHOLDER, store.render("熟人"))
        result = store.edit(actor="panel", reason="补怕什么", block="identity",
                            path="fears", value="她怕被丢下")
        self.assertTrue(result["ok"], result)
        self.assertEqual([], codes(store.check()))
        self.assertIn("【怕什么】她怕被丢下", store.render("熟人"))

    def test_blocking_errors_still_raise(self):
        """保留的一条：blocking 的 error 仍然抛出 SoulLoadError。"""
        edit_card(self.root, "manner", lambda data: data.update({"stance": "   "}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        self.assertTrue([issue for issue in caught.exception.issues
                         if issue.level == model.ERROR and issue.blocking])

    def test_degrade_only_covers_the_five_cells(self):
        """降级只盖这五格；其余（整块缺失、manner 空、红线写错）照旧硬拒。"""
        self.assertEqual(("display", "synonyms", "appearance", "fears", "likes"),
                         tuple(model.MISSING_CODES))
        self.assertEqual(("self_names.display", "self_names.synonyms",
                          "appearance", "fears", "likes"), model.DEGRADABLE_PATHS)

    def test_edit_does_not_create_other_missing_fields(self):
        """降级载入没把 edit 变成第二只造形状的手：五格之外的 path 仍要 structural。"""
        store = load(self.root)
        result = store.edit(actor="panel", reason="试新建", block="identity",
                            path="her_notes", value="昨晚的梦")
        self.assertFalse(result["ok"], result)
        self.assertIn("path 无法定位", result["message"])

    def test_issues_carry_blocking_across_the_http_boundary(self):
        """面板要靠这个键显红：to_dict（HTTP 层用它）必须带上 blocking。"""
        edit_card(self.root, "identity", lambda data: data.pop("likes"))
        degraded = [issue.to_dict() for issue in load(self.root).check()
                    if issue.code == "IDENTITY_LIKES_MISSING"]
        self.assertEqual([{"code": "IDENTITY_LIKES_MISSING", "level": model.ERROR,
                           "message": degraded[0]["message"], "blocking": False}], degraded)
        self.assertTrue(model.Issue("X", model.ERROR, "y").blocking)   # 默认仍然拦

    def test_summary_carries_the_issue_next_to_the_actual_chars(self):
        """摘要：这一件照报实际字数（占位的字），Issue 码挂在同一行。"""
        edit_card(self.root, "identity", lambda data: data.pop("likes"))
        text = "\n".join(checks.summary(self.root))
        self.assertIn("七件·喜欢什么 %d/80 字" % model.char_count(model.PLACEHOLDER), text)
        self.assertIn("IDENTITY_LIKES_MISSING", text)
        self.assertIn("七件合计", text)          # 合计线照出：降级卡不是「摘要不可用」

    def test_placeholder_is_one_string_rendered_verbatim(self):
        """占位串只有一处定义，render 逐字注入、不加括号修饰。"""
        self.assertEqual("（这一栏还没写）", model.PLACEHOLDER)
        edit_card(self.root, "identity", lambda data: data.update({"likes": ""}))
        store = load(self.root)
        self.assertEqual(model.PLACEHOLDER, render.likes_line(store.snapshot().identity))
        self.assertEqual(model.PLACEHOLDER, render.sections(store.snapshot())[4][1])

    def test_degraded_display_and_synonyms_do_not_double_the_placeholder(self):
        """名字与别名都缺时【自称】只出一次占位，不是「（…）（（…））」两层。"""
        def strip_both_names(data):
            data["self_names"].pop("display")
            data["self_names"].pop("synonyms")

        edit_card(self.root, "identity", strip_both_names)
        store = load(self.root)
        self.assertEqual("【自称】" + model.PLACEHOLDER, store.render("熟人").splitlines()[0])
        found = codes(store.check())
        self.assertIn("IDENTITY_DISPLAY_MISSING", found)
        self.assertIn("IDENTITY_SYNONYMS_MISSING", found)


class CleanCardStaysCleanTest(unittest.TestCase):
    """降级不许误伤干净的卡：synonyms 写成空数组本来就是「没有别名」。"""

    def test_empty_synonyms_is_not_a_degrade(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = copy_fixture("valid", temp.name)
        data = read_json(root / "identity.json")
        self.assertEqual([], data["self_names"]["synonyms"])
        self.assertEqual([], codes(load(root).check()))


if __name__ == "__main__":
    unittest.main()
