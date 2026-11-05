"""切分只管切在哪：一泡几句、切点落在哪儿、硬切标记、动作行自成一泡，以及「不许切在词中间」。

数值不写在这份文件里：政策从 organs/mouth/data/policy.json 读（走 warehouse），动作行的括号对与
身体部位从 organs/mouth/data/action_note.json 读（借阅，不另造）。要改每泡句数、条数上限、动作行
长度上限或任何字表，改数据，不改测试。

**这一轮换了两条判据**，所以旧断言里有两条被推翻，改准的地方都写在各自的 docstring 里：
- 「最多 3 泡」退位给「每泡最多两句」，300 字段落的泡数从 2–3 泡变成 6 泡；
- 窗口挑切点只活在对超长句的兜底上，普通段落按句末切，窗口关到 0 一个字符都不变。
一条判据都没删：旧断言改成在新位置上仍然成立，措辞没放宽。

切分的粒度是「一句话一泡，最多两句一泡」：文本不超过 max_chars 的句子本来就整句成泡，
所以下面凡是断言切开的样例，都长到必须切。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from organs.mouth.pace import fill
from organs.mouth.split import split
from organs.mouth.warehouse import load

POLICY = load("policy")
RULES = POLICY["split"]
PACE = POLICY["pace"]
TABLE = load("action_note")

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

# 一条 214 字的超长单句：一个句末标点都没有（只有逗号与分号），所以只能走硬切那条兜底路
OVERLONG = (
    "她今天在整理旧照片，看到那张旧船票，忽然就笑了；说这张拍得真好，可惜那天风太大，头发全乱了，"
    "她把阳台上的旧纸箱搬到楼下，回来的时候手上全是灰，谁也没说什么，只是把湿毛巾递了过去；"
    "夜里她把照片摊在桌上，一张一张地翻，翻到第三张的时候停住了，说这张要单独收起来，别丢了，"
    "我说乱一点才像那天，她笑了一下，把照片推到我面前，说那就留着吧，别扔了，留着以后再看；"
    "后来电来了，她反而没话了，坐在那儿看窗外，看了很久，窗外的风也停了，天也黑了"
)

# 120 字 8 句：每泡两句时泡数正好是句数的一半
EIGHT = (
    "今天下午她把阳台上的旧纸箱一个人搬到楼下。"
    "她说这箱子是搬家那年留下来的。"
    "我擦完手把湿毛巾递回她手边。"
    "后来电来了她反而一句话都没有了。"
    "夜里她把照片摊在桌上一张一张地翻。"
    "我记得暖气停了整整三天半。"
    "窗外的风也慢慢停下来了。"
    "她还是坐在那儿看着窗外。"
)

# 验收那句：动作句没有括号，也必须单独成泡
ACCEPT_DIALOGUE = "……谁肥了。尾巴甩了一下。蓝鲸才叫肥。我这是正常体型。"
ACCEPT_BRACKET = "……谁肥了。（尾巴甩了一下）。蓝鲸才叫肥。我这是正常体型。"


def _sentences_in(text):
    """数这段字里有几句：连着的一串句末标点算一句（「……」不是两句）。"""
    count, inside = 0, False
    for char in text:
        mark = char in RULES["sentence_end"]
        if mark and not inside:
            count += 1
        inside = mark
    return count


def _texts(batch):
    """逐泡的文字：断言里贴出来才看得见切成了什么样。"""
    return [part.text for part in batch.parts]


def _policy_with(**changes):
    """同一份政策、只改一个数值（临时目录里落一份再读回来）：证明阈值在数据里，改数据即改行为。"""
    with tempfile.TemporaryDirectory() as root:
        data = dict(POLICY, split=dict(RULES, **changes))
        path = os.path.join(root, "policy.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        return load("policy", root=root)


class SplitTest(unittest.TestCase):
    def test_300_char_paragraph_yields_one_part_per_two_sentences(self):
        """300 字典型段落 → 两句一泡；每泡非空，拼回去与原文逐字相等。

        旧断言是「2–3 泡」（旧名 test_300_char_paragraph_yields_two_or_three_parts）。旧口径让
        max_parts=3 当判据，十几句挤成 3 泡、一泡三四句；现在两句上限优先，泡数 = ceil(句数 ÷ 每泡
        句数)，由数据算出。判据没删，换成了新口径下更紧的那一条。
        """
        self.assertEqual(300, len(PARAGRAPH), "样例段落不是 300 字，下面的条数结论就不作数")
        batch = split(PARAGRAPH, POLICY)
        sentences = _sentences_in(PARAGRAPH)
        want = -(-sentences // RULES["sentences_per_part"])
        self.assertEqual(want, len(batch.parts),
                         "泡数不是「句数 ÷ 每泡句数」：%r" % _texts(batch))
        self.assertLessEqual(len(batch.parts), RULES["max_parts"], "泡数超过防炸上限了")
        for index, part in enumerate(batch.parts, 1):
            self.assertTrue(part.text.strip(), "第 %d 泡是空的" % index)
            self.assertLessEqual(_sentences_in(part.text), RULES["sentences_per_part"],
                                 "第 %d 泡超过两句了：%r" % (index, part.text))
        self.assertEqual(PARAGRAPH, "".join(_texts(batch)),
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
        self.assertGreaterEqual(len(batch.parts), 2, _texts(batch))
        for index, part in enumerate(batch.parts, 1):
            body = part.text.strip()
            self.assertTrue(body)
            self.assertNotIn(body[-1], left_banned,
                             "第 %d 泡以禁用左字收尾（切在词中间了）：%r" % (index, body[-8:]))
            self.assertNotIn(body[0], right_banned,
                             "第 %d 泡以禁用右字开头（切在词中间了）：%r" % (index, body[:8]))
            self.assertFalse(body.endswith("而且，"), "切在「而且，」后面了：%r" % body[-8:])
        self.assertEqual(BANNED_BOUNDARY, "".join(_texts(batch)))

    def test_banned_boundary_blocks_the_only_cut(self):
        """整段只有一个可切点，而它在「而且，」后面：宁可一整泡，也不许切在词中间。"""
        batch = split("而且，后来大家都记得。", POLICY)
        self.assertEqual(1, len(batch.parts), _texts(batch))

    def test_oversized_single_sentence_is_hard_cut_and_flagged(self):
        """400 字一口气没有停顿符号 → 硬切发生，且每一泡都带 degraded=true 与一句人话 reason。

        旧判据不许丢：max_chars 从主判据退成兜底之后，这一条仍然是它的验收。
        """
        text = "测" * 400
        batch = split(text, POLICY)
        self.assertGreaterEqual(len(batch.parts), 2, "超长单句没被硬切")
        self.assertLessEqual(len(batch.parts), RULES["max_parts"])
        for index, part in enumerate(batch.parts, 1):
            self.assertTrue(part.degraded, "第 %d 泡是硬切的，却没标 degraded" % index)
            self.assertTrue(part.reason.strip(), "第 %d 泡硬切了却没给理由" % index)
            self.assertIn("硬切", part.reason)
            self.assertLessEqual(len(part.text), RULES["max_chars"])
        self.assertEqual(text, "".join(_texts(batch)))

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
        self.assertEqual(text, "".join(_texts(batch)))

    def test_blank_lines_do_not_make_empty_parts(self):
        """空行不许切出空泡：空白并到邻居里，拼回去仍然逐字相等。"""
        text = LINE_A + LINE_B + "\n\n" + LINE_C + LINE_D
        batch = split(text, POLICY)
        self.assertEqual(2, len(batch.parts), [len(part.text) for part in batch.parts])
        for index, part in enumerate(batch.parts, 1):
            self.assertTrue(part.text.strip(), "第 %d 泡是空白泡" % index)
        self.assertEqual(text, "".join(_texts(batch)))

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
        """层级序列与窗口都写在 policy.json 里：代码只认顺序，不认具体是哪个字。

        这一轮之后两张表只服务「单句超长」那条兜底路：普通段落按句末切，用不上窗口。
        """
        preference = list(RULES["cut_preference"])
        self.assertGreater(RULES["cut_window_chars"], 0)
        for name in ("sentence_end", "semicolon", "newline", "comma"):
            self.assertIn(name, preference)          # 层级名就是同一节里的那几组停顿字符
            self.assertIn(name, RULES)
        self.assertLess(preference.index("sentence_end"), preference.index("comma"),
                        "句末该排在逗号前面，否则窗口挑切点时等于没挑")

    def test_parts_never_end_on_a_comma(self):
        """句末优先于逗号：切点只落在句末，一泡不会在逗号上收尾，且每泡不超 max_chars。

        旧名 test_sentence_end_beats_comma_within_the_window——旧口径靠窗口把切点从句末换回来；
        句级切分之后普通段落的切点本来就落在句末，于是这条从「窗口能做到」变成「结构上不可能在
        逗号上收尾」。断言没放松，是收紧了。
        """
        batch = split(PARAGRAPH, POLICY)
        ends = set(RULES["sentence_end"])
        self.assertGreaterEqual(len(batch.parts), 2)
        for index, part in enumerate(batch.parts, 1):
            self.assertIn(part.text.rstrip()[-1], ends,
                          "第 %d 泡在逗号上收尾了：%r" % (index, part.text[-8:]))
        self.assertLessEqual(max(len(part.text) for part in batch.parts), RULES["max_chars"])
        self.assertEqual(PARAGRAPH, "".join(_texts(batch)))

    def test_window_only_shows_on_the_overflow_cut(self):
        """窗口只剩在「单句超长」那条兜底路上起作用：关到 0，同一句会退回逗号切点。

        旧名 test_window_zero_falls_back_to_the_old_cut——旧口径里窗口是普通段落的切点挑选器。
        现在两头都断言：普通段落关不关窗口一字不变（切点在句末，窗口无事可做），而超长单句关掉
        窗口就退回逗号。位置换了，判据没丢。
        """
        narrow = dict(POLICY, split=dict(RULES, cut_window_chars=0))
        self.assertEqual(_texts(split(PARAGRAPH, POLICY)), _texts(split(PARAGRAPH, narrow)),
                         "普通段落还受窗口影响：切点没落在句末")
        batch = split(OVERLONG, POLICY)
        self.assertGreater(len(OVERLONG), RULES["max_chars"], "样例不是超长单句，走不到兜底那条路")
        self.assertFalse(any(part.degraded for part in batch.parts),
                         "句子里有停顿，不该按字数硬切：%r" % _texts(batch))
        tails = [part.text.rstrip()[-1] for part in batch.parts]
        self.assertIn(RULES["semicolon"][0], tails, "兜底路上窗口没换到更好的切点：%r" % tails)
        old = [part.text.rstrip()[-1] for part in split(OVERLONG, narrow).parts]
        self.assertIn(RULES["comma"][0], old, "窗口关掉后本该退回逗号切点：%r" % old)
        self.assertEqual(OVERLONG, "".join(_texts(batch)))

    # —— 一句一条：每泡最多两句 ——————————————————————————

    def test_a_bare_action_sentence_stands_alone(self):
        """验收那句 → 3 泡：动作句自己一泡，其余两句一泡，一泡都不超过两句。

        「尾巴甩了一下。」没有括号。只认括号就会切成 2 泡、把动作和台词挤在一条里，所以无括号那
        一种形态是这条验收的必要条件，不是补充。
        """
        batch = split(ACCEPT_DIALOGUE, POLICY)
        self.assertEqual(["……谁肥了。", "尾巴甩了一下。", "蓝鲸才叫肥。我这是正常体型。"], _texts(batch))
        self.assertLessEqual(len(batch.parts), RULES["max_parts"])
        for index, part in enumerate(batch.parts, 1):
            self.assertLessEqual(_sentences_in(part.text), RULES["sentences_per_part"],
                                 "第 %d 泡超过两句：%r" % (index, part.text))

    def test_a_bracketed_action_sentence_stands_alone(self):
        """括号形态一样单独成泡：整句就是一个括注，括号对借自 action_note.json。"""
        batch = split(ACCEPT_BRACKET, POLICY)
        self.assertEqual(["……谁肥了。", "（尾巴甩了一下）。", "蓝鲸才叫肥。我这是正常体型。"], _texts(batch))

    def test_eight_sentences_make_four_parts(self):
        """120 字 8 句 → 4 泡（泡数 = 句数 ÷ 2），每泡两句，且不超过防炸上限。"""
        self.assertEqual(120, len(EIGHT), "样例不是 120 字，下面的结论就不作数")
        self.assertEqual(8, _sentences_in(EIGHT))
        batch = split(EIGHT, POLICY)
        self.assertEqual(4, len(batch.parts), _texts(batch))
        self.assertLessEqual(len(batch.parts), RULES["max_parts"])
        for index, part in enumerate(batch.parts, 1):
            self.assertEqual(RULES["sentences_per_part"], _sentences_in(part.text),
                             "第 %d 泡不是两句：%r" % (index, part.text))
        self.assertEqual(EIGHT, "".join(_texts(batch)))

    # —— 动作行单独成条 ——————————————————————————————

    def test_pronoun_keeps_a_sentence_out_of_action_lines(self):
        """「我这是正常体型。」含人称代词：它是台词，不许当动作行自成一泡。"""
        batch = split("……谁肥了。我这是正常体型。", POLICY)
        self.assertEqual(["……谁肥了。我这是正常体型。"], _texts(batch))

    def test_body_part_sentence_is_an_action_line(self):
        """「尾巴垂下去。」以身体部位开头、无人称代词、不长 → 自成一泡。"""
        batch = split("……谁肥了。尾巴垂下去。蓝鲸才叫肥。", POLICY)
        self.assertEqual(["……谁肥了。", "尾巴垂下去。", "蓝鲸才叫肥。"], _texts(batch))

    def test_action_tables_are_borrowed_not_rebuilt(self):
        """括号对与身体部位来自 action_note.json：把那张表换空，同一句就不再自成一泡。

        表若在 split.py 里另造过一份，换数据就不会改变行为——这一条就是「别另造」的活证据。
        """
        self.assertTrue(TABLE["body_parts"])
        self.assertTrue(TABLE["brackets"])
        batch = split(ACCEPT_DIALOGUE, POLICY, {"body_parts": [], "brackets": []})
        self.assertEqual(["……谁肥了。尾巴甩了一下。", "蓝鲸才叫肥。我这是正常体型。"], _texts(batch))

    def test_action_part_gap_stays_inside_the_clamp(self):
        """动作那一泡的间隔仍由 pace.fill 给，落在 policy 的夹取范围 [min_gap, max_gap] 里。"""
        batch = fill(split(ACCEPT_DIALOGUE, POLICY), POLICY, "reply-action")
        self.assertEqual(0.0, batch.parts[0].gap_before)
        for index, part in enumerate(batch.parts[1:], 2):
            self.assertGreaterEqual(part.gap_before, PACE["min_gap"],
                                    "第 %d 泡间隔 %r 掉到下限外" % (index, part.gap_before))
            self.assertLessEqual(part.gap_before, PACE["max_gap"],
                                 "第 %d 泡间隔 %r 顶破上限" % (index, part.gap_before))
        self.assertEqual(_texts(split(ACCEPT_DIALOGUE, POLICY)), _texts(batch),
                         "pace.fill 动了字")

    def test_smaller_action_line_cap_drops_the_action_line(self):
        """临时目录把 action_line_max_chars 调小 → 原本算动作行的那句不再单独成泡（改数据即改行为）。"""
        small = _policy_with(action_line_max_chars=1)
        batch = split(ACCEPT_DIALOGUE, small)
        self.assertEqual(["……谁肥了。尾巴甩了一下。", "蓝鲸才叫肥。我这是正常体型。"], _texts(batch))
        self.assertIn("尾巴甩了一下。", _texts(split(ACCEPT_DIALOGUE, POLICY)),
                      "默认数据下这句本来是动作行，两边一比才说明是那个上限在起作用")

    def test_sentences_per_part_lives_in_data(self):
        """每泡几句来自 policy：调成 1 就一句一泡，调成 3 就三句一泡。"""
        one = split(PARAGRAPH, _policy_with(sentences_per_part=1))
        self.assertEqual(_sentences_in(PARAGRAPH), len(one.parts), _texts(one))
        for index, part in enumerate(one.parts, 1):
            self.assertEqual(1, _sentences_in(part.text), "第 %d 泡不是一句：%r" % (index, part.text))
        for part in split(PARAGRAPH, _policy_with(sentences_per_part=3)).parts:
            self.assertLessEqual(_sentences_in(part.text), 3)

    def test_pronoun_table_lives_in_data(self):
        """人称代词表也来自数据：把表清空，「尾巴是我的。」就从台词变成动作行。"""
        text = "……谁肥了。尾巴是我的。蓝鲸才叫肥。"
        self.assertEqual(["……谁肥了。尾巴是我的。", "蓝鲸才叫肥。"], _texts(split(text, POLICY)))
        loose = split(text, _policy_with(action_pronouns=""))
        self.assertEqual(["……谁肥了。", "尾巴是我的。", "蓝鲸才叫肥。"], _texts(loose))

    def test_sentence_and_action_thresholds_live_in_data(self):
        """句数上限、动作行长度上限、人称代词、退役说明都在 policy 的 split 节里。"""
        for name in ("sentences_per_part", "action_line_max_chars"):
            self.assertIsInstance(RULES[name], int, "政策缺数值键：%r" % name)
            self.assertGreater(RULES[name], 0)
        self.assertIsInstance(RULES["action_pronouns"], str, "人称代词表必须在数据里")
        self.assertTrue(RULES["action_pronouns"].strip())
        self.assertIn("retired_note", RULES, "旧值退役要留一句说明，旧键不许删")
        self.assertGreater(RULES["max_parts"], RULES["sentences_per_part"],
                           "防炸上限该比每泡句数大，否则等于没退位")
        for old in ("max_chars", "max_parts", "cut_window_chars", "cut_preference"):
            self.assertIn(old, RULES, "旧键不许删：%r" % old)

    def test_max_parts_is_a_blow_up_guard(self):
        """泡数超过 max_parts 才合并：调成 1，整段并成一个泡，一个字都不丢。"""
        batch = split(PARAGRAPH, _policy_with(max_parts=1))
        self.assertEqual(1, len(batch.parts), [len(part.text) for part in batch.parts])
        self.assertEqual(PARAGRAPH, batch.parts[0].text)

    def test_a_run_of_pauses_is_not_its_own_part(self):
        """「……」不单独成泡：纯停顿跟着邻居走，一个字都不丢。"""
        text = "谁肥了。……。蓝鲸才叫肥。"
        batch = split(text, POLICY)
        for index, part in enumerate(batch.parts, 1):
            self.assertTrue(any(char not in RULES["sentence_end"] for char in part.text),
                            "第 %d 泡只有标点：%r" % (index, part.text))
        self.assertEqual(text, "".join(_texts(batch)))


if __name__ == "__main__":
    unittest.main()
