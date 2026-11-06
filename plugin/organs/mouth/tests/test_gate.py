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
from organs.mouth import claim as claims
from organs.mouth import gate
from organs.mouth import repeat as repeats
from organs.mouth import rewrite as rewriting
from organs.mouth.action_note import ActionBroken
from organs.mouth.audit import AuditBroken
from organs.mouth import kin as kins
from organs.mouth.claim import ClaimBroken
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

    def test_second_hit_keeps_the_line(self):
        """改一次还是命中（非红线）→ 留原话发出，不再丢空。

        这条断言是被「二次命中不丢空」那条新规矩改准的（以前是 dropped + text=""）：在最该回答的
        那一句上沉默，比说话带点毛病糟得多。判据仍然判了：两次命中都在、账本有一行 after-rewrite。
        这一例没给红线表，所以 fatal_status 是 not-checked（判不了是不是红线），话照样发。
        """
        verdict = enforce(SPEECH, CLAUSES, _same)
        self.assertEqual(verdict.decision, "pass")
        self.assertEqual(verdict.text, SPEECH)              # 发的是进门那句原话
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertEqual(len(verdict.hits), 2)              # 两次命中都在，按发生顺序
        self.assertEqual(verdict.hits[0].clause_id, "courtesy-open")
        self.assertEqual(verdict.hits[1].clause_id, "courtesy-open")
        self.assertEqual("not-checked", verdict.fatal_status, "没给红线表：这一关没跑")
        self.assertIn(load("rewrite")["after_rewrite"]["marks"]["clause"], verdict.reason)
        self.assertIn("没拿到红线表", verdict.reason)        # 判不了这件事要在账上说出来

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

    def test_the_kept_line_still_reports_the_clause_that_stopped_it(self):
        """二次命中留原话也记一笔（这条断言被新规矩改准：以前是「弃掉那一笔」）。

        记的是第二次拦下它的那一条与那句人话——「这句改过、还是不行、按原话发了」，账面要看得见。
        """
        seen, sink = self._spy()
        verdict = enforce(SPEECH, CLAUSES, _same, sink)     # 改一次还是命中 → 留原话 + 记一笔
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(1, len(seen), "留原话也要留一笔账")
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


