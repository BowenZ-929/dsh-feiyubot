"""闸只管判：命中给一句人话、改一次还不行就弃。

条款一个字都不写在这份文件里：清单从 organs/mouth/data/clauses.json 读（走 warehouse），
要改判据就改数据、不改测试、更不改 gate.py。凡是出现具体字眼的地方都在数据里，
这里只出现 id 与 kind 这类结构性标签。

「条款改一条，行为跟着变」是这套闸的全部意义，所以下面有一组测试把清单拷到临时目录、
改一条再按同样的读法读回来——验的是「读数据」，不是验内存里那个 dict。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from organs.mouth import audit as recitation
from organs.mouth import gate
from organs.mouth.action_note import ActionBroken
from organs.mouth.audit import AuditBroken
from organs.mouth.gate import ClauseBroken, Hit, Verdict, check, enforce
from organs.mouth.warehouse import load

CLAUSES = load("clauses")["clauses"]

# 一句真实的客服腔：开场白 + 排比 + 收尾承诺，三件事凑齐
SPEECH = ("您好，请问有什么可以帮您的吗？"
          "首先请您确认一下订单号，其次我们会尽快处理，综上感谢您的理解。")


def _human(text: str) -> str:
    """确定性桩重写器：按条款顺序把命中的片段整段删掉，再收一遍标点。

    「换成人话」在这里就是「删掉话术，只留实字」；不抄任何条款原文，只按清单里的正则走。
    """
    out = text
    for clause in CLAUSES:
        out = re.sub(clause["pattern"], "", out)
    out = re.sub(r"[，,]{2,}", "，", out)
    out = re.sub(r"[，,]\s*([。！？])", r"\1", out)
    return out.strip("，,。？！； ")


def _same(text: str) -> str:
    """确定性桩重写器：原样返回——改了等于没改。"""
    return text


def _write_and_load(tmp: str, data: dict) -> list:
    """把改过的清单落进临时目录，再按仓库的读法读回来（读法与线上一致，只换根目录）。"""
    path = os.path.join(tmp, "clauses.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    return load("clauses", root=tmp)["clauses"]


def _all_on(clauses: list) -> list:
    """把每条都显式打开的副本：验的是「条款本身还成不成立」，不受默认停用影响。

    默认清单里 explainer-connector 是停用的（「首先」「总之」在口语里太常见，默认开着等于给
    误杀留口子），所以凡是拿它当素材的用例，先把它打开——停用这件事另有专门的测试，
    不许因为默认停了就悄悄少验一条正则。
    """
    return [dict(clause, enabled=True) for clause in clauses]


class CustomerServiceSpeech(unittest.TestCase):
    """第 1 条：典型客服腔被拦下，而且拦它的是一句人话。"""

    def test_customer_service_speech_is_stopped(self):
        hit = check(SPEECH, CLAUSES)
        self.assertIsInstance(hit, Hit)                    # 不是布尔，是一条命中的证据
        self.assertEqual(hit.clause_id, "courtesy-open")
        self.assertEqual(hit.kind, "反例")
        self.assertTrue(hit.matched)                        # 命中的那段原文要带回来
        self.assertIn(hit.matched, SPEECH)
        self.assertIn("「%s」" % hit.matched, hit.reason)    # 人话原因里点名命中片段
        self.assertGreater(len(hit.reason), 12)             # 一句话，不是一个词

    def test_plain_speech_passes(self):
        """没毛病的口语不许误伤：返回 None，而不是空 Hit。"""
        self.assertIsNone(check("订单号发我，我看看。", CLAUSES))
        self.assertIsNone(check("", CLAUSES))

    def test_clause_order_is_priority(self):
        """条款顺序即优先级：同一句换个顺序，先跳出来的就是另一条。"""
        on = _all_on(CLAUSES)              # 倒序时先跳出来的那条默认是停用的，先打开它
        self.assertEqual(check(SPEECH, on).clause_id, "courtesy-open")
        flipped = list(reversed(on))
        self.assertEqual(check(SPEECH, flipped).clause_id, "explainer-connector")


class FalseKillsComeBack(unittest.TestCase):
    """误杀回归：正常话一条都不许拦。

    闸是 fail-closed（命中 → 重写一次 → 二次命中就弃），所以误杀的代价是她干脆**不说话**，
    比漏拦一句贵得多。下面这些都是「我帮你做事」的普通句子，主语不是柜台，必须原样放行。
    """

    NORMAL = (
        "你好呀，我帮你拿吧。",
        "你好，我帮你把窗关上了。",
        "你好，我帮你问过了，他说行。",
        "我帮你查了下，明天有票。",
        "需要我帮你说一声吗。",
        "帮你留了半碗，别饿着。",
        "我可以为你做顿饭。",
        "你好，请问我帮你问的那件事怎么样了。",
    )

    def test_normal_speech_never_hits(self):
        for text in self.NORMAL:
            with self.subTest(text=text):
                self.assertIsNone(check(text, CLAUSES))

    def test_normal_speech_is_sent_untouched(self):
        for text in self.NORMAL:
            with self.subTest(text=text):
                verdict = enforce(text, CLAUSES, _human)
                self.assertEqual(verdict.decision, "pass")
                self.assertEqual(verdict.text, text)


class CounterStructureStillHits(unittest.TestCase):
    """收紧之后柜台句式还得拦得住：收紧不是把条款弄成摆设。"""

    FORMS = (
        "请问有什么可以帮您的吗",
        "有什么能帮到你的吗",
        "我能为您效劳吗",
    )

    def test_counter_forms_still_hit(self):
        for text in self.FORMS:
            with self.subTest(text=text):
                hit = check(text, CLAUSES)
                self.assertIsNotNone(hit)
                self.assertEqual(hit.clause_id, "courtesy-open")
                self.assertTrue(hit.matched)


class EnforceOnceAndDrop(unittest.TestCase):
    """第 2–4 条：改写一次就过 / 二次命中即弃 / 改不了也弃。"""

    def test_rewrite_once_passes(self):
        verdict = enforce(SPEECH, CLAUSES, _human)
        self.assertIsInstance(verdict, Verdict)
        self.assertEqual(verdict.decision, "rewritten")
        self.assertEqual(verdict.text, _human(SPEECH))
        self.assertNotEqual(verdict.text, SPEECH)           # 文本真的变了
        self.assertEqual(len(verdict.hits), 1)              # 只有改写前那一次命中
        self.assertEqual(verdict.hits[0].clause_id, "courtesy-open")   # 改写前拦它的仍是那条
        self.assertIsNone(check(verdict.text, CLAUSES))     # 第二次不再命中
        self.assertIn("不再命中", verdict.reason)

    def test_second_hit_drops(self):
        verdict = enforce(SPEECH, CLAUSES, _same)
        self.assertEqual(verdict.decision, "dropped")
        self.assertEqual(verdict.text, "")                  # 命中句一个字也不许发出去
        self.assertEqual(len(verdict.hits), 2)              # 两次命中都在，按发生顺序
        self.assertEqual(verdict.hits[0].clause_id, "courtesy-open")
        self.assertEqual(verdict.hits[1].clause_id, "courtesy-open")
        self.assertIn("改了一次还是这样", verdict.reason)
        self.assertIn("弃掉", verdict.reason)

    def test_rewrite_missing_drops(self):
        """rewrite=None 视为改不了：直接弃，不许 fail-open 把命中句发出去。"""
        verdict = enforce(SPEECH, CLAUSES, None)
        self.assertEqual(verdict.decision, "dropped")
        self.assertEqual(verdict.text, "")
        self.assertEqual(len(verdict.hits), 1)
        self.assertIn("改不了", verdict.reason)             # 是「改不了」，不是「没命中」
        self.assertIn("没给重写器", verdict.reason)
        self.assertNotIn("没命中", verdict.reason)

    def test_rewrite_error_drops(self):
        """重写器自己炸了也只算改不了：错误写进 reason，出口不抛错。"""
        def boom(text: str) -> str:
            raise RuntimeError("模型没回话")

        verdict = enforce(SPEECH, CLAUSES, boom)
        self.assertEqual(verdict.decision, "dropped")
        self.assertEqual(verdict.text, "")
        self.assertIn("RuntimeError", verdict.reason)
        self.assertIn("模型没回话", verdict.reason)

    def test_rewrite_empty_drops(self):
        """改回来是一段空白，等于没改：同样弃掉，不拿空串冒充通过。"""
        verdict = enforce(SPEECH, CLAUSES, lambda text: "   ")
        self.assertEqual(verdict.decision, "dropped")
        self.assertEqual(verdict.text, "")
        self.assertIn("空的", verdict.reason)

    def test_pass_carries_no_hits(self):
        verdict = enforce("订单号发我，我看看。", CLAUSES, _human)
        self.assertEqual(verdict.decision, "pass")
        self.assertEqual(verdict.text, "订单号发我，我看看。")
        self.assertEqual(verdict.hits, [])
        self.assertIn("没命中", verdict.reason)


class ClausesComeFromData(unittest.TestCase):
    """第 5 条：条款改一条，行为跟着变——删掉、改掉、加一条，三种都验。"""

    SINGLE = "首先你要把订单号给我。"        # 只该踩 explainer-connector 这一条

    def test_clause_removed_no_longer_hits(self):
        on = _all_on(CLAUSES)              # 这素材踩的那条默认停用，先打开才验得到「删掉就不拦」
        self.assertIsNotNone(check(self.SINGLE, on))
        with tempfile.TemporaryDirectory() as tmp:
            data = {"clauses": [c for c in on if c["id"] != "explainer-connector"]}
            fewer = _write_and_load(tmp, data)
            self.assertIsNone(check(self.SINGLE, fewer))     # 删一条，同一句立刻不拦

    def test_clause_edited_no_longer_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"clauses": []}
            for clause in _all_on(CLAUSES):
                if clause["id"] == "explainer-connector":
                    clause = dict(clause, pattern="这句不匹配任何东西")
                data["clauses"].append(clause)
            edited = _write_and_load(tmp, data)
            self.assertIsNone(check(self.SINGLE, edited))    # 改一条，行为跟着改
            self.assertIsNotNone(check(SPEECH, edited))      # 别的条款还在，没被一起关掉

    def test_clause_added_blocks_previously_passing(self):
        passing = "订单号发我，我看看。"
        self.assertIsNone(check(passing, CLAUSES))           # 原本通过
        with tempfile.TemporaryDirectory() as tmp:
            data = {"clauses": list(CLAUSES) + [{
                "id": "extra-ban",
                "kind": "反例",
                "pattern": "订单号",
                "reason": "临时加的一条：这个词这一轮不许出现",
                "note": "只在这份临时清单里存在",
            }]}
            more = _write_and_load(tmp, data)
            hit = check(passing, more)
            self.assertIsNotNone(hit)                        # 加一条，原本通的那句被拦下
            self.assertEqual(hit.clause_id, "extra-ban")


class BrokenClausesAreLoud(unittest.TestCase):
    """第 6 条：坏条款当场响，而且报错要点名是哪一条。"""

    def test_missing_pattern_names_the_clause(self):
        bad = [{"id": "no-pattern", "kind": "反例", "reason": "缺 pattern 的坏条款"}]
        with self.assertRaises(ClauseBroken) as caught:
            check("随便一句", bad)
        self.assertIn("no-pattern", str(caught.exception))
        self.assertIn("pattern", str(caught.exception))

    def test_bad_regex_names_the_clause(self):
        bad = [{"id": "bad-regex", "kind": "可判废", "pattern": "(未闭合", "reason": "正则编译不过"}]
        with self.assertRaises(ClauseBroken) as caught:
            check("随便一句", bad)
        self.assertIn("bad-regex", str(caught.exception))
        self.assertIn("正则编译不过", str(caught.exception))

    def test_duplicate_id_names_the_clause(self):
        bad = [{"id": "twice", "kind": "反例", "pattern": "一", "reason": "第一条"},
               {"id": "twice", "kind": "可判废", "pattern": "二", "reason": "第二条"}]
        with self.assertRaises(ClauseBroken) as caught:
            check("随便一句", bad)
        self.assertIn("twice", str(caught.exception))
        self.assertIn("重复", str(caught.exception))

    def test_enforce_also_refuses_broken_clauses(self):
        bad = [{"id": "no-reason", "kind": "反例", "pattern": "一"}]
        with self.assertRaises(ClauseBroken) as caught:
            enforce("随便一句", bad, _human)
        self.assertIn("no-reason", str(caught.exception))

    def test_empty_list_is_not_a_free_pass(self):
        """空清单等于没闸，不许静默放行。"""
        with self.assertRaises(ClauseBroken):
            check("随便一句", [])


class SinkSeesEveryDrop(unittest.TestCase):
    """弃言要留痕迹：闸只把「谁拦的、为什么」交给注入的 sink，记到哪儿不是闸的事。"""

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_dropped_reports_the_clause_that_finally_stopped_it(self):
        seen, sink = self._spy()
        verdict = enforce(SPEECH, CLAUSES, _same, sink)     # 改一次还是命中 → dropped
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual(1, len(seen), "一次弃言只记一笔")
        self.assertEqual(verdict.hits[-1].clause_id, seen[0][0])
        self.assertEqual(verdict.reason, seen[0][1])        # 记的是那句人话，不是整句原文

    def test_pass_and_rewrite_leave_no_trace(self):
        seen, sink = self._spy()
        self.assertEqual("pass", enforce("订单号发我，我看看。", CLAUSES, _human, sink).decision)
        self.assertEqual("rewritten", enforce(SPEECH, CLAUSES, _human, sink).decision)
        self.assertEqual([], seen, "话发出去了，就没有弃言可记")

    def test_missing_rewriter_also_reports(self):
        seen, sink = self._spy()
        enforce(SPEECH, CLAUSES, None, sink)     # 这一轮 /mouth/gate 走的就是这条路
        self.assertEqual(1, len(seen))
        self.assertEqual("courtesy-open", seen[0][0])
        self.assertIn("弃掉", seen[0][1])

    def test_sink_errors_are_not_swallowed(self):
        """记账失败也是失败：sink 抛错就照实往上抛，不许咽掉变成「今天 0 次」。"""
        def boom(clause_id: str, reason: str) -> None:
            raise OSError("账本目录不可写")

        with self.assertRaises(OSError):
            enforce(SPEECH, CLAUSES, None, boom)


class DisabledClausesAreVisible(unittest.TestCase):
    """停用只是跳过匹配，不是删除：跳过要说出来，全停用要当场响。"""

    ID = "explainer-connector"
    LINE = "总之先这样吧，明天再说。"       # 只踩 explainer-connector 这一条

    def test_default_list_keeps_it_off(self):
        found = [c for c in CLAUSES if c["id"] == self.ID]
        self.assertEqual(1, len(found), "默认清单里该有这条（停用不是删除）")
        self.assertIs(False, found[0].get("enabled"), "它该是 enabled=false")
        others = [c["id"] for c in CLAUSES if c["id"] != self.ID and "enabled" in c]
        self.assertEqual([], others, "其余条款按缺省打开，不许顺手写 enabled")

    def test_off_clause_is_skipped_but_named(self):
        self.assertIsNone(check(self.LINE, CLAUSES))
        verdict = enforce(self.LINE, CLAUSES, None)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual([self.ID], verdict.disabled, "跳过了哪条要说出来，不许静默")

    def test_turned_back_on_hits_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"clauses": [dict(c, enabled=True) if c["id"] == self.ID else c
                                for c in CLAUSES]}
            on = _write_and_load(tmp, data)
            hit = check(self.LINE, on)
            self.assertIsNotNone(hit)
            self.assertEqual(self.ID, hit.clause_id)

    def test_all_disabled_is_not_a_free_pass(self):
        """一条生效条款都没有 ≠ 没命中：那是这次压根没法判，当场响。"""
        data = [dict(c, enabled=False) for c in CLAUSES]
        with self.assertRaises(ClauseBroken) as caught:
            check("随便一句", data)
        self.assertIn("生效", str(caught.exception))
        with self.assertRaises(ClauseBroken):
            enforce("随便一句", data, _human)

    def test_enabled_must_be_a_boolean(self):
        bad = [{"id": "half-on", "kind": "反例", "pattern": "一", "reason": "enabled 写成字符串",
                "enabled": "false"}]
        with self.assertRaises(ClauseBroken) as caught:
            check("随便一句", bad)
        self.assertIn("half-on", str(caught.exception))
        self.assertIn("enabled", str(caught.exception))


class ReciteAuditRidesTheGate(unittest.TestCase):
    """复述审计接在条款后面：同一条路（改一次还不行就弃），状态挂在 Verdict.audit 上。

    素材与类别全从 data/audit.json 读：这里只验「接上了没有、判不了看得出来没有」。
    """

    AUDIT = load("audit")
    SOUL = AUDIT["setting_kinds"][0]
    VERDICT = AUDIT["verdicts"]["setting"]
    LINE = "我嘴上不认，行动先到。"
    INJECT = [{"kind": SOUL, "text": "嘴上不认，行动先到：拒绝的话说完，事已经做了"}]

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_clause_comes_first(self):
        """条款先判（既有逻辑不动）：两边都撞时先报条款。"""
        hit = check(SPEECH, CLAUSES, self.INJECT)
        self.assertEqual("courtesy-open", hit.clause_id)

    def test_check_sees_recitation(self):
        hit = check(self.LINE, CLAUSES, self.INJECT)
        self.assertIsInstance(hit, Hit)
        self.assertEqual(self.VERDICT, hit.clause_id)       # 违规类别（数据里的说法）
        self.assertEqual(self.SOUL, hit.kind)               # 来源段是哪一类
        self.assertIn("嘴上不认", hit.matched)
        self.assertIn(self.SOUL, hit.reason)

    def test_without_rewriter_it_is_dropped(self):
        seen, sink = self._spy()
        verdict = enforce(self.LINE, CLAUSES, None, sink, inject=self.INJECT)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)                   # 复述的那句一个字也不发
        self.assertEqual(self.VERDICT, verdict.hits[0].clause_id)
        self.assertEqual(1, len(seen))                       # 弃言照旧记一笔
        self.assertEqual(self.VERDICT, seen[0][0])
        self.assertEqual("ok", verdict.audit.status)

    def test_rewrite_clears_it(self):
        verdict = enforce(self.LINE, CLAUSES, lambda text: "我记下了。", inject=self.INJECT)
        self.assertEqual("rewritten", verdict.decision)
        self.assertIsNone(check(verdict.text, CLAUSES, self.INJECT))   # 改完真的不再复述
        self.assertIsNotNone(verdict.audit.hit)              # 读数记的是进门那一段

    def test_unavailable_is_visible_but_not_dropped(self):
        """没给注入原文：不弃这句（缺料不拿她的沉默兜），但状态必须看得出来。"""
        verdict = enforce(self.LINE, CLAUSES, None)
        self.assertEqual("pass", verdict.decision)           # 决策与既有五种结局一字不变
        self.assertEqual(self.LINE, verdict.text)
        self.assertIsNotNone(verdict.audit)
        self.assertEqual(recitation.STATUS_UNAVAILABLE, verdict.audit.status)
        self.assertIsNone(verdict.audit.hit)
        self.assertTrue(verdict.audit.reason)                # 有一句人话
        self.assertEqual(self.AUDIT["threshold"], verdict.audit.threshold)
        self.assertIsNone(check(self.LINE, CLAUSES))          # check 不承载状态，看 audit

    def test_threshold_comes_from_policy(self):
        policy = dict(self.AUDIT, threshold=self.AUDIT["threshold"] + 10)
        self.assertIsNone(check(self.LINE, CLAUSES, self.INJECT, policy))
        self.assertIsNone(enforce(self.LINE, CLAUSES, None, None, self.INJECT, policy).audit.hit)

    def test_broken_inject_is_loud(self):
        with self.assertRaises(AuditBroken) as caught:
            enforce(self.LINE, CLAUSES, None, None, [{"kind": "没有这一类", "text": "随便一句"}])
        self.assertIn("没有这一类", str(caught.exception))


class ActionNoteRidesTheGate(unittest.TestCase):
    """动作括注（第三轮 B）接在条款后面：同一条路（改一次还不行就弃），判没判挂在 action_status 上。

    判据表与闸用的两个名字全从 data/action_note.json 读：这里只验「接上了没有、判没判看得出来没有」。
    """

    NOTE = load("action_note")
    CLAUSE_ID = NOTE["hit"]["clause_id"]
    KIND = NOTE["hit"]["kind"]
    LINE = "（尾巴尖在水里轻轻蜷了一下）"      # 括号里带了没给过的场景（她真写过的那句）
    CLEAN = "（主人夸我了嘿嘿）"                # 括号里有话，但没有场景词
    REWRITTEN = "尾巴垂下去。"                  # 改写之后不再带场景

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_check_reports_it(self):
        hit = check(self.LINE, CLAUSES, action=self.NOTE)
        self.assertIsInstance(hit, Hit)
        self.assertEqual(self.CLAUSE_ID, hit.clause_id)      # 名字取自数据，闸不自己造
        self.assertEqual(self.KIND, hit.kind)
        self.assertIn("没给过", hit.reason)                   # 是一句人话，说清毛病在哪

    def test_check_does_not_judge_without_the_table(self):
        """没给判据表就整段不跑——但也不许把「没跑」说成「没问题」。"""
        self.assertIsNone(check(self.LINE, CLAUSES))
        self.assertIsNone(check(self.CLEAN, CLAUSES, action=self.NOTE))   # 给了表也没命中

    def test_missing_rewriter_drops_and_the_ledger_names_it(self):
        seen, sink = self._spy()
        verdict = enforce(self.LINE, CLAUSES, None, sink, action=self.NOTE)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)                   # 带场景的那句一个字也不发
        self.assertEqual(self.CLAUSE_ID, verdict.hits[0].clause_id)
        self.assertEqual("ok", verdict.action_status)         # 这一关真的判过了
        self.assertEqual(1, len(seen), "一次弃言只记一笔")
        self.assertEqual(self.CLAUSE_ID, seen[0][0])          # 账本里是数据里那个名字
        self.assertEqual(verdict.reason, seen[0][1])
        self.assertIn("没给重写器", verdict.reason)

    def test_rewrite_clears_it(self):
        verdict = enforce(self.LINE, CLAUSES, lambda text: self.REWRITTEN, action=self.NOTE)
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.REWRITTEN, verdict.text)
        self.assertEqual("ok", verdict.action_status)
        self.assertIsNone(check(verdict.text, CLAUSES, action=self.NOTE))  # 改完真的不再带场景

    def test_second_hit_drops(self):
        seen, sink = self._spy()
        verdict = enforce(self.LINE, CLAUSES, _same, sink, action=self.NOTE)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertEqual([self.CLAUSE_ID, self.CLAUSE_ID],
                         [one.clause_id for one in verdict.hits])   # 两次命中都在，按发生顺序
        self.assertIn("改了一次还是这样", verdict.reason)
        self.assertEqual(1, len(seen))

    def test_not_checked_is_not_the_same_as_checked_and_clean(self):
        """验收 5：「没判」与「判过没问题」必须分得开——同一句、同一份结局，状态不一样。"""
        unchecked = enforce(self.CLEAN, CLAUSES, None)
        checked = enforce(self.CLEAN, CLAUSES, None, action=self.NOTE)
        self.assertEqual("pass", unchecked.decision)
        self.assertEqual("pass", checked.decision)            # 结局一样：都放行
        self.assertEqual("not-checked", unchecked.action_status)
        self.assertEqual("ok", checked.action_status)          # 状态不一样：一个没判、一个判过
        self.assertNotEqual(unchecked.action_status, checked.action_status)
        self.assertIn(self.NOTE["reason"]["clear"], checked.reason)   # 放行要有据：查过了就说出来
        self.assertNotIn(self.NOTE["reason"]["clear"], unchecked.reason)

    def test_unchecked_still_lets_the_sentence_through(self):
        """没给表时按既有规矩放行（不替调用方做主），但它救不了带场景的那句：给表就拦。"""
        self.assertEqual("pass", enforce(self.LINE, CLAUSES, None).decision)
        self.assertEqual("dropped", enforce(self.LINE, CLAUSES, None, action=self.NOTE).decision)

    def test_clause_comes_first(self):
        """条款先判（既有逻辑不动）：两边都撞时先报条款。"""
        hit = check(SPEECH + self.LINE, CLAUSES, action=self.NOTE)
        self.assertEqual("courtesy-open", hit.clause_id)

    def test_broken_table_is_loud(self):
        """坏表当场响并点名（不许静默当成「没有判据」放行），空词表也算坏。"""
        with self.assertRaises(ActionBroken) as caught:
            enforce(self.LINE, CLAUSES, _human, action=dict(self.NOTE, env_words=[]))
        self.assertIn("env_words", str(caught.exception))
        with self.assertRaises(ActionBroken) as caught:
            enforce(self.LINE, CLAUSES, _human, action={})
        self.assertIn("where", str(caught.exception))


class GateStaysClean(unittest.TestCase):
    """第 7 条与硬约束：代码里没有条款，也没有时钟、随机数、宿主。"""

    def setUp(self):
        self.source = Path(gate.__file__).read_text(encoding="utf-8")

    def test_gate_source_has_no_clause_pattern(self):
        self.assertTrue(CLAUSES)                            # 清单非空，否则下面几条是空转
        for clause in CLAUSES:
            self.assertNotIn(clause["pattern"], self.source)
            self.assertNotIn(clause["reason"], self.source)

    def test_gate_stays_pure(self):
        self.assertNotRegex(self.source,
                            r"(?m)^\s*(?:import|from)\s+(?:time|datetime|random)\b")

    def test_gate_source_has_no_action_note_word(self):
        """动作括注的词表与名字也不许进闸：闸只接判据，不持判据。"""
        note = load("action_note")
        for word in note["env_words"]:
            self.assertNotIn(word, self.source)
        self.assertNotIn(note["hit"]["clause_id"], self.source)
        for line in note["reason"].values():
            self.assertNotIn(line, self.source)

    def test_gate_imports_no_host(self):
        for word in ("astrbot", "deepseek-ai", "cordis", "OneBot", "NapCat", "aiocqhttp"):
            self.assertNotIn(word, self.source)

    def test_gate_writes_nothing(self):
        """闸只判：收 sink 也不自己记账，写盘一律在 drops.py 与 api 层。"""
        for banned in ("open(", "write_text", ".mkdir", "json.dump", "os.remove", "shutil"):
            self.assertNotIn(banned, self.source)


if __name__ == "__main__":
    unittest.main()
