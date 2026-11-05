"""拒绝要说成一句人话：三形状各有说法、说法全在数据里、还得过八股闸、还得投给心。

这句台词一个字都不写死在这份测试里：说法从 organs/mouth/data/refusal.json 读（走 warehouse），
改说法就是改数据。验四件事——

1. 三种形状各说一次，说的都是数据里那句（没被闸拦，也不是客服腔）；
2. **拒绝的话照样过闸**：把某条说法人为写成客服腔，必须被条款拦下——「这是拒绝」不是免检章；
3. 三形状都有事件可投：事件带 shape / 完整名 / why / ts，投递只走注入的 sink（None 就是不投）；
4. 账本记三行（refuse.jsonl），sent 落进每一行，「投过没投过」不必猜；
5. 软转向（soft）也是拒绝：她真说过的那段「这个啊 + 转移 + 反问」要认出来，原话一个字符都不改、
   事件照旧记 refused-topic、账本那行记 shape=soft；而正面答了（时间词/数字）的一段一律不算，
   没有原问题（asked）时只报「判不了」，不报「不是拒绝」。
"""
from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.mouth import refusal
from organs.mouth.gate import check
from organs.mouth.refusal import RefusalBroken, RefusalVerdict
from organs.mouth.warehouse import load

BOOK = load("refusal")
CLAUSES = load("clauses")["clauses"]
SHAPES = tuple(BOOK["shapes"])
FULL_NAMES = {key: BOOK["shapes"][key]["full_name"] for key in SHAPES}
REASON = BOOK["reason"]
SOURCE = Path(refusal.__file__).read_text(encoding="utf-8")
NOW = "2026-01-02T03:04:05"          # 时刻由调用方给，这里给一个固定的

# 一句货真价实的客服腔：命中 courtesy-close（「感谢理解 + 我们会尽快处理」）
CUSTOMER_SERVICE = "感谢您的理解，我们会尽快为您处理。"

# 客服腔那两条条款的 id（结构性标签，不是台词）：拒绝说法一条都不许踩
COURTESY_IDS = ("courtesy-open", "courtesy-close")

# 说法自己的规矩（长度上下限、破折号、禁用词表）也在数据里：测试里一个阈值、一个词都不写死
RULES = BOOK["style_rules"]
PUNCT = load("policy")["visible"]["strip"]      # 去标点借现成那张可见字符表，不另造一份

# 软转向那一节的判据（词表与理由都在数据里：测试里一个阈值、一个理由模板都不写死）
SOFT = BOOK["soft"]

# 她真说过的那段：开头垫话 + 转移 + 反问，一个正面回答的字都没有
SOFT_ANSWER = ("……这个啊。鱼在海里游着游着，有些同伴就游远了，不算丢下……"
               "行了行了，我又不是不能自己待着。倒是你，怎么突然问这个。")
SOFT_ASKED = "你以前是不是也被人丢下过？"

# 正面答了的一段（我妈几点做手术那段的答）：时间词与数字都在里面
PLAIN_ANSWER = "明天上午九点做，第二台，我陪她去。"
PLAIN_ASKED = "我妈明天几点做手术？"


def _copy_book() -> dict:
    """把数据整份拷一份再改：验的是「读数据」，不是改内存里那个模块级 dict。"""
    return json.loads(json.dumps(BOOK, ensure_ascii=False))


def _courtesy_clauses() -> list:
    """只留客服腔那两条条款的清单：专门问「这句像不像客服」，不问别的。"""
    return [dict(clause) for clause in CLAUSES if clause["id"] in COURTESY_IDS]


def _body(shape: str) -> str:
    """去掉标点后的说法本体：字长与开头两个字都按这个算（标点不算她说的内容）。"""
    return "".join(ch for ch in refusal.say(shape, BOOK) if ch not in PUNCT)