class RecitedLineIsCutOut(unittest.TestCase):
    """复述命中之后：只删被审计命中的那一句，不把整泡弃成空串（这一轮改的）。

    一句话的判据：**宁可她少说一句，不许她整句不说**。以前重写改不动、或改完还照搬，整条弃成空串——
    她沉默了，比念台词更糟。素材与记号全从 data/audit.json 读，这里只验四件事：删哪一句、删完剩下
    的照发、只剩那一句时才弃（reason 带 recited-only）、删完剩下的正文照样过其余判据。
    """

    BOOK = load("audit")
    RECITED = BOOK["recited"]                 # 记号与那句人话都在数据里
    MARKS = BOOK["sentence_marks"]
    REPEAT = load("repeat")
    SOUL = BOOK["setting_kinds"][0]
    VERDICT = BOOK["verdicts"]["setting"]
    SETTING = "背着主人偷吃"                   # 魂里那段原文（素材，不是判据）
    REST = "今天风挺大的"                      # 同一泡里的另一句（素材）
    BOTH = SETTING + MARKS[0] + REST + MARKS[0]

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def _inject(self, *texts):
        return [{"kind": self.SOUL, "text": text} for text in (texts or (self.SETTING,))]

    def test_only_the_recited_sentence_is_cut_out(self):
        """一泡两句 + 重写回原样：出包只剩另一句，两条命中都留着，账上写清删了哪一句。"""
        seen, sink = self._spy()
        verdict = enforce(self.BOTH, CLAUSES, _same, sink,
                          inject=self._inject(), policy=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertTrue(verdict.salvaged, "救回来的那一笔要看得出来")
        self.assertFalse(verdict.kept_after_rewrite, "留原话就是留一句抄来的话")
        self.assertEqual(self.REST + self.MARKS[0], verdict.text)
        self.assertEqual(2, len(verdict.hits), "改前改后两次命中都留着")
        self.assertEqual(self.VERDICT, verdict.hits[-1].clause_id)
        self.assertIn(self.VERDICT, seen[0][0])
        self.assertIn(self.RECITED["status"], seen[0][1])
        self.assertIn(self.SETTING, seen[0][1], "账上要说清删掉的是哪一句")
        self.assertIn(self.RECITED["status"], verdict.reason)

    def test_a_bubble_with_only_that_sentence_is_dropped(self):
        """一泡只一句且念设定：删完没正文可发才弃，reason 带 recited-only（不是「整条判废」）。"""
        seen, sink = self._spy()
        verdict = enforce(self.SETTING + self.MARKS[0], CLAUSES, _same, sink,
                          inject=self._inject(), policy=self.BOOK)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertFalse(verdict.salvaged)
        self.assertIn(self.RECITED["status"], verdict.reason)
        self.assertIn(self.RECITED["status"], seen[0][1])

    def test_a_rewrite_that_raises_still_keeps_the_rest(self):
        """重写抛错（超时那一格）：退化成删那一句，不许整泡消失。"""
        seen, sink = self._spy()

        def _slow(text):
            raise TimeoutError("改写超时")

        verdict = enforce(self.BOTH, CLAUSES, _slow, sink,
                          inject=self._inject(), policy=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertTrue(verdict.salvaged)
        self.assertEqual(self.REST + self.MARKS[0], verdict.text)
        self.assertIn(self.RECITED["status"], seen[0][1])

    def test_without_a_rewriter_it_still_keeps_the_rest(self):
        """没给重写器：同样删那一句，不再「改不了就弃空」。"""
        seen, sink = self._spy()
        verdict = enforce(self.BOTH, CLAUSES, None, sink,
                          inject=self._inject(), policy=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertTrue(verdict.salvaged)
        self.assertEqual(self.REST + self.MARKS[0], verdict.text)
        self.assertEqual(1, len(seen))
        self.assertIn(self.SETTING, seen[0][1])

    def test_a_comma_is_not_a_sentence_end(self):
        """逗号不算句末：删的是含逗号的那一整句，后半截跟着一起走，别的句子一个字不动。"""
        seen, sink = self._spy()
        line = self.SETTING + "，" + "你别学" + self.MARKS[0] + self.REST + self.MARKS[0]
        verdict = enforce(line, CLAUSES, None, sink,
                          inject=self._inject(), policy=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.REST + self.MARKS[0], verdict.text)
        self.assertIn("你别学", seen[0][1], "被删掉的是含逗号的那一整句")

    def test_two_recited_sentences_are_cut_one_by_one(self):
        """删完剩的正文还在照搬：接着删那一句，递归深度由句数封顶（删到没正文可发才弃）。"""
        seen, sink = self._spy()
        verdict = enforce(self.BOTH, CLAUSES, None, sink,
                          inject=self._inject(self.SETTING, self.REST), policy=self.BOOK)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertEqual(1, len(seen), "弃掉的那一次记一笔账")
        self.assertIn(self.RECITED["status"], seen[0][1])
        self.assertEqual(2, len(verdict.hits), "两句照搬各留一条命中，弃的时候都在")

    def test_the_rest_of_the_bubble_still_goes_through_the_other_judges(self):
        """删过不等于放行：剩下的正文撞上内容重复，照旧弃掉，reason 用数据里那句 leftover。"""
        seen, sink = self._spy()
        verdict = enforce(self.BOTH, CLAUSES, None, sink, inject=self._inject(),
                          policy=self.BOOK, recent=[self.REST + self.MARKS[0]],
                          repeat_table=self.REPEAT)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertIn(self.REPEAT["hit"]["clause_id"], verdict.reason)
        self.assertIn(self.RECITED["status"], seen[-1][1])


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

    def test_second_hit_keeps_the_line(self):
        """二次命中（非红线）留原话：这条断言是被「二次命中不丢空」那条新规矩改准的。

        动作括注仍然是拦下它的那一条（两次命中都在、账本一行 action-after-rewrite），只是这一句
        不再整句消失。
        """
        seen, sink = self._spy()
        verdict = enforce(self.LINE, CLAUSES, _same, sink, action=self.NOTE)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.LINE, verdict.text)
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertEqual([self.CLAUSE_ID, self.CLAUSE_ID],
                         [one.clause_id for one in verdict.hits])   # 两次命中都在，按发生顺序
        self.assertEqual(1, len(seen))
        self.assertIn(load("rewrite")["after_rewrite"]["marks"]["action"], seen[0][1])

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

    def test_gate_source_has_no_rewrite_word(self):
        """重写的说法、三条铁律与那两条正则也不许进闸：闸只接判据，不持判据。"""
        book = load("rewrite")
        for word in (book["keep"]["digits"], book["keep"]["latin"], book["prompt"]["head"]):
            self.assertNotIn(word, self.source)
        for line in book["prompt"]["rules"]:
            self.assertNotIn(line, self.source)
        for line in book["reason"].values():
            self.assertNotIn(line.replace("{why}", "x"), self.source)
        for line in book["why"].values():
            self.assertNotIn(line.replace("{budget_ms}", "1").replace("{error}", "x"), self.source)

    def test_gate_imports_no_host(self):
        for word in ("astrbot", "deepseek-ai", "cordis", "OneBot", "NapCat", "aiocqhttp"):
            self.assertNotIn(word, self.source)

    def test_gate_writes_nothing(self):
        """闸只判：收 sink 也不自己记账，写盘一律在 drops.py 与 api 层。"""
        for banned in ("open(", "write_text", ".mkdir", "json.dump", "os.remove", "shutil"):
            self.assertNotIn(banned, self.source)


class RepeatRidesTheGate(unittest.TestCase):
    """内容去重接在动作括注后面、复述审计前面：同一条路（改一次还不行就弃），判没判挂在 repeat_status 上。

    素材与名字全从 data/repeat.json 读：这里只验「接上了没有、拿不到历史看得出来没有」。
    """

    BOOK = load("repeat")
    CLAUSE_ID = BOOK["hit"]["clause_id"]
    KIND = BOOK["hit"]["kind"]
    TAG = BOOK["tag"]
    # 第 1 条是原句；第 2 条换了开场、动作行只差标点；第 3 条换了动作行、开场同第 1 条
    FIRST = "主人回来啦，我把碗放桌上了。\n（尾巴尖蜷了一下）"
    SECOND = "天有点凉了，你多穿点。\n（尾巴尖蜷了一下。）"
    THIRD = "主人回来啦，灯我给你留着呢。"
    CLEAN = "明天风大，我把窗关好了。"

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_check_reports_it(self):
        hit = check(self.THIRD, CLAUSES, recent=[self.FIRST, self.SECOND])
        self.assertIsInstance(hit, Hit)
        self.assertEqual(self.CLAUSE_ID, hit.clause_id)       # 两个名字都取自数据，闸不自己造
        self.assertEqual(self.KIND, hit.kind)
        self.assertIn(self.TAG, hit.reason)                   # 弃言账靠这个记号认判据
        self.assertIn(hit.matched, hit.reason)                # 人话里点名撞上的那几个字

    def test_check_does_not_judge_without_history(self):
        """recent 为 None 就整段不跑——但也不许把「没跑」说成「没问题」。"""
        self.assertIsNone(check(self.THIRD, CLAUSES))
        self.assertIsNone(check(self.CLEAN, CLAUSES, recent=[self.FIRST]))   # 给了历史也没命中

    def test_clause_comes_first(self):
        """条款先判（既有逻辑不动）：两边都撞时先报条款。"""
        hit = check(SPEECH + self.THIRD, CLAUSES, recent=[self.FIRST])
        self.assertEqual("courtesy-open", hit.clause_id)

    def test_action_note_comes_before_repeat(self):
        """判据顺序：条款 → 动作括注 → 内容去重。两边都撞时先报动作括注。"""
        note = load("action_note")
        line = "（尾巴尖在水里轻轻蜷了一下）"
        hit = check(line, CLAUSES, action=note, recent=[line])
        self.assertEqual(note["hit"]["clause_id"], hit.clause_id)

    def test_first_line_with_empty_history_passes(self):
        verdict = enforce(self.FIRST, CLAUSES, None, recent=[])
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.FIRST, verdict.text)
        self.assertEqual("ok", verdict.repeat_status)          # 判过了
        self.assertIn(self.BOOK["reason"]["clear"].format(rounds=self.BOOK["rounds"]),
                      verdict.reason)                          # 放行要有据

    def test_second_line_is_rewritten(self):
        verdict = enforce(self.SECOND, CLAUSES, lambda text: self.CLEAN, recent=[self.FIRST])
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.CLEAN, verdict.text)
        self.assertEqual(1, len(verdict.hits))
        self.assertEqual(self.CLAUSE_ID, verdict.hits[0].clause_id)
        self.assertIsNone(check(verdict.text, CLAUSES, recent=[self.FIRST]))   # 改完真的不再重复

    def test_third_line_kept_and_the_ledger_carries_the_tag(self):
        """二次命中留原话（这条断言被新规矩改准了：以前 dropped + text=""）。

        「她复读了」这件事仍然看得见：两次命中都在、账本里是数据里那个名字、reason 带 repeat 的记号
        与 after-rewrite 的记号。
        """
        seen, sink = self._spy()
        verdict = enforce(self.THIRD, CLAUSES, _same, sink, recent=[self.FIRST, self.SECOND])
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.THIRD, verdict.text)              # 发的是进门那句原话
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertEqual([self.CLAUSE_ID, self.CLAUSE_ID],
                         [one.clause_id for one in verdict.hits])   # 两次命中都在，按发生顺序
        self.assertEqual(1, len(seen), "留原话也要留一笔账")
        self.assertEqual(self.CLAUSE_ID, seen[0][0])           # 账本里是数据里那个名字
        self.assertIn(self.TAG, seen[0][1])                    # reason 里带着 repeat 记号
        self.assertIn(load("rewrite")["after_rewrite"]["marks"]["repeat"], seen[0][1])
        self.assertEqual(verdict.reason, seen[0][1])

    def test_rewritten_text_is_judged_against_the_same_history(self):
        """改写回来又撞上同一句：第二次照样判（窗口没变，判据也没变），但留的是进门那句原话。

        这条断言被新规矩改准：以前 dropped + text=""。现在 decision 是 pass、text 是进门那句
        （THIRD，不是那个没改成的新稿 FIRST）、hits 两条都在。
        """
        verdict = enforce(self.THIRD, CLAUSES, lambda text: self.FIRST, recent=[self.FIRST])
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.THIRD, verdict.text)
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertEqual([self.CLAUSE_ID, self.CLAUSE_ID],
                         [one.clause_id for one in verdict.hits])

    def test_not_checked_is_not_the_same_as_checked_and_clean(self):
        """验收 4：同一句、同一份结局，状态必须分得开——一个没比、一个比过没重复。"""
        unchecked = enforce(self.CLEAN, CLAUSES, None)
        checked = enforce(self.CLEAN, CLAUSES, None, recent=[])
        self.assertEqual("pass", unchecked.decision)
        self.assertEqual("pass", checked.decision)             # 结局一样：都放行
        self.assertEqual("not-checked", unchecked.repeat_status)
        self.assertEqual("ok", checked.repeat_status)           # 状态不一样
        self.assertIn(load("repeat")["reason"]["clear"].format(rounds=self.BOOK["rounds"]),
                      checked.reason)
        self.assertNotIn(load("repeat")["reason"]["clear"].format(rounds=self.BOOK["rounds"]),
                         unchecked.reason)

    def test_unchecked_cannot_rescue_the_repeat(self):
        """拿不到历史时按既有规矩放行（不替调用方做主），但给历史就拦得下来。"""
        self.assertEqual("pass", enforce(self.THIRD, CLAUSES, None).decision)
        self.assertEqual("dropped", enforce(self.THIRD, CLAUSES, None, recent=[self.FIRST]).decision)

    def test_gate_source_has_no_repeat_word(self):
        """去重的名字与说法也不许进闸：闸只接判据，不持判据。"""
        source = Path(gate.__file__).read_text(encoding="utf-8")
        book = load("repeat")
        for word in (book["hit"]["clause_id"], book["hit"]["kind"], book["kinds"]["opening"],
                     book["kinds"]["action_line"]):
            self.assertNotIn(word, source)
        for line in book["reason"].values():
            self.assertNotIn(line.format(rounds=1, matched="", prev_no=1, tag="x"), source)


