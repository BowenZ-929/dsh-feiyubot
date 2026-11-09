"""复述审计：判「照搬」的那半个闸。

阈值、窗口、词表、说法一句都不抄在这份文件里：全部从 organs/mouth/data/audit.json 读（走
warehouse），要改判据就改数据。下面出现的几段素材是测试自己的材料——魂里那句原文、对方说的
那句话——不是判据本身：判据在数据里，素材只是拿来喂它的。

四条主线：照搬设定类要拦、带标记的原话放行、原话重合只记读数、拿不到注入
原文要如实报 audit-unavailable（不许假装通过）。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from organs.mouth import audit as recitation
from organs.mouth.audit import STATUS_OK, STATUS_UNAVAILABLE, AuditBroken, AuditHit, AuditResult
from organs.mouth.warehouse import load

DATA = load("audit")
THRESHOLD = DATA["threshold"]
WINDOW = DATA["window"]
SOUL = DATA["setting_kinds"][0]           # 设定类第一类就是魂（顺序也写在数据里）
MEMORY = DATA["setting_kinds"][2]         # 记忆原文：拿来验「设定类一律拦」
QUOTES = DATA["quote_kinds"]
QUOTE = QUOTES[0]          # 老断言只用第一类（对方原话）；记忆原文是第二类（它本来就是他说的，可引述）
PLAIN = DATA["plain_kind"]
VERDICT_SETTING = DATA["verdicts"]["setting"]
VERDICT_QUOTE = DATA["verdicts"]["quote_unmarked"]

SKIP = DATA["skip"]                        # 按他说的放过那一笔：名字、记号、说法都在数据里
SKIP_CLAUSE = SKIP["clause_id"]
SKIP_MARK = SKIP["status"]
SKIP_REASON = SKIP["reason"]

MARKER = "你上次说"                        # 引述标记的一个实例；下面先断言它确实在数据里
SOUL_LINE = "嘴上不认，行动先到：拒绝的话说完，事已经做了"
QUOTE_LINE = "你妈住院了吧"


class SoulLineIsRecitation(unittest.TestCase):
    """第 1 条与第 4 条：魂里的原文被照搬回来，必须拦住，而且理由说得清。"""

    def test_verbatim_soul_line_is_stopped(self):
        result = recitation.audit(SOUL_LINE, [{"kind": SOUL, "text": SOUL_LINE}])
        self.assertEqual(STATUS_OK, result.status)
        self.assertIsInstance(result.hit, AuditHit)          # 不是布尔，是一条证据
        self.assertEqual(VERDICT_SETTING, result.hit.kind)    # 违规类别：设定段照搬
        self.assertEqual(SOUL, result.hit.seg_kind)          # 来源段是哪一类
        self.assertIn("嘴上不认", result.hit.matched)          # 撞上的那几个字
        self.assertIn("行动先到", result.hit.matched)
        self.assertGreaterEqual(len(result.hit.matched), THRESHOLD)
        self.assertIn(result.hit.matched, result.hit.reason)   # 人话里点名片段
        self.assertIn(SOUL, result.hit.reason)                 # 人话里点名来源段
        self.assertEqual(len(result.hit.matched), result.lcs_len)
        self.assertEqual(THRESHOLD, result.threshold)

    def test_partial_soul_line_is_stopped(self):
        """第 4 条：只说半句也拦——这半句正是最容易穿帮的。"""
        said = "我嘴上不认，行动先到"
        result = recitation.audit(said, [{"kind": SOUL, "text": SOUL_LINE}])
        self.assertIsNotNone(result.hit)
        self.assertGreaterEqual(result.lcs_len, THRESHOLD)
        self.assertIn("嘴上不认", result.hit.matched)

    def test_marker_cannot_smuggle_setting_text(self):
        """设定类带了引述标记也照拦：自己的东西没有「引用自己」这一说。"""
        said = MARKER + "嘴上不认，行动先到"
        result = recitation.audit(said, [{"kind": SOUL, "text": SOUL_LINE}])
        self.assertIsNotNone(result.hit)
        self.assertEqual(SOUL, result.hit.seg_kind)


class QuotedSpeechRules(unittest.TestCase):
    """原话重合可观测，但字面匹配不承担语义归属判断。"""

    def setUp(self):
        self.assertTrue(MARKER in DATA["quote_markers"], "素材里的标记必须在数据词表里")
        self.inject = [{"kind": QUOTE, "text": QUOTE_LINE}]

    def test_brief_case_passes(self):
        """第 2 条原话那句：撞得不够长（比阈值少一个字），因此不拦。"""
        result = recitation.audit("你上次说你妈住院了", self.inject)
        self.assertEqual(STATUS_OK, result.status)
        self.assertIsNone(result.hit)
        self.assertEqual(THRESHOLD - 1, result.lcs_len)

    def test_conversational_overlap_is_not_proof_of_misattribution(self):
        for kind in QUOTES:
            for source, reply in (
                ("改去松林公园吧，图书馆这次不去了。", "那就改去松林公园。"),
                ("我要一杯无糖乌龙茶。", "一杯无糖乌龙茶，记住了。"),
                ("周六去河岸图书馆。", "周六去河岸图书馆，对吧？"),
            ):
                with self.subTest(kind=kind, reply=reply):
                    result = recitation.audit(reply, [{"kind": kind, "text": source}])
                    self.assertIsNone(result.hit)
                    self.assertGreaterEqual(result.lcs_len, 6)

    def test_conversation_overlap_does_not_hide_later_setting_copy(self):
        result = recitation.audit("那就改去松林公园。" + SOUL_LINE, [
            {"kind": QUOTE, "text": "改去松林公园吧"},
            {"kind": SOUL, "text": SOUL_LINE},
        ])
        self.assertEqual(SOUL, result.hit.seg_kind)

    def test_marked_quote_passes(self):
        """撞够了、标记就在前面 → 放行，并在人话里点名是哪个标记。"""
        result = recitation.audit(MARKER + QUOTE_LINE, self.inject)
        self.assertEqual(STATUS_OK, result.status)
        self.assertIsNone(result.hit)
        self.assertEqual(len(QUOTE_LINE), result.lcs_len)
        self.assertIn(MARKER, result.reason)

    def test_unmarked_quote_records_overlap_without_blocking(self):
        """同字原话本身不足以证明冒充，读数仍保留重合依据。"""
        result = recitation.audit(QUOTE_LINE, self.inject)
        self.assertIsNone(result.hit)
        self.assertIn(QUOTE_LINE, result.reason)
        self.assertIn("不重写或删句", result.reason)

    def test_distant_marker_does_not_turn_overlap_into_a_violation(self):
        """标记太远仍记录为未标记重合，但不将距离当作删句依据。"""
        said = MARKER + "啊" * (WINDOW + 1) + QUOTE_LINE
        result = recitation.audit(said, self.inject)
        self.assertIsNone(result.hit)
        self.assertIn(QUOTE_LINE, result.reason)

    def test_marker_with_short_overlap_passes(self):
        """有标记、但压根没撞够阈值 → 放行（不是每一句带标记的都算复述）。"""
        result = recitation.audit(MARKER + "今天下雨", self.inject)
        self.assertIsNone(result.hit)
        self.assertEqual(STATUS_OK, result.status)


class UnavailableIsNotOk(unittest.TestCase):
    """第 5 条：拿不到注入文本要如实报 audit-unavailable，绝不许返回 ok。"""

    def test_none_is_unavailable(self):
        result = recitation.audit(SOUL_LINE, None)
        self.assertEqual(STATUS_UNAVAILABLE, result.status)
        self.assertNotEqual(STATUS_OK, result.status)     # 「判不了」不是「没问题」
        self.assertIsNone(result.hit)
        self.assertGreater(len(result.reason), 10)        # 有一句人话，不是空串
        self.assertIn(STATUS_UNAVAILABLE, result.reason)  # 人话里点名这个状态
        self.assertEqual(THRESHOLD, result.threshold)

    def test_empty_list_is_unavailable(self):
        self.assertEqual(STATUS_UNAVAILABLE, recitation.audit(SOUL_LINE, []).status)

    def test_blank_string_is_unavailable(self):
        self.assertEqual(STATUS_UNAVAILABLE, recitation.audit(SOUL_LINE, "   ").status)

    def test_default_inject_is_unavailable(self):
        """inject 不给就是这个状态，不是默认通过。"""
        self.assertEqual(STATUS_UNAVAILABLE, recitation.audit(SOUL_LINE).status)


class UnknownKindsAreLoud(unittest.TestCase):
    """第 6 条：未知 kind 当场报错并指名那一个；形状不对也一样响。"""

    def test_unknown_kind_names_it(self):
        with self.assertRaises(AuditBroken) as caught:
            recitation.audit(SOUL_LINE, [{"kind": "没听说过的类别", "text": SOUL_LINE}])
        self.assertIn("没听说过的类别", str(caught.exception))

    def test_missing_kind_is_loud(self):
        with self.assertRaises(AuditBroken) as caught:
            recitation.audit(SOUL_LINE, [{"text": SOUL_LINE}])
        self.assertIn("kind", str(caught.exception))

    def test_missing_text_is_loud(self):
        with self.assertRaises(AuditBroken) as caught:
            recitation.audit(SOUL_LINE, [{"kind": SOUL}])
        self.assertIn("text", str(caught.exception))

    def test_shape_must_be_a_list_or_a_string(self):
        with self.assertRaises(AuditBroken) as caught:
            recitation.audit(SOUL_LINE, 42)
        self.assertIn("inject", str(caught.exception))      # 指名是哪一项
        self.assertIn("int", str(caught.exception))         # 报错里带上读到的形状

    def test_plain_string_counts_as_setting(self):
        """inject 传纯字符串一律按设定类从严：撞上就拦，来源段记成数据里的那一类。"""
        result = recitation.audit(SOUL_LINE, SOUL_LINE)
        self.assertIsNotNone(result.hit)
        self.assertEqual(PLAIN, result.hit.seg_kind)
        self.assertEqual(VERDICT_SETTING, result.hit.kind)


class ThresholdBoundary(unittest.TestCase):
    """第 7 条：恰好差一个字不拦、恰好够就拦；而且算的是连续子串，不是子序列。"""

    def setUp(self):
        self.near = "甲" * THRESHOLD
        self.inject = [{"kind": MEMORY, "text": self.near + "乙丙丁"}]   # 注入段更长，看的是输出那侧

    def test_one_short_of_threshold_passes(self):
        result = recitation.audit("甲" * (THRESHOLD - 1), self.inject)
        self.assertIsNone(result.hit, "比阈值少一个字不许拦")
        self.assertEqual(THRESHOLD - 1, result.lcs_len)

    def test_exactly_threshold_is_stopped(self):
        result = recitation.audit("甲" * THRESHOLD, self.inject)
        self.assertIsNotNone(result.hit, "恰好撞够阈值就该拦")
        self.assertEqual(THRESHOLD, result.lcs_len)

    def test_contiguous_not_subsequence(self):
        """跳着撞字不算复述：只有连着的那一截才计数。"""
        result = recitation.audit("嘴上先到", [{"kind": SOUL, "text": SOUL_LINE}])
        self.assertIsNone(result.hit)
        self.assertEqual(2, result.lcs_len)      # 「嘴上」「先到」各两个字，子序列会是四个
        self.assertLess(result.lcs_len, THRESHOLD)

    def test_common_courtesy_word_is_not_recitation(self):
        """阈值为什么不是 4：常用客套串撞满四个字也不该判复述。"""
        said = "我知道了"
        self.assertLess(len(said), THRESHOLD)
        self.assertIsNone(recitation.audit(said, [{"kind": MEMORY, "text": said}]).hit)

    def test_one_short_never_becomes_a_hit_so_nothing_is_rewritten(self):
        """第 5 条边界：差一个字（阈值 6、连着 5 个）时 lcs_len 就是 5、hit 是 None。

        没有命中，就没有「命中」这一件事交给闸的重写器——所以这一格一次 rewrite 都不会调，
        与「改重写不动」那一格处置正相反（那才删那一句、只剩一句才弃）。阈值为什么是 6：
        data/audit.json 的 threshold_note。
        """
        drop = set(DATA["strip"]) | set(DATA["blank"])
        said = "".join(ch for ch in SOUL_LINE if ch not in drop)[:THRESHOLD - 1]
        self.assertEqual(THRESHOLD - 1, len(said))
        result = recitation.audit(said, [{"kind": SOUL, "text": said + "你别学"}])
        self.assertEqual(STATUS_OK, result.status)
        self.assertEqual(THRESHOLD - 1, result.lcs_len)
        self.assertIsNone(result.hit)
        self.assertEqual(THRESHOLD, result.threshold)


class DataIsTheSourceOfTruth(unittest.TestCase):
    """判据全部来自数据：改一个数，行为跟着变；数据缺键当场响。"""

    def test_threshold_comes_from_policy(self):
        strict = dict(DATA, threshold=len(SOUL_LINE) + 1)
        result = recitation.audit(SOUL_LINE, [{"kind": SOUL, "text": SOUL_LINE}], strict)
        self.assertIsNone(result.hit, "阈值调高以后同一句就不该拦")
        self.assertEqual(len(SOUL_LINE) + 1, result.threshold)

    def test_window_comes_from_policy(self):
        said = MARKER + "啊" * 3 + QUOTE_LINE
        inject = [{"kind": QUOTE, "text": QUOTE_LINE}]
        self.assertIsNone(recitation.audit(said, inject).hit)
        tight = dict(DATA, window=1)                       # 窗口调小到 1，同一句就变没标记
        result = recitation.audit(said, inject, tight)
        self.assertIsNone(result.hit)
        self.assertIn("没有引述标记", result.reason)

    def test_missing_key_is_loud(self):
        with self.assertRaises(AuditBroken) as caught:
            recitation.audit(SOUL_LINE, [{"kind": SOUL, "text": SOUL_LINE}], {})
        self.assertIn("threshold", str(caught.exception))

    def test_quote_kind_cannot_be_a_setting_kind(self):
        # 一类段只能归一边：把某个设定类塞进「可引述」那一族，数据自洽校验必须当场炸。
        overlap = DATA["setting_kinds"][0]
        both = dict(DATA, quote_kinds=list(DATA["quote_kinds"]) + [overlap])
        with self.assertRaises(AuditBroken) as caught:
            recitation.audit(SOUL_LINE, [{"kind": SOUL, "text": SOUL_LINE}], both)
        self.assertIn("setting_kinds", str(caught.exception))


class AskedToReciteIsMarkedNotJudged(unittest.TestCase):
    """对方明说要她重复／引用时，这一关只记账、不判废——但**命中照旧算命中**。

    豁免的是「判不判废」，不是「算不算撞上」：所以 status 仍是 ok、hit 仍带着（面板上看得见她
    引了设定），另加 skipped 与一句取自数据的 skip_reason。为什么留着标记：我们不想让她养成引用
    设定的习惯，只是不在这句上拦。开不开豁免由闸算一次传进来（repeat.requested），本文件不判。
    """

    def setUp(self):
        self.inject = [{"kind": SOUL, "text": SOUL_LINE}]

    def test_the_hit_stays_and_the_skip_is_marked(self):
        result = recitation.audit(SOUL_LINE, self.inject, None, requested=True)
        self.assertEqual(STATUS_OK, result.status)            # 真的比过了，不是没比
        self.assertIsInstance(result.hit, AuditHit)           # 命中照旧带着
        self.assertTrue(result.skipped)
        self.assertEqual(SKIP_CLAUSE, result.skip_clause_id)  # 记账的名字取自数据
        self.assertIn(SKIP_MARK, result.skip_reason)          # 账本靠这个记号认门
        self.assertIn(result.hit.matched, result.skip_reason)  # 人话里点名撞上的那截
        self.assertIn(SOUL, result.skip_reason)               # 也点名来源段那一类

    def test_without_the_request_nothing_is_added(self):
        result = recitation.audit(SOUL_LINE, self.inject, None, requested=False)
        self.assertFalse(result.skipped)
        self.assertEqual("", result.skip_clause_id)
        self.assertEqual("", result.skip_reason)
        self.assertIsInstance(result.hit, AuditHit)

    def test_a_request_without_a_hit_marks_nothing(self):
        """没撞上就没有「判废」可放：不记那一笔，也不加跳过。"""
        result = recitation.audit("今天风大。", self.inject, None, requested=True)
        self.assertIsNone(result.hit)
        self.assertFalse(result.skipped)

    def test_unavailable_stays_unavailable(self):
        """判不了就是判不了：缺料这一态不受 request 影响，也不许因此变成 ok。"""
        result = recitation.audit(SOUL_LINE, None, None, requested=True)
        self.assertEqual(STATUS_UNAVAILABLE, result.status)
        self.assertFalse(result.skipped)

    def test_requested_quote_needs_no_violation_exemption(self):
        """引用原话不再作为违规，不虚构一次按请求豁免。"""
        result = recitation.audit(QUOTE_LINE, [{"kind": QUOTE, "text": QUOTE_LINE}], None, True)
        self.assertIsNone(result.hit)
        self.assertFalse(result.skipped)
        self.assertIn(QUOTE_LINE, result.reason)

    def test_broken_skip_section_is_loud(self):
        """记账那一节坏了就当场响并点名：说不清「为什么没判废」等于没记。"""
        for changes, spot in (({"skip": {}}, "clause_id"),
                              ({"skip": dict(SKIP, status="")}, "status"),
                              ({"skip": dict(SKIP, reason="这一句没带记号")}, "skip.status")):
            with self.subTest(changes=changes):
                with self.assertRaises(AuditBroken) as caught:
                    recitation.audit(SOUL_LINE, self.inject, dict(DATA, **changes), True)
                self.assertIn(spot, str(caught.exception))

    def test_the_request_markers_have_only_one_home(self):
        """标记词表只有 data/repeat.json 那一份：audit.py 里出现一条就是第二处真源。"""
        source = Path(recitation.__file__).read_text(encoding="utf-8")
        for marker in load("repeat")["request_markers"]:
            self.assertNotIn(marker, source)
        self.assertNotIn(SKIP_CLAUSE, source)      # 记账的名字也不许进代码
        self.assertNotIn(SKIP_REASON, source)      # 那句人话同样只在数据里


class TheCutOutSentenceDataIsWhole(unittest.TestCase):
    """删那一句要用的项都在数据里，而且自洽：句界表非空、三句人话都带同一个记号。

    闸（gate.py 的 _strip_recited）读的是同一份数据。缺一格就是「删不动」或「认不出这一笔」
    ——两件事都不许悄悄发生，所以这里当场验，别等线上才发现账本上少一个记号。
    """

    def test_sentence_marks_are_a_non_empty_table(self):
        marks = DATA["sentence_marks"]
        self.assertIsInstance(marks, str)
        self.assertTrue(marks)

    def test_every_cut_out_reason_carries_the_mark(self):
        mark = DATA["recited"]["status"]
        for key in ("recited_strip", "recited_only", "recited_leftover"):
            with self.subTest(key=key):
                self.assertIn(mark, DATA["reason"][key])

    def test_the_cut_out_reasons_say_what_was_removed(self):
        """账本要读得出删了哪一句：删句与只剩一句那两句人话都带着那个位置。"""
        self.assertIn("{removed}", DATA["reason"]["recited_strip"])
        self.assertIn("{removed}", DATA["reason"]["recited_only"])


class AuditStaysClean(unittest.TestCase):
    """硬约束：本文件不取时钟、不碰随机数、不写盘、不 import 宿主，也不把说法抄进代码。"""

    def setUp(self):
        self.source = Path(recitation.__file__).read_text(encoding="utf-8")

    def test_no_clock_and_no_random(self):
        self.assertNotRegex(self.source,
                            r"(?m)^\s*(?:import|from)\s+(?:time|datetime|random)\b")

    def test_writes_nothing(self):
        for banned in ("open(", "write_text", ".mkdir", "json.dump", "os.remove", "shutil"):
            self.assertNotIn(banned, self.source)

    def test_imports_no_host(self):
        for word in ("astrbot", "deepseek-ai", "cordis", "OneBot", "NapCat", "aiocqhttp"):
            self.assertNotIn(word, self.source)

    def test_no_wording_is_copied_into_code(self):
        """人话说法与引述标记词表都只在数据里：代码里出现一个字都算第二处真源。"""
        for template in DATA["reason"].values():
            self.assertNotIn(template, self.source)
        for word in DATA["quote_markers"]:
            self.assertNotIn(word, self.source)
        for word in DATA["verdicts"].values():
            self.assertNotIn(word, self.source)


if __name__ == "__main__":
    unittest.main()