class ThreeShapesSayIt(unittest.TestCase):
    """第 1 条：三种拒绝各说一次，说的就是数据里那句，而且是原样发出的。"""

    def test_each_shape_says_its_own_line(self):
        said = {}
        for shape in SHAPES:
            verdict = refusal.enforce(shape, BOOK, CLAUSES, now=NOW)
            self.assertIsInstance(verdict, RefusalVerdict)
            self.assertEqual(verdict.shape, shape)
            self.assertEqual(verdict.full_name, FULL_NAMES[shape])
            self.assertEqual(verdict.text, BOOK["shapes"][shape]["say"])   # 说法原样来自数据
            self.assertTrue(verdict.text)
            self.assertEqual(verdict.gate_decision, "pass")                # 闸没拦
            self.assertTrue(verdict.refused)                               # 说出口了
            said[shape] = verdict.text
        self.assertEqual(len(set(said.values())), len(SHAPES))             # 三句各不相同

    def test_full_name_is_accepted_too(self):
        """心那边的完整名（refused-topic 等）也认，落回来的短名与数据一致。"""
        for shape in SHAPES:
            verdict = refusal.enforce(FULL_NAMES[shape], BOOK, CLAUSES, now=NOW)
            self.assertEqual(verdict.shape, shape)
            self.assertEqual(verdict.text, BOOK["shapes"][shape]["say"])

    def test_unknown_shape_names_the_shape(self):
        with self.assertRaises(RefusalBroken) as caught:
            refusal.enforce("refused-nope", BOOK, CLAUSES, now=NOW)
        message = str(caught.exception)
        self.assertIn("refused-nope", message)            # 指名
        for full in FULL_NAMES.values():
            self.assertIn(full, message)                  # 顺带列出认得的

    def test_say_and_event_also_refuse_unknown_shapes(self):
        for call in (lambda: refusal.say("nope", BOOK),
                     lambda: refusal.event("nope", "why", NOW, table=BOOK)):
            with self.assertRaises(RefusalBroken):
                call()


class SayingsAreNotOneMould(unittest.TestCase):
    """回修钉死：三句不许是一个模具，也不许把「不是故障」塞进她嘴里自证。

    上一版三句都长成「……——不是 X，是 Y」，并排一念就听出是一个厂出的；「不是听漏了」
    「不是掉线」这类自证，等于把接口现实讲到她嘴里——「不是故障」这层归 note 与 event.why，
    她本人只负责明确地不接。这一组钉四件事：没有破折号模具、字长在数据给的上下限里、
    本体与开头两个字都两两不同、禁用词一个都不出现。规矩全在 refusal.json 的 style_rules 节。
    """

    def test_no_dash_mould(self):
        self.assertTrue(RULES["dashes"])                        # 禁用表空着就等于没验
        dashed = [shape for shape in SHAPES
                  if any(dash in refusal.say(shape, BOOK) for dash in RULES["dashes"])]
        self.assertEqual(dashed, [])                            # 含破折号的：0 条

    def test_lengths_sit_between_the_limits(self):
        for shape in SHAPES:
            body = _body(shape)
            self.assertGreaterEqual(len(body), RULES["say_len_min"], (shape, body))
            self.assertLessEqual(len(body), RULES["say_len_max"], (shape, body))

    def test_openings_and_bodies_are_all_different(self):
        bodies = [_body(shape) for shape in SHAPES]
        self.assertEqual(len(set(bodies)), len(SHAPES))         # 本体两两不同
        openings = [body[:2] for body in bodies]
        self.assertEqual(len(set(openings)), len(SHAPES))       # 开头两个字也互不相同

    def test_no_banned_word(self):
        banned = RULES["banned_words"]
        self.assertTrue(banned)                                 # 空词表等于没验
        for shape in SHAPES:
            said = refusal.say(shape, BOOK)
            self.assertEqual([word for word in banned if word in said], [], (shape, said))