class ARequestToRepeatSkipsTheGate(unittest.TestCase):
    """asked 一路传进内容去重那一关：对方要她再说一遍时，这一关只记账、不判废。

    判据与名字一个都不在这份文件里：跳过用的 clause_id 与 reason 记号从 data/repeat.json 读
    （hit.skip_clause_id 与 skip_reason），这里只验「接上了没有、账记了没有」。
    """

    BOOK = load("repeat")
    CLAUSE_ID = BOOK["hit"]["clause_id"]
    SKIP_CLAUSE_ID = BOOK["hit"]["skip_clause_id"]
    FIRST = "主人回来啦，我把碗放桌上了。\n（尾巴尖蜷了一下）"
    THIRD = "主人回来啦，灯我给你留着呢。"
    CLEAN = "明天风大，我把窗关好了。"
    ASKED = "你刚才说的那句再说一遍"

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_check_returns_no_hit_when_the_user_asked_for_it(self):
        self.assertIsNone(check(self.THIRD, CLAUSES, recent=[self.FIRST], asked=self.ASKED))
        self.assertIsInstance(check(self.THIRD, CLAUSES, recent=[self.FIRST]), Hit)

    def test_enforce_sends_it_verbatim_and_records_one_row(self):
        seen, sink = self._spy()
        verdict = enforce(self.THIRD, CLAUSES, None, sink,
                          recent=[self.FIRST], asked=self.ASKED)
        self.assertEqual("pass", verdict.decision)                  # 话照发
        self.assertEqual(self.THIRD, verdict.text)                  # 一个字都不改
        self.assertEqual([], verdict.hits)                          # 跳过不是命中
        self.assertEqual(repeats.STATUS_SKIPPED, verdict.repeat_status)
        self.assertEqual(1, len(seen), "跳过也要留一行账")
        self.assertEqual(self.SKIP_CLAUSE_ID, seen[0][0])           # 账本里是数据里那个名字
        self.assertIn("skipped-by-request", seen[0][1])
        self.assertIn("skipped-by-request", verdict.reason)         # 面板上也看得见

    def test_without_the_request_it_is_still_dropped(self):
        seen, sink = self._spy()
        verdict = enforce(self.THIRD, CLAUSES, None, sink,
                          recent=[self.FIRST], asked="今天吃啥")
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertEqual("ok", verdict.repeat_status)               # 真的比过、判废了
        self.assertEqual(self.CLAUSE_ID, seen[0][0])

    def test_an_empty_asked_never_exempts(self):
        for asked in ("", None, "   "):
            with self.subTest(asked=asked):
                verdict = enforce(self.THIRD, CLAUSES, None, None,
                                  recent=[self.FIRST], asked=asked)
                self.assertEqual("dropped", verdict.decision)
                self.assertEqual("ok", verdict.repeat_status)

    def test_a_request_without_a_repeat_records_nothing(self):
        seen, sink = self._spy()
        verdict = enforce(self.CLEAN, CLAUSES, None, sink,
                          recent=[self.FIRST], asked=self.ASKED)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual([], seen)                                  # 没撞上就没有账可记
        self.assertEqual("ok", verdict.repeat_status)               # 比过了、没重复

    def test_without_history_the_skip_cannot_happen_at_all(self):
        verdict = enforce(self.THIRD, CLAUSES, None, None, asked=self.ASKED)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual("not-checked", verdict.repeat_status)


class ARequestToReciteAlsoSkipsTheAudit(unittest.TestCase):
    """对方要她重复／引用时，复述审计也只记账、不判废——标记不丢，豁免也不外溢。

    判据与名字一个都不在这里：设定段、记账用的名字、两个记号全从 data 读。这里验四件事：
    跳过写进账本；命中照旧带着（skipped 与 hit 同时为真）；一句话同时撞两关时各记一行；
    没有请求时豁免一点都不外溢。
    """

    BOOK = load("audit")
    SKIP = BOOK["skip"]
    REPEAT = load("repeat")
    SETTING = "嘴上不认，行动先到：拒绝的话说完，事已经做了"     # 魂里那句原文（设定类）
    SAID = "我嘴上不认，行动先到。"                              # 她照搬回来的一句（单行，只撞审计）
    BEFORE = "主人回来啦，我把碗放桌上了。"                       # 上一轮她那句：开场是这几个字
    BOTH = "主人回来啦，我嘴上不认，行动先到。"                   # 同时撞两关：同一开场 + 复述设定段
    ASKED = "你刚才说的那句再说一遍"

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def _inject(self):
        return [{"kind": self.BOOK["setting_kinds"][0], "text": self.SETTING}]

    def test_the_audit_is_skipped_and_the_line_goes_out(self):
        """"同一句、有请求：decision 保持 pass、text 一个字不改、hits 空，但审计那节留着 hit。"""
        seen, sink = self._spy()
        verdict = enforce(self.SAID, CLAUSES, None, sink, inject=self._inject(),
                          policy=self.BOOK, recent=[], repeat_table=self.REPEAT,
                          asked=self.ASKED)
        self.assertEqual("pass", verdict.decision)                 # 话照发
        self.assertEqual(self.SAID, verdict.text)                  # 一个字都不改
        self.assertEqual([], verdict.hits)                         # 跳过不是命中：闸不动这句
        self.assertTrue(verdict.audit.skipped)                     # 但命中照旧算命中
        self.assertIsNotNone(verdict.audit.hit)                    # 面板看得见她引了设定
        self.assertEqual(1, len(seen), "跳过也要留一行账")
        self.assertEqual(self.SKIP["clause_id"], seen[0][0])       # 账本里是数据里那个名字
        self.assertIn(self.SKIP["status"], seen[0][1])
        self.assertIn(self.SKIP["status"], verdict.reason)         # 面板上也看得见

    def test_check_reports_no_hit_when_the_user_asked_for_it(self):
        self.assertIsNone(check(self.SAID, CLAUSES, inject=self._inject(),
                                policy=self.BOOK, asked=self.ASKED))
        self.assertIsInstance(check(self.SAID, CLAUSES, inject=self._inject(),
                                    policy=self.BOOK), Hit)

    def test_one_line_hitting_both_rows_of_skips(self):
        """一句话同时撞上内容重复与复述审计：两关各记一行、各带自己的记号，不合并成一条。"""
        seen, sink = self._spy()
        verdict = enforce(self.BOTH, CLAUSES, None, sink, inject=self._inject(),
                          policy=self.BOOK, recent=[self.BEFORE],
                          repeat_table=self.REPEAT, asked=self.ASKED)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.BOTH, verdict.text)
        self.assertEqual(2, len(seen), "两关各记一行账，不合并")
        self.assertEqual(self.REPEAT["hit"]["skip_clause_id"], seen[0][0])   # 先记内容重复那一关
        self.assertEqual(self.SKIP["clause_id"], seen[1][0])                 # 再记复述审计那一关
        self.assertNotIn(self.SKIP["status"], seen[0][1])                    # 各带自己的记号
        self.assertIn(self.SKIP["status"], seen[1][1])
        self.assertNotEqual(seen[0][1], seen[1][1], "两行各是各的说法，不许复用一句")
        self.assertEqual(repeats.STATUS_SKIPPED, verdict.repeat_status)
        self.assertTrue(verdict.audit.skipped)
        self.assertIsNotNone(verdict.audit.hit)
        self.assertIn(self.SKIP["status"], verdict.reason)
        self.assertIn("skipped-by-request", verdict.reason)

    def test_without_the_request_it_is_still_dropped(self):
        """豁免不许外溢：asked 里没有请求标记，同一句照旧判废。"""
        seen, sink = self._spy()
        verdict = enforce(self.SAID, CLAUSES, None, sink, inject=self._inject(),
                          policy=self.BOOK, recent=[], repeat_table=self.REPEAT,
                          asked="今天吃啥")
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertFalse(verdict.audit.skipped)
        self.assertIsNotNone(verdict.audit.hit)
        self.assertEqual(self.BOOK["verdicts"]["setting"], verdict.hits[0].clause_id)

    def test_an_empty_asked_never_exempts(self):
        for asked in ("", None, "   "):
            with self.subTest(asked=asked):
                verdict = enforce(self.SAID, CLAUSES, None, None, inject=self._inject(),
                                  policy=self.BOOK, recent=[], repeat_table=self.REPEAT,
                                  asked=asked)
                self.assertEqual("dropped", verdict.decision)
                self.assertFalse(verdict.audit.skipped)

    def test_a_request_that_recites_nothing_records_nothing(self):
        """他虽说了「再说一遍」，但这一句本来就没复述：没有「判废」可放，账一行都不记。"""
        seen, sink = self._spy()
        verdict = enforce("明天风大，我把窗关好了。", CLAUSES, None, sink, inject=self._inject(),
                          policy=self.BOOK, recent=[], repeat_table=self.REPEAT,
                          asked=self.ASKED)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual([], seen)
        self.assertFalse(verdict.audit.skipped)

    def test_only_one_place_decides_and_the_wording_stays_in_data(self):
        """闸只调 repeat.requested 算一次；跳过那一笔的名字与说法一个字都不许进代码。"""
        source = Path(gate.__file__).read_text(encoding="utf-8")
        calls = re.findall(r"(?m)^\s+wanted = repeat\.requested\(", source)
        self.assertEqual(2, len(calls), "check 与 enforce 各算一次，别处不再重算")
        self.assertNotIn(self.SKIP["clause_id"], source)
        self.assertNotIn(self.SKIP["reason"], source)


