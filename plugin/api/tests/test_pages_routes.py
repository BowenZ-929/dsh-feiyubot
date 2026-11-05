"""面板判据：页面上每个可点元素都必须对着一条真实路由；心与脑没有任何写入口。

这条判据来自参考实现的事故：按钮打 `POST api/pin`，服务端根本没有这条路由——
UI 承诺了不存在的能力，刷新即失效。

被看住的三件事：
1. pages/soul.html 里每一次写请求（call/fetch 的 METHOD + 路径）都必须在 api.ROUTES 里，
   且方法对得上（POST 端点不能被页面当 GET 用，反之亦然）；
2. 页面上每个静态按钮都必须有 JS 绑定（没有绑定的按钮点了什么都不发生 = 死按钮）；
3. pages/heart.html 的可点元素全部列出并逐一断言：只有一条回跳链接，没有任何按钮、
   输入框、表单，也没有任何写动词——「人不能改心」不只是口头承诺。（脑页另算：
   它有一条写（接受提案，写魂不写脑），判据在 tests/test_pages_brain.py。）

**这是静态回归测试，不是审计。** 它比对的是源码里的页面与路由表，防的是以后有人改了页面
却不改路由（或反过来）。审计要打「当前正在跑的进程」（CONTRIBUTING §1.3）——本项目踩过：
源码 `ROUTES` 里有四条路由，跑着的旧进程全回 404，照那个结果动手会误删四个好按钮。
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
# 心页只许拉自己这十条：页面自检用（顺序无所谓，集合相等就够）
HEART_ROUTES = ("/heart", "/heart/overview", "/heart/baseline", "/heart/mood",
                "/heart/energy", "/heart/offset", "/heart/engage", "/heart/effective",
                "/heart/pending", "/heart/sleep", "/heart/refusals")


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


class HeartHasNoWriteEntry(unittest.TestCase):
    """「人不能改心」的机器证据：把心页的可点元素全部列出来，一个一个过。

    脑不再跟心同一条断言：脑页要「接受提案」（POST /soul/proposal，写的是魂不是脑），
    判据因此从「一个按钮都没有」换成 test_pages_brain.py 那三条——写只有一条、其余全 GET
    且都登记在路由表里、没有任何改熟稔/改记忆的控件。
    """

    def test_clickable_elements_are_only_the_back_link(self):
        for page, mount in (("heart.html", "/heart"),):
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


class HeartPageOnlySpeaksGet(unittest.TestCase):
    """心页只读的机器证据（本轮补齐）：它要拉数据，所以光「没有写控件」不够。

    四件事一起看住：
    1. 页面上每一次 fetch 的路径都在 api.ROUTES 里，且那条路由就是 GET；
    2. 画的是 mood 的轨迹：<canvas> + trajectory + fetch("/heart/mood") 三样都在；
    3. 没有任何非 GET 的写法（连 "method" 这种第二参数都不许出现），也不挂定时器；
    4. 念头块、窗口块、睡眠账、意图排序真的在页上，且各自读的是自己那一块的键——
      只断言"没有写控件"的话，把这几块的取数删掉也照样是绿的。
    """

    def setUp(self) -> None:
        self.text = _text("heart.html")

    def test_every_fetch_is_a_registered_get_route(self):
        paths = FETCH_RE.findall(self.text)
        self.assertGreaterEqual(len(paths), 3, "心页该从端点拉读数，少了说明解析没抓到")
        for path in paths:
            route = path.partition("?")[0]
            self.assertIn(route, api.ROUTES, "心页在 fetch 一条不存在的路由：%s" % route)
            self.assertEqual("GET", api.ROUTES[route],
                             "心页只能 GET：%s 在路由表里是 %s" % (route, api.ROUTES[route]))
            self.assertIn(route, HEART_ROUTES, "心页只许拉心自己的端点：%s" % route)

    def test_mood_trajectory_is_drawn_on_a_canvas(self):
        self.assertIn("<canvas", self.text, "轨迹要自绘：一张 <canvas>，不引外部库")
        self.assertIn("trajectory", self.text, "画的是 mood 的轨迹，源码里得看得见 trajectory")
        self.assertIn('fetch("/heart/mood"', self.text, "轨迹数据来自 GET /heart/mood")
        self.assertIn('getContext("2d")', self.text, "曲线是页内自绘的 2d 折线")

    def test_no_non_get_request_words_and_no_timers(self):
        for needle in ("method", '"POST"', '"PUT"', '"DELETE"', '"PATCH"',
                       "XMLHttpRequest", "sendBeacon"):
            self.assertNotIn(needle, self.text, "心页出现了非 GET 写法：%s" % needle)
        for needle in ("setTimeout", "setInterval"):
            self.assertNotIn(needle, self.text, "心页不挂定时器：%s" % needle)

    def test_pending_and_window_blocks_read_their_own_keys(self):
        """念头（/heart/pending）与近期窗口（判决带的 window）各读各的键。"""
        for needle, note in (('fetch("/heart/pending"', "念头块来自 GET /heart/pending"),
                             ("thoughts", "念头列表读 pending 的 thoughts"),
                             ("ttl_left_min", "念头要显示剩余 TTL"),
                             ("此刻没有未了的念头", "没念头时给一句话，不显示空白"),
                             ("ignored-recently", "窗口命中时的理由码要写在页上"),
                             ("signal.weight", "窗口块显示 weight"),
                             ("signal.consecutive_ignored", "窗口块显示 consecutive_ignored"),
                             ("signal.rows", "窗口块显示窗内事件条数")):
            self.assertTrue(needle in self.text, "%s：页面上找不到 %r" % (note, needle))

    def test_sleep_and_intent_blocks_read_their_own_keys(self):
        """睡眠账（/heart/sleep）与意图排序（/heart 的 intents）各读各的键。

        只断言"没有写控件"的话，把这两块的取数删掉也照样是绿的——所以逐键点名：
        睡眠账七样（上次入睡 / 睡了多久 / 两个连续计数 / drift / 生效窗口 / wake_cap）、
        意图排序六样（order 的每条的 intent·weight·hit·why，加 top / pass / threshold）。
        """
        for needle, note in (('fetch("/heart/sleep"', "睡眠账来自 GET /heart/sleep"),
                             ("data.effective_window", "睡眠块显示生效窗口"),
                             ("data.wake_cap", "睡眠块显示 wake_cap"),
                             ("data.drift_hours", "睡眠块显示 drift"),
                             ("data.consecutive_late_nights", "睡眠块显示连续熬夜夜数"),
                             ("data.consecutive_short_nights", "睡眠块显示连续没睡够夜数"),
                             ("data.last_asleep_at", "睡眠块显示上次入睡"),
                             ("data.last_duration_min", "睡眠块显示睡了多久"),
                             ("data.sleep_window", "/heart/energy 那一行显示这次用的窗口"),
                             ("renderIntents", "意图排序有自己的渲染"),
                             ("block.order", "排序块读 order"),
                             ("row.intent", "order 的每一行显示 intent"),
                             ("row.weight", "order 的每一行显示 weight"),
                             ("row.hit", "order 的每一行显示 hit"),
                             ("row.why", "order 的每一行显示 why"),
                             ("block.top", "排序块显示 top"),
                             ("block.pass", "排序块显示 pass"),
                             ("block.threshold", "排序块显示阈值"),
                             ("清醒度系数", "排序块显示清醒度系数")):
            self.assertTrue(needle in self.text, "%s：页面上找不到 %r" % (note, needle))

    def test_refusals_block_reads_its_own_keys(self):
        """拒绝记录（GET /heart/refusals 的额度 + /heart 的 refusals 块）各读各的键。

        只断言"没有写控件"的话，把这一块的取数删掉也照样是绿的——所以逐键点名：
        额度三样（用了几次 / 上限 / 还剩几次）、冷却中的话题四样（话题 / 剩余分钟 /
        第几次 / 理由），以及空着时那一句话。
        """
        for needle, note in (('fetch("/heart/refusals"', "额度来自 GET /heart/refusals"),
                             ("data.quota", "/heart/refusals 的额度块"),
                             ("refusalQuota.used_today", "额度显示今天用了几次"),
                             ("refusalQuota.limit", "额度显示上限"),
                             ("refusalQuota.left", "额度显示还剩几次"),
                             ("renderRefusalCooling(data.refusals)",
                              "冷却中的话题来自 /heart 的 refusals 块"),
                             ("row.cooling", "只渲染冷却中的话题"),
                             ("row.topic", "冷却中的话题要写出话题名"),
                             ("row.remaining_min", "冷却中的话题要写剩余分钟"),
                             ("row.count", "冷却中的话题要写拒过第几次"),
                             ("row.reason", "冷却中的话题要写理由"),
                             ("此刻没有在冷却的话题", "空时给一句话，不显示空白")):
            self.assertTrue(needle in self.text, "%s：页面上找不到 %r" % (note, needle))

    def test_axes_and_voice_come_from_the_effective_route(self):
        """六轴档位与语气档都从合成点读：页面自己不做第二套换算。"""
        self.assertIn('fetch("/heart/effective"', self.text,
                      "档位与语气档都来自 /heart/effective")
        self.assertIn("data.axes", self.text, "六轴档位用 effective 的 axes")
        self.assertIn("item.inner_word", self.text, "轴档读 effective 给的档位词")
        self.assertIn("item.greyed", self.text, "待定轴按 greyed 标灰，不写数")
        self.assertIn("voice.tone", self.text, "语气档显示 voice.tone")
        self.assertIn("voice.direction", self.text, "语气档显示 voice.direction")
        self.assertIn("voice.bans", self.text, "禁令显示 voice.bans")
        self.assertIn(': "无"', self.text, "没有禁令时要写「无」")
        self.assertIn("语气档", self.text, "面板上要有一块语气档")
        self.assertNotIn("tier_of", self.text, "页面不许自己换算档位")

    def test_heart_page_says_it_is_readonly(self):
        self.assertIn("只读：这一页没有任何能改她的控件", self.text)
        self.assertIn("只读", self.text)
        for forbidden in ("<button", "<form", "<textarea", "<select", "<input"):
            self.assertNotIn(forbidden, self.text.lower(), "心页出现了写控件：%s" % forbidden)


if __name__ == "__main__":
    unittest.main()
