"""M1 红线断言：校验对象是 render() 产出的红线段，不是文件行。

三条硬要求：生效条目 ≤40 条、每条禁令恰好出现 1 次、命中软话词即拒。
参考实现「一条禁令占一行」，所以它写「≤40 行」；本仓渲染成「；」拼接的一行，物理行恒为 1，
那条照抄过来恒真——这里只数条目，不数物理行。
落点两处：载入时（load）与每次写盘之前（_commit / migrate / rollback）——不过就不许落盘。
pin 与压缩产物走同一份校验（M4），否则压缩就是绕过 M1 的后门。
"""
from __future__ import annotations

import tempfile
import unittest

from organs.soul import model, pin, redline
from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture, edit_card, read_bytes


def codes(issues) -> list:
    return [issue.code for issue in issues]


def _redline_line(text: str) -> str:
    """render() 产出的那一段【红线】：断言的校验对象就是它。"""
    for line in text.splitlines():
        if line.startswith(redline.REDLINE_HEAD):
            return line
    return ""


class SegmentSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def test_segment_is_the_redline_part_of_render(self):
        snapshot = self.store.snapshot()
        segment = redline.segment(snapshot.bounds)
        self.assertEqual(segment, _redline_line(self.store.render()))
        self.assertTrue(segment.startswith(redline.REDLINE_HEAD))

    def test_clean_fixture_passes_all_three(self):
        self.assertEqual([], redline.check_bounds(self.store.snapshot().bounds))

    def test_retired_line_is_not_asserted(self):
        """撤下的线（active=false）不进注入，也就不进断言：它能带软话词而不被拒。"""
        result = self.store.edit(actor="bowen", reason="撤一条", block="bounds",
                                 path="items[0].active", value=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual([], redline.check_bounds(self.store.snapshot().bounds))


class SoftWordTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def test_soft_word_rejects_load(self):
        edit_card(self.root, "bounds",
                  lambda data: data["items"][0].update({"rule": "尽量避免嘲讽"}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        self.assertIn(redline.SOFT_WORD, codes(caught.exception.issues))

    def test_soft_word_blocks_the_write_and_nothing_lands(self):
        """验收一：塞「尽量避免嘲讽」→ 保存被拒，拒因含「软话词」；删掉恢复正常。"""
        before = read_bytes(self.root / "bounds.json")
        result = self.store.edit(actor="bowen", reason="加一句软话", block="bounds",
                                 path="items[0].rule", value="尽量避免嘲讽")
        self.assertFalse(result["ok"], result)
        self.assertEqual(redline.SOFT_WORD, result["issues"][0]["code"])
        self.assertIn("软话词", result["message"] + result["issues"][0]["message"])
        self.assertEqual(before, read_bytes(self.root / "bounds.json"))
        self.assertEqual([], self.store.history())
        fixed = self.store.edit(actor="bowen", reason="改回硬话", block="bounds",
                                path="items[0].rule", value="不嘲讽、不玩梗")
        self.assertTrue(fixed["ok"], fixed)

    def test_soft_word_vocabulary_is_configurable(self):
        bounds = self.store.snapshot().bounds
        self.assertEqual([], redline.check_bounds(bounds, soft_words=("禁止套话",)))
        found = redline.check_bounds(bounds, soft_words=("【待定稿",))
        self.assertEqual([redline.SOFT_WORD], codes(found))

    def test_default_soft_words_are_the_three_from_the_ledger(self):
        self.assertEqual(("尽量", "可以尝试", "最好不要"), redline.SOFT_WORDS)


class OccurrenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def test_same_rule_twice_is_rejected(self):
        edit_card(self.root, "bounds",
                  lambda data: data["items"][1].update({"rule": data["items"][0]["rule"]}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        self.assertIn(redline.DUPLICATED, codes(caught.exception.issues))
        self.assertIn("出现 2 次", "；".join(issue.message for issue in caught.exception.issues))

    def test_rule_that_is_a_substring_of_another_is_rejected(self):
        """一条禁令是另一条的子串：读数就是 2 次，按重复拒（这也是「一次只许写一行」）。"""
        edit_card(self.root, "bounds", lambda data: data["items"].__setitem__(0, {
            "id": "b01", "rule": "不嘲讽",
            "match": {"type": "literal", "pattern": "不嘲讽"},
            "action": "block", "active": True, "enforced_by": "soul·字面"}))
        edit_card(self.root, "bounds", lambda data: data["items"].__setitem__(1, {
            "id": "b02", "rule": "有人难过时，不嘲讽、不玩梗",
            "match": {"type": "literal", "pattern": "有人难过时"},
            "action": "block", "active": True, "enforced_by": "soul·字面"}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        self.assertIn(redline.DUPLICATED, codes(caught.exception.issues))

    def test_write_with_a_vanishing_rule_is_rejected(self):
        result = self.store.edit(actor="bowen", reason="改重", block="bounds",
                                 path="items[0].rule", value="【待定稿·二】")
        self.assertFalse(result["ok"], result)
        self.assertIn(redline.DUPLICATED, [item["code"] for item in result["issues"]])
        self.assertIn("出现", result["message"])


class ItemCountTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def _append(self, count: int) -> None:
        def mutate(data):
            start = len(data["items"])
            for index in range(count):
                number = start + index
                data["items"].append({
                    "id": "x%02d" % number, "rule": "第 %d 条不许的事" % number,
                    "match": {"type": "literal", "pattern": "第 %d 条不许的事" % number},
                    "action": "block", "active": True, "enforced_by": "soul·字面"})
        edit_card(self.root, "bounds", mutate)

    def test_forty_items_are_allowed_and_forty_one_are_not(self):
        self._append(38)                       # 2 + 38 = 40 条：正好卡在线上
        self.assertEqual(40, len(load(self.root).snapshot().bounds.items))
        self._append(1)                        # 41 条：过线（数条目，不数物理行）
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        self.assertIn(redline.TOO_MANY_ITEMS, codes(caught.exception.issues))


class SameCheckForPinAndProductTest(unittest.TestCase):
    """压缩后模型看到的是 pin + 产物：两者都过同一份 M1 断言，压缩才不是后门。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.pin_text = pin.take_pin(load(self.root).snapshot().bounds)

    def test_product_with_a_soft_word_is_rejected(self):
        product = self.pin_text + "\n\n" + "尽量把事情讲清楚"
        issues = pin.admit_compression(2000, product, self.pin_text)
        self.assertIn(redline.SOFT_WORD, codes(issues))

    def test_pin_itself_is_asserted(self):
        dirty = self.pin_text + "\n尽量别顶嘴"
        issues = pin.admit_compression(2000, dirty + "\n\n正文", dirty)
        self.assertIn(redline.SOFT_WORD, codes(issues))


if __name__ == "__main__":
    unittest.main()