class ASecondHitKeepsTheOriginalLine(unittest.TestCase):
    """改了一次还是命中（而且不是红线）→ 留进门那句原话，不再丢空。

    以前二次命中即弃：她明明改了、也没改成红线，整泡却消失了。规矩是**在最该回答的那一句上沉默，
    比「说话带点毛病」糟得多**——所以 decision 是 pass、text 是进门那句（不是那个没改成的新稿），
    hits 两条都留着，账本记一行「<通道>-after-rewrite」。
    三关（条款 / 动作括注 / 内容重复）各验一遍：通道名与记号全从数据读。
    放宽的前提是**给了红线表**（下面每一例都传 fatal=FATAL）；没给表的调用方照旧二次命中即弃。

    **复述审计那一关不在这一格里**（这一轮改的）：照搬只长在那一句上，留原话等于留一句抄来的话，
    所以它二次命中改成「删那一句」（见 RecitedLineIsCutOut 与 ReciteAuditRidesTheGate）。
    """

    FATAL = load("fatal")
    MARKS = load("rewrite")["after_rewrite"]["marks"]
    BOOK = load("repeat")
    NOTE = load("action_note")
    AUDIT = load("audit")
    SETTING = "嘴上不认，行动先到：拒绝的话说完，事已经做了"
    SETTING_HALF = "嘴上不认，行动先到"      # 上面那句的前半截（素材：照搬回来的那一句）
    REST = "今天风挺大的"                    # 同一泡里的另一句（素材：不是判据）
    FIRST = "主人回来啦，我把碗放桌上了。"
    AGAIN = "主人回来啦，灯我给你留着呢。"
    NOTED = "（尾巴在水里蜷了一下）可爱归可爱，那也不是胖。"

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def _inject(self):
        return [{"kind": self.AUDIT["setting_kinds"][0], "text": self.SETTING}]

    def test_the_clause_that_survives_the_rewrite(self):
        seen, sink = self._spy()
        verdict = enforce(SPEECH, CLAUSES, _same, sink, fatal=self.FATAL)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(SPEECH, verdict.text, "发的是进门那句原话，不是没改成的新稿")
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertEqual(2, len(verdict.hits), "两次命中都留着")
        self.assertIsNone(verdict.fatal)
        self.assertEqual("ok", verdict.fatal_status)
        self.assertEqual(1, len(seen))
        self.assertEqual(verdict.hits[-1].clause_id, seen[0][0])
        self.assertIn(self.MARKS["clause"], seen[0][1])

    def test_the_action_note_that_survives_the_rewrite(self):
        seen, sink = self._spy()
        verdict = enforce(self.NOTED, CLAUSES, lambda text: self.NOTED + "。", sink,
                          action=self.NOTE, fatal=self.FATAL)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.NOTED, verdict.text)
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertEqual(self.NOTE["hit"]["clause_id"], verdict.hits[-1].clause_id)
        self.assertIn(self.MARKS["action"], seen[0][1])

    def test_the_repeat_that_survives_the_rewrite(self):
        seen, sink = self._spy()
        verdict = enforce(self.AGAIN, CLAUSES, lambda text: text + "主人", sink,
                          recent=[self.FIRST], repeat_table=self.BOOK, fatal=self.FATAL)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.AGAIN, verdict.text)
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertEqual("ok", verdict.repeat_status)
        self.assertIn(self.MARKS["repeat"], seen[0][1])

    def test_the_recitation_that_survives_the_rewrite_is_cut_out(self):
        """复述二次命中不在「留原话」那一格：只删照搬的那一句，剩下的照发，两条命中都留着。"""
        seen, sink = self._spy()
        mark = self.AUDIT["sentence_marks"][0]
        line = self.SETTING_HALF + mark + self.REST + mark
        verdict = enforce(line, CLAUSES, _same, sink, inject=self._inject(),
                          policy=self.AUDIT, fatal=self.FATAL)
        self.assertEqual("rewritten", verdict.decision)
        self.assertTrue(verdict.salvaged)
        self.assertFalse(verdict.kept_after_rewrite, "留原话就是留一句抄来的话")
        self.assertEqual(self.REST + mark, verdict.text)
        self.assertEqual(2, len(verdict.hits))
        self.assertIsNotNone(verdict.audit.hit, "审计那节的命中照旧带着")
        self.assertEqual(self.AUDIT["verdicts"]["setting"], verdict.hits[-1].clause_id)
        self.assertIn(self.AUDIT["recited"]["status"], seen[0][1])
        self.assertIn(self.SETTING_HALF, seen[0][1], "账上要说清删掉的是哪一句")

    def test_without_a_red_line_table_it_still_keeps_the_line(self):
        """没给红线表也留原话：判不了是不是红线，不等于可以吞掉这句话。

        区别只在读数上：fatal_status 是 not-checked（这一关没跑），账行里补一句取自数据的
        「这一轮没拿到红线表」。**「给不给表」不是「吞不吞这句」的开关**。
        """
        seen, sink = self._spy()
        verdict = enforce(SPEECH, CLAUSES, _same, sink)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(SPEECH, verdict.text)
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertIsNone(verdict.fatal)
        self.assertEqual("not-checked", verdict.fatal_status)
        self.assertIn(self.MARKS["clause"], seen[0][1])
        self.assertIn(load("rewrite")["after_rewrite"]["unchecked"], seen[0][1][-len(
            load("rewrite")["after_rewrite"]["unchecked"]):])

    def test_the_quota_strip_still_wins_over_keeping_the_original(self):
        """配额那条老例外不变：它优先剥行（把动作行拿掉比留原话强），不是「留原话」。"""
        verdict = enforce(self.NOTED, CLAUSES, lambda text: text, None, quota="forbid",
                          fatal=self.FATAL)
        self.assertEqual("rewritten", verdict.decision)
        self.assertTrue(verdict.salvaged)
        self.assertFalse(verdict.kept_after_rewrite)


