"""说话页（pages/chat.html）的判据：一条写、三态齐、想起什么带证据、心只给词。

这一页与 heart / brain 的处境都不同：它是第一个**真会说话**的页面，也是第一个把
POST /chat 端到端画出来的页面。所以判据钉在四件事上：

1. **写只有一条**：页面里 call(METHOD, "字面路径") 恰好一条，就是 POST /chat；
   路径不落地就说明有人把端点换掉或把参数拼成了第二处。裸 fetch 一次都不许出现——
   绕开 call() 就等于绕开挂载前缀（iframe 里会打到宿主根）。
2. **按钮都是活的**：每个静态 <button id="..."> 都有 el("<id>").addEventListener 绑定；
   输入框只有一个，且没有 <form>（enctype 提交会整页刷新，把这一场冲掉）。
3. **三个状态各自成一件事**：空态（还没开始 + 怎么开始）、加载态（「她在想…」+
   发送按钮 disabled）、错误态（后端 message 原样 + 按 kind 的中文归类）。
4. **心只给档位词**：mood.tier / voice.tone / engage.state / gate.decision /
   hit.clause_id·reason 都在页上；renderHeart 里没有任何数值换算（toFixed / Number /
   Math / parseFloat），整页也不出现 mood.now、mood.value、energy 这类读数。
5. **三个读数也看得见**：本次温度与档位（「温度 0.5 · 事实档」）、连续性（「最近 12 轮 /
   更早还有 N 轮没有加载」，N=0 时是「没有更早的对话」）、锚句（已配（N 字）/ 未配锚句）
   都落在页上；三格各用一次 textContent，缺字段时如实写「没给」，不留白。

**这是静态回归测试，不是审计。** 它比对源码里的页面与路由表；活体验收（真 devserver +
真模型）的原始输出在交付回报里。断言 api.ROUTES["/chat"] == "POST" 的那条依赖并行
任务把端点登记进路由表——在它落地之前那条会红，这是预期，不许为了变绿去改 api/core.py。
"""
from __future__ import annotations

import re
import unittest
from urllib.parse import urljoin

from api import api
from api.pages import PAGE_FILES, read_page

PAGE = "chat.html"
MOUNT = "/chat"

CALL_RE = re.compile(r'call\(\s*"(GET|POST|PUT|DELETE|PATCH)"\s*,\s*"([^"]+)"')
FETCH_RE = re.compile(r'fetch\(\s*"([^"]+)"')
BUTTON_ID_RE = re.compile(r'<button[^>]*\bid="([^"]+)"')
INPUT_RE = re.compile(r'<input\b[^>]*>')
ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"')

NUMERIC_IN_HEART = ("toFixed(", "Number(", "Math.", "parseFloat(", "parseInt(")
FORBIDDEN_READINGS = ("mood.now", "mood.value", "heart.mood.now", "energy", "valence", "arousal")


def _text() -> str:
    return (read_page(PAGE) or b"").decode("utf-8")


def _calls() -> list:
    return [(method, path.partition("?")[0]) for method, path in CALL_RE.findall(_text())]


