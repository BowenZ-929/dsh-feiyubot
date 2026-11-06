"""编记忆这一关只管判：把过去说过的话挂在嘴上、细节又对不上，就该拦；说心意的要放行。

判据与说法一个字都不写在这份文件里：标记表、态度豁免表、细节四路、两把尺子的阈值、四句人话、
闸用的两个名字全从 organs/mouth/data/fabricate.json 读（走 warehouse）。要改判据就改数据、不改测试、
更不改 fabricate.py。

「数据改一条，行为跟着变」是这一关的全部意义，所以末尾有一组测试把 fabricate.json 拷到临时目录、
改一处再按同样的读法读回来——验的是「读数据」，不是验内存里那个 dict。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from organs.mouth import fabricate
from organs.mouth import gate as the_gate
from organs.mouth.fabricate import (FabricateBroken, FabricateHit, check, clear_reason,
                                    fallback_reason, fallback_text)
from organs.mouth.warehouse import load

CLAUSES = load("clauses")["clauses"]
CLAIM_BOOK = load("claim")
AUDIT_BOOK = load("audit")

BOOK = load("fabricate")
CLAUSE_ID = BOOK["hit"]["clause_id"]
KIND = BOOK["hit"]["kind"]

#: 素材（不是判据）：标记 + 时间细节，这一轮一条都没想起来时她最会说的那种话。
INVENTED = "你当时说要去看病，是周三上午吧？"
#: 同一件事他真的说过：细节与召回一致，这句要放行。
RECALLED = [{"text": "他说周三上午去看病"}]


def _write_and_load(tmp: str, book: dict) -> dict:
    """把改过的判据落进临时目录，再按仓库的读法读回来（读法与线上一致，只换根目录）。"""
    path = os.path.join(tmp, "fabricate.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(book, handle, ensure_ascii=False)
    return load("fabricate", root=tmp)


class NotCheckedIsNotTheSameAsJudged(unittest.TestCase):
    """recalled 的两种形状必须分得开：None 是这一关没跑，[] 是这一轮真的什么都没想起来。"""

    def test_none_means_this_gate_did_not_run(self):
        """拿不到这一轮想起来的是什么，就没有「对不上」可言——不判，也不诬陷。"""
        self.assertIsNone(check(INVENTED, None))

    def test_an_empty_recall_is_a_real_judgement(self):
        """空清单是「给了账、这一轮一条都没想起来」：标记 + 细节就是编的，照判。"""
        hit = check(INVENTED, [])
        self.assertIsInstance(hit, FabricateHit)

    def test_the_two_statuses_are_different_words(self):
        self.assertNotEqual(fabricate.STATUS_OK, fabricate.STATUS_NOT_CHECKED)

    def test_the_table_is_still_validated_without_a_recall(self):
        """没给 recalled 也要先把表校验一遍：坏表不许因为这一轮没给账就悄悄跳过。"""
        with self.assertRaises(FabricateBroken):
            check(INVENTED, None, {})


class HittingWordsAreEvidence(unittest.TestCase):
    """命中返回的是一条证据（哪个标记、什么细节、哪一句、为什么），不是布尔。"""

    def test_the_marker_and_the_detail_come_back(self):
        hit = check(INVENTED, [])
        self.assertEqual("你当时说", hit.marker)
        self.assertEqual("周三", hit.detail)
        self.assertEqual(BOOK["detail"]["kinds"]["time"], hit.detail_kind)

    def test_the_two_gate_names_come_from_the_data(self):
        hit = check(INVENTED, [])
        self.assertEqual(CLAUSE_ID, hit.clause_id)
        self.assertEqual(KIND, hit.kind)

    def test_the_reason_names_the_marker_the_detail_and_the_tag(self):
        hit = check(INVENTED, [])
        self.assertIn("「%s」" % hit.marker, hit.reason)
        self.assertIn("「%s」" % hit.detail, hit.reason)
        self.assertIn(BOOK["tag"], hit.reason)
        self.assertNotIn("{", hit.reason, "模板里的占位符必须填干净，不许把 {tag} 这样发出去")

    def test_the_matched_span_starts_at_the_marker(self):
        hit = check("今天风挺大的。" + INVENTED, [])
        self.assertTrue(hit.matched.startswith("你当时说"), hit.matched)
        self.assertNotIn("今天风挺大的", hit.matched)


class DetailIsWhatMakesItAFact(unittest.TestCase):
    """四路细节判据各认一遍：只有标记没有细节，不判。"""

    def test_a_number_is_a_detail(self):
        hit = check("你上次说三遍就好了", [])
        self.assertEqual(BOOK["detail"]["kinds"]["numerals"], hit.detail_kind)

    def test_a_quoted_phrase_is_a_detail(self):
        hit = check("你说过“明天早点来”", [])
        self.assertEqual(BOOK["detail"]["kinds"]["quote"], hit.detail_kind)

    def test_a_long_enough_stretch_is_a_detail(self):
        hit = check("你当时说下周三要交房租", [])
        self.assertIn(hit.detail_kind, (BOOK["detail"]["kinds"]["time"],
                                        BOOK["detail"]["kinds"]["length"]))

    def test_a_bare_marker_is_not_judged(self):
        """「我照做了」只是一个标记：没有具体到什么，就不判——空口不是编造。"""
        self.assertIsNone(check("我照做了", []))
        self.assertIsNone(check("我记着", []))


class AttitudeIsNotAFact(unittest.TestCase):
    """豁免表：说心意的句子整句跳过——宁可放过一句带细节的，也不冤枉一句只是在表心意。"""

    def test_never_forgetting_you_passes(self):
        self.assertIsNone(check("我没忘你", []))
        self.assertIsNone(check("我不会忘你", []))
        self.assertIsNone(check("我记着你这人", []))

    def test_the_exemption_beats_a_time_word_in_the_same_sentence(self):
        """同一句里既有态度又有时间词，按态度放过：这一格是这一关的取舍，写在数据里。"""
        self.assertIsNone(check("我记得你这人，周三那天你也在", []))


class AligningIsOfflineArithmetic(unittest.TestCase):
    """「对得上」两把尺子各验一遍：同一个输入，任何人都能复算出同一个结论。"""

    def test_the_same_detail_passes(self):
        """验收 2：细节与召回一致 → 放行。"""
        self.assertIsNone(check("你当时说是周三上午去看病", RECALLED))

    def test_a_different_detail_is_still_a_lie(self):
        self.assertIsInstance(check("你当时说是周三上午去看病",
                                    [{"text": "他说周四下午去上班"}]), FabricateHit)

    def test_a_run_of_min_run_chars_aligns(self):
        """第一把尺子：贴住某条 recalled 的连续 min_run 个字就算对得上。"""
        table = load("fabricate")
        claimed = "其实你说过周三上午去看病这事我可没忘"
        hit = check(claimed, RECALLED, table)
        self.assertIsNone(hit)

    def test_a_rearranged_detail_aligns_by_bigrams(self):
        """第二把尺子：换了顺序、字还是那几个，2-gram 覆盖率过线也算对得上。"""
        self.assertIsNone(check("你当时说说好周三、周四、周五去",
                                [{"text": "周四周三周五说好去"}]))

    def test_a_recall_without_text_never_aligns(self):
        """没有正文的那一条对不上任何话：不猜它的意思，也不拿它当抗辩。"""
        self.assertIsInstance(check(INVENTED, [{"source": "brain"}]), FabricateHit)


class SayingItHonestly(unittest.TestCase):
    """说不出来的时候那句实话：留给闸在二次命中时替换整泡用。"""

    def test_the_honest_line_comes_from_the_data(self):
        self.assertEqual(BOOK["fallback"], fallback_text())
        self.assertIn("没记到", fallback_text())

    def test_the_fallback_reason_names_what_was_replaced(self):
        hit = check(INVENTED, [])
        why = fallback_reason(hit)
        self.assertIn(hit.detail, why)
        self.assertIn(fallback_text(), why)
        self.assertIn(BOOK["tag"], why)

    def test_the_clear_reason_says_this_gate_ran(self):
        self.assertEqual(BOOK["reason"]["clear"], clear_reason())


class TheTableIsTheSingleSource(unittest.TestCase):
    """数据改一条，行为跟着变；坏表当场响，不静默降级。"""

    def test_a_removed_marker_stops_judging(self):
        book = dict(BOOK, markers=[m for m in BOOK["markers"] if "当时" not in m])
        with tempfile.TemporaryDirectory() as tmp:
            changed = _write_and_load(tmp, book)
        self.assertIsNone(check(INVENTED, [], changed))

    def test_a_shorter_detail_threshold_catches_more(self):
        book = dict(BOOK, detail=dict(BOOK["detail"], min_chars=2))
        line = "你当时说那个"
        self.assertIsNone(check(line, []))
        hit = check(line, [], book)
        self.assertIsInstance(hit, FabricateHit)
        self.assertEqual(BOOK["detail"]["kinds"]["length"], hit.detail_kind)

    def test_a_stricter_ratio_stops_calling_it_aligned(self):
        book = dict(BOOK, align={"min_run": 99, "bigram_overlap": 0.999})
        self.assertIsNone(check("你当时说是周三上午去看病", RECALLED))
        self.assertIsInstance(check("你当时说是周三上午去看病", RECALLED, book), FabricateHit)

    def test_the_written_book_is_what_runs(self):
        """磁盘上那份 fabricate.json 就是判据本体：改了它，check 的结论跟着变。"""
        with tempfile.TemporaryDirectory() as tmp:
            book = dict(BOOK, fallback="这句实话换成另一句。")
            changed = _write_and_load(tmp, book)
        self.assertEqual("这句实话换成另一句。", fallback_text(changed))

    def test_a_broken_book_is_loud(self):
        for spot in ("hit", "markers", "exempt", "detail", "align", "reason"):
            with self.subTest(spot=spot):
                book = {key: value for key, value in BOOK.items() if key != spot}
                with self.assertRaises(FabricateBroken):
                    check(INVENTED, [], book)

    def test_a_bad_regex_is_loud(self):
        book = dict(BOOK, markers=["(未闭合"])
        with self.assertRaises(FabricateBroken):
            check(INVENTED, [], book)

    def test_a_ratio_out_of_range_is_loud(self):
        book = dict(BOOK, align={"min_run": 6, "bigram_overlap": 1.5})
        with self.assertRaises(FabricateBroken):
            check(INVENTED, [], book)


class RecalledShapeIsMachineWritten(unittest.TestCase):
    """形状坏了当场抛：猜它的意思，等于替她造一份能对得上的记忆。"""

    def test_not_a_list_is_loud(self):
        for bad in ("一条记忆", {"text": "他说周三上午去看病"}, 3):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(FabricateBroken):
                    check(INVENTED, bad)

    def test_a_row_that_is_not_a_mapping_is_loud(self):
        with self.assertRaises(FabricateBroken):
            check(INVENTED, ["他说周三上午去看病"])


class TheModuleHoldsNoCriteria(unittest.TestCase):
    """判据全在数据里：本文件里不许出现标记、豁免、阈值、说法。"""

    def test_no_marker_or_exemption_lives_in_the_code(self):
        """标记表与豁免表不进代码：grep 得到它们，就是第二处真源。"""
        source = Path(fabricate.__file__).read_text(encoding="utf-8")
        for pattern in list(BOOK["markers"]) + list(BOOK["exempt"]):
            self.assertNotIn(pattern, source)

    def test_no_saying_lives_in_the_code(self):
        """四句人话也不进代码：账本里看到的说法只能是数据里那一份。"""
        source = Path(fabricate.__file__).read_text(encoding="utf-8")
        self.assertNotIn(BOOK["fallback"], source)
        for name, line in BOOK["reason"].items():
            head = line.split("{")[0]
            if len(head) < 4 or head == "「":
                continue                  # 模板以占位符开头时没有够长的字面量可验
            self.assertNotIn(head, source, name)

    def test_no_clock_and_no_host(self):
        source = Path(fabricate.__file__).read_text(encoding="utf-8")
        for word in ("time", "datetime", "random"):
            self.assertIsNone(re.search(r"^\s*import %s\b" % word, source, re.M))
        for organ in ("organs.soul", "organs.mind", "organs.heart", "organs.home"):
            self.assertNotIn(organ, source)


class ItRidesTheGate(unittest.TestCase):
    """接上闸之后：命中走既有的「先改一次」，改不掉就把那一泡换成那句实话——不是整条丢。

    闸的顺序题也在这里验：条款 → 动作配额 → 动作括注 → 内容重复 → 冒充做过 → 编记忆 → 复述审计。
    """

    #: 素材：她替自己编出来的一段过去（标记 + 时间细节）
    LINE = INVENTED
    #: 同一件事他真说过：这句是实话，放行
    TRUTH = "你当时说是周三上午去看病"
    #: 改写之后的说法（不再认领那段过去）
    FIXED = "医院那边你自己拿主意，我不乱猜。"
    #: 一句客服腔（撞条款那条），用来验「条款先判」
    SPEECH = "你好，请问有什么可以帮您的吗？"

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_no_recall_at_all_is_not_checked(self):
        """验收 5：recalled=None 判不了——不判、不诬陷，状态如实记 not-checked。"""
        self.assertIsNone(the_gate.check(self.LINE, CLAUSES, recalled=None))
        verdict = the_gate.enforce(self.LINE, CLAUSES, None, recalled=None)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.LINE, verdict.text)
        self.assertEqual(fabricate.STATUS_NOT_CHECKED, verdict.fabricate_status)
        self.assertNotEqual(fabricate.STATUS_OK, verdict.fabricate_status)

    def test_an_empty_recall_is_judged(self):
        """recalled=[] 是「给了账、这一轮一条都没想起来」：同一句话就是编的，状态也不一样。"""
        self.assertIsInstance(the_gate.check(self.LINE, CLAUSES, recalled=[]),
                              the_gate.Hit)
        verdict = the_gate.enforce(self.LINE, CLAUSES, None, recalled=[])
        self.assertEqual(fabricate.STATUS_OK, verdict.fabricate_status)

    def test_without_a_rewriter_the_bubble_becomes_the_honest_line(self):
        """验收 1：不给重写器 → 那一泡换成那句实话（贴 before / after），账本照样记一笔。"""
        seen, sink = self._spy()
        verdict = the_gate.enforce(self.LINE, CLAUSES, None, sink, recalled=[])
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.LINE, self.LINE)                     # before
        self.assertEqual(BOOK["fallback"], verdict.text)           # after
        self.assertTrue(verdict.salvaged, "换成实话是救人那一族，与配额剥行同一口径")
        self.assertFalse(verdict.kept_after_rewrite, "留原话就是留一段编出来的过去")
        self.assertEqual(CLAUSE_ID, verdict.hits[-1].clause_id)
        self.assertEqual(1, len(seen), "救回来的那一笔也要记账")
        self.assertEqual(CLAUSE_ID, seen[0][0])
        self.assertIn(BOOK["fallback"], seen[0][1], "账本里读得出换成了什么")

    def test_the_rewrite_clears_it(self):
        verdict = the_gate.enforce(self.LINE, CLAUSES, lambda text: self.FIXED, recalled=[])
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.FIXED, verdict.text)
        self.assertFalse(verdict.salvaged)

    def test_a_second_hit_becomes_the_honest_line(self):
        """改完还命中：换成实话（不是整条丢，也不是留原话），hits 两条都留着。"""
        verdict = the_gate.enforce(self.LINE, CLAUSES, lambda text: text, recalled=[])
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(BOOK["fallback"], verdict.text)
        self.assertEqual([CLAUSE_ID, CLAUSE_ID],
                         [one.clause_id for one in verdict.hits])

    def test_a_rewrite_that_raises_still_gets_the_honest_line(self):
        def broken(text):
            raise RuntimeError("改不动")
        verdict = the_gate.enforce(self.LINE, CLAUSES, broken, recalled=[])
        self.assertEqual(BOOK["fallback"], verdict.text)
        self.assertTrue(verdict.salvaged)

    def test_keep_unrewritten_does_not_keep_a_made_up_memory(self):
        """keep_unrewritten 盖不过这一条：留原话就是留一段编出来的过去。"""
        verdict = the_gate.enforce(self.LINE, CLAUSES, None, None, recalled=[],
                                   keep_unrewritten=True)
        self.assertEqual(BOOK["fallback"], verdict.text)
        self.assertFalse(verdict.kept_after_rewrite)

    def test_an_aligned_detail_passes(self):
        """验收 2：细节与召回一致 → 放行；放行要有据：那一关查过了就说出来。"""
        verdict = the_gate.enforce(self.TRUTH, CLAUSES, None, recalled=RECALLED)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.TRUTH, verdict.text)
        self.assertIn(BOOK["reason"]["clear"], verdict.reason)
        self.assertEqual(fabricate.STATUS_OK, verdict.fabricate_status)

    def test_attitude_passes_even_with_an_empty_recall(self):
        """验收 3：态度不是事实——一条都没想起来也放行。"""
        for line in ("我没忘你", "我记得你这人", "我不会忘"):
            with self.subTest(line=line):
                self.assertIsNone(the_gate.check(line, CLAUSES, recalled=[]))
                verdict = the_gate.enforce(line, CLAUSES, None, recalled=[])
                self.assertEqual("pass", verdict.decision)
                self.assertEqual(line, verdict.text)

    def test_a_bare_marker_is_not_judged(self):
        self.assertIsNone(the_gate.check("我照做了", CLAUSES, recalled=[]))

    def test_the_clause_comes_first(self):
        """条款先判（既有逻辑不动）：两边都撞时先报条款。"""
        hit = the_gate.check(self.SPEECH + self.LINE, CLAUSES, recalled=[])
        self.assertEqual("courtesy-open", hit.clause_id)

    def test_the_claim_comes_before_it(self):
        """顺序：冒充做过排在编记忆前面——先说做过没做过，再说记得没记得。"""
        claim_line = "%s%s%s" % (CLAIM_BOOK["person"], CLAIM_BOOK["bare_prefixes"][0],
                                 CLAIM_BOOK["action_verbs"][0])
        hit = the_gate.check(claim_line + "。" + self.LINE, CLAUSES, actions=[],
                             claim=CLAIM_BOOK, recalled=[])
        self.assertEqual(CLAIM_BOOK["hit"]["clause_id"], hit.clause_id)

    def test_it_comes_before_the_recitation_audit(self):
        """顺序：编记忆排在复述审计前面（它比「抄没抄」更早：先说记得没记得，再说是不是抄来的）。"""
        inject = [{"kind": AUDIT_BOOK["setting_kinds"][0], "text": self.LINE}]
        hit = the_gate.check(self.LINE, CLAUSES, inject=inject, policy=AUDIT_BOOK,
                             recalled=[])
        self.assertEqual(CLAUSE_ID, hit.clause_id)

    def test_the_gate_does_not_hold_the_words(self):
        """闸只接判据、不持判据：那句实话与四句人话一个字都不许出现在 gate.py 里。

        只验「说法」，不验那一关的中文名（「编记忆」是注释里用它讲清判据顺序的名字，不是一句说法）。
        """
        source = Path(the_gate.__file__).read_text(encoding="utf-8")
        self.assertNotIn(BOOK["fallback"], source)
        for name, line in BOOK["reason"].items():
            head = line.split("{")[0]
            if len(head) < 4 or head == "「":
                continue
            self.assertNotIn(head, source, name)


if __name__ == "__main__":
    unittest.main()