class KeepUnrewrittenKeepsHerLine(unittest.TestCase):
    """改不动就不弃句：原话留着发、账本记一笔带 rewrite-failed，**输出绝不为空**。

    这是「宁可发一句略有瑕疵的，也不许静默变哑」那条规矩的闸侧出口：开了这个开关之后，
    rewrite 是 None、抛错、改回来是空的，都不再把整句吞掉。默认关着，旧行为一个字节都不变。
    """

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_a_failing_rewriter_keeps_the_text(self):
        seen, sink = self._spy()

        def boom(text):
            raise rewriting.Unrewritable(rewriting.timeout_why())

        verdict = enforce(SPEECH, CLAUSES, boom, sink, keep_unrewritten=True)
        self.assertEqual("pass", verdict.decision)                  # 发出去，不弃
        self.assertEqual(SPEECH, verdict.text)                      # 原话一个字不改
        self.assertTrue(verdict.text.strip())                       # 绝不为空
        self.assertEqual(1, len(verdict.hits))                      # 命中留着：闸本想过改它
        self.assertIn("rewrite-failed", verdict.reason)
        self.assertEqual(1, len(seen))
        self.assertIn("rewrite-failed", seen[0][1])                 # 账本里看得见这一笔
        self.assertEqual(verdict.hits[0].clause_id, seen[0][0])

    def test_an_empty_rewrite_keeps_the_text(self):
        verdict = enforce(SPEECH, CLAUSES, lambda text: "", keep_unrewritten=True)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(SPEECH, verdict.text)

    def test_no_rewriter_at_all_keeps_the_text(self):
        verdict = enforce(SPEECH, CLAUSES, None, keep_unrewritten=True)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(SPEECH, verdict.text)
        self.assertIn("rewrite-failed", verdict.reason)

    def test_off_by_default_the_old_behaviour_stays(self):
        self.assertEqual("dropped", enforce(SPEECH, CLAUSES, None).decision)
        self.assertEqual("dropped", enforce(SPEECH, CLAUSES, lambda text: "").decision)

    def test_a_quota_hit_still_prefers_stripping(self):
        """配额命中是例外：剥掉动作行比留着原话强，所以改不动时先剥行，不整句留着。"""
        book = load("quota")
        line = "（尾巴在水里晃了一下）可爱归可爱，那也不是胖。"
        verdict = enforce(line, CLAUSES, None, None, quota=book["quota_values"]["forbid"][0],
                          keep_unrewritten=True)
        self.assertEqual("rewritten", verdict.decision)
        self.assertTrue(verdict.salvaged)
        self.assertEqual("可爱归可爱，那也不是胖。", verdict.text)


class QuotaOverruns(unittest.TestCase):
    """第 5 组判据：装配标了「本轮不许写动作」，动作行就必须走干净。

    验收 1/2/3/4/5/6 全在这一组里。判据与说法一个字都不写在这份文件里：名字与模板从
    organs/mouth/data/quota.json 读（走 warehouse），这里只出现 id、kind 这类结构性标签。
    """

    BOOK = load("quota")
    ACTION = "（尾巴在水里晃了一下）可爱归可爱，那也不是胖。"
    BODY = "可爱归可爱，那也不是胖。"

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_quota_is_judged_before_action_note(self):
        """验收 1 的顺序前提：两边都撞时先报配额，不是动作括注——否则这句整条丢了。"""
        hit = check(self.ACTION, CLAUSES, action=load("action_note"), quota="forbid")
        self.assertEqual(self.BOOK["hit"]["clause_id"], hit.clause_id)
        self.assertEqual(self.BOOK["hit"]["kind"], hit.kind)
        self.assertIn("action-over-quota", hit.reason)

    def test_without_a_rewriter_the_action_line_is_stripped(self):
        """验收 1：没有重写器也必须剥行成功，正文一个字不差，账本记一笔。"""
        seen, sink = self._spy()
        verdict = enforce(self.ACTION, CLAUSES, None, sink, quota="forbid")
        self.assertEqual(self.BODY, verdict.text)              # 一个字不多、不少
        self.assertEqual("rewritten", verdict.decision)
        self.assertTrue(verdict.salvaged)
        self.assertEqual("ok", verdict.quota_status)
        self.assertEqual([self.BOOK["hit"]["clause_id"]],
                         [one.clause_id for one in verdict.hits])
        self.assertIn("action-over-quota", verdict.hits[0].reason)
        self.assertEqual(1, len(seen), "救回来也记一笔，不然看不出闸动过手")
        self.assertEqual(self.BOOK["hit"]["clause_id"], seen[0][0])
        self.assertIn("action-over-quota", seen[0][1])
        self.assertEqual(verdict.reason, seen[0][1])

    def test_second_quota_hit_is_stripped_not_dropped(self):
        """验收 1 的另一种走法：给了重写器但改回来还是带动作 → 剥行，不丢整句。"""
        seen, sink = self._spy()
        verdict = enforce(self.ACTION, CLAUSES, _same, sink, quota="forbid")
        self.assertEqual(self.BODY, verdict.text)
        self.assertEqual("rewritten", verdict.decision)
        self.assertTrue(verdict.salvaged)
        self.assertEqual([self.BOOK["hit"]["clause_id"], self.BOOK["hit"]["clause_id"]],
                         [one.clause_id for one in verdict.hits])   # 两次命中都在
        self.assertIn("action-over-quota", seen[0][1])

    def test_unmarked_is_untouched_and_not_checked(self):
        """验收 2：没标 → 一个字节都不改，状态是 not-checked（不是 allow）。"""
        seen, sink = self._spy()
        verdict = enforce(self.ACTION, CLAUSES, None, sink)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.ACTION, verdict.text)
        self.assertFalse(verdict.salvaged)
        self.assertEqual("not-checked", verdict.quota_status)
        self.assertEqual([], verdict.hits)
        self.assertEqual([], seen, "没标就没这一关，也没有账可记")

    def test_marked_strips_while_unmarked_leaves_it_alone(self):
        """验收 2 的对照：同一句、同一份判据，标了才剥、没标原样。"""
        self.assertEqual(self.BODY, enforce(self.ACTION, CLAUSES, None, quota="forbid").text)
        self.assertEqual(self.ACTION, enforce(self.ACTION, CLAUSES, None).text)

    def test_allow_is_checked_and_lets_action_note_judge(self):
        """验收 3：标了 allow 一律不命中（状态 ok），这才轮到动作括注去判它写没写场景。"""
        allowed = enforce(self.ACTION, CLAUSES, None, quota="allow")
        self.assertEqual("pass", allowed.decision)
        self.assertEqual(self.ACTION, allowed.text)
        self.assertEqual("ok", allowed.quota_status)
        self.assertIn(self.BOOK["reason"]["clear"], allowed.reason)      # 放行要有据
        note = load("action_note")
        hit = check(self.ACTION, CLAUSES, action=note, quota="allow")
        self.assertEqual(note["hit"]["clause_id"], hit.clause_id)        # 轮到动作括注拦它
        self.assertEqual("dropped", enforce(self.ACTION, CLAUSES, None, action=note,
                                            quota="allow").decision)

    def test_unmarked_is_not_the_same_as_allowed(self):
        """验收 3 的反面：allow 是判过、没标是没跑，两件事在状态与理由上都必须分得开。"""
        unchecked = enforce(self.BODY, CLAUSES, None)
        checked = enforce(self.BODY, CLAUSES, None, quota="allow")
        self.assertEqual("not-checked", unchecked.quota_status)
        self.assertEqual("ok", checked.quota_status)
        self.assertNotIn(self.BOOK["reason"]["clear"], unchecked.reason)
        self.assertIn(self.BOOK["reason"]["clear"], checked.reason)

    def test_action_only_leaves_nothing_and_is_dropped(self):
        """验收 4：纯动作行没有正文 → 剥完只剩空白 → 整句弃掉。"""
        seen, sink = self._spy()
        verdict = enforce("（尾巴晃了一下）", CLAUSES, None, sink, quota="forbid")
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertFalse(verdict.salvaged)
        self.assertEqual(self.BOOK["hit"]["clause_id"], seen[0][0])
        self.assertIn("action-over-quota", seen[0][1])

    def test_inline_bracket_is_not_stripped(self):
        """验收 5：行内括号是台词里夹的一个字，正文不许被剥掉。"""
        line = "他说（笑）就走了"
        verdict = enforce(line, CLAUSES, None, quota="forbid")
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(line, verdict.text)

    def test_stripped_body_still_runs_the_other_criteria(self):
        """验收 6：剥过不等于放行——正文里还有动作带场景，动作括注照样拦下。

        正文不能以身体部位开头（那样的整行本身就是动作，会被一起剥掉），所以用一句以场景词
        开头、身体部位在后的话：剥得掉的只有前面那个动作，正文留在那儿，再由动作括注判它。
        """
        seen, sink = self._spy()
        note = load("action_note")
        verdict = enforce("（尾巴晃了一下）外面下雨了，尾巴都翘起来了。", CLAUSES, None, sink,
                          action=note, quota="forbid")
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertEqual(note["hit"]["clause_id"], verdict.hits[-1].clause_id)
        self.assertEqual(note["hit"]["clause_id"], seen[0][0])   # 账本记的是剥行后拦下它的那条
        self.assertIn("action-over-quota", verdict.reason)

    def test_stripped_body_still_runs_the_clauses(self):
        """验收 6：剥完的正文再扫一遍条款——用一个「只从行首开始算」的条款把这件事钉住。

        真条款用的都是 re.search（哪儿命中都算），剥掉行首的动作不可能让它从「不中」变成
        「中」；要证明「正文确实又被扫了一遍」，只能拿一条锚在行首的条款来验。这条条款只活在
        这份测试里，不进数据。
        """
        anchored = {"id": "starts-with-cute", "kind": "可判废", "pattern": "^可爱",
                    "reason": "一开口就是套话开场，像在念稿子"}
        clauses = list(CLAUSES) + [anchored]
        self.assertIsNone(check(self.ACTION, clauses))          # 原句行首是括注，不中
        seen, sink = self._spy()
        verdict = enforce(self.ACTION, clauses, None, sink, quota="forbid")
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertEqual(anchored["id"], verdict.hits[-1].clause_id)
        self.assertEqual(anchored["id"], seen[0][0])
        self.assertIn("action-over-quota", verdict.reason)

    def test_customer_service_speech_in_the_body_is_never_sent(self):
        """验收 6 的现实版：正文里有客服腔，这句一个字也发不出去（这次是条款先拦下）。"""
        line = "（尾巴晃了一下）" + SPEECH
        verdict = enforce(line, CLAUSES, None, quota="forbid")
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertEqual("courtesy-open", verdict.hits[-1].clause_id)

    def test_other_criteria_keep_the_line_instead_of_dropping_it(self):
        """别的判据第二次命中不再弃整句：留原话（配额那条「剥行」例外只属于配额）。

        这条断言是被「二次命中不丢空」那条新规矩改准的：以前这里 dropped + text=""。现在 decision
        是 pass、text 是进门那句、kept_after_rewrite=True、salvaged=False（它不是剥行救回来的）。
        """
        note = load("action_note")
        verdict = enforce("（尾巴在水里晃了一下）", CLAUSES, _same, action=note)   # 没标配额
        self.assertEqual("pass", verdict.decision)
        self.assertEqual("（尾巴在水里晃了一下）", verdict.text)
        self.assertFalse(verdict.salvaged)
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertEqual(note["hit"]["clause_id"], verdict.hits[-1].clause_id)

    def test_broken_quota_is_loud(self):
        """认不出的写法当场响：不许猜成 allow 放行。"""
        from organs.mouth.quota import QuotaBroken
        with self.assertRaises(QuotaBroken):
            enforce(self.ACTION, CLAUSES, None, quota="随便写的")

    def test_gate_source_has_no_quota_word(self):
        """配额的名字与说法也不许进闸：闸只接判据，不持判据。"""
        source = Path(gate.__file__).read_text(encoding="utf-8")
        self.assertNotIn(self.BOOK["hit"]["clause_id"], source)
        for line in self.BOOK["reason"].values():
            self.assertNotIn(line.split("{")[0], source)