def _between(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return text[head:text.index(end, head)]


class SpeaksThroughTheCallHelper(unittest.TestCase):
    def test_calls_chat_exactly_once_as_a_post(self):
        calls = _calls()
        self.assertEqual(calls, [("POST", "/chat")],
                         "说话页该只有一条写：POST /chat，实际 %r" % (calls,))
        self.assertEqual(_text().count('call("POST", "/chat"'), 1,
                         "POST /chat 出现了不止一次（或写成了别的形状）")

    def test_chat_route_is_registered_as_post(self):
        """端点在并行任务里登记：这条红 = /chat 还没进 ROUTES，别去 core.py 自己加。"""
        self.assertIn("/chat", api.ROUTES, "/chat 还没登记进 api.ROUTES")
        self.assertEqual(api.ROUTES["/chat"], "POST", "/chat 的方法该是 POST")

    def test_every_call_path_is_registered_or_is_a_page(self):
        for method, route in _calls():
            self.assertTrue(route in api.ROUTES or route in PAGE_FILES,
                            "页面在打一条既不是路由也不是页面的路径：%s" % route)
            if route in api.ROUTES:
                self.assertEqual(api.ROUTES[route], method,
                                 "方法对不上：页面用 %s 打 %s，路由是 %s"
                                 % (method, route, api.ROUTES[route]))

    def test_no_bare_fetch_bypasses_the_helper(self):
        text = _text()
        self.assertEqual(FETCH_RE.findall(text), [], "出现了裸 fetch(\"path\")")
        self.assertEqual(re.findall(r"""fetch\(\s*["']""", text), [],
                         "有 fetch 直接写死路径：绕过了 call() 的前缀补全")
        self.assertEqual(text.count("fetch(BASE + path, init)"), 1,
                         "fetch 被复制到了 call() 之外，或没补挂载前缀")
        self.assertIn('var BASE = MOUNT.slice(-5) === "/chat" ? MOUNT.slice(0, -5) : "";',
                      text, "BASE 要按挂载路径算：直开核时为空串，挂在壳里时是壳前缀")


class ControlsAreAlive(unittest.TestCase):
    def test_one_input_and_no_form(self):
        text = _text()
        inputs = INPUT_RE.findall(text)
        self.assertGreaterEqual(len(inputs), 1, "说话页至少要一个输入框")
        self.assertIn('id="chat-text"', inputs[0], "输入框要能被 JS 拿到：%s" % inputs[0])
        self.assertNotIn("<form", text.lower(), "不许有 <form>（提交会整页刷新）")

    def test_every_static_button_has_a_handler(self):
        text = _text()
        ids = BUTTON_ID_RE.findall(text)
        self.assertGreaterEqual(len(ids), 1, "至少一个静态按钮")
        self.assertIn("send-btn", ids, "缺发送按钮")
        for button_id in ids:
            self.assertIn('el("%s").addEventListener' % button_id, text,
                          "按钮 %s 没有绑定任何事件（死按钮）" % button_id)

    def test_enter_also_sends(self):
        text = _text()
        self.assertIn('el("chat-text").addEventListener("keydown"', text)
        self.assertIn('event.key === "Enter"', text, "回车也要能发")
        self.assertIn('el("send-btn").disabled', text, "加载时按钮要禁用")

    def test_only_one_bubble_for_the_user_line(self):
        text = _text()
        self.assertEqual(text.count('bubble("me"'), 1, "用户那句只能画一次")
        self.assertIn("用户那句只显示一次", text)


class LinksStayOnReadablePages(unittest.TestCase):
    def test_anchors_resolve_to_a_page_or_a_get_route(self):
        anchors = ANCHOR_RE.findall(_text())
        self.assertEqual(anchors, ["/"], "回跳只许回根（/chat 是 POST）：%r" % (anchors,))
        for href in anchors:
            target = urljoin(MOUNT, href).partition("?")[0]
            self.assertTrue(target in PAGE_FILES or api.ROUTES.get(target) == "GET",
                            "链接 %r 解析成 %s，既不是页面也不是 GET 路由" % (href, target))
            self.assertNotEqual(target, MOUNT, "<a> 不许指回 POST /chat")


class ChunksBecomeBubbles(unittest.TestCase):
    def test_reply_is_rendered_chunk_by_chunk(self):
        text = _text()
        self.assertIn("renderChunks", text, "回复要按 chunks 分条渲染")
        self.assertIn("chunk.gap_before", text, "每一泡要读 gap_before")
        self.assertIn("chunk.send_at", text, "每一泡要读 send_at")
        self.assertIn("gap.toFixed(1)", text, "间隔要显示成 +1.2s 这种可读形式")
        self.assertIn('bubble("her"', text, "每一泡一个气泡")

    def test_send_at_number_and_string_take_different_branches(self):
        """核里嘴给的 send_at 是「从这批开始算起第几秒」（float），别处可能给时刻字符串。

        数字要渲染成「首发」/「第 3.0 秒」，字符串原样显示，缺失写一句——三条分支都得在，
        否则第一泡会显示 "0"、第二泡 "3.0s · 3"，看着像坏掉的时间戳。
        """
        text = _text()
        self.assertEqual(text.count("function stampText"), 1, "send_at 要由一处按类型渲染")
        self.assertIn("stampText(chunk.send_at, i)", text, "分泡时把 send_at 交给类型判断")
        self.assertIn('typeof sendAt === "number"', text, "缺数字分支（核里给的是第几秒）")
        self.assertIn('typeof sendAt === "string"', text, "缺字符串分支（别处可能给时刻）")
        self.assertIn("首发", text, "第一泡按秒数渲染要写「首发」")
        self.assertIn('"第 "', text, "后续泡按秒数渲染写「第 3.0 秒」")
        self.assertIn("时间没带上", text, "send_at 缺失时要写一句，不能留空")

    def test_seconds_are_never_glued_as_a_clock_string(self):
        """老写法（gapText + " · " + String(send_at)）会把秒数当时刻拼出来：钉它不许回来。"""
        text = _text()
        self.assertNotIn('gapText + " · " + stamp', text, "把秒数当时刻直接拼的老写法回来了")
        self.assertNotIn("var stamp = chunk.send_at", text, "先 String() 再接时刻的老写法回来了")
        self.assertIn('parts.join(" · ")', text, "间隔与发车点用数组拼，类型已经分过")

    def test_renders_only_what_the_contract_sends(self):
        text = _text()
        for key in ("data.chunks", "data.recalled", "data.heart", "data.gate"):
            self.assertIn(key, text, "回复里的 %s 没被读到" % key)


class RecallColumn(unittest.TestCase):
    def test_the_column_says_what_she_remembered(self):
        text = _text()
        self.assertIn("这次她想起了什么", text)
        self.assertIn('id="recall-list"', text)
        self.assertIn('id="recall-empty"', text)
        self.assertIn("这次没想起什么", text, "recalled 为空时要有这一句")

    def test_every_item_carries_source_type_and_evidence(self):
        text = _text()
        for needle, note in (("item.text", "每条要显示正文"),
                             ("item.source", "每条要显示来源"),
                             ("item.source_type", "每条要显示来源类型"),
                             ("item.evidence_key", "每条要显示证据指针"),
                             ("证据：", "证据要带标签"),
                             ("今日", "来源档：今日"),
                             ("热", "来源档：热"),
                             ("长期", "来源档：长期"),
                             ("亲历", "来源类型：亲历"),
                             ("日程模拟", "来源类型：日程模拟")):
            self.assertIn(needle, text, "%s：页面上找不到 %r" % (note, needle))


class HeartGivesWordsNotNumbers(unittest.TestCase):
    def test_three_states_and_gate_are_shown(self):
        text = _text()
        for needle, note in (("mood.tier", "心境档"),
                             ("voice.tone", "语气档"),
                             ("engage.state", "参与态"),
                             ("gate.decision", "命中门判决"),
                             ("hit.clause_id", "命中的条款号"),
                             ("hit.reason", "命中的理由"),
                             ('"pass"', "判决 pass"),
                             ('"dropped"', "判决 dropped"),
                             ("档位", "页上要写明这是档位词")):
            self.assertIn(needle, text, "%s：页面上找不到 %r" % (note, needle))

    def test_heart_renderer_does_no_numeric_conversion(self):
        body = _between(_text(), "function renderHeart", "function dropsText")
        for needle in NUMERIC_IN_HEART:
            self.assertNotIn(needle, body, "心的渲染里在做数值换算：%s" % needle)

    def test_no_heart_reading_is_injected_anywhere(self):
        text = _text()
        for needle in FORBIDDEN_READINGS:
            self.assertNotIn(needle, text, "页面出现了心的读数：%s" % needle)

    def test_no_innerhtml(self):
        self.assertNotIn("innerHTML", _text(), "后端文本一律用 textContent 落地，不拼 HTML")


class AssemblyIsVisible(unittest.TestCase):
    """装配结果也要能被看见：谁进了上下文、各占多少字、回复允许多长。

    判据不只要标题在：sections 真的在循环、cap 真的被读、超限有标记、reply_budget 的
    档位与字数上限真的落在页上，不认识的 key 也不会被静默丢掉（照实显示原 key）。
    """

    def test_block_has_a_title_and_an_empty_state(self):
        text = _text()
        self.assertIn("这次怎么装的", text, "缺装配块标题")
        self.assertIn('id="assembly-list"', text)
        self.assertIn('id="assembly-empty"', text)
        self.assertIn("这次没有装配记录", text, "没有装配记录时要有空态")

    def test_sections_loop_shows_chars_and_cap(self):
        text = _text()
        for needle, note in (("data.assembly", "装配结果来自 POST /chat 的回包"),
                             ("assembly.sections", "逐段读 sections"),
                             ("section.key", "每一段显示自己的 key"),
                             ("section.chars", "每一段显示 chars"),
                             ("section.cap", "每一段显示 cap"),
                             ("超限", "chars 超过 cap 要标超限"),
                             ("魂六件", "段落中文名：魂六件"),
                             ("脑装包", "段落中文名：脑装包"),
                             ("心读数", "段落中文名：心读数"),
                             ("嘴语气", "段落中文名：嘴语气")):
            self.assertIn(needle, text, "%s：页面上找不到 %r" % (note, needle))

    def test_unknown_keys_are_shown_not_dropped(self):
        text = _text()
        self.assertIn("SECTION_TEXT[key]", text, "中文名要按表查，查不到就回原 key")
        self.assertIn("没给 key", text, "连 key 都没有时也要写一句，不能留白")

    def test_reply_budget_is_shown(self):
        text = _text()
        self.assertIn("assembly.reply_budget", text, "回复预算来自 reply_budget")
        self.assertIn("budget.tier", text, "回复预算要显示档位")
        self.assertIn("budget.chars", text, "回复预算要显示字数上限")
        self.assertIn("没给档位", text, "缺档位时要写一句，不许显示空白")


class ReadoutsAreVisible(unittest.TestCase):
    """温度 / 连续性 / 锚句三个读数也要能被看见：字面在、分支在、缺字段时不空白。

    判据不只看标题在：temperature_tier 与 temperature 真的被读到，turns_loaded 与 notice
    分两段拼，anchors 的 configured 真假两条路都走到，三个读数各用一次 textContent。
    """

    def test_block_sits_next_to_the_assembly_record(self):
        text = _text()
        self.assertIn("这一轮的温度、连续性与锚句", text, "缺这块读数的标题")
        self.assertLess(text.index("这次怎么装的"), text.index("这一轮的温度、连续性与锚句"),
                        "读数块该挨着「这次怎么装的」")
        for box in ("llm-temperature", "continuity-line", "anchors-line"):
            self.assertIn('id="%s"' % box, text, "缺读数格子：%s" % box)

    def test_temperature_and_tier_reach_the_page(self):
        body = _between(_text(), "function temperatureText", "function continuityText")
        self.assertIn("conf.temperature", body, "温度要从回包的 llm 里读")
        self.assertIn("temperature_tier", body, "档位要从回包的 llm 里读")
        self.assertIn('"温度 "', body, "读数的形状是「温度 0.5 · 事实档」")
        self.assertIn("没给温度", body, "缺温度时要写一句，不许空白")
        self.assertIn("没给档位", body, "缺档位时要写一句，不许空白")
        text = _text()
        for words in ("事实档", "默认档"):
            self.assertIn(words, text, "档位中文名缺：%s" % words)

    def test_continuity_line_says_loaded_and_omitted(self):
        body = _between(_text(), "function continuityText", "function anchorsText")
        self.assertIn("turns_loaded", body, "要显示最近几轮加载了几条")
        self.assertIn('"最近 "', body, "前半句是「最近 N 轮」")
        self.assertIn("conf.notice", body, "后半句是后端给的 notice 原文")
        self.assertIn("没有更早的对话", body, "notice 是空串（omitted 为 0）时要写这一句")
        self.assertIn("没给轮数", body, "连轮数都没有时也要写一句")

    def test_anchors_line_has_both_states(self):
        body = _between(_text(), "function anchorsText", "function renderReadouts")
        self.assertIn("conf.configured === true", body, "缺「已配」这一支")
        self.assertIn("已配（", body, "配了要写「已配（N 字）」")
        self.assertIn("conf.configured === false", body, "缺「未配」这一支")
        self.assertIn("未配锚句", body, "没配要照后端那句话写「未配锚句」")
        self.assertIn("没给", body, "整个字段都没有时如实写「没给」")
        self.assertIn("conf.items", body, "要逐条显示家·仓库的台词卡")
        self.assertIn("item.injected", body, "注入的是半句")
        self.assertIn("item.say", body, "全文要显示（人得能核对）")
        self.assertIn("item.body", body, "body 要显示（效果示例只给人看）")
        self.assertIn("body：", body, "body 要带标签")
        self.assertIn("规矩：", body, "注入的行为规矩要写明")

    def test_three_readouts_are_written_with_textcontent_only(self):
        body = _between(_text(), "function renderReadouts", "function send")
        self.assertEqual(3, body.count("textContent"), "三个读数各自一次 textContent：%r" % (body,))
        self.assertNotIn("innerHTML", body)

    def test_readouts_come_from_the_reply_body(self):
        text = _text()
        self.assertIn("renderReadouts(data)", text, "回包里那三块没交给渲染")
        for key in ("data.llm", "data.continuity", "data.anchors"):
            self.assertIn(key, text, "回包里的 %s 没被读到" % key)


class ThreeStatesArePresent(unittest.TestCase):
    def test_empty_state_says_how_to_start(self):
        text = _text()
        self.assertIn('id="chat-empty"', text, "缺空态")
        self.assertIn("还没开始", text, "空态要写「还没开始」")
        self.assertIn("回车", text, "空态要写怎么开始")

    def test_loading_state_is_visible_and_locks_the_button(self):
        text = _text()
        self.assertIn('id="loading-card"', text, "缺加载态")
        self.assertIn("她在想…", text, "加载态要写「她在想…」")
        self.assertIn('el("send-btn").disabled = on;', text, "加载时发送按钮要禁用")
        self.assertIn("button:disabled", text, "禁用要有可见的变化")
        self.assertIn('if (on) { show("loading-card"); } else { hide("loading-card"); }', text,
                      "加载态要跟着请求起落")

    def test_error_state_shows_the_message_and_a_chinese_kind(self):
        text = _text()
        self.assertIn('id="error-card"', text, "缺错误态")
        self.assertIn('show("error-card")', text)
        for kind, words in (("no-key", "缺 key"), ("timeout", "超时"),
                            ("rate_limit", "限流"), ("auth", "鉴权")):
            self.assertIn('"%s"' % kind, text, "缺 kind：%s" % kind)
            self.assertIn(words, text, "缺 kind %s 的中文归类：%s" % (kind, words))
        self.assertIn("data.message", text, "后端 message 要原样显示")
        self.assertIn('node("p", "err-msg"', text, "message 落在文本节点里（不拼 HTML）")


class PageIsMountedAtChat(unittest.TestCase):
    def test_chat_path_serves_this_page(self):
        self.assertEqual(PAGE_FILES["/chat"], PAGE, "/chat 没挂到 chat.html")

    def test_page_is_not_an_empty_template(self):
        raw = read_page(PAGE)
        self.assertIsNotNone(raw, "read_page 读不到 chat.html")
        text = raw.decode("utf-8")
        self.assertGreater(len(text), 4000, "页面太短，像空模板")
        self.assertIn("<!doctype html>", text)
        self.assertIn("<script>", text)

    def test_page_declares_it_has_one_write(self):
        self.assertIn("这一页只有一条写", _text())


if __name__ == "__main__":
    unittest.main()
