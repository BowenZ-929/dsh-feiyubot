"""口癖账（organs/mind/usage.py）的验收：去标点、取开头、判命中、落账、窗口读数、三条边界。

判据：
1. **normalize 只去标点与空白**：unicodedata.category 以 P/Z 开头的那类字符去掉，
   正文一个字都不改（不替换、不改写）；全是标点的一张卡归一成空串。
2. **opening 是去标点后的前 N 个字**：N 来自数据（anchors.guard.opening_chars）。
3. **match 认前缀不认整句**：连标点都算上就永远对不上——两边都先归一。
   「……是我，回晚了」对「……是我记岔了」在 prefix_chars=2 时命中（第 3 个字就分岔：
   回 / 记，所以按默认的 3 不命中——这条差异写在断言旁边，不是漏了）；
   「这个啊，又来了」对「这个啊……以后再说吧」在 prefix_chars=3 时命中；正常句子不命中。
   全标点的台词归一成空前缀，**不许**匹配一切（空前缀是假命中）。
4. **append / read**：一行一轮、只追加；读时只取最近 window 行；坏行跳过不炸。
5. **report 的三条边界**：rounds < min_rounds 不 over（防小样本）；rate == threshold
   不 over（「超过」才是超）；rate > threshold 才 over，降档后 injected 是 reduced_count。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from organs.mind import usage

FIVE = [
    {"scene": "被夸", "say": "哼，才知道啊。", "body": "尾巴翘起来晃了两下"},
    {"scene": "被关心", "say": "行了行了，我又不是玻璃做的。", "body": "别开头、脸微微发红"},
    {"scene": "不想聊", "say": "这个啊……以后再说吧。", "body": ""},
    {"scene": "认错", "say": "……是我记岔了。", "body": "低头、尾巴垂下去不敢动"},
    {"scene": "你要走", "say": "你忙你的，我在这儿。", "body": "尾巴慢慢摆一下，乖巧的笑了笑"},
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
    def test_sorry_pair_hits_at_two_chars(self):
        """「……是我，回晚了」对「……是我记岔了」：去标点后头两个字相同即命中。

        默认的 prefix_chars=3 在这一对上**不**命中——第 3 个字是「回」与「记」，
        这正是前缀长度进数据（人能改）的意义，所以这里按 2 断言、按 3 反证。
        """
        self.assertEqual((True, "认错"), usage.match("……是我，回晚了。", FIVE, 2))
        self.assertEqual((False, ""), usage.match("……是我，回晚了。", FIVE, 3))

    def test_this_one_pair_hits_at_three_chars(self):
        self.assertEqual((True, "不想聊"), usage.match("这个啊，又来了。", FIVE, 3))

    def test_ordinary_sentence_hits_nothing(self):
        self.assertEqual((False, ""), usage.match("今天挺好的，你问医院那回事。", FIVE, 3))
        self.assertEqual((False, ""), usage.match("", FIVE, 3))
        self.assertEqual((False, ""), usage.match("这个啊又来了", [], 3), "没有台词就没有命中")

    def test_all_punctuation_line_never_matches_everything(self):
        """一条全是标点的台词归一成空前缀：空前缀会匹配一切，那是假命中，必须跳过。"""
        hollow = [{"scene": "空的", "say": "……——", "body": ""},
                  {"scene": "被夸", "say": "哼，才知道啊。", "body": ""}]
        self.assertEqual((False, ""), usage.match("今天挺好的。", hollow, 3))
        self.assertEqual((True, "被夸"), usage.match("哼，才知道啊。", hollow, 3),
                         "空台词不该把后面那条真台词挡掉")


class AppendAndRead(UsageTestBase):
    def test_append_writes_one_line_and_read_slices_the_window(self):
        row = usage.append(True, "这个啊又来了", "不想聊", root=self.root)
        self.assertEqual({"ts", "hit", "opening", "scene"}, set(row))
        self.assertEqual("不想聊", row["scene"])
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

class LeadingWhitespaceTest(unittest.TestCase):
    """说话格式要求「用换行分隔」：开头多一个换行，不该把命中判成没命中。"""

    def test_leading_newline_still_matches(self):
        anchors = [{"say": "哼，才知道啊。", "scene": "被夸"}]
        hit, scene = usage.match(chr(10) + "哼，才知……", anchors, 3)
        self.assertTrue(hit, "开头换行把命中吞掉了")
        self.assertEqual("被夸", scene)


if __name__ == "__main__":
    unittest.main()