class ClaimRidesTheGate(unittest.TestCase):
    """冒充做过（这一轮加的）接在内容重复后面、复述审计前面：改不动就删那一句，删完只剩空白才弃。

    判据表与闸用的两个名字全从 data/claim.json 读：这里只验「接上了没有、判没判看得出来没有」。
    """

    BOOK = load("claim")
    CLAUSE_ID = BOOK["hit"]["clause_id"]
    KIND = BOOK["hit"]["kind"]
    VERB = BOOK["action_verbs"][0]
    LINE = BOOK["person"] + BOOK["bare_prefixes"][0] + VERB     # 这一轮手头什么都没做时她会说的那句
    OTHER = "今天风挺大的。"
    FIXED = "这件事我做不了，我没手。"                           # 改写之后不再说做过

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_check_reports_it_with_an_empty_ledger(self):
        hit = check(self.LINE, CLAUSES, actions=[], claim=self.BOOK)
        self.assertIsInstance(hit, Hit)
        self.assertEqual(self.CLAUSE_ID, hit.clause_id)      # 名字取自数据，闸不自己造
        self.assertEqual(self.KIND, hit.kind)
        self.assertIn(self.VERB, hit.reason)                  # 是一句人话：说清哪个动作对不上账

    def test_no_ledger_is_not_checked(self):
        """actions=None：拿不到账，这一关没跑——不判冒充，claim_status 如实记 not-checked。"""
        self.assertIsNone(check(self.LINE, CLAUSES, actions=None, claim=self.BOOK))
        verdict = enforce(self.LINE, CLAUSES, None, actions=None, claim=self.BOOK)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.LINE, verdict.text)
        self.assertEqual(claims.STATUS_NOT_CHECKED, verdict.claim_status)

    def test_an_empty_ledger_is_judged(self):
        """actions=[]：给了账、账上空着——同一句话同一份结局，状态与上面那条不一样。"""
        verdict = enforce(self.LINE, CLAUSES, None, actions=[], claim=self.BOOK)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual(claims.STATUS_OK, verdict.claim_status)
        self.assertNotEqual(claims.STATUS_OK, claims.STATUS_NOT_CHECKED)

    def test_a_done_action_passes_with_its_reason(self):
        """真做过（账上有这个动词）→ 放行；放行要有据：那一关查过了就说出来。"""
        verdict = enforce(self.LINE, CLAUSES, None,
                          actions=[{"verb": self.VERB}], claim=self.BOOK)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.LINE, verdict.text)
        self.assertIn(self.BOOK["reason"]["clear"], verdict.reason)

    def test_speech_is_exempt(self):
        book = self.BOOK
        line = "%s%s%s" % (book["person"], book["speech_verbs"][0], book["suffixes"][0])
        self.assertIsNone(check(line, CLAUSES, actions=[], claim=self.BOOK))
        self.assertEqual("pass", enforce(line, CLAUSES, None, actions=[], claim=self.BOOK).decision)

    def test_the_rewrite_clears_it(self):
        verdict = enforce(self.LINE, CLAUSES, lambda text: self.FIXED,
                          actions=[], claim=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.FIXED, verdict.text)
        self.assertFalse(verdict.salvaged)

    def test_no_rewriter_strips_that_sentence_and_drops_the_rest(self):
        """验收 1：不给重写器 → 那一句被删掉；整条只有那一句 → 空、dropped、照着名字记账。"""
        seen, sink = self._spy()
        verdict = enforce(self.LINE, CLAUSES, None, sink, actions=[], claim=self.BOOK)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertEqual([self.CLAUSE_ID], [one.clause_id for one in verdict.hits])
        self.assertEqual(1, len(seen))
        self.assertEqual(self.CLAUSE_ID, seen[0][0])
        self.assertEqual(verdict.reason, seen[0][1])
        self.assertIn(self.LINE, verdict.reason)              # 账本里读得出删掉的是哪一句

    def test_the_other_sentence_is_kept(self):
        """验收 5：混合句只删前一句，后一句原样留下——这一泡照发（rewritten + salvaged）。"""
        seen, sink = self._spy()
        line = self.LINE + "。" + self.OTHER
        verdict = enforce(line, CLAUSES, None, sink, actions=[], claim=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.OTHER, verdict.text)
        self.assertTrue(verdict.salvaged, "删句是救人那一族，与配额剥行同一口径")
        self.assertEqual(1, len(seen), "删句救回来的那一笔也要记账")
        self.assertEqual(self.CLAUSE_ID, seen[0][0])
        self.assertIn(self.LINE, seen[0][1])

    def test_every_claim_sentence_is_stripped(self):
        """剩下的正文里还有一句冒充：接着删那一句，不整条丢（删一句少一句）。"""
        line = self.LINE + "。" + self.LINE + "。" + self.OTHER
        verdict = enforce(line, CLAUSES, None, actions=[], claim=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.OTHER, verdict.text)
        self.assertEqual([self.CLAUSE_ID] * 2, [one.clause_id for one in verdict.hits])

    def test_a_second_hit_after_the_rewrite_strips_the_sentence(self):
        """改完还命中：删那一句（不是整条丢，也不是留原话）——hits 两条都在。"""
        line = self.LINE + "。" + self.OTHER
        verdict = enforce(line, CLAUSES, _same, None, actions=[], claim=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.OTHER, verdict.text)
        self.assertEqual([self.CLAUSE_ID, self.CLAUSE_ID],
                         [one.clause_id for one in verdict.hits])

    def test_keep_unrewritten_does_not_keep_a_lie(self):
        """keep_unrewritten 盖不过这一条：留原话就是留一句谎，改不动时选的是删句。"""
        line = self.LINE + "。" + self.OTHER
        verdict = enforce(line, CLAUSES, None, None, actions=[], claim=self.BOOK,
                          keep_unrewritten=True)
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.OTHER, verdict.text)
        self.assertFalse(verdict.kept_after_rewrite)

    def test_the_stripped_body_still_faces_the_other_criteria(self):
        """删过不等于放行：剩下的正文还踩条款，照样弃掉整条。"""
        verdict = enforce(self.LINE + "。" + SPEECH, CLAUSES, None,
                          actions=[], claim=self.BOOK)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertEqual("courtesy-open", verdict.hits[-1].clause_id)

    def test_the_clause_comes_first(self):
        """条款先判（既有逻辑不动）：两边都撞时先报条款。"""
        hit = check(SPEECH + self.LINE, CLAUSES, actions=[], claim=self.BOOK)
        self.assertEqual("courtesy-open", hit.clause_id)

    def test_the_quota_comes_before_it(self):
        """顺序：动作配额排在冒充做过前面（两关都踩时先报配额）。"""
        line = "（尾巴晃了一下）" + self.LINE
        hit = check(line, CLAUSES, quota="forbid", actions=[], claim=self.BOOK)
        self.assertEqual(load("quota")["hit"]["clause_id"], hit.clause_id)

    def test_unchecked_lets_the_line_through(self):
        """没给账时按既有规矩放行（不替调用方做主）：这一关拦不下任何人。"""
        self.assertEqual("pass", enforce(self.LINE, CLAUSES, None, actions=None).decision)

    def test_broken_table_is_loud(self):
        with self.assertRaises(ClaimBroken) as caught:
            enforce(self.LINE, CLAUSES, None, actions=[], claim={})
        self.assertIn("hit", str(caught.exception))

    def test_gate_source_has_no_claim_word(self):
        """冒充做过的说法也不许进闸：闸只接判据，不持判据。"""
        source = Path(gate.__file__).read_text(encoding="utf-8")
        for line in self.BOOK["reason"].values():
            self.assertNotIn(line.split("{")[0], source)


