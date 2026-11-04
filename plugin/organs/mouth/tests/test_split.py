"""切分只管切在哪：条数、边界、硬切标记、「不许切在词中间」，以及「句末优先于逗号」。

数值不写在这份文件里：政策从 organs/mouth/data/policy.json 读（走 warehouse），
要改条数上限、最大字数或禁用字表，改数据，不改测试。

切分的粒度是「不一次砸一大段」：文本不超过 max_chars 时本来就只该是 1 泡，
所以下面凡是断言「切成 2 泡」的样例，都长到必须切。
"""
from __future__ import annotations

import unittest

from organs.mouth.split import split
from organs.mouth.warehouse import load

POLICY = load("policy")
RULES = POLICY["split"]

# 300 字的典型中文段落（多处句末标点 + 逗号，没有一句超过 max_chars，所以不该硬切）
PARAGRAPH = (
    "今天下午她把阳台上的旧纸箱搬到楼下，回来的时候手上全是灰。"
    "她说这箱子是搬家那年留下的，里头还是些没拆的信和照片。"
    "我没接话，只把湿毛巾递过去。"
    "她擦完手，忽然问我还记不记得那年冬天停电的事。"
    "记得，暖气停了三天，我们裹着被子在客厅说话，说得乱七八糟。"
    "后来电来了，她反而没话了，坐在那儿看窗外，看了很久。"
    "夜里她把照片摊在桌上，一张一张地翻，翻到第三张的时候停住了，说这张拍得真好，可惜那天风太大，头发全乱了。"
    "我说乱一点才像那天。"
    "她笑了一下，把照片推到我面前，说那就留着吧，别扔了。"
    "她说有些事当时不觉得，过了几年才会突然想起来。"
    "我点头，把箱子也留下了，没舍得扔。"
    "窗外忽然起风了，阳台上的衣服晃了两下，又停住了。"
)

# 塞满「而且，」「的事，」这类禁用边界的句子，长度够切开两泡
BANNED_BOUNDARY = (
    "她今天在整理旧照片，看到那张而且，就是去年秋天在海边拍的，"
    "她说的事，后来大家都记得，只是谁也没再提过那天的风有多大。"
    "她把照片摊在桌上，一张一张地翻，翻到第三张的时候停住了，"
    "说这张拍得真好，可惜那天风太大，头发全乱了，而且，我记得她那天穿的是那件旧外套。"
    "她把阳台上的旧纸箱搬到楼下，回来的时候手上全是灰，谁也没说什么，只是把湿毛巾递了过去。"
)

LINE_A = "她今天在整理旧照片，看到那张旧船票，忽然就笑了，说这张拍得真好，可惜那天风太大，头发全乱了。"
LINE_B = "我说乱一点才像那天，她笑了一下，把照片推到我面前，说那就留着吧，别扔了，留着以后再看。"
LINE_C = "夜里她把照片摊在桌上，一张一张地翻，翻到第三张的时候停住了，说这张要单独收起来，别丢了。"
LINE_D = "她把阳台上的旧纸箱搬到楼下，回来的时候手上全是灰，谁也没说什么，只是把湿毛巾递了过去。"


