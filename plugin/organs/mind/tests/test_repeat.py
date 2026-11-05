"""近轮禁用的纯函数验收：动作行怎么认、重复几遍怎么数、开场取几个字、只抽她那一侧。

判据（每条对应交付里的一句话）：
1. **动作行只认整行圆括号**：中文全角（）与半角()都算，原文照搬；夹在句子里的括号不算，
   只有半边括号的也不算。
2. **去重计次**：同一个动作行出现三次只留一条、标 3；顺序按第一次出现。
3. **开场取 6 个字**：去标点、去动作行之后按 usage.opening 的口径截断——不另写一套去标点。
4. **只抽她那一侧**：said 是人的，一个字都不许进来；若某行 who 写成她的自称，那一行的
   said 才算她的。
5. **窗口只取最近 N 轮**：更早那轮的动作行不在禁令里。
6. **配额数的是轮数**：最近 N 轮里几轮带动作（一轮两行仍是一轮），窗口外那轮不算，
   也只数她那一侧。
7. **删动作行只删整行那几行**（连着它后面多余的空行）：删完是空就留原文并报「留了原文」，
   夹在句子里的括号与正文一个字节都不动。

全部是纯函数：不碰盘、不读 assembly.json，配置以参数传进来。
"""
from __future__ import annotations

import unittest

from organs.mind import repeat, usage

ACTION = "（尾巴尖在水里慢慢蜷了一下）"
OTHER = "(鳍抖了一下)"
HUMAN_ACTION = "（人比划了一下）"
HER_NAME = "知汐"
CONF = {"rounds": 3, "opening_chars": 6, "note": "这一轮不许再用下面这些"}


def _row(said: str, reply: str, who: str = "人") -> dict:
    return {"ts": "", "who": who, "said": said, "reply": reply}


class ActionLinesTest(unittest.TestCase):
    def test_whole_line_parens_count_in_both_widths(self):
        text = "%s\n\n……这个啊。\n\n%s\n\n鱼在海里游着。" % (ACTION, OTHER)
        self.assertEqual([ACTION, OTHER], repeat.action_lines(text),
                         "整行的圆括号（全角与半角）都该算动作行，且原文照搬")

    def test_paren_inside_a_sentence_is_not_an_action_line(self):
        for text in ("她（笑了）了一下", "（只包住半句）后面还有字", "前面有字（只包住后半句）",
                     "（", "）", ""):
            self.assertEqual([], repeat.action_lines(text), "不是整行的括号被当成了动作行：%r" % text)

    def test_only_the_whole_line_is_kept_around_blank_lines(self):
        text = "  %s  \n\n正文" % ACTION
        self.assertEqual([ACTION], repeat.action_lines(text), "带空白的整行该按 strip 之后认")


class TallyTest(unittest.TestCase):
    def test_duplicates_collapse_and_are_counted(self):
        self.assertEqual([(ACTION, 3)], repeat.tally([ACTION, ACTION, ACTION]))

    def test_first_seen_order_is_kept(self):
        self.assertEqual([("乙", 1), ("甲", 2)], repeat.tally(["乙", "甲", "甲"]))


class BannedTest(unittest.TestCase):
    def test_repeated_action_line_appears_once_with_its_count(self):
        rows = [_row("你还记得鱼吗", "%s\n\n……这个啊。" % ACTION),
                _row("有过吗", "%s\n\n……是。有过。" % ACTION),
                _row("嗯", "%s\n\n……是。有过。" % ACTION)]
        got = repeat.banned(rows, CONF, HER_NAME)
        self.assertEqual(3, got["rounds"])
        self.assertEqual([{"text": ACTION, "count": 3}], got["action_lines"],
                         "三条一样的动作行该只留一条并标 3 次")

    def test_opening_takes_six_chars_after_punctuation(self):
        reply = "（尾巴尖在水里慢慢蜷了一下）\n\n……这个啊。\n\n鱼在海里游着。"
        got = repeat.banned([_row("问", reply)], CONF, HER_NAME)
        want = usage.opening(repeat.without_actions(reply), CONF["opening_chars"])
        self.assertEqual([want], got["openings"], "开场的口径必须与 usage.opening 一致")
        self.assertEqual(6, len(want), "开场该是 6 个字")
        self.assertNotIn("（", want, "开场没去标点")

    def test_opening_is_not_the_action_line_again(self):
        reply = "（尾巴尖在水里慢慢蜷了一下）\n\n……是。有过。\n\n鱼游着游着就散了。"
        got = repeat.banned([_row("问", reply)], CONF, HER_NAME)
        self.assertEqual([ACTION], [row["text"] for row in got["action_lines"]])
        for head in got["openings"]:
            self.assertNotIn("尾巴", head, "开场又是那个动作行：禁令里同一句话出现了两遍")

    def test_only_her_side_comes_in(self):
        rows = [_row(HUMAN_ACTION, "%s\n\n……这个啊。" % ACTION, who="人")]
        got = repeat.banned(rows, CONF, HER_NAME)
        text = str(got)
        self.assertNotIn("人比划", text, "人的话进了禁令：%r" % (got,))
        self.assertEqual([{"text": ACTION, "count": 1}], got["action_lines"])

    def test_a_row_written_under_her_own_name_counts_as_hers(self):
        rows = [_row("（她自己那一侧）", "……嗯。", who=HER_NAME)]
        got = repeat.banned(rows, CONF, HER_NAME)
        self.assertIn("（她自己那一侧）", [row["text"] for row in got["action_lines"]],
                      "who 写成她自称的那一行，said 也是她说的")

    def test_window_takes_only_the_latest_rounds(self):
        rows = [_row("问0", "(最早的那一轮)"), _row("问1", "%s\n\n一。" % ACTION),
                _row("问2", "%s\n\n二。" % ACTION), _row("问3", "%s\n\n三。" % ACTION)]
        got = repeat.banned(rows, CONF, HER_NAME)
        self.assertEqual([{"text": ACTION, "count": 3}], got["action_lines"],
                         "最近 3 轮以外的动作行不该进禁令：%r" % (got,))
        self.assertNotIn("(最早的那一轮)", str(got))

    def test_without_actions_drops_whole_lines_only(self):
        text = "正文（不是动作行）\n%s\n尾巴尖在水里" % ACTION
        self.assertEqual("正文（不是动作行）\n尾巴尖在水里", repeat.without_actions(text))