class RefusalStillPassesTheGate(unittest.TestCase):
    """第 2 条：拒绝的话也要过八股闸——三句都不像客服，写歪了必须被拦。"""

    def test_three_lines_pass_the_whole_gate(self):
        for shape in SHAPES:
            self.assertIsNone(check(refusal.say(shape, BOOK), CLAUSES))

    def test_three_lines_do_not_hit_the_customer_service_clauses(self):
        """专问客服腔那两条：三句一条都不许踩。"""
        courtesy = _courtesy_clauses()
        self.assertTrue(courtesy)
        for shape in SHAPES:
            self.assertIsNone(check(refusal.say(shape, BOOK), courtesy))

    def test_injected_customer_service_line_is_dropped(self):
        """把某条说法人为写成客服腔：闸必须拦下，弃言照旧记账。"""
        book = _copy_book()
        book["shapes"]["topic"]["say"] = CUSTOMER_SERVICE
        dropped = []
        verdict = refusal.enforce("topic", book, CLAUSES, now=NOW, refuse_sink=lambda event: None,
                                  drop_sink=lambda clause_id, reason: dropped.append((clause_id, reason)))
        self.assertEqual(verdict.text, "")                       # 弃了就是空串
        self.assertEqual(verdict.gate_decision, "dropped")
        self.assertFalse(verdict.refused)                        # 这句没说出去
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0][0], "courtesy-close")        # 拦它的是客服收尾那条
        self.assertIn("courtesy-close", dropped[0][1])           # 人话原因里也点名
        self.assertTrue(verdict.sent)                            # 拒了这件事照样投给心
        self.assertEqual(verdict.event["shape"], "topic")

    def test_rewrite_gets_one_chance_and_then_it_goes(self):
        """命中 → 交注入的 rewrite 改一次 → 改完不再命中就发。"""
        book = _copy_book()
        book["shapes"]["mood"]["say"] = CUSTOMER_SERVICE
        verdict = refusal.enforce("mood", book, CLAUSES, rewrite=lambda _text: refusal.say("mood", BOOK),
                                  now=NOW)
        self.assertEqual(verdict.gate_decision, "rewritten")
        self.assertEqual(verdict.text, refusal.say("mood", BOOK))
        self.assertTrue(verdict.refused)

    def test_same_line_twice_is_still_dropped(self):
        """改一次还命中 = 弃掉（二次命中即弃），账本照记。"""
        book = _copy_book()
        book["shapes"]["nobody"]["say"] = CUSTOMER_SERVICE
        dropped = []
        verdict = refusal.enforce("nobody", book, CLAUSES, rewrite=lambda text: text,
                                  now=NOW, drop_sink=lambda cid, why: dropped.append(cid))
        self.assertEqual(verdict.text, "")
        self.assertEqual(verdict.gate_decision, "dropped")
        self.assertEqual(dropped, ["courtesy-close"])

    def test_no_saying_literal_in_the_module(self):
        """说法全在数据里：代码里一个字都不许出现。"""
        for shape in SHAPES:
            self.assertNotIn(BOOK["shapes"][shape]["say"], SOURCE)
            self.assertNotIn(BOOK["shapes"][shape]["why"], SOURCE)


class EventGoesToTheHeart(unittest.TestCase):
    """第 3 条：说完投一条事件给心；投递只走注入的 sink，本库不写心的任何地方。"""

    def test_event_carries_shape_full_name_why_and_time(self):
        event = refusal.event("nobody", "为什么拒", NOW, table=BOOK)
        self.assertEqual(set(event), {"ts", "shape", "full_name", "why"})
        self.assertEqual(event["ts"], NOW)                     # 时刻原样来自调用方
        self.assertEqual(event["shape"], "nobody")
        self.assertEqual(event["full_name"], FULL_NAMES["nobody"])
        self.assertIn("为什么拒", event["why"])

    def test_event_without_why_or_time_raises(self):
        with self.assertRaises(RefusalBroken):
            refusal.event("topic", "", NOW, table=BOOK)
        with self.assertRaises(RefusalBroken):
            refusal.event("topic", "有理由", None, table=BOOK)

    def test_sink_is_the_only_door(self):
        sent = []
        verdict = refusal.enforce("mood", BOOK, CLAUSES, now=NOW, refuse_sink=sent.append)
        self.assertTrue(verdict.sent)
        self.assertEqual(sent, [verdict.event])

    def test_no_sink_means_not_sent_but_the_refusal_stands(self):
        verdict = refusal.enforce("mood", BOOK, CLAUSES, now=NOW)
        self.assertFalse(verdict.sent)
        self.assertEqual(verdict.event["shape"], "mood")       # 事件照旧装好、照旧返回
        self.assertTrue(verdict.refused)
        self.assertIn(REASON["unsent"], verdict.reason)

    def test_reason_carries_the_judgement_note(self):
        """判据注记进 reason：复盘时看得见这次按哪条判的。"""
        verdict = refusal.enforce("topic", BOOK, CLAUSES, now=NOW, refuse_sink=lambda event: None)
        self.assertIn(BOOK["shapes"]["topic"]["note"], verdict.reason)
        self.assertIn(FULL_NAMES["topic"], verdict.reason)
        self.assertIn(REASON["sent"], verdict.reason)


