"""M4 压缩前取 pin：只做接口与拒收，不实现压缩。

take_pin() 每条红线一行（条目 id + 禁令首句），整体 ≤1200 字节；
三条拒收：跨度 <1200 字节、产物为空、产物首段与 pin 不逐字一致。
pin 与产物都还要过 M1 断言（见 test_redline_assertion 的同名一组）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from organs.soul import model, pin, redline
from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture

DATA = Path(__file__).resolve().parents[1] / "data"


def codes(issues) -> list:
    return [issue.code for issue in issues]


class TakePinTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def test_each_active_line_is_one_row_with_id_and_first_sentence(self):
        text = self.store.pin()
        rows = text.splitlines()
        self.assertEqual(2, len(rows))
        self.assertTrue(rows[0].startswith("b01｜【待定稿】"), rows[0])
        self.assertIn("｜", rows[1])

    def test_pin_fits_the_1200_byte_budget_on_real_data(self):
        """真数据 7 条红线：pin 678 字节，离 1200 的顶还远（见交付记录的实跑数字）。"""
        text = pin.take_pin(load(DATA).snapshot().bounds)
        self.assertLessEqual(pin.pin_bytes(text), pin.PIN_LIMIT)
        self.assertEqual(7, len(text.splitlines()))

    def test_retired_lines_are_not_pinned(self):
        self.store.edit(actor="bowen", reason="撤一条", block="bounds",
                        path="items[1].active", value=False)
        self.assertEqual(1, len(self.store.pin().splitlines()))

    def test_first_sentence_stops_at_sentence_end(self):
        self.assertEqual("不撂挑子：只冲这件事。", pin.first_sentence("不撂挑子：只冲这件事。后面还有"))
        self.assertEqual("没有句末标点的一条", pin.first_sentence("没有句末标点的一条"))

    def test_over_budget_pin_raises_instead_of_truncating(self):
        items = tuple(model.BoundsItem(id="x%02d" % index, rule="禁" * 60, match_type="literal",
                                       pattern="禁", action="block", active=True)
                      for index in range(30))
        with self.assertRaises(ValueError):
            pin.take_pin(model.Bounds(items=items))


class AdmitCompressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.pin_text = load(self.root).pin()

    def _product(self, body: str = "这一段被压成一句目录指针。") -> str:
        return self.pin_text + "\n\n" + body

    def test_a_proper_request_passes_both_sides(self):
        issues = pin.admit_compression(2000, self._product(), self.pin_text)
        self.assertEqual([], codes(issues))

    def test_span_smaller_than_1200_bytes_is_rejected(self):
        """验收三：跨度 800 字节的压缩请求被拒。"""
        issues = pin.admit_compression(800, self._product(), self.pin_text)
        self.assertEqual([pin.SPAN_TOO_SMALL], codes(issues))
        self.assertIn("800", issues[0].message)

    def test_empty_product_is_rejected(self):
        for product in ("", "   \n  ", None):
            issues = pin.admit_compression(2000, product, self.pin_text)
            self.assertIn(pin.PRODUCT_EMPTY, codes(issues), repr(product))

    def test_product_must_repeat_the_pin_verbatim(self):
        reworded = "约束：红线都还在。\n\n" + "这一段被压成一句目录指针。"
        issues = pin.admit_compression(2000, reworded, self.pin_text)
        self.assertEqual([pin.ROUNDTRIP_MISMATCH], codes(issues))

    def test_product_that_only_summarises_the_pin_is_rejected(self):
        """「复述式回填」不算回填：首段必须逐字等于 pin。"""
        summary = "红线：不自称 AI、不刷脏话等两条。"
        issues = pin.check_pin_roundtrip(self.pin_text, summary + "\n\n正文")
        self.assertEqual([pin.ROUNDTRIP_MISMATCH], codes(issues))

    def test_product_with_a_soft_word_is_rejected(self):
        issues = pin.admit_compression(2000, self._product("尽量把她压短一点"), self.pin_text)
        self.assertIn(redline.SOFT_WORD, codes(issues))

    def test_compress_hook_is_a_stub_not_an_implementation(self):
        """压缩入口只留了 hook：准入过了就直接抛 NotImplementedError，没有别的出路。"""
        with self.assertRaises(NotImplementedError):
            pin.compress_once(self.pin_text, "跨" * 1200, self._product())
        rejected = pin.compress_once(self.pin_text, "短跨", self._product())
        self.assertEqual([pin.SPAN_TOO_SMALL], codes(rejected))


if __name__ == "__main__":
    unittest.main()
