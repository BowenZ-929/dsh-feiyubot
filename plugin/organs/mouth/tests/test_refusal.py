"""拒绝要说成一句人话：三形状各有说法、说法全在数据里、还得过八股闸、还得投给心。

这句台词一个字都不写死在这份测试里：说法从 organs/mouth/data/refusal.json 读（走 warehouse），
改说法就是改数据。验四件事——

1. 三种形状各说一次，说的都是数据里那句（没被闸拦，也不是客服腔）；
2. **拒绝的话照样过闸**：把某条说法人为写成客服腔，必须被条款拦下——「这是拒绝」不是免检章；
3. 三形状都有事件可投：事件带 shape / 完整名 / why / ts，投递只走注入的 sink（None 就是不投）；
4. 账本记三行（refuse.jsonl），sent 落进每一行，「投过没投过」不必猜。
"""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
