"""注入渲染：只有六件正文，没有第七段（性格归心，不在魂里）。"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest

from organs.soul import model, render
from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture, edit_card


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        edit_card(self.root, "manner", lambda data: data.update({"stance": "整体姿态"}))
        self.store = load(self.root)

    def test_two_loads_render_identically(self):
        self.assertEqual(self.store.render("熟人"), load(self.root).render("熟人"))

    def test_render_has_exactly_six_sections(self):
        text = self.store.render("熟人")
        for head in ("【自称】", "【形象】", "【性格底色】", "【怕什么】", "【待人姿态】", "【红线】"):
            self.assertIn(head, text)
        self.assertNotIn("【我是谁】", text)      # 那一行是世界怎么看她，归脑·人名录
        self.assertEqual(6, len(text.splitlines()))

    def test_appearance_sits_between_self_name_and_character(self):
        """形象在【自称】之后、【性格底色】之前：先她是谁，再她样子，再她的底色。"""
        heads = [line.split("】")[0] + "】" for line in self.store.render("熟人").splitlines()]
        self.assertEqual(["【自称】", "【形象】", "【性格底色】", "【怕什么】", "【待人姿态】",
                          "【红线】"], heads)

    def test_fears_sits_between_character_and_stance(self):
        """怕什么在【性格底色】之后、【待人姿态】之前：先她是谁、她的底色，再她怕什么。"""
        lines = self.store.render("熟人").splitlines()
        self.assertEqual("【怕什么】", lines[3].split("】")[0] + "】")
        self.assertEqual("【待人姿态】", lines[4].split("】")[0] + "】")

    def test_budget_counts_the_five_items_only(self):
        snapshot = self.store.snapshot()
        self.assertEqual(model.body_chars(snapshot.identity, snapshot.manner, snapshot.bounds),
                         render.budget_used(snapshot))
        self.assertLessEqual(render.budget_used(snapshot), model.limits.body_total)
        self.assertGreater(len(self.store.render("熟人")), render.budget_used(snapshot))  # 标题不数进去

    def test_three_audiences_render_identically(self):
        """矩阵归脑：熟人/生人/群不再各出一版，旧档参数连一个字都不许影响输出。"""
        self.assertEqual(self.store.render("熟人"), self.store.render("生人"))
        self.assertEqual(self.store.render("熟人"), self.store.render("群"))
        self.assertEqual(self.store.render(""), self.store.render("长辈"))

    def test_manner_is_one_stance_field(self):
        """魂里没有对象档：Manner 只剩 stance 一格，阵容自己也不再是魂的概念。"""
        self.assertEqual(["stance"], [field.name for field in dataclasses.fields(model.Manner)])
        self.assertFalse(hasattr(model, "AUDIENCES"))
        self.assertFalse(hasattr(render, "audience_row"))

    def test_stance_renders_verbatim_without_a_tier_suffix(self):
        text = self.store.render("熟人")
        self.assertIn("整体姿态", text)
        self.assertNotIn("（熟人：", text)        # 分档后缀随矩阵一起没了

    def test_redlines_carry_no_item_ids(self):
        text = self.store.render("熟人")
        for item in self.store.snapshot().bounds.items:
            self.assertIn(item.rule, text)
            self.assertNotIn(item.id, text)

    def test_self_statements_are_separated_and_not_doubled(self):
        snapshot = self.store.snapshot()
        expected = "".join(item + "。" for item in snapshot.identity.self_statements)
        self.assertEqual(expected, render.self_statements(snapshot.identity))

    def test_no_tier_words_left_in_the_soul(self):
        """档位词随六轴卡一起归心：魂里既没有 tier()，文本里也没有档位词。"""
        self.assertFalse(hasattr(render, "tier"))
        text = self.store.render("熟人")
        for word in ("很低", "偏低", "偏高", "很高", "/5"):
            self.assertNotIn(word, text)

    def test_match_bounds_returns_item_ids(self):
        self.assertEqual([], self.store.match_bounds("今天天气不错"))


if __name__ == "__main__":
    unittest.main()
