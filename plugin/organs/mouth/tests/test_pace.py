"""节奏只管多久一条：首条立即、后续在夹取范围内、同 seed 不变、摘要说人话。

**15ms 是通道往返，不是节奏**：那个 15ms 是面板请求往返的实测（plugin/api/README.md:130 记的是
传输），本文件算的是说话节奏，量级在秒。下面 test_gap_is_not_the_channel_round_trip 就是拿这一条
当断言看着的——谁把节奏写成 0.015，它就红。

政策数据与仓库自检也放在这里：节奏的每一个数值都必须从 data/policy.json 来，
数据读不出来或缺件不响，这条线就无从谈起。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.mouth import pace as pace_module
from organs.mouth.model import PolicyMiss
from organs.mouth.pace import fill, gaps, visible_chars
from organs.mouth.split import split
from organs.mouth.warehouse import ENV_VAR, WarehouseMiss, load

POLICY = load("policy")
RULES = POLICY["pace"]

# 三句样例：一泡短（log 还没顶到上限）、一泡长（顶到上限）、一泡中等
TEXTS = ["在呢。", "她今天在整理旧照片，看到那张旧船票，忽然就笑了。", "后来大家都记得。"]

# 够切开两泡的长样例（一泡只装得下这么多，必须切）
LONG = ("她今天在整理旧照片，看到那张旧船票，忽然就笑了，说这张拍得真好，可惜那天风太大，头发全乱了。"
        "我说乱一点才像那天，她笑了一下，把照片推到我面前，说那就留着吧，别扔了，留着以后再看。"
        "夜里她把照片摊在桌上，一张一张地翻，翻到第三张的时候停住了，说这张要单独收起来，别丢了。"
        "她把阳台上的旧纸箱搬到楼下，回来的时候手上全是灰，谁也没说什么，只是把湿毛巾递了过去。")

# 政策里必须有的键：缺一个就说明数值被搬进代码里了
REQUIRED_KEYS = {
    "split": ("max_parts", "max_chars", "hard_cut_reason", "sentence_end", "semicolon",
              "comma", "newline", "trailing_closers", "left_banned", "right_banned"),
    "pace": ("first_gap", "log_base", "min_gap", "max_gap", "jitter_span", "round_digits"),
    "visible": ("strip",),
    "summary": ("item", "immediate", "gap", "sep"),
}
NUMBER_KEYS = {
    "split": ("max_parts", "max_chars"),
    "pace": ("first_gap", "log_base", "min_gap", "max_gap", "jitter_span", "round_digits"),
}
WORD_KEYS = {
    "split": ("hard_cut_reason", "sentence_end", "semicolon", "comma", "newline",
              "trailing_closers", "left_banned", "right_banned"),
    "visible": ("strip",),
    "summary": ("item", "immediate", "gap", "sep"),
}


class GapsTest(unittest.TestCase):
    def test_first_part_is_immediate_and_others_within_clamp(self):
        """首条 0.0；其余都 > 0，且落在 policy 的夹取范围 [min_gap, max_gap] 里。"""
        values = gaps(TEXTS, POLICY, "reply-1")
        self.assertEqual(len(TEXTS), len(values))
        self.assertEqual(0.0, values[0], "首条不是立即发")
        for index, value in enumerate(values[1:], 2):
            self.assertGreater(value, 0.0, "第 %d 条间隔不是正数" % index)
            self.assertGreaterEqual(value, RULES["min_gap"])
            self.assertLessEqual(value, RULES["max_gap"])

    def test_gap_is_not_the_channel_round_trip(self):
        """**15ms 是通道往返，不是节奏**：任何一个间隔都不许等于 0.015，量级也不该是毫秒。"""
        values = gaps(TEXTS, POLICY, "reply-1")
        for index, value in enumerate(values):
            self.assertNotEqual(0.015, value,
                                "15ms 是通道往返，不是节奏：第 %d 条被写成了 15ms" % (index + 1))
        self.assertGreater(RULES["min_gap"], 0.015, "节奏下限掉到通道量级了：15ms 是通道往返，不是节奏")

    def test_same_seed_gives_identical_gaps(self):
        """同一个 seed 两次调用必须完全一样（预览与实发对得上）。"""
        first = gaps(TEXTS, POLICY, "reply-42")
        second = gaps(TEXTS, POLICY, "reply-42")
        self.assertEqual(first, second, "同一个 seed 拿到了两组间隔，预览会骗人")

    def test_different_seeds_shift_the_short_gaps(self):
        """换 seed 会长短不一样（sha256 抖动是活的，不是把抖动写死成常数）。"""
        texts = ["嗯。", "在呢。", "好。"]
        one = gaps(texts, POLICY, "seed-a")[1:]
        other = gaps(texts, POLICY, "seed-b")[1:]
        self.assertNotEqual(one, other, "换 seed 间隔一字不变，抖动没起作用")
        for value in one + other:
            self.assertGreaterEqual(value, RULES["min_gap"])
            self.assertLessEqual(value, RULES["max_gap"])

    def test_send_at_accumulates_gaps(self):
        """send_at 是累加秒：第 n 条 = 前面所有间隔之和，且严格递增。"""
        filled = fill(split(LONG, POLICY), POLICY, "reply-7")
        self.assertGreaterEqual(len(filled.parts), 2, [len(part.text) for part in filled.parts])
        clock = 0.0
        for index, part in enumerate(filled.parts):
            clock = round(clock + part.gap_before, RULES["round_digits"])
            self.assertAlmostEqual(clock, part.send_at, places=RULES["round_digits"])
            if index:
                self.assertGreater(part.send_at, filled.parts[index - 1].send_at)

    def test_summary_is_human_readable(self):
        """摘要逐条可读：1/2 立即 · 2/2 +3.0s，一条对一条，带秒数。"""
        filled = fill(split(LONG, POLICY), POLICY, "reply-9")
        label = POLICY["summary"]
        total = len(filled.parts)
        rows = filled.summary.split(label["sep"])
        self.assertEqual(total, len(rows), filled.summary)
        for index, (row, part) in enumerate(zip(rows, filled.parts), 1):
            self.assertIn("%d/%d " % (index, total), row)
            when = (label["immediate"] if part.gap_before == 0.0
                    else label["gap"].format(gap=part.gap_before))
            self.assertIn(when, row)
        self.assertTrue(rows[0].endswith(label["immediate"]), rows[0])
        self.assertIn("+", filled.summary)
        self.assertIn("s", filled.summary)

    def test_fill_does_not_touch_text(self):
        """fill 只填节奏，一个字都不改。"""
        batch = split(LONG, POLICY)
        filled = fill(batch, POLICY, "reply-3")
        self.assertEqual([part.text for part in batch.parts], [part.text for part in filled.parts])
        self.assertEqual([part.degraded for part in batch.parts],
                         [part.degraded for part in filled.parts])

    def test_visible_chars_ignores_whitespace_and_punctuation(self):
        """可见字数 = 去掉空白与标点后的字数（停顿不发声，不该算成话多）。"""
        self.assertEqual(2, visible_chars("在呢。", POLICY))
        self.assertEqual(2, visible_chars(" 在\n呢，。", POLICY))
        self.assertEqual(7, visible_chars("hello, 世界!", POLICY))
        self.assertEqual(0, visible_chars("…… ——", POLICY))

    def test_docstring_says_15ms_is_transport_not_rhythm(self):
        """这句必须写在 pace 自己的 docstring 里：15ms 是通道往返，不是节奏。"""
        doc = pace_module.__doc__ or ""
        self.assertIn("15ms 是通道往返，不是节奏", doc)
        self.assertIn("不是随机数", doc)
        # 「有出处」这件事要真去读一次：行号会漂，钉行号等于永远绿。
        readme = Path(__file__).resolve().parents[3] / "api" / "README.md"
        self.assertTrue(readme.is_file(), "README 不在，出处无从核起：%s" % readme)
        self.assertIn("实测间隔（①→④）：15ms", readme.read_text(encoding="utf-8"))


class WarehouseAndPolicyTest(unittest.TestCase):
    def test_policy_json_loads_with_every_key(self):
        """policy.json 读得出来且含全部键：数值在数据里，不在代码里。"""
        for name, keys in REQUIRED_KEYS.items():
            self.assertIsInstance(POLICY.get(name), dict, "政策缺节：%r" % name)
            for key in keys:
                self.assertIn(key, POLICY[name], "政策缺键：%s.%s" % (name, key))
        for name, keys in NUMBER_KEYS.items():
            for key in keys:
                self.assertIsInstance(POLICY[name][key], (int, float), "%s.%s 不是数值" % (name, key))
        for name, keys in WORD_KEYS.items():
            for key in keys:
                self.assertIsInstance(POLICY[name][key], str, "%s.%s 不是文字/字表" % (name, key))
        self.assertLessEqual(RULES["min_gap"], RULES["max_gap"])
        self.assertGreater(RULES["log_base"], 1.0)
        self.assertGreater(POLICY["split"]["max_parts"], 0)

    def test_warehouse_miss_raises_instead_of_returning_empty(self):
        """缺件抛 WarehouseMiss（名字与找过的路径都带着），不返回 {}。"""
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(WarehouseMiss) as caught:
                load("没这件", root=root)
            self.assertEqual("没这件", caught.exception.name)
            self.assertTrue(caught.exception.path.endswith("没这件.json"), caught.exception.path)
            self.assertIn(root, caught.exception.path)

    def test_warehouse_env_var_overrides_bundled_data(self):
        """环境变量 MOUTH_DATA_DIR 能把根目录换走：缺件照样响，不会退回包内 data/。"""
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {ENV_VAR: root}):
                with self.assertRaises(WarehouseMiss):
                    load("policy")
                with open(os.path.join(root, "policy.json"), "w", encoding="utf-8") as handle:
                    handle.write('{"pace": {"min_gap": 0.1}}')
                self.assertEqual({"pace": {"min_gap": 0.1}}, load("policy"))

    def test_policy_miss_is_loud(self):
        """代码要用的键在数据里没有 → PolicyMiss，不许悄悄补一个默认值。"""
        with self.assertRaises(PolicyMiss):
            gaps(["嗯。", "在呢。"], {"visible": {"strip": ""}}, "seed")


if __name__ == "__main__":
    unittest.main()
