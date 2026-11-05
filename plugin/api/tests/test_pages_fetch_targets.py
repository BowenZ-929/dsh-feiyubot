"""页面取数不许打错：把每页真正 fetch 的地址，逐条打到活进程上验。

背景：心页曾经三个 bug 一起过——取数写成绝对路径（iframe 里打到宿主根 404）、
把 `/heart`（页面本身，HTML）当 JSON 端点打、占位文案冒充真数据。**三十多条页面测试一条都没红。**

所以这条测试不比对字符串，它真起一个 devserver、真发 HTTP。判据按方法分开，因为
`/chat` 这类地址**同时是页面（GET）和端点（POST）**，一刀切会冤枉真端点：

1. **GET 目标**：真发 GET，`Content-Type` 必须是 `application/json`。
   打到页面上就是 HTML，当场红——源码里 `/heart` 与 `/heart/overview` 长得一样像，只有真打才知道。
2. **非 GET 目标**：必须在路由表里注册为那个方法；再用一个不属于它的方法打过去，
   必须回 405 + JSON（证明它是 JSON 端点，不是页面）。**不拿真方法打，避免写盘。**
3. **任何方法**：不许把页面地址当 GET 接口打（页面地址见 `PAGE_FILES`）。
4. 前缀只许有一份推导（`panel-base.js`），不许页面自己写 `var MOUNT` / `var BASE`，
   也不许裸 `fetch("/…")`。
"""
from __future__ import annotations

import re
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from typing import List, Tuple

from api import devserver
from api.api import ROUTES
from api.pages import PAGE_FILES
from api.tests.helpers import copy_fixture
from organs.soul import load

PAGES = Path(__file__).resolve().parents[2] / "pages"

#: 页面里所有会发请求的写法 → (方法, 路径)。panelFetch 一律当 GET。
TARGET_RE = re.compile(
    r'panelFetch\(\s*["\']([^"\']+)["\']'
    r'|call\(\s*"(GET|POST|PUT|PATCH|DELETE)"\s*,\s*["\']([^"\']+)["\']'
    r'|fetch\(\s*["\']([^"\']+)["\']'
)
#: 每页至少扫出这么多条，否则说明正则没匹配上（会变成「假绿」）
MIN_TARGETS = {"soul.html": 5, "heart.html": 8, "brain.html": 6,
               "home.html": 9, "chat.html": 1, "panel.html": 0,
               "day.html": 0, "settings.html": 0}
#: 拿它们去探非 GET 端点，只为「证明是 JSON 端点」，不会真写
PROBE_METHOD = {"POST": "DELETE", "PUT": "DELETE", "PATCH": "DELETE", "DELETE": "PATCH"}


def _targets(page: str) -> List[Tuple[str, str]]:
    text = (PAGES / page).read_text(encoding="utf-8")
    found = set()
    for bare, method, call_path, fetch_path in TARGET_RE.findall(text):
        raw = bare or call_path or fetch_path
        path = raw.partition("?")[0]
        if not path.startswith("/"):
            continue                      # 相对地址交给浏览器解析，前缀由共享片段管
        found.add(((method or "GET"), path))
    return sorted(found)


class PagesFetchOnlyJson(unittest.TestCase):
    """真起服务器：每页每个取数目标都要回 JSON，且不能把页面当接口。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        store = load(copy_fixture("valid", cls._tmp.name))
        handler = type("_PageFetchHandler", (devserver._Handler,),
                       {"store": store, "log_message": lambda *args: None})
        try:
            cls.server = HTTPServer(("127.0.0.1", 0), handler)
        except OSError as exc:                 # 本机不让绑端口：跳过，不假绿
            cls._tmp.cleanup()
            raise unittest.SkipTest("起不了本地端口：%s" % exc)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls._tmp.cleanup()

    def _request(self, method: str, path: str) -> Tuple[int, str]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path)
            res = conn.getresponse()
            res.read()
            return res.status, (res.getheader("Content-Type") or "")
        finally:
            conn.close()

    def test_every_page_yields_targets(self):
        """先保证扫描本身有效：扫不出东西的页面等于没被看住。"""
        for page, minimum in MIN_TARGETS.items():
            targets = _targets(page)
            self.assertGreaterEqual(len(targets), minimum,
                                    "%s 只扫出 %d 条取数目标（至少要 %d 条）"
                                    % (page, len(targets), minimum))

    def test_get_targets_answer_json(self):
        for page in MIN_TARGETS:
            for method, path in _targets(page):
                if method != "GET":
                    continue
                status, ctype = self._request("GET", path)
                self.assertIn("application/json", ctype,
                              "%s 用 GET 打了 %s，回来的不是 JSON（HTTP %s / %s）"
                              "——打到页面上了？页面的 JSON 一般在它下面一层（如 /heart/overview）"
                              % (page, path, status, ctype or "无 Content-Type"))

    def test_write_targets_are_registered_and_json(self):
        for page in MIN_TARGETS:
            for method, path in _targets(page):
                if method == "GET":
                    continue
                self.assertEqual(method, ROUTES.get(path),
                                 "%s 写了 %s %s，可路由表里没有这条" % (page, method, path))
                probe = PROBE_METHOD.get(method, "DELETE")
                status, ctype = self._request(probe, path)
                self.assertEqual(405, status,
                                 "%s 的 %s %s 该只收 %s，用 %s 打却回了 %s"
                                 % (page, method, path, method, probe, status))
                self.assertIn("application/json", ctype,
                              "%s 的 %s %s 用 %s 打回来的不是 JSON（是页面？）"
                              % (page, method, path, probe))

    def test_get_targets_are_never_page_addresses(self):
        addresses = set(PAGE_FILES)
        for page in MIN_TARGETS:
            for method, path in _targets(page):
                if method != "GET":
                    continue            # /chat 这类：GET 是页面、POST 才是端点，不是错
                self.assertNotIn(path, addresses,
                                 "%s 把页面地址 %s 当成接口在打" % (page, path))


class EveryPageUsesTheSharedPrefix(unittest.TestCase):
    """前缀只许有一份推导：`panel-base.js`。谁再各写一遍，这里红。"""

    def test_pages_do_not_hand_roll_the_prefix(self):
        for page in MIN_TARGETS:
            text = (PAGES / page).read_text(encoding="utf-8")
            self.assertNotIn("var MOUNT", text, "%s 自己推导了挂载前缀" % page)
            self.assertNotIn("var BASE", text, "%s 自己推导了挂载前缀" % page)

    def test_every_page_with_requests_loads_the_shared_fragment(self):
        for page, minimum in MIN_TARGETS.items():
            if minimum == 0:
                continue
            text = (PAGES / page).read_text(encoding="utf-8")
            self.assertRegex(text, r'<script src="panel-base\.js" data-page="[a-z]+"',
                             "%s 没有用相对地址引入 panel-base.js" % page)

    def test_no_hardcoded_absolute_fetch(self):
        for page in MIN_TARGETS:
            text = (PAGES / page).read_text(encoding="utf-8")
            self.assertNotRegex(text, r'fetch\(\s*"/',
                                "%s 里还有裸 fetch(\"/…\")：那会打到宿主根" % page)


if __name__ == "__main__":
    unittest.main()
