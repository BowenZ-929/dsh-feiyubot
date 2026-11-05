"""家的面板判据：每个可点元素对着一条真实路由；抽屉那块没有任何写控件。

形状照 test_pages_routes.py（同一套正则、同一类断言），看住的是家这一页的五件事：

1. 页面上每一次 call(METHOD, path) 都在 api.ROUTES 里、方法对得上，且属于 HOME_ROUTES 那十条；
2. 静态按钮都有 addEventListener，动态按钮（点房看件、点件翻看）也各自指着一条 GET 路由；
3. **唯一的写入口是 POST /home/write**：全页只许出现一次写调用，其余九条一律 GET；
4. **抽屉是只读房**：那一块不许出现任何写控件，那一格归人（data-owner="human"）；
5. 十间房不许有「新建房间」，只读处不许灰化，三种态钉在 data-state 上、审计缩水不自动修。

这是静态回归测试，不是审计：它比对源码里的页面与路由表。活体要打正在跑的进程
（CONTRIBUTING §1.3）——本项目踩过源码有路由、跑着的旧进程全回 404。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import urljoin

from api import api
from api.home import HOME_ROUTES
from api.pages import PAGE_FILES

PAGES = Path(__file__).resolve().parents[2] / "pages"
PAGE_MOUNT = {name: path for path, name in PAGE_FILES.items()}

CALL_RE = re.compile(r'call\(\s*"(GET|POST|PUT|DELETE)"\s*,\s*"([^"]+)"')
FETCH_RE = re.compile(r'fetch\(\s*"([^"]+)"')
BUTTON_ID_RE = re.compile(r'<button[^>]*\bid="([^"]+)"')
ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"')
DRAWER_SECTION_RE = re.compile(r'<section[^>]*\bid="drawer-block"[^>]*>(.*?)</section>', re.S)
WRITE_VERBS = ('"put"', '"delete"', '"patch"')
READONLY_ROOMS = ("客厅", "卧室", "橱柜", "抽屉", "台历")


def _text(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


def _calls(text: str):
    return [(method, path.partition("?")[0]) for method, path in CALL_RE.findall(text)]


class HomePageIsMounted(unittest.TestCase):
    def test_page_is_registered_under_home(self):
        """/home 已经挂到 home.html，且文件真在 pages/ 里。"""
        self.assertEqual(PAGE_FILES["/home"], "home.html")
        self.assertTrue((PAGES / "home.html").is_file(), "api/pages.py 登记了 home.html，文件却不在")

    def test_title_is_the_home_page(self):
        self.assertRegex(_text("home.html"), r"<title>[^<]*家[^<]*</title>",
                         "家的页面的 <title> 要写着家")


class EveryClickableHasARoute(unittest.TestCase):
    def test_calls_only_registered_routes(self):
        """页面上每个 call(METHOD, path) 都在路由表里，方法对得上，且属于家的十条。"""
        text = _text("home.html")
        calls = _calls(text)
        self.assertGreaterEqual(len(calls), 10, "家这一页该有十次调用（九读一写），少了说明解析没抓到")
        for method, route in calls:
            self.assertIn(route, api.ROUTES, "页面在打一条不存在的路由：%s" % route)
            self.assertEqual(api.ROUTES[route], method,
                             "方法对不上：页面用 %s 打 %s，路由是 %s" % (method, route, api.ROUTES[route]))
            if route == "/healthz":
                continue  # 页脚那一行（核自报版本）：全局只读端点，不属于家的十条
            self.assertIn(route, HOME_ROUTES, "家这一页打了别的器官的路由：%s" % route)

    def test_fetches_only_registered_paths(self):
        """任何裸 fetch("path") 也要落在路由表或页面表里（防止绕开 call 帮助函数）。"""
        for path in FETCH_RE.findall(_text("home.html")):
            route = path.partition("?")[0]
            self.assertTrue(route in api.ROUTES or route in PAGE_FILES,
                            "页面在 fetch 一条不存在的路径：%s" % route)

    def test_every_static_button_has_a_handler(self):
        """没绑事件的按钮 = 点了没反应的死按钮。"""
        text = _text("home.html")
        ids = BUTTON_ID_RE.findall(text)
        self.assertEqual(sorted(ids), ["audit-verify", "refresh-all", "write-submit"],
                         "家这一页静态按钮数变了，核对表要同步：%r" % (ids,))
        for button_id in ids:
            self.assertIn('el("%s").addEventListener' % button_id, text,
                          "按钮 %s 没有绑定任何事件" % button_id)

    def test_dynamic_buttons_are_backed_by_a_route(self):
        """点房看件、点件翻看都是 JS 现造的按钮：它们走的两条 GET 路由必须存在。"""
        text = _text("home.html")
        self.assertIn('createElement("button")', text)
        for route in ("/home/list", "/home/read"):
            self.assertIn('call("GET", "%s?room=' % route, text, "动态按钮要打 %s" % route)
            self.assertEqual(HOME_ROUTES[route], "GET")

    def test_links_resolve_to_real_pages(self):
        """本页的链接按挂载路径解析后，必须落到一张真实页面。"""
        for href in ANCHOR_RE.findall(_text("home.html")):
            target = urljoin("/home", href).partition("?")[0]
            self.assertTrue(target in PAGE_FILES,
                            "%s 里的链接 %r 解析成 %s，不是一张页面" % ("home.html", href, target))


class TheOnlyWriteEntryIsHomeWrite(unittest.TestCase):
    """家是可写的（architecture §9.2），但写入口只有一条：POST /home/write。"""

    def test_only_one_write_call(self):
        posts = [route for method, route in _calls(_text("home.html")) if method == "POST"]
        self.assertEqual(posts, ["/home/write"], "全页只许有 POST /home/write 这一次写：%r" % (posts,))
        self.assertEqual(HOME_ROUTES["/home/write"], "POST")

    def test_no_other_write_verbs(self):
        text = _text("home.html").lower()
        for verb in WRITE_VERBS:
            self.assertNotIn(verb, text, "页面出现了 %s" % verb)

    def test_write_form_has_the_three_fields(self):
        """放一件东西：选房 + 写内容 + 一句「这是什么」。"""
        text = _text("home.html")
        for field_id in ('id="write-room"', 'id="write-content"', 'id="write-note"', 'id="write-name"'):
            self.assertIn(field_id, text, "写入口少了一栏：%s" % field_id)

    def test_page_warns_about_writing_her_current_state(self):
        """不许把「她此刻如何」当物件写进来（§9.2）：钉抽屉那一格的块，不钉文案。"""
        self.assertIn('id="drawer-block"', _text("home.html"))

    def test_body_is_built_from_the_card_shape(self):
        """提交的卡要用后端真收的字段（kind/name/note/chars/path），不许自己编一个 body 形状。"""
        text = _text("home.html")
        self.assertIn('{ role: "human", by: "panel", room: room, cards: [card] }', text)
        self.assertIn('var card = { kind: kind, name: name, note: note, chars: content.length };', text)


class DrawerIsReadOnly(unittest.TestCase):
    """抽屉是只读房：那一块不许有任何写控件，那一格归人（data-owner="human"）。"""

    def _drawer(self) -> str:
        section = DRAWER_SECTION_RE.search(_text("home.html"))
        self.assertIsNotNone(section, "页面上没有 drawer-block 这一块")
        return section.group(1)

    def test_no_write_control_in_the_drawer_block(self):
        for forbidden in ("<button", "<input", "<textarea", "<select", "<form", "contenteditable"):
            self.assertNotIn(forbidden, self._drawer().lower(),
                             "抽屉那一块出现了写控件：%s" % forbidden)

    def test_drawer_is_owned_by_a_human_not_the_program(self):
        self.assertRegex(_text("home.html"), r'id="drawer-block"[^>]*data-owner="human"')

    def test_drawer_renderer_never_writes(self):
        """渲染抽屉的那段 JS 里不许出现写请求。"""
        text = _text("home.html")
        start = text.index("function showDrawer")
        end = text.index("// —— 借阅条")
        chunk = text[start:end]
        self.assertNotIn('"POST"', chunk)
        self.assertNotIn("fetch(", chunk)


class TenRoomsAreFixed(unittest.TestCase):
    def test_no_new_room_affordance(self):
        """十间房固定：room-grid 上没有「新建房间」这个动作。"""
        text = _text("home.html")
        self.assertIn('id="room-grid"', text, "十间房那一格要在")
        self.assertNotIn('data-action="create-room"', text, "room-grid 上出现了新建房的入口")

    def test_rooms_come_from_one_source(self):
        """宫格与写入口的下拉都吃 /home/rooms 这一份数据，不各写一份房名表。"""
        text = _text("home.html")
        self.assertIn('call("GET", "/home/rooms")', text)
        start = text.index("function showRooms")
        end = text.index("// —— 一间房里有什么")
        chunk = text[start:end]
        self.assertIn('el("room-grid")', chunk)
        self.assertIn('el("write-room")', chunk)

    def test_readonly_rooms_are_not_greyed_out(self):
        """只读不灰化：五间只读房各有一句「谁写的」，而且页面不许 disable 任何控件。"""
        text = _text("home.html")
        self.assertNotIn("disabled", text.lower(), "只读不许灰化：页面出现了 disabled")
        for room in READONLY_ROOMS:
            self.assertIn('"%s":' % room, text, "只读房 %s 没有「由谁写」那句话" % room)
        self.assertIn("WRITTEN_BY_PROGRAM", text)


class ThreeStatesAreAllPresent(unittest.TestCase):
    """三态钉在同一个容器的 data-state 上：三个取值各自立住，不靠读文案。"""

    def test_loading_state(self):
        self.assertIn('nodeState("list-state", "loading")', _text("home.html"))

    def test_empty_state_is_not_confused_with_missing(self):
        """200 但空数组是 empty，不是 error；也不许折叠成「没有数据」。"""
        text = _text("home.html")
        self.assertIn('nodeState("list-state", "empty")', text)
        self.assertNotIn("没有数据", text)

    def test_error_state_names_the_three_causes(self):
        """错误三分：核没起 / 连不上 / 数据坏（带码与话原样上去）。"""
        text = _text("home.html")
        self.assertIn('nodeState("list-state", "error")', text)
        self.assertIn("function problem(res)", text)
        self.assertIn('HTTP " + res.status', text)

    def test_audit_shrink_is_reported_not_repaired(self):
        """审计只增不改：行数比上次少要红字报错、不自动修。"""
        text = _text("home.html")
        self.assertIn('id="audit-shrink"', text)
        self.assertIn("localStorage", text)
        self.assertIn("home.audit.lines", text)
        self.assertNotIn("repaired = true", text)

    def test_audit_verify_button_targets_the_verify_route(self):
        text = _text("home.html")
        self.assertIn('call("GET", "/home/audit/verify")', text)
        self.assertEqual(HOME_ROUTES["/home/audit/verify"], "GET")

    def test_one_data_source_no_second_poller(self):
        """一套组件一份数据源：全页只有一次 setInterval/timer 也不行，刷新只有一个入口。"""
        text = _text("home.html")
        for forbidden in ("setInterval", "setTimeout"):
            self.assertNotIn(forbidden, text, "页面自己起了第二个轮询：%s" % forbidden)
        self.assertEqual(text.count("Promise.all("), 1, "读端点该在同一个 Promise.all 里一次拉齐")


class StyleFollowsTheHost(unittest.TestCase):
    def test_colors_come_from_host_variables(self):
        text = _text("home.html")
        self.assertGreaterEqual(text.count("--dsw-alias-"), 8, "颜色该全取宿主变量")
        self.assertEqual(len(re.findall(r'var\(--dsw-alias-[a-z0-9-]+,', text)),
                         text.count("var(--dsw-alias-"), "每个宿主变量都要带兜底值")

    def test_dark_theme_is_followed_not_invented(self):
        text = _text("home.html")
        self.assertIn("prefers-color-scheme: dark", text)
        self.assertIn('[data-theme="dark"]', text)


if __name__ == "__main__":
    unittest.main()