class Ledger(unittest.TestCase):
    """第 4 条：本地账本记三行，sent 看得见「投过没投过」。"""

    def test_three_rows_after_three_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "refuse.jsonl")
            rows = []
            for shape in SHAPES:
                verdict = refusal.enforce(shape, BOOK, CLAUSES, now=NOW,
                                          refuse_sink=lambda event: None)
                rows.append(refusal.record(path, verdict.event, verdict.sent))
            lines = Path(path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            on_disk = [json.loads(line) for line in lines]
            self.assertEqual([row["shape"] for row in on_disk], list(SHAPES))
            self.assertEqual([row["full_name"] for row in on_disk],
                             [FULL_NAMES[shape] for shape in SHAPES])
            self.assertTrue(all(row["sent"] is True for row in on_disk))
            self.assertTrue(all(row["ts"] == NOW for row in on_disk))
            self.assertEqual([row["why"] for row in on_disk],
                             [BOOK["shapes"][shape]["why"] for shape in SHAPES])

    def test_row_says_whether_it_was_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "refuse.jsonl")
            with_sink = refusal.record(path, refusal.event("topic", "有理由", NOW, table=BOOK), True)
            without = refusal.record(path, refusal.event("topic", "有理由", NOW, table=BOOK), False)
            self.assertTrue(with_sink["sent"])
            self.assertFalse(without["sent"])
            self.assertEqual(len(Path(path).read_text(encoding="utf-8").splitlines()), 2)

    def test_record_refuses_a_row_without_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                refusal.record(os.path.join(tmp, "refuse.jsonl"),
                               {"ts": NOW, "full_name": "refused-topic", "why": "有理由"}, True)

    def test_path_of_three_levels(self):
        self.assertEqual(refusal.path_of(None).name, refusal.LEDGER_NAME)
        with mock.patch.dict(os.environ, {refusal.ENV_VAR: os.path.join(tempfile.gettempdir(), "tracedir")}):
            self.assertEqual(refusal.path_of(None), Path(tempfile.gettempdir()) / "tracedir" / refusal.LEDGER_NAME)
        self.assertEqual(refusal.path_of("/tmp/one.jsonl"), Path("/tmp/one.jsonl"))
        self.assertEqual(refusal.path_of("/tmp/dir"), Path("/tmp/dir") / refusal.LEDGER_NAME)


class DataIsBrokenLoudly(unittest.TestCase):
    """数据坏了当场响并指名：缺键不补默认值，不猜一个形状继续走。"""

    def test_missing_field_names_shape_and_field(self):
        book = _copy_book()
        del book["shapes"]["mood"]["say"]
        with self.assertRaises(RefusalBroken) as caught:
            refusal.say("mood", book)
        message = str(caught.exception)
        self.assertIn("mood", message)
        self.assertIn("say", message)

    def test_missing_reason_section_raises(self):
        book = _copy_book()
        del book["reason"]
        with self.assertRaises(RefusalBroken):
            refusal.enforce("topic", book, CLAUSES, now=NOW)

    def test_enforce_needs_a_time_from_the_caller(self):
        with self.assertRaises(RefusalBroken) as caught:
            refusal.enforce("topic", BOOK, CLAUSES, now=None)
        self.assertIn("时刻", str(caught.exception))