class SplitTest(unittest.TestCase):
    def test_300_char_paragraph_yields_two_or_three_parts(self):
        """300 字典型段落 → 2–3 泡；每泡都非空，拼回去与原文逐字相等。"""
        self.assertEqual(300, len(PARAGRAPH), "样例段落不是 300 字，下面的条数结论就不作数")
        batch = split(PARAGRAPH, POLICY)
        self.assertGreaterEqual(len(batch.parts), 2, "300 字只切出 1 泡：切分没起作用")
        self.assertLessEqual(len(batch.parts), 3, "300 字切出 4 泡以上：条数上限没守住")
        for index, part in enumerate(batch.parts, 1):
            self.assertTrue(part.text.strip(), "第 %d 泡是空的" % index)
        self.assertEqual(PARAGRAPH, "".join(part.text for part in batch.parts),
                         "拼回去与原文不等：切的时候丢了字或改了字")

    def test_normal_paragraph_never_hard_cuts(self):
        """没有任何一段超过 max_chars 时，一个 degraded 也不许出现（硬切是有代价的退路）。"""
        batch = split(PARAGRAPH, POLICY)
        self.assertEqual([], [part.text for part in batch.parts if part.degraded])
        self.assertLessEqual(max(len(part.text) for part in batch.parts), RULES["max_chars"])

    def test_cut_points_never_land_inside_a_word(self):
        """「而且，」「的事，」这类地方不许切：没有一泡以禁用左字收尾、也没有一泡以禁用右字开头。"""
        batch = split(BANNED_BOUNDARY, POLICY)
        left_banned = set(RULES["left_banned"])
        right_banned = set(RULES["right_banned"])
        self.assertGreaterEqual(len(batch.parts), 2, [part.text for part in batch.parts])
        for index, part in enumerate(batch.parts, 1):
            body = part.text.strip()
            self.assertTrue(body)
            self.assertNotIn(body[-1], left_banned,
                             "第 %d 泡以禁用左字收尾（切在词中间了）：%r" % (index, body[-8:]))
            self.assertNotIn(body[0], right_banned,
                             "第 %d 泡以禁用右字开头（切在词中间了）：%r" % (index, body[:8]))
            self.assertFalse(body.endswith("而且，"), "切在「而且，」后面了：%r" % body[-8:])
        self.assertEqual(BANNED_BOUNDARY, "".join(part.text for part in batch.parts))

    def test_banned_boundary_blocks_the_only_cut(self):
        """整段只有一个可切点，而它在「而且，」后面：宁可一整泡，也不许切在词中间。"""
        batch = split("而且，后来大家都记得。", POLICY)
        self.assertEqual(1, len(batch.parts), [part.text for part in batch.parts])

    def test_oversized_single_sentence_is_hard_cut_and_flagged(self):
        """400 字一口气没有停顿符号 → 硬切发生，且每一泡都带 degraded=true 与一句人话 reason。"""
        text = "测" * 400
        batch = split(text, POLICY)
        self.assertGreaterEqual(len(batch.parts), 2, "超长单句没被硬切")
        self.assertLessEqual(len(batch.parts), RULES["max_parts"])
        for index, part in enumerate(batch.parts, 1):
            self.assertTrue(part.degraded, "第 %d 泡是硬切的，却没标 degraded" % index)
            self.assertTrue(part.reason.strip(), "第 %d 泡硬切了却没给理由" % index)
            self.assertIn("硬切", part.reason)
            self.assertLessEqual(len(part.text), RULES["max_chars"])
        self.assertEqual(text, "".join(part.text for part in batch.parts))

    def test_short_sentence_stays_one_part(self):
        """「在呢。」这种一句就完的，就 1 泡，间隔与发送时刻都是 0.0。"""
        batch = split("在呢。", POLICY)
        self.assertEqual(1, len(batch.parts))
        self.assertEqual("在呢。", batch.parts[0].text)
        self.assertEqual(0.0, batch.parts[0].gap_before)
        self.assertEqual(0.0, batch.parts[0].send_at)

    def test_line_break_is_a_legal_cut_point(self):
        """换行是合法切点：两行就是两泡，换行留在前一泡里（一个字都不丢）。"""
        text = LINE_A + LINE_B + "\n" + LINE_C + LINE_D
        batch = split(text, POLICY)
        self.assertEqual(2, len(batch.parts), [len(part.text) for part in batch.parts])
        self.assertTrue(batch.parts[0].text.endswith("\n"), repr(batch.parts[0].text[-6:]))
        self.assertEqual(text, "".join(part.text for part in batch.parts))

    def test_blank_lines_do_not_make_empty_parts(self):
        """空行不许切出空泡：空白并到邻居里，拼回去仍然逐字相等。"""
        text = LINE_A + LINE_B + "\n\n" + LINE_C + LINE_D
        batch = split(text, POLICY)
        self.assertEqual(2, len(batch.parts), [len(part.text) for part in batch.parts])
        for index, part in enumerate(batch.parts, 1):
            self.assertTrue(part.text.strip(), "第 %d 泡是空白泡" % index)
        self.assertEqual(text, "".join(part.text for part in batch.parts))

    def test_split_leaves_gaps_to_pace(self):
        """切分只填 text/degraded/reason：gap_before、send_at 留 0.0，summary 留空等 pace。"""
        batch = split(PARAGRAPH, POLICY)
        self.assertEqual([0.0] * len(batch.parts), [part.gap_before for part in batch.parts])
        self.assertEqual([0.0] * len(batch.parts), [part.send_at for part in batch.parts])
        self.assertEqual("", batch.summary)

    def test_blank_text_yields_no_parts(self):
        """只有空白的文本没有要发的泡，返回空批次而不是一泡空白。"""
        self.assertEqual([], split("   \n ", POLICY).parts)
        self.assertEqual([], split("", POLICY).parts)

    # —— 分层窗口：句末优先于逗号 ——————————————————————————

    def test_cut_preferences_live_in_data(self):
        """层级序列与窗口都写在 policy.json 里：代码只认顺序，不认具体是哪个字。"""
        preference = list(RULES["cut_preference"])
        self.assertGreater(RULES["cut_window_chars"], 0)
        for name in ("sentence_end", "semicolon", "newline", "comma"):
            self.assertIn(name, preference)          # 层级名就是同一节里的那几组停顿字符
            self.assertIn(name, RULES)
        self.assertLess(preference.index("sentence_end"), preference.index("comma"),
                        "句末该排在逗号前面，否则窗口挑切点时等于没挑")

    def test_sentence_end_beats_comma_within_the_window(self):
        """同一段 300 字：按总长算出的切点落在逗号上，窗口里有句末就换到句末。

        换切点不许放松任何老判据：还是 3 泡以内、每泡不超 max_chars、拼回去逐字等于原文。
        """
        batch = split(PARAGRAPH, POLICY)
        ends = set(RULES["sentence_end"])
        self.assertGreaterEqual(len(batch.parts), 2)
        for index, part in enumerate(batch.parts, 1):
            self.assertIn(part.text.rstrip()[-1], ends,
                          "第 %d 泡在逗号上收尾了（窗口里有句末却没换过去）：%r"
                          % (index, part.text[-8:]))
        self.assertLessEqual(max(len(part.text) for part in batch.parts), RULES["max_chars"])
        self.assertEqual(PARAGRAPH, "".join(part.text for part in batch.parts))

    def test_window_zero_falls_back_to_the_old_cut(self):
        """把窗口关到 0 就是旧行为：同一段字会泡在逗号上——说明换切点的是那个窗口，不是别的。"""
        narrow = dict(POLICY, split=dict(RULES, cut_window_chars=0))
        tails = [part.text.rstrip()[-1] for part in split(PARAGRAPH, narrow).parts]
        self.assertIn(RULES["comma"][0], tails, "窗口关掉后本该退回逗号切点：%r" % tails)


if __name__ == "__main__":
    unittest.main()
