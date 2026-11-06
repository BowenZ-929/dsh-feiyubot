"""imgphrase：只认一张短语表，认不出就 None——猜错一张图比反问一句贵。

判据：五种说法要认；句末标点与「你的／你那张」这类定语要剥掉；表外的话、光秃秃的一个名字
一律 None（那些走 `name`，不走这句话）。这里只测「认得出什么」，找不到图是 assets 的事。
"""
from __future__ import annotations

import unittest

from organs.hand import imgphrase


class FrameTest(unittest.TestCase):
    def test_the_five_named_ways_of_asking_are_understood(self):
        cases = {
            "给我看你的自画像": "自画像",
            "让我看看自画像": "自画像",
            "把我的自画像给我看": "我的自画像",
            "发张自画像给我看": "自画像",
            "看看你的自画像": "自画像",
        }
        for said, want in cases.items():
            with self.subTest(said=said):
                self.assertEqual(want, imgphrase.parse(said))

    def test_trailing_punctuation_and_particles_are_shaved_off(self):
        for said in ("给我看自画像。", "给我看自画像！", "给我看自画像吧", "给我看 自画像 ."):
            with self.subTest(said=said):
                self.assertEqual("自画像", imgphrase.parse(said))

    def test_the_owner_prefix_is_shaved_once_and_only_once(self):
        self.assertEqual("自画像", imgphrase.parse("给我看你的自画像"))
        self.assertEqual("自画像", imgphrase.parse("给我看你那张自画像"))
        self.assertEqual("自画像", imgphrase.parse("给我看你的那张自画像"))
        self.assertEqual("自画像", imgphrase.parse("看看你的那张自画像"))

    def test_a_file_name_survives_intact(self):
        self.assertEqual("江迟-人设卡.jpg", imgphrase.parse("给我看你的江迟-人设卡.jpg"))


class NothingTest(unittest.TestCase):
    """认不出就 None：不猜、不返回空串、不把整句话当名字。"""

    def test_phrases_outside_the_table_are_not_guessed(self):
        for said in ("给你讲个事", "今天天气不错", "发张好看的", "随便来一张", "看一下",
                     "给我看看", "看看你的", "给我看 你的"):
            with self.subTest(said=said):
                self.assertIsNone(imgphrase.parse(said))

    def test_a_bare_name_is_not_a_sentence(self):
        """光给一个名字是 `name` 那条路，不是这张表：这里不许替它认下来。"""
        self.assertIsNone(imgphrase.parse("自画像"))

    def test_empty_and_non_string_are_none(self):
        for value in ("", "   ", None, 7, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(imgphrase.parse(value))


class TableTest(unittest.TestCase):
    def test_the_table_is_fixed_and_small(self):
        self.assertEqual(("给我看看", ""), imgphrase.FRAMES[0], "长的框排在短的框前面")
        self.assertEqual(7, len(imgphrase.FRAMES), "加说法就是改这张表，用例跟着改")


if __name__ == "__main__":
    unittest.main()