class SoftRedirectIsStillARefusal(unittest.TestCase):
    """第 5 条之一：那句「这个啊 + 转向 + 反问」要认成拒绝，而且一个字符都不许改。"""

    def test_scan_reads_the_words_she_actually_said(self):
        scan = refusal.soft_scan(SOFT_ANSWER, SOFT_ASKED)
        self.assertIsInstance(scan, refusal.SoftVerdict)
        self.assertTrue(scan.is_soft)
        self.assertEqual(scan.status, refusal.SOFT_STATUS_OK)
        self.assertIn(scan.opener, SOFT["openers"])                 # 命中的词来自数据
        self.assertIn(scan.redirect, SOFT["redirect_markers"])
        self.assertIn(scan.question, SOFT["question_markers"])
        self.assertTrue(scan.reason)

    def test_enforce_makes_it_soft_and_leaves_the_text_alone(self):
        sent = []
        verdict = refusal.enforce("topic", BOOK, CLAUSES, now=NOW, asked=SOFT_ASKED,
                                  answer=SOFT_ANSWER, refuse_sink=sent.append)
        self.assertEqual(verdict.shape, refusal.KIND_SOFT)
        self.assertEqual(verdict.kind, refusal.KIND_SOFT)
        self.assertEqual(verdict.full_name, FULL_NAMES[SOFT["event_shape"]])
        self.assertEqual(verdict.text, SOFT_ANSWER)                  # 逐字相同，一个字符都不改
        self.assertTrue(verdict.refused)
        self.assertEqual(verdict.gate_decision, SOFT["gate"])         # 她的原话不过闸
        self.assertTrue(verdict.sent)
        self.assertIn(SOFT["reasons"]["soft"].split("{")[0], verdict.soft.reason)

    def test_event_is_still_refused_topic_with_the_soft_why(self):
        sent = []
        verdict = refusal.enforce("topic", BOOK, CLAUSES, now=NOW, asked=SOFT_ASKED,
                                  answer=SOFT_ANSWER, refuse_sink=sent.append)
        self.assertEqual(sent, [verdict.event])                      # 投出去的正是这一条
        self.assertEqual(set(verdict.event), {"ts", "shape", "full_name", "why"})
        self.assertEqual(verdict.event["shape"], SOFT["event_shape"])
        self.assertEqual(verdict.event["full_name"], FULL_NAMES[SOFT["event_shape"]])
        self.assertEqual(verdict.event["why"], SOFT["why"])           # 措辞来自数据
        self.assertEqual(verdict.event["ts"], NOW)

    def test_ledger_row_says_shape_soft(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "refuse.jsonl")
            verdict = refusal.enforce("topic", BOOK, CLAUSES, now=NOW, asked=SOFT_ASKED,
                                      answer=SOFT_ANSWER, refuse_sink=lambda event: None)
            row = refusal.record(path, verdict.event, verdict.sent, verdict.kind)
            line = Path(path).read_text(encoding="utf-8")
            self.assertIn('"shape": "soft"', line)                   # 账本行原文里就有
            self.assertEqual(json.loads(line)["shape"], refusal.KIND_SOFT)
            self.assertEqual(row["shape"], refusal.KIND_SOFT)
            self.assertEqual(row["full_name"], FULL_NAMES[SOFT["event_shape"]])


