"""脑面板页的判据：只读为主，写只有两条——接受提案写魂，名片写人写的四栏。

这一页与 heart.html 的处境不同：脑的只读端点在 BRAIN_ROUTES 里，但「接受提案」要走
POST /soul/proposal（写的是魂的 manner.stance，不是脑）；名片要走
PATCH /brain/people/<id>/card（写的是人写的称呼/雷点/备注/照片说明，不是熟稔，也不是记忆）。
所以判据换成这几条机器可查的线：

1. **写只有两条**：页面里 call(METHOD, "字面路径") 中 POST 恰好一次且路径必须是
   /soul/proposal；PATCH 恰好一次且必须是名片（call("PATCH", cardPath(...))）；其余全是 GET
   且都登记在 api.ROUTES 里（方法对得上）。任何新增的 call 都会让 call 总数断言失败——
   这是给「以后顺手加个写按钮」留的绊索。
2. **没有改熟稔 / 改记忆的控件**：没有 contenteditable、没有 <textarea> / <select> / <form>、
   没有内联 oninput / onchange；页面上唯一的静态 <input> 是回想查询框（type="search"）。
   名片四栏的控件由 JS 建，且只有白名单那四栏——CARD_FIELDS 里没有 familiarity。
3. **只读不灰化**：只读块写「由程序写：<理由>」；熟稔那一栏明写
   「熟稔由程序写：记忆写入路径更新，人改不了」，页面上没有任何改熟稔的输入。
4. **四档与遗忘进度是真数据**：/brain/tiers（四档 count + recent）与 /brain/cold（分页）都在
   调用里；retention / clarity 取自条目本身，缺了就不显示那一栏；旧占位文案
   （未暴露 / 暂不可写 / 读不到 / 还没有名片写端点）一律为 0。

**这是静态回归测试，不是审计。** 比对的是源码里的页面与路由表，防的是页面承诺了不存在的路由
（参考实现栽过：按钮打 POST api/pin，服务端没有这条路由）。活体验收（真 devserver + 真数据根）
的原始输出在交付回报里，不在这个文件里。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from api import api
from api.brain import BRAIN_ROUTES
from api.pages import PAGE_FILES

PAGES = Path(__file__).resolve().parents[2] / "pages"
PAGE = "brain.html"

CALL_RE = re.compile(r'call\(\s*"(GET|POST|PUT|DELETE|PATCH)"\s*,\s*"([^"]+)"')
# 名片的调用把路径交给 cardPath()（人名可能是中文，得 encodeURIComponent），所以不匹配
# CALL_RE 的字面量，单列一条：方法 + 那个构造函数。
CARD_CALL_RE = re.compile(r'call\(\s*"(GET|PATCH)"\s*,\s*cardPath\(')
CARD_PATH_RE = re.compile(
    r'function cardPath\([^)]*\)\s*\{\s*return "/brain/people/"\s*\+\s*'
    r'encodeURIComponent\([^)]*\)\s*\+\s*"/card";\s*\}')
CARD_FIELDS_RE = re.compile(r"var CARD_FIELDS = \[(.*?)\];", re.S)
CARD_FIELD_RE = re.compile(r'\["([^"]+)",\s*"([^"]+)",\s*"(input|textarea)"\]')
FETCH_RE = re.compile(r'fetch\(\s*"([^"]+)"')
BUTTON_ID_RE = re.compile(r'<button[^>]*\bid="([^"]+)"')
INPUT_RE = re.compile(r'<input\b[^>]*>')
ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"')
WRITE_VERBS = ("put", "delete")

# 这一页的取数/写数口子：五个轮询 GET（overview / today / people / proposals / selfnarration）
# + 四档 GET(/brain/tiers) + 冷仓两页 GET（首页与带 cursor 的翻页各一个调用点）
# + 回想 GET + 接受提案 POST + 页脚 /healthz = 11。
# 名片的 GET/PATCH 走 cardPath()，由 CARD_CALL_RE 单独钉。
# /brain/today 仍是轮询契约的一部分（test_panel_api 要求每条只读路由都在调用里），
# 现状里的「今日条目」直接用它；四档的今日档改用 /brain/tiers。
EXPECTED_CALLS = 11
ALLOWED_POSTS = {"/soul/proposal"}
OLD_PLACEHOLDERS = ("未暴露", "暂不可写", "读不到", "还没有名片写端点")


def _text(name: str = PAGE) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


def _calls() -> list:
    return [(method, path.partition("?")[0]) for method, path in CALL_RE.findall(_text())]


class OldPlaceholdersAreGone(unittest.TestCase):
    """四处占位文案换成真数据之后，这些词一个都不许留。"""

    def test_no_placeholder_copy(self):
        text = _text()
        for word in OLD_PLACEHOLDERS:
            self.assertNotIn(word, text, "旧占位文案还在：%s" % word)


class WritesAreTheProposalAndTheCard(unittest.TestCase):
    def test_exactly_one_post_and_it_is_soul_proposal(self):
        """POST 只有一条：接受提案（它写的是魂，不是脑）。"""
        posts = [route for method, route in _calls() if method == "POST"]
        self.assertEqual(posts, ["/soul/proposal"],
                         "写入口不止一条，或打的不是接受提案：%r" % (posts,))

    def test_exactly_one_patch_and_it_is_the_card(self):
        """PATCH 只有一条，且打的是名片：call("PATCH", cardPath(...))。"""
        text = _text()
        card_calls = CARD_CALL_RE.findall(text)
        self.assertEqual(sorted(card_calls), ["GET", "PATCH"],
                         "名片的读/写该各一次（GET 历史 + PATCH 写四栏）：%r" % (card_calls,))
        self.assertEqual(text.count('call("PATCH"'), 1, "PATCH 不止一处")
        self.assertNotIn('call("PATCH", "', text, "PATCH 直接写死路径：人名没经 encodeURIComponent")

    def test_card_path_is_encoded_and_has_no_prefix_bypass(self):
        """人名可以是中文：路径段必须 encodeURIComponent，且只由 cardPath() 一处拼。"""
        text = _text()
        self.assertRegex(text, CARD_PATH_RE,
                         "cardPath() 没按 /brain/people/<encodeURIComponent(id)>/card 拼")
        self.assertEqual(text.count('"/brain/people/"'), 1,
                         "名片路径被拼到了第二处，改一处会漏一处")

    def test_card_patch_body_carries_actor(self):
        """body 必须带 actor（服务端靠它记历史里的签字人）。"""
        text = _text()
        body = text[text.index("function saveCard"):]
        body = body[:body.index("function loadCard")]
        self.assertIn("call(\"PATCH\", cardPath(person.id)", body)
        self.assertIn("{ actor: ACTOR, fields: fields }", body,
                      "PATCH body 没带 actor 或没带 fields")
        self.assertIn("fields[spec[0]]", body,
                      "送的字段名该来自 CARD_FIELDS 的中文白名单，不另编")

    def test_card_route_answers_get_and_patch(self):
        """真路由对得上：GET/PATCH 打得进 dispatch_card（不是 404 也不是 405）。"""
        self.assertNotIn("/brain/people/小明/card", api.ROUTES,
                         "名片是动态路由，不该进静态路由表")
        for method in ("GET", "PATCH"):
            status, payload = api.handle(method, "/brain/people/小明/card",
                                         {"actor": "面板", "fields": {}}, None)
            self.assertEqual(status, 503,
                             "%s 名片没落到 dispatch_card：%r" % (method, payload))
        # brain=None 是 503（路由已命中，只是脑没载入）；给个桩对象才能走到方法检查那一支。
        status, _ = api.handle("DELETE", "/brain/people/小明/card", None, None,
                               brain=object())
        self.assertEqual(status, 405, "名片的 DELETE 该被 405 挡掉")

    def test_card_whitelist_is_the_four_chinese_labels(self):
        text = _text()
        block = CARD_FIELDS_RE.search(text)
        self.assertIsNotNone(block, "找不到 CARD_FIELDS：名片四栏没登记")
        fields = CARD_FIELD_RE.findall(block.group(1))
        self.assertEqual([label for label, _, _ in fields],
                         ["称呼", "雷点", "备注", "照片说明"],
                         "名片白名单字段变了，核对服务端 CARD_FIELDS：%r" % (fields,))
        self.assertEqual([key for _, key, _ in fields],
                         ["display", "prefs", "notes", "photo_note"])
        self.assertNotIn("familiarity", block.group(1), "熟稔被写进了可写字段")

    def test_allowed_posts_are_registered(self):
        for route in ALLOWED_POSTS:
            self.assertIn(route, api.ROUTES, "接受提案的路由没登记：%s" % route)
            self.assertEqual(api.ROUTES[route], "POST")

    def test_everything_else_is_get_and_registered(self):
        """除那一条 POST，call() 里的字面路径全是 GET，且都登记在路由表里、方法对得上。"""
        calls = _calls()
        self.assertTrue(calls, "页面没有取数口子")
        for method, route in calls:
            if method == "POST":
                continue
            self.assertEqual(method, "GET", "这一页不该有 %s 请求：%s" % (method, route))
            self.assertIn(route, api.ROUTES, "页面在打一条不存在的路由：%s" % route)
            self.assertEqual(api.ROUTES[route], "GET",
                             "方法对不上：页面用 GET 打 %s，路由是 %s" % (route, api.ROUTES[route]))

    def test_tiers_and_cold_are_called(self):
        """四档与冷仓必须真连端点，不许再靠 /brain/today 与回想拼。"""
        used = {route for _, route in _calls()}
        self.assertIn("/brain/tiers", used, "四档没打 /brain/tiers")
        self.assertIn("/brain/cold", used, "冷仓没打 /brain/cold")

    def test_call_sites_are_frozen(self):
        """call 的条数钉死：加一条就说明有人往这一页塞了新的取数/写数口子，回来核对。"""
        self.assertEqual(len(_calls()), EXPECTED_CALLS,
                         "call( 的条数变了：%r" % (_calls(),))

    def test_no_put_or_delete_anywhere(self):
        text = _text().lower()
        for verb in WRITE_VERBS:
            self.assertNotIn('"%s"' % verb, text, "页面出现了 %s 请求" % verb.upper())
        self.assertEqual(text.count('"patch"'), 1, "PATCH 只该有名片那一条")

    def test_reject_is_pure_ui(self):
        """拒绝只记一条、不发请求：文案在页面上，且拒绝分支里没有 call(。"""
        text = _text()
        self.assertIn("拒绝（不发请求）", text)
        self.assertIn("只记在这一页，没有发请求、没有落盘", text)
        body = text[text.index("function rejectProposal"):]
        body = body[:body.index("el(\"recall-btn\")")]
        self.assertNotIn("call(", body, "rejectProposal 里出现了请求：拒绝必须是纯 UI")
        self.assertNotIn("fetch(", body, "rejectProposal 里出现了请求：拒绝必须是纯 UI")

    def test_no_bare_fetch_bypasses_the_helper(self):
        """除 call() 里那唯一一个 fetch，页面不许自己 fetch，防绕开帮助函数。

        绕过 call() 的第二条路是「自己写绝对路径」：任何一处 fetch("/brain/...") 都会在
        iframe 里打到宿主根（壳只有 /qqagent/panel 前缀才剥），所以连字面量都不许出现。
        """
        text = _text()
        self.assertEqual(len(FETCH_RE.findall(text)), 0, "出现了裸 fetch(\"path\")")
        self.assertEqual(text.count("fetch(BASE + path, init)"), 1,
                         "fetch 被复制到了 call() 之外，或没补挂载前缀")
        self.assertEqual(re.findall(r"""fetch\(\s*["']""", text), [],
                         "有 fetch 直接写死路径：绕过了 call() 的前缀补全")


class RequestsFollowTheMountPrefix(unittest.TestCase):
    """iframe 里的活路：页面挂在 /qqagent/panel/brain，壳只剥 /qqagent/panel 前缀。

    页面自己挂在 /brain 时 BASE 是空串，请求原样打 /brain/...；
    挂在 /qqagent/panel/brain 时 BASE 是 /qqagent/panel，请求变成 /qqagent/panel/brain/...
    （壳剥掉前缀后落到核的 /brain/...）。名片是相对挂载的路径，所以它也必须走 call()。
    """

    def test_mount_and_base_are_derived_from_pathname(self):
        text = _text()
        self.assertIn('var MOUNT = location.pathname.replace(/\\/+$/, "");', text,
                      "缺挂载前缀：iframe 里的绝对路径会打到宿主根")
        self.assertIn('var BASE = MOUNT.slice(-6) === "/brain" ? MOUNT.slice(0, -6) : "";', text,
                      "BASE 要按挂载路径算：直开核时为空串，挂在壳里时是壳前缀")
        self.assertEqual(text.count("BASE + path"), 1,
                         "补前缀的地方该只有 call() 一处")
        self.assertNotIn('fetch("/brain', text)
        self.assertNotIn("fetch('/brain", text)
        self.assertNotIn('fetch("/soul', text)

    def test_write_goes_through_the_same_prefix(self):
        """POST /soul/proposal 与 PATCH 名片都必须走 call()。"""
        text = _text()
        self.assertEqual(1, text.count('call("POST", "/soul/proposal"'))
        self.assertEqual(1, text.count('call("PATCH", cardPath('),
                         "名片写入绕开了 call()：iframe 里会打到宿主根")
        self.assertIn("return fetch(BASE + path, init)", text,
                      "写与读要共用一个出口，只给 /brain/* 补前缀等于在壳里写不进去")


class NoControlCanChangeAffinityOrMemory(unittest.TestCase):
    """只读的机器证据：这一页没有能改熟稔、也没有能改记忆的控件。"""

    def test_no_editable_regions(self):
        text = _text().lower()
        self.assertNotIn("contenteditable", text, "页面有可编辑区域")
        self.assertNotIn("<textarea", text, "页面有文本域")
        self.assertNotIn("<select", text, "页面有下拉框")
        self.assertNotIn("<form", text, "页面有表单（会带来提交按钮）")
        for handler in ("oninput=", "onchange=", "onkeyup="):
            self.assertNotIn(handler, text, "页面有内联编辑事件：%s" % handler)

    def test_the_only_static_input_is_the_recall_query(self):
        """静态标记里只有一个 <input>：回想查询框（type=search，不带 name）。

        名片四栏的控件由 JS 现建（node(spec[2])），标记里不留输入框：这样「页面上多了个输入框」
        只可能来自 CARD_FIELDS 那张白名单表。
        """
        inputs = INPUT_RE.findall(_text())
        self.assertEqual(len(inputs), 1, "输入控件不止一个：%r" % (inputs,))
        tag = inputs[0]
        self.assertIn('type="search"', tag, "唯一的输入框必须是只读查询（type=search）：%s" % tag)
        self.assertIn('id="recall-q"', tag, "唯一的输入框必须是回想查询框：%s" % tag)
        self.assertNotIn("name=", tag, "查询框不该带 name（它不是任何表单的提交字段）")

    def test_card_controls_are_built_from_the_whitelist_only(self):
        text = _text()
        self.assertIn('var input = node(spec[2]);', text,
                      "名片的输入控件该由 CARD_FIELDS 现建，而不是在标记里手写")
        self.assertNotIn('node("input"', text, "多出来一个手写的输入框")
        self.assertNotIn('node("textarea"', text, "多出来一个手写的文本域")

    def test_familiarity_is_read_only(self):
        """熟稔只读：页面上只有一次读它（显示），没有任何能改它的控件。"""
        text = _text()
        self.assertEqual(text.count("familiarity"), 1,
                         "熟稔出现在了第二个地方（多半是某个输入控件）")
        self.assertIn("person.familiarity", text, "熟稔该只作为显示读数被读一次")
        self.assertIn("熟稔由程序写：记忆写入路径更新，人改不了", text,
                      "熟稔那一栏要明写「由程序写」")
        for shape in ('name="familiarity"', "familiarity:", '"熟稔", "familiarity"'):
            self.assertNotIn(shape, text, "出现了改熟稔的控件形状：%s" % shape)

    def test_readonly_blocks_say_who_writes_them(self):
        """只读不灰化：写「由程序写：……」，不写 disabled、不灰掉。"""
        text = _text()
        self.assertGreaterEqual(text.count("由程序写："), 8,
                                "只读块的「由程序写：」少了，别处大概被灰化了")
        self.assertIn("熟稔由记忆写入路径更新，人改会变成第二份真源", text)
        # 只看标记部分：JS 里把按钮在提交中临时禁用是正常交互，
        # 但标记里不许把只读块写成 disabled（灰化等于承诺"以后会开放"）。
        markup = text.split("<script>")[0].lower()
        self.assertNotIn("disabled", markup, "只读块被灰化了：只读要写理由，不是禁用")


class MemoryPageTemplateIsComplete(unittest.TestCase):
    """记忆页（ui.md §2）：四档分开 + 来源标记 + 真遗忘进度 + 各自空态。"""

    def test_four_tiers_are_separate_blocks(self):
        text = _text()
        for tier_id in ("tier-today", "tier-hot", "tier-longterm", "tier-cold"):
            self.assertIn('id="%s"' % tier_id, text, "缺一档：%s" % tier_id)
        for title in ("今日", "热", "长期", "冷仓"):
            self.assertIn("<h3>%s" % title, text, "缺一档的标题：%s" % title)

    def test_source_badges_are_text_not_icons(self):
        text = _text()
        self.assertIn("日程模拟", text, "来源标记缺日程模拟")
        self.assertIn("亲历", text, "来源标记缺亲历")
        self.assertIn('node("span", "badge ', text, "来源标记该是文字徽标")

    def test_forget_progress_comes_from_retention_and_clarity(self):
        """遗忘进度用条目自带的 retention（0–1 → 百分比）与 clarity；缺了就不显示那一栏。"""
        text = _text()
        self.assertIn("遗忘进度：", text, "每条记忆都该带遗忘进度栏")
        self.assertIn('node("span", "row-meta forget"', text)
        self.assertIn("entry.retention", text, "遗忘进度没读 retention")
        self.assertIn("entry.clarity", text, "遗忘进度没读 clarity")
        self.assertIn("Math.round(num * 100)", text, "retention 要按百分比显示（0–1 → %）")
        body = text[text.index("function forgetText"):]
        body = body[:body.index("function memoryRow")]
        self.assertIn("isNaN(num)", body, "retention 不是数就该整栏不显示，不许编数")
        self.assertIn('return "";', body, "拿不到 retention 时该返回空串（不显示那一栏）")
        self.assertIn("if (forget) {", text, "空字符串时不该渲染遗忘进度")

    def test_about_block_uses_memory_dates(self):
        """关于他：日期来自 memories 的 ts，不再是「无日期」。"""
        text = _text()
        self.assertNotIn("无日期", text, "关于他还在写「无日期」")
        self.assertIn("person.memories", text, "关于他没读 memories")
        self.assertIn("dayText(memory.ts)", text, "关于他的日期该取每条记忆的 ts")
        self.assertIn('slice(0, 5)', text, "关于他最多列 5 条")

    def test_empty_states_are_per_tier_and_errors_are_not_empty(self):
        """空数组写「空」，读失败写状态码与原因：两件事不折叠成一句话。"""
        text = _text()
        self.assertNotIn("没有数据", text)
        for empty in ("空（今日档今天没有条目）", "空（热档没有条目）",
                      "空（长期档没有生效条目）", "空（冷仓 0 条）"):
            self.assertIn(empty, text, "缺这一档的空态：%s" % empty)
        self.assertIn("读失败（HTTP ", text, "出错要报 HTTP 状态，不许写成空")
        self.assertIn("if (status === 0)", text, "载入中 / 空 / 出错要分三支")
        self.assertIn("if (status !== 200)", text, "出错分支单独写")

    def test_cold_pagination_only_shows_next_page_with_a_cursor(self):
        text = _text()
        self.assertIn("next_cursor", text, "冷仓没读 next_cursor")
        self.assertIn("下一页", text, "冷仓缺「下一页」按钮")
        body = text[text.index("function renderColdMore"):]
        body = body[:body.index("function loadCold")]
        self.assertIn("!STATE.cold.next", body,
                      "没有 next_cursor 时不该画出「下一页」")
        self.assertIn("?cursor=", text, "翻页没把 cursor 带回去")


class ThreeStatesArePresent(unittest.TestCase):
    def test_loading_error_and_empty_are_distinct(self):
        text = _text()
        self.assertIn('id="loading-card"', text, "缺加载态")
        self.assertIn('id="error-card"', text, "缺错误态")
        self.assertIn("503", text, "错误态要分开写 503 核没起")
        self.assertIn("400 参数不对", text, "错误态要分开写 400 参数不对")
        self.assertIn("空（", text, "空态要写「空」")
        self.assertIn("载入中…", text, "加载态要写「载入中…」")

    def test_one_poll_loop_only(self):
        """一套组件一份数据源：只许一个轮询。"""
        self.assertEqual(_text().count("setInterval("), 1, "轮询不止一处")


class ClickablesPointAtRealRoutes(unittest.TestCase):
    def test_every_static_button_has_a_handler(self):
        text = _text()
        ids = BUTTON_ID_RE.findall(text)
        self.assertEqual(ids, ["recall-btn", "retry-btn"],
                         "静态按钮变了，核对表要同步：%r" % (ids,))
        for button_id in ids:
            self.assertIn('el("%s").addEventListener' % button_id, text,
                          "按钮 %s 没有绑定任何事件（死按钮）" % button_id)

    def test_card_buttons_point_at_the_card_route(self):
        """名片两个按钮：保存 = PATCH …/card、历史 = GET …/card。"""
        text = _text()
        self.assertIn("var CARD_SAVE = ", text)
        self.assertIn("PATCH /brain/people/<id>/card", text, "保存按钮没写它打哪条路由")
        self.assertIn("var CARD_HISTORY = ", text)
        self.assertIn("GET /brain/people/<id>/card", text, "历史按钮没写它打哪条路由")
        self.assertIn('save.addEventListener("click"', text)
        self.assertIn('hist.addEventListener("click"', text)
        more = text[text.index("function renderColdMore"):]
        more = more[:more.index("function loadCold")]
        self.assertIn('more.addEventListener("click"', more, "「下一页」按钮没绑事件（死按钮）")

    def test_anchors_resolve_to_pages(self):
        text = _text()
        for href in ANCHOR_RE.findall(text):
            self.assertTrue(href in ("..", "heart"), "多了一条去向不明的链接：%r" % href)
        self.assertEqual(text.count('<a href='), 2)


class PageIsMountedAtBrain(unittest.TestCase):
    def test_brain_path_serves_this_page(self):
        self.assertEqual(PAGE_FILES["/brain"], "brain.html")

    def test_page_declares_the_boundary(self):
        text = _text()
        self.assertIn("只能看", text)
        self.assertIn("architecture.md §9.2", text)
        self.assertIn("接受提案", text)
        self.assertIn("写的是魂，不是脑", text)

    def test_brain_routes_are_all_readonly(self):
        """页面用到的 /brain* 字面路由都必须是只读方法（写只走名片那条动态路由）。"""
        for route, method in BRAIN_ROUTES.items():
            self.assertEqual(method, "GET", "脑出现了写路由：%s %s" % (method, route))
        used = {route for _, route in _calls() if route.startswith("/brain")}
        self.assertTrue(used, "页面没读任何脑端点")
        self.assertTrue(used.issubset(set(BRAIN_ROUTES)),
                        "页面读了不存在的脑端点：%r" % (used - set(BRAIN_ROUTES)))


if __name__ == "__main__":
    unittest.main()