class AckAskStanceShape(unittest.TestCase):
    """三件套：先应一声 → 再问一句 → 表个态。判据全在数据里，这里只写结构与素材。"""

    #: 数据里那条条款的 id（改数据里的 id 就改这一个串；pattern 一个字都不抄进来）
    CLAUSE = "ack-ask-stance"
    #: 三样全齐
    FULL = "好。你今天干嘛了？我觉得挺好的。"
    #: 只有应答 + 问句（没有表态）——三样不全，不许命中
    NO_STANCE = "好。你今天干嘛了？"
    #: 顺序反过来（表态在前、应答在后）——顺序不对，不许命中
    WRONG_ORDER = "我觉得挺好的。好。你今天干嘛了？"

    def _clause(self) -> dict:
        rows = [one for one in _all_on(CLAUSES) if one["id"] == self.CLAUSE]
        self.assertEqual(1, len(rows), "数据里该有且只有一条 %r" % self.CLAUSE)
        return rows[0]

    def _only(self) -> list:
        """只留这一条：命中与否只说明这一条形状，不掺别的条款。"""
        return [self._clause()]

    def test_the_three_parts_together_hit(self):
        hit = check(self.FULL, self._only())
        self.assertIsInstance(hit, Hit)
        self.assertEqual(hit.clause_id, self.CLAUSE)
        self.assertTrue(hit.reason.strip())

    def test_ack_and_question_without_a_stance_do_not_hit(self):
        """只有两样：应答 + 问句，没有表态那一句——不命中（三样都要）。"""
        self.assertIsNone(check(self.NO_STANCE, self._only()))

    def test_the_order_is_part_of_the_clause(self):
        """三样都在，但顺序是「表态 → 应答 → 问句」——顺序不对，不命中。"""
        self.assertIsNone(check(self.WRONG_ORDER, self._only()))

    def test_stance_before_the_question_does_not_hit(self):
        """表态落在问号前面：位置不对，不命中（pattern 里表态必须排在问句之后）。"""
        self.assertIsNone(check("好。我觉得挺好的。你今天干嘛了？", self._only()))

    def test_an_ordinary_reply_does_not_hit(self):
        self.assertIsNone(check("行吧，那你先忙。", self._only()))

    def test_the_shape_is_data(self):
        """改数据就改行为：把应答词表改成只认一个绝不可能出现的字，同一句立刻不命中。"""
        clause = self._clause()
        star = dict(clause, pattern=clause["pattern"].replace("好|行|嗯|成|可以", "龘"))
        self.assertIsNone(check(self.FULL, [star]))
        loosened = dict(clause, pattern="^.+$")
        self.assertIsInstance(check(self.NO_STANCE, [loosened]), Hit)

    def test_replies_across_parts_hit_when_judged_whole(self):
        """逐泡判看不见它，整段判看得见：切分每泡最多两句，这条形状跨得过泡的边界。"""
        parts = ["好。你今天干嘛了？", "我觉得挺好的。"]
        self.assertIsNone(check(parts[0], self._only()), "一泡两句：三件套不该在逐泡这一遍命中")
        self.assertIsInstance(check("".join(parts), self._only()), Hit)

    def test_rewrite_once_then_keep_the_original(self):
        """命中改一次；改完仍命中（不是红线）→ 按原话发，并记一行账（五态走既有那一条）。"""
        noted = []
        verdict = enforce(self.FULL, self._only(), _same, lambda cid, why: noted.append(cid),
                          keep_unrewritten=True)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.FULL, verdict.text, "二次命中不许把这句话吞掉")
        self.assertTrue(verdict.kept_after_rewrite)
        self.assertEqual([self.CLAUSE], noted)