class PositiveAnswerIsNotARefusal(unittest.TestCase):
    """第 5 条之二：正面答了（时间词 / 数字）就一律不算软转向。"""

    def test_scan_says_it_was_answered(self):
        scan = refusal.soft_scan(PLAIN_ANSWER, PLAIN_ASKED)
        self.assertFalse(scan.is_soft)
        self.assertEqual(scan.status, refusal.SOFT_STATUS_OK)        # 比过了，只是比出来不是
        hits = [word for word in SOFT["answer_markers"]
                if word in PLAIN_ANSWER and word in scan.reason]
        self.assertTrue(hits, scan.reason)                           # 判词点名了命中的那条信号

    def test_enforce_keeps_it_hard(self):
        verdict = refusal.enforce("topic", BOOK, CLAUSES, now=NOW, asked=PLAIN_ASKED,
                                  answer=PLAIN_ANSWER, refuse_sink=lambda event: None)
        self.assertNotEqual(verdict.shape, refusal.KIND_SOFT)
        self.assertEqual(verdict.kind, refusal.KIND_HARD)
        self.assertFalse(verdict.soft.is_soft)

    def test_no_soft_event_and_no_new_row(self):
        """出口就是这么用软转向的：判成才投、才记。这一段判不成，于是事件一条不投、账本一行不添。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "refuse.jsonl")
            sent = []
            scan = refusal.soft_scan(PLAIN_ANSWER, PLAIN_ASKED)
            if scan.is_soft:
                verdict = refusal.enforce("topic", BOOK, CLAUSES, now=NOW, asked=PLAIN_ASKED,
                                          answer=PLAIN_ANSWER, refuse_sink=sent.append)
                refusal.record(path, verdict.event, verdict.sent, verdict.kind)
            self.assertFalse(scan.is_soft)
            self.assertEqual(sent, [])                               # 一条拒绝事件都没投
            self.assertFalse(Path(path).exists())                    # 账本一行都没添


class WithoutTheQuestionItCannotJudge(unittest.TestCase):
    """第 5 条之三：没有原问题就是「判不了」——不是「不是拒绝」，两者不许混。"""

    def test_no_asked_is_unavailable(self):
        for asked in (None, "", "   \n"):
            scan = refusal.soft_scan(SOFT_ANSWER, asked)
            self.assertEqual(scan.status, refusal.SOFT_STATUS_UNAVAILABLE)
            self.assertFalse(scan.is_soft)
            self.assertEqual((scan.opener, scan.redirect, scan.question), ("", "", ""))
            self.assertIn("判不了", scan.reason)
            self.assertIn(SOFT["reasons"]["unavailable"].split("：")[0], scan.reason)

    def test_unavailable_reads_differently_from_not_a_refusal(self):
        missing = refusal.soft_scan(SOFT_ANSWER, None)
        not_soft = refusal.soft_scan("行了行了，我知道了。", SOFT_ASKED)
        self.assertNotEqual(missing.status, not_soft.status)         # 一个没比过，一个比过了
        self.assertNotEqual(missing.reason, not_soft.reason)

    def test_the_verdict_has_no_door_to_send_anything(self):
        """判词本身不投事件：soft_scan 连 sink 的口子都没有（没口子＝投不出去）。"""
        self.assertEqual(set(inspect.signature(refusal.soft_scan).parameters),
                         {"answer", "asked", "table"})

    def test_enforce_takes_the_hard_road_when_the_question_is_missing(self):
        sent = []
        verdict = refusal.enforce("topic", BOOK, CLAUSES, now=NOW, refuse_sink=sent.append)
        self.assertEqual(verdict.kind, refusal.KIND_HARD)
        self.assertEqual(verdict.event["why"], BOOK["shapes"]["topic"]["why"])  # 不是软转向那句
        self.assertEqual(verdict.soft.status, refusal.SOFT_STATUS_UNAVAILABLE)  # 判不了如实挂着


class HardShapesStayHard(unittest.TestCase):
    """第 5 条之四：软转向接进来以后，三句硬拒绝的说法与事件一个字都没变。"""

    def test_three_lines_stay_hard_letter_for_letter(self):
        for shape in SHAPES:
            bare = refusal.enforce(shape, BOOK, CLAUSES, now=NOW, refuse_sink=lambda event: None)
            scanned = refusal.enforce(shape, BOOK, CLAUSES, now=NOW, refuse_sink=lambda event: None,
                                      asked=SOFT_ASKED, answer=PLAIN_ANSWER)
            self.assertEqual(bare.kind, refusal.KIND_HARD)
            self.assertEqual(scanned.kind, refusal.KIND_HARD)
            self.assertEqual(scanned.shape, shape)
            self.assertEqual(scanned.text, BOOK["shapes"][shape]["say"])
            self.assertEqual(scanned.event, bare.event)              # 事件逐字不变
            self.assertEqual((scanned.gate_decision, scanned.reason),
                             (bare.gate_decision, bare.reason))

    def test_hard_rows_keep_the_short_shape_when_none_is_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "refuse.jsonl")
            event = refusal.event("mood", "有理由", NOW, table=BOOK)
            row = refusal.record(path, event, True)
            self.assertEqual(row["shape"], "mood")                   # 不给就沿用旧行格式
            self.assertEqual(set(json.loads(Path(path).read_text(encoding="utf-8"))), set(row))
            hard = refusal.record(path, event, True, refusal.KIND_HARD)
            self.assertEqual(hard["shape"], refusal.KIND_HARD)       # 给了就记大类

    def test_record_refuses_a_blank_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                refusal.record(os.path.join(tmp, "refuse.jsonl"),
                               refusal.event("topic", "有理由", NOW, table=BOOK), True, "  ")


class SoftJudgementFollowsTheData(unittest.TestCase):
    """第 5 条之五：判据改数据即改行为——openers 清空，同一段就不再是软转向。"""

    def _book_in(self, tmp: str, edit) -> dict:
        """把整份数据写进临时目录、改一处，再按那个根目录读回来（验的是「读数据」）。"""
        book = _copy_book()
        edit(book)
        Path(tmp, "refusal.json").write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        return load("refusal", root=tmp)

    def test_clearing_the_openers_stops_the_recognition(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleared = self._book_in(tmp, lambda book: book["soft"].__setitem__("openers", []))
            self.assertEqual(cleared["soft"]["openers"], [])
            scan = refusal.soft_scan(SOFT_ANSWER, SOFT_ASKED, table=cleared)
            self.assertFalse(scan.is_soft)                           # 同一段，词表清空就不认了
            verdict = refusal.enforce("topic", cleared, CLAUSES, now=NOW, asked=SOFT_ASKED,
                                      answer=SOFT_ANSWER, refuse_sink=lambda event: None)
            self.assertEqual(verdict.kind, refusal.KIND_HARD)
            self.assertEqual(verdict.text, cleared["shapes"]["topic"]["say"])
        self.assertTrue(refusal.soft_scan(SOFT_ANSWER, SOFT_ASKED).is_soft)   # 原数据照样认

    def test_clearing_both_marker_tables_stops_it_too(self):
        def clear_both(book):
            book["soft"]["redirect_markers"] = []
            book["soft"]["question_markers"] = []
        with tempfile.TemporaryDirectory() as tmp:
            cleared = self._book_in(tmp, clear_both)
            self.assertFalse(refusal.soft_scan(SOFT_ANSWER, SOFT_ASKED, table=cleared).is_soft)

    def test_either_marker_table_is_enough(self):
        """转移与反问是「或」不是「与」：只留一边，同一段照样认得出（判据写在数据那张表上）。"""
        for kept, dropped in (("redirect_markers", "question_markers"),
                              ("question_markers", "redirect_markers")):
            with tempfile.TemporaryDirectory() as tmp:
                cleared = self._book_in(tmp, lambda book: book["soft"].__setitem__(dropped, []))
                self.assertTrue(refusal.soft_scan(SOFT_ANSWER, SOFT_ASKED, table=cleared).is_soft,
                                kept)

    def test_missing_soft_section_raises_and_names_it(self):
        book = _copy_book()
        del book["soft"]
        with self.assertRaises(RefusalBroken) as caught:
            refusal.soft_scan(SOFT_ANSWER, SOFT_ASKED, table=book)
        self.assertIn(refusal.SOFT_KEY, str(caught.exception))

    def test_missing_key_names_key_and_section(self):
        book = _copy_book()
        del book["soft"]["answer_markers"]
        with self.assertRaises(RefusalBroken) as caught:
            refusal.soft_scan(SOFT_ANSWER, SOFT_ASKED, table=book)
        self.assertIn("answer_markers", str(caught.exception))

    def test_broken_pattern_raises_instead_of_reading_as_a_miss(self):
        book = _copy_book()
        book["soft"]["openers"] = ["(这个啊"]
        with self.assertRaises(RefusalBroken) as caught:
            refusal.soft_scan(SOFT_ANSWER, SOFT_ASKED, table=book)
        self.assertIn("(这个啊", str(caught.exception))


class SoftEdges(unittest.TestCase):
    """第 5 条之六：只有语气词、或只有转移没有垫话，都不是软转向。"""

    def test_only_an_opener_is_not_a_redirect(self):
        scan = refusal.soft_scan("行了行了，我知道了。", SOFT_ASKED)
        self.assertFalse(scan.is_soft)
        self.assertIn(scan.opener, SOFT["openers"])                  # 垫话确实命中了
        self.assertEqual((scan.redirect, scan.question), ("", ""))    # 可没有转移，也没有反问

    def test_redirect_without_an_opener_is_not_soft(self):
        scan = refusal.soft_scan("鱼游得挺远的。倒是你呢？", SOFT_ASKED)
        self.assertFalse(scan.is_soft)
        self.assertEqual(scan.opener, "")
        self.assertTrue(scan.redirect or scan.question)              # 转移/反问在，但没有垫话

    def test_leading_punctuation_does_not_hide_the_opener(self):
        plain = "这个啊，倒是你呢？"
        self.assertTrue(refusal.soft_scan(plain, SOFT_ASKED).is_soft)
        self.assertTrue(refusal.soft_scan("……" + plain, SOFT_ASKED).is_soft)


if __name__ == "__main__":
    unittest.main()
