"""口癖账（organs/mind/usage.py）的验收：去标点、取开头、判命中、落账、窗口读数、三条边界。

判据：
1. **normalize 只去标点与空白**：unicodedata.category 以 P/Z 开头的那类字符去掉，
   正文一个字都不改（不替换、不改写）；全是标点的一张卡归一成空串。
2. **opening 是去标点后的前 N 个字**：N 来自数据（anchors.guard.opening_chars）。
3. **命中按 phrase 判、认前缀不认整句**：连标点都算上就永远对不上——两边都先归一。
   前缀几个字由调用方给（anchors.guard.prefix_chars），所以「行了行」命中、「行了我」不命中
   ——这条差异写在断言旁边，不是漏了。全标点的 phrase 归一成空前缀，**不许**匹配一切
   （空前缀是假命中）。**example 不参与判命中**：老卡没有 phrase 时读数这一侧退回 say 的前缀，
   命中的那一笔记空 phrase。
4. **append / read**：一行一轮、只追加；读时只取最近 window 行；坏行跳过不炸。
   每行带 id 与 phrase（近轮禁用按 id 数次数）；没命中的一行两栏都是空串。
5. **repeats**：只数最近 window 轮里 hit 且有 id 的行；老行没有 id 就当没命中过。
6. **report 的三条边界**：rounds < min_rounds 不 over（防小样本）；rate == threshold
   不 over（「超过」才是超）；rate > threshold 才 over，降档后 injected 是 reduced_count。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.mind import usage

#: 五张现行台词卡。形状照 organs.home.anchors.read_all 给的现成样子：phrase（≤4 字，进提示词）
#: + example（整句例句，不进提示词）+ when（语境提示，不是触发条件），外加老字段账本那一侧还在用的
#: scene（读侧给的兼容别名，等于 when）。
FIVE = [
    {"id": "被夸", "phrase": "才知道啊", "example": "哼，才知道啊。",
     "when": "被夸，且不想直接承认高兴", "scene": "被夸，且不想直接承认高兴"},
    {"id": "被关心", "phrase": "行了行了", "example": "行了行了，我又不是玻璃做的。",
     "when": "被关心，且不想被当成弱者", "scene": "被关心，且不想被当成弱者"},
    {"id": "不想聊", "phrase": "以后再说", "example": "这个啊……以后再说吧。",
     "when": "不想继续聊，但要留门", "scene": "不想继续聊，但要留门"},
    {"id": "认错", "phrase": "记岔了", "example": "……是我记岔了。",
     "when": "发现自己记错了", "scene": "发现自己记错了"},
    {"id": "你要走", "phrase": "我在这儿", "example": "你忙你的，我在这儿。",
     "when": "他要走或要忙", "scene": "他要走或要忙"},
]
GUARD = {"window": 50, "threshold": 0.3, "opening_chars": 6, "prefix_chars": 3,
         "reduced_count": 2, "min_rounds": 20}
REPORT_KEYS = {"window", "threshold", "min_rounds", "rounds", "hits", "rate",
               "over", "injected", "available"}


class UsageTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _seed(self, count: int, hits: int) -> None:
        """造 count 轮账，最后 hits 轮命中（窗口只取末尾，命中放哪儿都一样）。"""
        for index in range(count):
            hit = index >= count - hits
            usage.append(hit, "这个啊又来了", "不想聊" if hit else "", root=self.root)


class NormalizeAndOpening(UsageTestBase):
    def test_punctuation_and_spaces_are_dropped_verbatim(self):
        self.assertEqual("是我回晚了", usage.normalize("……是我，回晚了。"))
        self.assertEqual("这个啊又来了", usage.normalize(" 这个啊 又来了 "))
        self.assertEqual("", usage.normalize("……——？！"))
        self.assertEqual("", usage.normalize(None))
        self.assertEqual("abc", usage.normalize("abc"), "没有标点就一个字都不许动")

    def test_control_whitespace_is_stripped_too(self):
        """\t 与 \n 的 category 是 Cc：说话格式要求「用换行分隔」，开头带换行不该算没命中。

        所以口径是 P/Z/C 三族一起去掉。这一条钉住口径本身，
        免得有人顺手换成 isspace() 或加一张空白词表而没人发现。
        """
        self.assertEqual("这个啊又来了", usage.normalize("这个啊\t又来了\n"))

    def test_opening_is_the_first_chars_after_normalizing(self):
        self.assertEqual("这个啊又来了", usage.opening("这个啊，又来了。", 6))
        self.assertEqual("这个啊", usage.opening("这个啊，又来了。", 3))
        self.assertEqual("", usage.opening("这个啊，又来了。", 0), "0 个字就是空串，不是整句")
        self.assertEqual("是我回晚了", usage.opening("……是我，回晚了。", 99), "不够长就全给")


class MatchUsesThePrefix(UsageTestBase):
    def test_prefix_length_is_the_callers_choice(self):
        """同一个 phrase、两个前缀长度：「行了行」命中、「行了我」不命中。

        前缀几个字进数据（anchors.guard.prefix_chars），所以这里按 3 断言、按 3 反证另一头——
        判据本身只有一条：去标点后以 phrase 的前 N 个字开头就算命中。
        """
        self.assertEqual(True, usage.match("行了行了，我没事。", FIVE, 3)[0], "前三个字对上了")
        self.assertEqual((False, ""), usage.match("行了我没事。", FIVE, 3),
                         "第三个字是「了」不是「行」，按 3 字前缀不该命中")

    def test_card_id_and_phrase_come_back_with_the_card(self):
        """命中一律按 phrase 判：带上这一笔是**哪张卡**（id）与**哪句口癖**（phrase）。"""
        self.assertEqual((True, "不想聊", "以后再说"),
                         usage.match("以后再说吧，现在不行。", FIVE, 3, with_card=True))
        self.assertEqual((True, FIVE[2]["scene"]), usage.match("以后再说吧，现在不行。", FIVE, 3),
                         "老形状（两元组）仍是 scene：bench 的 C 轨按两个变量接")

    def test_ordinary_sentence_hits_nothing(self):
        self.assertEqual((False, ""), usage.match("今天挺好的，你问医院那回事。", FIVE, 3))
        self.assertEqual((False, ""), usage.match("", FIVE, 3))
        self.assertEqual((False, ""), usage.match("以后再说吧", [], 3), "没有台词就没有命中")
        self.assertEqual((False, "", ""), usage.match("以后再说吧", [], 3, with_card=True))

    def test_the_whole_example_is_not_a_phrase_hit(self):
        """整句例句不是口癖：句子里有 phrase、但不是以它开头 → 不算命中（那是照抄审计那一档）。"""
        self.assertIsNone(usage.hit("哼，才知道啊。", FIVE, 3))
        self.assertIsNone(usage.hit("这个啊……以后再说吧。", FIVE, 3))

    def test_all_punctuation_phrase_never_matches_everything(self):
        """一条全是标点的 phrase 归一成空前缀：空前缀会匹配一切，那是假命中，必须跳过。"""
        hollow = [{"id": "空的", "phrase": "……——", "example": "……——"},
                  {"id": "被夸", "phrase": "才知道啊", "example": "哼，才知道啊。"}]
        self.assertIsNone(usage.hit("今天挺好的。", hollow, 3))
        self.assertEqual("被夸", usage.hit("才知道什么呀。", hollow, 3)["id"],
                         "空 phrase 不该把后面那条真台词挡掉")

    def test_a_legacy_card_without_phrase_still_reads(self):
        """老卡（只有 say）读数这一侧退回 say 的前缀：账要看得见，但那一笔不算口癖命中。"""
        legacy = [{"id": "被夸", "say": "哼，才知道啊。", "scene": "被夸这个场合"}]
        self.assertEqual((True, "被夸这个场合"), usage.match("哼，才知道", legacy, 3))
        self.assertEqual((True, "被夸", ""), usage.match("哼，才知道", legacy, 3, with_card=True),
                         "老句不是口癖：命中的那一笔 phrase 记空")


class AppendAndRead(UsageTestBase):
    def test_append_writes_one_line_and_read_slices_the_window(self):
        row = usage.append(True, "以后再说", "不想聊", root=self.root,
                           card_id="不想聊", phrase="以后再说")
        self.assertEqual({"ts", "hit", "opening", "scene", "id", "phrase"}, set(row),
                         "一行一轮的那份形状：多出来的 id / phrase 是近轮禁用要数的两栏")
        self.assertEqual("不想聊", row["scene"])
        self.assertEqual("不想聊", row["id"], "命中要记是哪一张卡（按 id 数次数）")
        self.assertEqual("以后再说", row["phrase"], "命中要记是哪一句口癖")
        self.assertTrue(row["ts"], "ts 要用器官自己的钟给出一个时刻")

        self._seed(4, 2)                                     # 再加 4 行，共 5 行
        whole = usage.read(root=self.root)
        self.assertEqual(5, whole["rounds"])
        self.assertEqual(3, whole["hits"], "第一行 + 末尾两行命中")
        self.assertAlmostEqual(0.6, whole["rate"])
        window = usage.read(root=self.root, window=2)
        self.assertEqual(2, window["rounds"])
        self.assertEqual(2, window["hits"])
        self.assertEqual(1.0, window["rate"])
        self.assertEqual(["不想聊", "不想聊"], [row["scene"] for row in window["rows"]])
        self.assertEqual(0, usage.read(root=self.root, window=0)["rounds"], "window=0 一行都不算")

    def test_bad_lines_are_skipped_not_fatal(self):
        self._seed(1, 1)
        path = usage.path_of(self.root)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("这不是 JSON\n")
            handle.write("[1, 2, 3]\n")
            handle.write("\n")
        usage.append(False, "", "", root=self.root)
        got = usage.read(root=self.root)
        self.assertEqual(2, got["rounds"], "坏行被当成了正常一轮：%r" % (got["rows"],))
        self.assertEqual(1, got["hits"])

    def test_empty_ledger_is_zero_not_a_division_by_zero(self):
        got = usage.read(root=self.root / "nope")
        self.assertEqual({"rounds": 0, "hits": 0, "rate": 0.0, "rows": []}, got)


class ReportBoundaries(UsageTestBase):
    def test_short_history_never_trips_the_guard(self):
        """rounds < min_rounds 不 over：刚开口两轮都用了锚句，不该立刻降档。"""
        self._seed(5, 5)
        got = usage.report(GUARD, injected_available=5, root=self.root)
        self.assertEqual(5, got["rounds"])
        self.assertEqual(1.0, got["rate"])
        self.assertFalse(got["over"], "5 轮全命中就降档了：min_rounds 没起作用")
        self.assertEqual(5, got["injected"], "没 over 时该全给")

    def test_rate_equal_to_threshold_is_not_over(self):
        """rate == threshold 不 over：数据说的是「超过」阈值，不是「达到」。"""
        self._seed(10, 3)
        guard = dict(GUARD, window=10, min_rounds=5)
        got = usage.report(guard, injected_available=5, root=self.root)
        self.assertEqual(0.3, got["rate"])
        self.assertFalse(got["over"], "3/10 == 0.3 就被判超了阈值")

    def test_rate_over_threshold_downgrades_and_reports_reduced_count(self):
        self._seed(10, 4)
        guard = dict(GUARD, window=10, min_rounds=5)
        got = usage.report(guard, injected_available=5, root=self.root)
        self.assertEqual(0.4, got["rate"])
        self.assertTrue(got["over"])
        self.assertEqual(2, got["injected"], "降档后该只注入 reduced_count 条")
        self.assertEqual(5, got["available"])

    def test_window_trims_and_the_eight_keys_are_frozen(self):
        self._seed(60, 60)
        got = usage.report(GUARD, injected_available=5, root=self.root)
        self.assertEqual(REPORT_KEYS, set(got), "report 的键是对外冻结的那一份：%s" % sorted(got))
        self.assertEqual(50, got["window"])
        self.assertEqual(50, got["rounds"], "窗口只算最近 50 轮")
        self.assertEqual(50, got["hits"])
        self.assertEqual(1.0, got["rate"])
        self.assertTrue(got["over"])
        self.assertEqual(2, got["injected"])
        self.assertEqual(20, got["min_rounds"])
        self.assertEqual(0.3, got["threshold"])


if __name__ == "__main__":
    unittest.main()

class RepeatsTest(UsageTestBase):
    """近轮禁用要的那一份读数：最近 window 轮里每张卡（按 id）各命中过几次。"""

    def test_counts_hits_by_card_id_inside_the_window(self):
        for _ in range(2):
            usage.append(True, "才知道什么呀", "被夸", root=self.root,
                         card_id="被夸", phrase="才知道啊")
        usage.append(True, "以后再说吧", "不想聊", root=self.root,
                     card_id="不想聊", phrase="以后再说")
        usage.append(False, "今天挺好的", "", root=self.root)
        self.assertEqual({"被夸": 2, "不想聊": 1}, usage.repeats(12, root=self.root))

    def test_rows_without_id_are_not_counted(self):
        """老行读不到 id：当它没命中过。拿 when／scene 猜一个 id，会把一张从没出现过的卡禁掉。"""
        usage.append(True, "这个啊又来了", "不想聊", root=self.root)        # 老形状：没有 id
        path = usage.path_of(self.root)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": "t", "hit": True, "opening": "哼才知道",
                                     "scene": "被夸"}, ensure_ascii=False) + "\n")
        self.assertEqual({}, usage.repeats(12, root=self.root))

    def test_outside_the_window_is_not_counted(self):
        usage.append(True, "才知道什么呀", "被夸", root=self.root,
                     card_id="被夸", phrase="才知道啊")
        usage.append(False, "嗯", "", root=self.root)
        usage.append(False, "嗯", "", root=self.root)
        self.assertEqual({}, usage.repeats(2, root=self.root), "窗口只算最近 2 轮")
        self.assertEqual({"被夸": 1}, usage.repeats(3, root=self.root))
        self.assertEqual({}, usage.repeats(0, root=self.root), "window=0 一行都不算")


class LeadingWhitespaceTest(unittest.TestCase):
    """说话格式要求「用换行分隔」：开头多一个换行，不该把命中判成没命中。"""

    def test_leading_newline_still_matches(self):
        anchors = [{"id": "被夸", "phrase": "才知道啊", "example": "哼，才知道啊。",
                    "scene": "被夸这个场合"}]
        self.assertEqual((True, "被夸这个场合"), usage.match(chr(10) + "才知道……", anchors, 3),
                         "开头换行把命中吞掉了")


if __name__ == "__main__":
    unittest.main()