class KinRidesTheGate(unittest.TestCase):
    """认亲（这一轮加的）接在冒充做过后面、编记忆前面：改不动就中性化，换完只剩空白才弃。

    判据表与闸用的两个名字全从 data/kin.json 读：这里只验「接上了没有、判没判看得出来没有」。
    """

    BOOK = load("kin")
    CLAUSE_ID = BOOK["hit"]["clause_id"]
    KIND = BOOK["hit"]["kind"]
    MIN = BOOK["known_by"]["min"]                 # 「够不够熟」那一档也从数据读
    FIELD = BOOK["known_by"]["field"]
    STRANGER = {"in_book": False}                 # 名录里查不到这个号
    NEW_FACE = {"in_book": True, FIELD: MIN - 1}  # 刚被 ingest 建档，读数还没到档
    FAMILIAR = {"in_book": True, FIELD: MIN}      # 够得着（阈值那一档）
    TERM = BOOK["kin_terms"][0]
    MEMORY = BOOK["memory_markers"][0]
    NEUTRAL = BOOK["neutral"]["term_word"]
    SAY = BOOK["neutral"]["say_once"]
    HONEST = BOOK["neutral"]["memory_sentence"]
    OTHER = "今天风挺大的。"
    LINE = TERM + "，" + OTHER                        # 熟人才能触发的称呼
    MEMORY_LINE = MEMORY + "要去医院复查。"            # 熟人才能触发的记忆复述
    FIXED = "这句改好了，没有称呼。"                    # 改写之后不再认亲

    def _spy(self):
        seen = []
        return seen, (lambda clause_id, reason: seen.append((clause_id, reason)))

    def test_check_reports_it_for_a_stranger(self):
        hit = check(self.LINE, CLAUSES, known=self.STRANGER, kin=self.BOOK)
        self.assertIsInstance(hit, Hit)
        self.assertEqual(self.CLAUSE_ID, hit.clause_id)      # 名字取自数据，闸不自己造
        self.assertEqual(self.KIND, hit.kind)
        self.assertIn(self.TERM, hit.reason)                 # 是一句人话：说清命中的是哪个词

    def test_a_memory_marker_hits_too(self):
        hit = check(self.MEMORY_LINE, CLAUSES, known=self.STRANGER, kin=self.BOOK)
        self.assertEqual(self.CLAUSE_ID, hit.clause_id)

    def test_an_acquaintance_passes_with_its_reason(self):
        """验收 2：熟人（known=self.FAMILIAR）一律放行——同一个称呼照常叫，不许误伤。"""
        verdict = enforce(self.LINE, CLAUSES, None, known=self.FAMILIAR, kin=self.BOOK)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.LINE, verdict.text)
        self.assertEqual(kins.STATUS_OK, verdict.kin_status)
        self.assertIn(self.BOOK["reason"]["clear"], verdict.reason)

    def test_no_reading_is_not_checked(self):
        """验收 5：known=None 判不了——不判、也不声称判过，状态如实记 not-checked。"""
        self.assertIsNone(check(self.LINE, CLAUSES, known=None, kin=self.BOOK))
        verdict = enforce(self.LINE, CLAUSES, None, known=None, kin=self.BOOK)
        self.assertEqual("pass", verdict.decision)
        self.assertEqual(self.LINE, verdict.text)
        self.assertEqual(kins.STATUS_NOT_CHECKED, verdict.kin_status)
        self.assertNotEqual(kins.STATUS_OK, kins.STATUS_NOT_CHECKED)

    def test_a_non_reading_is_loud(self):
        """验收 6：known 传 True / False / 1 / "true" 一律抛——一个布尔说不出「够不够熟」。"""
        for bad in (True, False, 1, "true"):
            with self.assertRaises(kins.KinBroken):
                enforce(self.LINE, CLAUSES, None, known=bad, kin=self.BOOK)

    def test_a_registered_but_unfamiliar_id_is_still_judged(self):
        """回修重点：刚建了档（读数没到档）说出熟人称呼，**仍然判**——在册不是熟。"""
        self.assertIsNotNone(check(self.LINE, CLAUSES, known=self.NEW_FACE, kin=self.BOOK))
        verdict = enforce(self.LINE, CLAUSES, None, None, known=self.NEW_FACE, kin=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertNotIn(self.TERM, verdict.text)
        self.assertEqual(kins.STATUS_OK, verdict.kin_status)

    def test_politeness_passes(self):
        """验收 3 / 4：对陌生人客气、问一句「你是？」，或回那句稳妥话术，都放行。"""
        for line in ("你是？", "今天天气不错。", self.SAY):
            self.assertEqual("pass",
                             enforce(line, CLAUSES, None, known=self.STRANGER, kin=self.BOOK).decision,
                             "这句不该被当成认亲：%r" % line)

    def test_the_rewrite_clears_it(self):
        verdict = enforce(self.LINE, CLAUSES, lambda text: self.FIXED,
                          known=self.STRANGER, kin=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertEqual(self.FIXED, verdict.text)
        self.assertFalse(verdict.salvaged)

    def test_no_rewriter_neutralizes_instead_of_dropping(self):
        """验收 1：不给重写器 → 称呼换成中性词，并带上那句稳妥话术；别的句子一个字不动。"""
        seen, sink = self._spy()
        verdict = enforce(self.LINE, CLAUSES, None, sink, known=self.STRANGER, kin=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertNotIn(self.TERM, verdict.text)
        self.assertIn(self.NEUTRAL, verdict.text)
        self.assertIn(self.SAY, verdict.text)                 # 带上数据里那句稳妥话术
        self.assertIn(self.OTHER, verdict.text)
        self.assertTrue(verdict.salvaged, "中性化与配额剥行、冒充删句同族")
        self.assertEqual([self.CLAUSE_ID], [one.clause_id for one in verdict.hits])
        self.assertEqual(1, len(seen), "中性化救回来的那一笔也要记账")
        self.assertEqual(self.CLAUSE_ID, seen[0][0])

    def test_the_memory_sentence_is_replaced_by_the_truth(self):
        seen, sink = self._spy()
        verdict = enforce(self.MEMORY_LINE, CLAUSES, None, sink, known=self.STRANGER, kin=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertIn(self.HONEST, verdict.text)
        self.assertNotIn(self.MEMORY, verdict.text)
        self.assertTrue(verdict.salvaged)

    def test_a_second_hit_after_the_rewrite_neutralizes(self):
        """改完还认亲：中性化（不是整条丢、也不是留原话）——hits 两条都在。"""
        verdict = enforce(self.LINE, CLAUSES, _same, None, known=self.STRANGER, kin=self.BOOK)
        self.assertEqual("rewritten", verdict.decision)
        self.assertNotIn(self.TERM, verdict.text)
        self.assertFalse(verdict.kept_after_rewrite)
        self.assertEqual([self.CLAUSE_ID, self.CLAUSE_ID],
                         [one.clause_id for one in verdict.hits])

    def test_keep_unrewritten_does_not_keep_it(self):
        """keep_unrewritten 盖不过这一条：留原话就是留一句替他认亲的话。"""
        verdict = enforce(self.LINE, CLAUSES, None, None, known=self.STRANGER, kin=self.BOOK,
                          keep_unrewritten=True)
        self.assertEqual("rewritten", verdict.decision)
        self.assertNotIn(self.TERM, verdict.text)
        self.assertFalse(verdict.kept_after_rewrite)

    def test_the_neutralized_body_still_faces_the_other_criteria(self):
        """换过不等于放行：剩下的正文还踩条款，照样弃掉整条。"""
        verdict = enforce(self.LINE + "。" + SPEECH, CLAUSES, None,
                          known=self.STRANGER, kin=self.BOOK)
        self.assertEqual("dropped", verdict.decision)
        self.assertEqual("", verdict.text)
        self.assertEqual("courtesy-open", verdict.hits[-1].clause_id)

    def test_the_clause_comes_first(self):
        """条款先判（既有逻辑不动）：两边都撞时先报条款。"""
        hit = check(SPEECH + self.LINE, CLAUSES, known=self.STRANGER, kin=self.BOOK)
        self.assertEqual("courtesy-open", hit.clause_id)

    def test_the_claim_comes_first(self):
        """顺序：冒充做过排在认亲前面（两关都踩时先报冒充）。"""
        claim = load("claim")
        line = self.TERM + "，" + claim["person"] + claim["bare_prefixes"][0] + claim["action_verbs"][0]
        hit = check(line, CLAUSES, actions=[], claim=claim, known=self.STRANGER, kin=self.BOOK)
        self.assertEqual(claim["hit"]["clause_id"], hit.clause_id)
        self.assertEqual(load("claim")["hit"]["clause_id"], hit.clause_id)

    def test_unchecked_lets_the_line_through(self):
        """既有的调用方不传这一格时按老规矩放行：这一关拦不下任何人。"""
        self.assertEqual("pass", enforce(self.LINE, CLAUSES, None).decision)

    def test_broken_table_is_loud(self):
        with self.assertRaises(kins.KinBroken) as caught:
            enforce(self.LINE, CLAUSES, None, known=self.STRANGER, kin={})
        self.assertIn("hit", str(caught.exception))

    def test_gate_source_has_no_kin_words(self):
        """认亲的说法也不许进闸：闸只接判据，不持判据。"""
        source = Path(gate.__file__).read_text(encoding="utf-8")
        for line in self.BOOK["reason"].values():
            self.assertNotIn(line.split("{")[0], source)
        self.assertNotIn(self.SAY, source)


if __name__ == "__main__":
    unittest.main()