class CountWithActionsTest(unittest.TestCase):
    """动作配额要的是「几轮带动作」，不是「几条动作行」——两条判据差得最远。"""

    WINDOW = 6

    def test_window_keeps_only_the_latest_rounds(self):
        """6 轮里 1 条动作 → 1；同一条动作落在第 7 轮（窗口外）→ 0。"""
        inside = [_row("问%d" % index, "……嗯。") for index in range(self.WINDOW - 1)]
        inside.append(_row("问最后", "%s\n\n……这个啊。" % ACTION))
        self.assertEqual(1, repeat.count_with_actions(inside, self.WINDOW, HER_NAME),
                         "最近 6 轮里那一条动作没数进来")

        outside = [_row("问最早", "%s\n\n……这个啊。" % ACTION)]
        outside.extend(_row("问%d" % index, "……嗯。") for index in range(self.WINDOW))
        self.assertEqual(0, repeat.count_with_actions(outside, self.WINDOW, HER_NAME),
                         "第 7 轮（窗口外）那一条动作被数进来了")

    def test_two_action_lines_in_one_reply_are_still_one_round(self):
        rows = [_row("问", "%s\n\n%s\n\n……这个啊。" % (ACTION, OTHER)),
                _row("又问", "……嗯。")]
        self.assertEqual(1, repeat.count_with_actions(rows, self.WINDOW, HER_NAME),
                         "同一条回复里两行动作被算成了两轮")

    def test_only_her_side_is_counted(self):
        rows = [_row(HUMAN_ACTION, "……嗯。", who="人")]
        self.assertEqual(0, repeat.count_with_actions(rows, self.WINDOW, HER_NAME),
                         "人的动作行算进了她那一侧")
        rows = [_row("（她自己那一侧）", "……嗯。", who=HER_NAME)]
        self.assertEqual(1, repeat.count_with_actions(rows, self.WINDOW, HER_NAME),
                         "who 写成她自称的那一行，said 也是她说的")

    def test_no_history_and_no_window_are_zero(self):
        for turns, window in (([], self.WINDOW), (None, self.WINDOW),
                              ([_row("问", "%s\n\n一。" % ACTION)], 0)):
            self.assertEqual(0, repeat.count_with_actions(turns, window, HER_NAME),
                             "没有历史 / 窗口为 0 却数出了动作：%r" % (turns,))


class StripActionLinesTest(unittest.TestCase):
    """配额满了之后的硬兜底：删的是整行那几行，不碰正文。"""

    def test_whole_lines_and_the_blank_run_after_them_go(self):
        text = "正文第一句。\n\n%s\n\n尾巴尖还在水里。" % ACTION
        got = repeat.strip_action_lines(text)
        self.assertEqual(("正文第一句。\n\n尾巴尖还在水里。", 1, False), got)

    def test_an_action_line_as_the_first_line_leaves_no_leading_gap(self):
        self.assertEqual(("正文。", 1, False), repeat.strip_action_lines("%s\n\n正文。" % ACTION))

    def test_every_withdrawn_line_is_counted(self):
        text = "%s\n\n正文。\n\n%s" % (ACTION, OTHER)
        clean, count, kept = repeat.strip_action_lines(text)
        self.assertEqual(("正文。", 2, False), (clean, count, kept), "删了两行却只报了行数以外的东西")

    def test_an_all_action_reply_is_kept_whole(self):
        clean, count, kept = repeat.strip_action_lines(ACTION)
        self.assertEqual((ACTION, 0, True), (clean, count, kept),
                         "整轮只有一个动作行时该保留原文并报「留了原文」")

    def test_parens_inside_a_sentence_are_untouched(self):
        text = "她（笑了）一下。"
        self.assertEqual((text, 0, False), repeat.strip_action_lines(text))


if __name__ == "__main__":
    unittest.main()
