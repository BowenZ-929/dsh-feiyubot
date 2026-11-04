"""面板判据：页面上每个可点元素都必须对着一条真实路由；心与脑没有任何写入口。

这条判据来自参考实现的事故：按钮打 `POST api/pin`，服务端根本没有这条路由——
UI 承诺了不存在的能力，刷新即失效。

被看住的三件事：
1. pages/soul.html 里每一次写请求（call/fetch 的 METHOD + 路径）都必须在 api.ROUTES 里，
   且方法对得上（POST 端点不能被页面当 GET 用，反之亦然）；
2. 页面上每个静态按钮都必须有 JS 绑定（没有绑定的按钮点了什么都不发生 = 死按钮）；
3. pages/heart.html 与 pages/brain.html 的可点元素全部列出并逐一断言：只有一条回跳链接，
   没有任何按钮、输入框、表单，也没有任何写动词——「人不能改心与脑」不只是口头承诺。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import urljoin

from api import api
from api.pages import PAGE_FILES

PAGES = Path(__file__).resolve().parents[2] / "pages"
PAGE_MOUNT = {name: path for path, name in PAGE_FILES.items()}

# 页面里所有「点下去会发请求」的写法：call("METHOD", "path") 与 fetch("path", {method})
CALL_RE = re.compile(r'call\(\s*"(GET|POST|PUT|DELETE)"\s*,\s*"([^"]+)"')
FETCH_RE = re.compile(r'fetch\(\s*"([^"]+)"')
BUTTON_ID_RE = re.compile(r'<button[^>]*\bid="([^"]+)"')
ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"')
WRITE_VERBS = ('"post"', '"put"', '"delete"', '"patch"')


def _text(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


class EveryClickableHasARoute(unittest.TestCase):
    def test_soul_page_calls_only_registered_routes(self):
        """页面上每个 call(METHOD, path) 都在路由表里，方法对得上。"""
        text = _text("soul.html")
        calls = CALL_RE.findall(text)
        self.assertGreaterEqual(len(calls), 8, "魂页该有若干次读写调用，少了说明解析没抓到")
        for method, path in calls:
            route = path.partition("?")[0]
            self.assertIn(route, api.ROUTES, "页面在打一条不存在的路由：%s" % route)
            self.assertEqual(api.ROUTES[route], method,
                             "方法对不上：页面用 %s 打 %s，路由是 %s" % (method, route, api.ROUTES[route]))

    def test_soul_page_fetches_only_registered_routes(self):
        """任何裸 fetch("path") 也要落在路由表里（防止绕开 call 帮助函数）。"""
        for path in FETCH_RE.findall(_text("soul.html")):
            route = path.partition("?")[0]
            self.assertTrue(route in api.ROUTES or route in PAGE_FILES,
                            "页面在 fetch 一条不存在的路径：%s" % route)

    def test_every_static_button_has_a_handler(self):
        """没绑事件的按钮 = 点了没反应的死按钮。"""
        text = _text("soul.html")
        ids = BUTTON_ID_RE.findall(text)
        self.assertEqual(len(ids), 4, "魂页静态按钮数变了，核对表要同步：%r" % (ids,))
        for button_id in ids:
            self.assertIn('el("%s").addEventListener' % button_id, text,
                          "按钮 %s 没有绑定任何事件" % button_id)

    def test_dynamic_buttons_are_backed_by_a_route(self):
        """红线列表里「撤这条」是 JS 现造的按钮，它走的路由同样必须存在。"""
        text = _text("soul.html")
        if 'createElement("button")' not in text:
            self.skipTest("页面没有动态创建的按钮")
        self.assertIn('call("POST", "/soul/redline"', text, "动态按钮要打 /soul/redline")
        self.assertIn("/soul/redline", api.ROUTES)
        self.assertEqual(api.ROUTES["/soul/redline"], "POST")


class EveryLinkPointsSomewhere(unittest.TestCase):
    def test_all_anchors_resolve_to_an_existing_page(self):
        """每页的 <a href> 按它自己的挂载路径解析后，必须落到一张真实页面（或一条 GET 路由）。"""
        for page, mount in PAGE_MOUNT.items():
            for href in ANCHOR_RE.findall(_text(page)):
                target = urljoin(mount, href).partition("?")[0]
                self.assertTrue(target in PAGE_FILES or api.ROUTES.get(target) == "GET",
                                "%s 里的链接 %r 解析成 %s，既不是页面也不是 GET 路由"
                                % (page, href, target))


class HeartAndBrainHaveNoWriteEntry(unittest.TestCase):
    """「人不能改心与脑」的机器证据：把两页的可点元素全部列出来，一个一个过。"""

    def test_clickable_elements_are_only_the_back_link(self):
        for page, mount in (("heart.html", "/heart"), ("brain.html", "/brain")):
            text = _text(page).lower()
            for forbidden in ("<button", "<form", "<textarea", "<select", "<input"):
                self.assertNotIn(forbidden, text, "%s 出现了写控件：%s" % (page, forbidden))
            for verb in WRITE_VERBS:
                self.assertNotIn(verb, text, "%s 出现了写请求：%s" % (page, verb))
            self.assertNotIn("contenteditable", text, "%s 有可编辑区域" % page)

            # 可点元素只有 <a>：逐条解析，必须都是回魂面板
            anchors = ANCHOR_RE.findall(_text(page))
            self.assertEqual(len(anchors), 1, "%s 的可点元素不止一条回跳：%r" % (page, anchors))
            target = urljoin(mount, anchors[0]).partition("?")[0]
            self.assertEqual(target, "/", "%s 的回跳该回魂面板，实际指向 %s" % (page, target))
            self.assertEqual(PAGE_FILES["/"], "soul.html")


if __name__ == "__main__":
    unittest.main()
