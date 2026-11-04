"""面板与端点：读、改待人倾向、接受提案、迁移、注入面、挂载契约、静态页。

验收口径（都被下面的断言看住）：
- 七个魂端点各有一条成功路径；提案端点成功与被拒（只两条依据、改红线）都有；
- 面板上心与脑只有只读入口：GET /heart 返回「未接入」，GET /brain 返回「已接入」，
  心与脑的路径上 POST/PUT/DELETE 一律 405（入口存在，但没有写），
  pages/heart.html 无任何写控件；pages/brain.html 的写只有一条（接受提案 → 写魂不写脑），
  pages/soul.html 里心与脑只以两条链接 + 只读说明 + 只读提案框出现；
- 只读端点无副作用：GET 前后五个数据文件字节不变；
- 面板改完这里就变：POST /soul/edit 改 tone → GET /soul/render 立刻是新句子（同一进程，不重启）。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from api import api, core, day as day_api, devserver, panel
from api.tests.helpers import copy_fixture, read_bytes
from organs.brain import BrainStore
from organs.soul import load

PLUGIN = Path(__file__).resolve().parents[2]
PAGE = PLUGIN / "pages" / "soul.html"
BLOCK_FILES = ("identity.json", "manner.json", "bounds.json", "history.jsonl")
AUDIENCE = "熟人"
PAGES = PLUGIN / "pages"
# 心的数据目录：跑完一轮测试，这里只许剩这三个文件。mood.read / energy.read 是
# "读时算、顺手落一笔"的设计，谁拿默认根跑一次就会往这儿丢运行时文件——守卫见
# PanelContractTest.test_zz_heart_data_dir_stays_clean。
HEART_DATA = PLUGIN / "organs" / "heart" / "data"
ALLOWED_HEART_DATA = {".gitignore", ".gitkeep", "baseline-card.json"}
BRAIN_ROUTES = ("/brain", "/brain/overview", "/brain/today", "/brain/people",
                "/brain/recall", "/brain/selfnarration", "/brain/proposals")
# 关键端点：方法 + 路径。条数由 api.ROUTES 自己算（见 RouteRegistryTest），这里只钉"必须在"。
REQUIRED_ROUTES = (
    ("GET", "/soul"), ("GET", "/soul/check"), ("GET", "/soul/history"),
    ("GET", "/soul/pin"), ("GET", "/soul/revisions"),
    ("GET", "/soul/render"), ("POST", "/soul/edit"), ("POST", "/soul/migrate"),
    ("POST", "/soul/proposal"), ("POST", "/soul/redline"), ("POST", "/soul/rollback"),
    ("POST", "/soul/acceptance"),
    # 口径变更（2026-11-05 心线落地）：心从占位页变成七条只读端点，全在这一张表里。
    ("GET", "/heart"), ("GET", "/heart/overview"), ("GET", "/heart/baseline"),
    ("GET", "/heart/mood"), ("GET", "/heart/energy"), ("GET", "/heart/offset"),
    ("GET", "/heart/engage"),
    # 2026-11-05 第二轮：/heart/effective 加入关键端点表
    ("GET", "/heart/effective"),
    # 第三轮念头层：/heart/pending 与心跳同一条只读口径（谁加路由谁登记）
    ("GET", "/heart/pending"),
    ("GET", "/brain"), ("GET", "/brain/overview"), ("GET", "/brain/today"),
    ("GET", "/brain/people"), ("GET", "/brain/recall"),
    ("GET", "/brain/selfnarration"), ("GET", "/brain/proposals"),
    ("GET", "/mouth/split"), ("GET", "/mouth/address"),
    ("GET", "/mouth/tone"), ("GET", "/mouth/gate"),
    ("GET", "/home/rooms"), ("GET", "/home/access"), ("GET", "/home/list"),
    ("GET", "/home/read"), ("GET", "/home/audit"), ("GET", "/home/audit/verify"),
    ("GET", "/home/drawer"), ("GET", "/home/archive/borrow"), ("GET", "/home/warehouse"),
    ("POST", "/home/write"),
    # 核自报身份（2026-11-05）：跑的是哪一版、什么时候起的、源码有没有在它脚下被改过。
    ("GET", "/healthz"),
    # 脑线新落的两条只读端点（2026-11-05，四档列表与冷仓分页）：先在这儿认下来，
    # 脑线自己的断言到了再各自归位。
    ("GET", "/brain/tiers"), ("GET", "/brain/cold"),
    # 日子线第一波（2026-11-05）：十条 = 四条只读 + 六条手工触发。
    # 写着"手工触发"的那几条不是只读页：tick 排当天、night 跑四步、deliver 入队、ack 记回执。
    ("POST", "/day/tick"), ("GET", "/day/plan"), ("POST", "/day/night"),
    ("GET", "/day/log"), ("GET", "/day/schedule"), ("POST", "/day/schedule/draw"),
    ("GET", "/day/pending"), ("POST", "/day/deliver"), ("POST", "/day/ack"),
    ("POST", "/day/reconcile"),
)
# 有必填查询参数的 GET：空 query 时必须是 400（不是 200，也不许崩）——缺哪个参数就得在 message 里点名。
# 以后加带必填参数的端点，放进这一组；别把它学成 skip（skip 掉就没人守「缺参要响」了）。
REQUIRED_PARAM_ROUTES = {"/mouth/split": "text", "/mouth/gate": "text",
                         "/home/list": "room", "/home/read": "room"}


def _proposal(target: str = "manner.stance", value: Any = "提案定的基调",
              evidence: Optional[list] = None) -> Dict[str, Any]:
    return {
        "id": "p-2024-01",
        "target": target,
        "value": value,
        "evidence": ["m-1", "m-2", "m-3"] if evidence is None else evidence,
        "reason": "三条记忆都指同一个方向",
    }


def _brain_store(tmp: str) -> BrainStore:
    return BrainStore(Path(tmp) / "brain")


class PanelApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)
        self.brain = _brain_store(self._tmp.name)

    def _call(self, method: str, path: str,
              body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        status, payload = api.handle(method, path, body, self.store, brain=self.brain)
        json.dumps(payload, ensure_ascii=False)  # 序列化不抛就是合格返回值
        return status, payload

    def _bytes(self) -> Dict[str, bytes]:
        return {name: read_bytes(self.root / name) for name in BLOCK_FILES}

    # —— 七个魂端点各一条成功路径 ——————————————————————————————
    def test_get_soul_ok(self):
        status, payload = self._call("GET", "/soul")
        self.assertEqual(200, status)
        self.assertIn("identity", payload)

    def test_get_soul_check_ok(self):
        status, payload = self._call("GET", "/soul/check")
        self.assertEqual(200, status)
        self.assertIsInstance(payload["issues"], list)

    def test_get_soul_history_ok(self):
        status, payload = self._call("GET", "/soul/history")
        self.assertEqual(200, status)
        self.assertEqual([], payload["history"])

    def test_get_soul_render_ok(self):
        status, payload = self._call("GET", "/soul/render?audience=" + AUDIENCE)
        self.assertEqual(200, status)
        self.assertEqual(AUDIENCE, payload["audience"])      # 参数照回显，只是不再分档
        self.assertIn("【自称】", payload["text"])
        self.assertIn("【待人姿态】", payload["text"])
        chars = payload["chars"]
        # 一件一套口径：六个 parts 一律四个键，不再区分 suggested/limit 两套键名
        for part in chars["parts"]:
            self.assertEqual({"name", "chars", "limit", "hard"}, set(part), part["name"])
        self.assertEqual(["名字与自称", "形象", "三句自述", "怕什么", "待人姿态", "红线条目"],
                         [part["name"] for part in chars["parts"]])   # 顺序即渲染顺序
        self.assertEqual(6, len(chars["parts"]))
        self.assertEqual(sum(part["chars"] for part in chars["parts"]), chars["used"])
        self.assertEqual(1000, chars["limit"])          # 合计是硬线
        hard = [part for part in chars["parts"] if part["hard"]]
        self.assertEqual(["形象", "怕什么"], [part["name"] for part in hard])  # 两件硬线
        self.assertEqual([60, 60], [part["limit"] for part in hard])
        soft = [part for part in chars["parts"] if not part["hard"]]
        self.assertEqual(4, len(soft), "其余四件的 limit 只是建议值")
        self.assertTrue(all(part["limit"] < chars["limit"] for part in soft))

    def test_post_soul_edit_ok(self):
        status, payload = self._call("POST", "/soul/edit", {
            "actor": "panel", "reason": "面板改基调", "block": "manner",
            "path": "stance", "value": "慢慢说，先听完",
        })
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, len(self.store.history()))

    def test_post_soul_proposal_ok(self):
        status, payload = self._call("POST", "/soul/proposal",
                                     {"actor": "panel", "proposal": _proposal()})
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        line = payload["history_line"]
        self.assertEqual(["m-1", "m-2", "m-3"], line["evidence"])
        self.assertEqual("p-2024-01", line["proposal_id"])
        self.assertEqual(1, len(self.store.history()))
        self.assertEqual("提案定的基调", self.store.snapshot().manner.stance)

    # —— 提案被拒 ——————————————————————————————————————
    def test_proposal_with_two_evidences_rejected(self):
        before = self._bytes()
        status, payload = self._call("POST", "/soul/proposal",
                                     {"actor": "panel",
                                      "proposal": _proposal(evidence=["m-1", "m-2"])})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("3 条依据记忆", payload["message"])
        self.assertEqual(before, self._bytes())

    def test_proposal_on_redline_rejected(self):
        before = self._bytes()
        status, payload = self._call("POST", "/soul/proposal", {
            "actor": "panel",
            "proposal": _proposal(target="bounds.items[0].rule", value="软化后的红线"),
        })
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("红线", payload["message"])
        self.assertEqual(before, self._bytes())

    def test_proposal_over_monthly_limit_rejected(self):
        status, payload = self._call("POST", "/soul/proposal", {
            "actor": "panel", "proposal": _proposal(), "monthly_limit": 0,
        })
        self.assertEqual(400, status)
        self.assertIn("上限", payload["message"])

    def test_proposal_body_missing_and_bad_limit(self):
        self.assertEqual(400, self._call("POST", "/soul/proposal", {"actor": "panel"})[0])
        self.assertEqual(400, self._call("POST", "/soul/proposal", None)[0])
        status, payload = self._call("POST", "/soul/proposal", {
            "actor": "panel", "proposal": _proposal(), "monthly_limit": "两",
        })
        self.assertEqual(400, status)
        self.assertIn("monthly_limit", payload["message"])

    # —— 只读端点无副作用 ——————————————————————————————————
    def test_read_endpoints_write_nothing(self):
        before = self._bytes()
        for path in ("/soul", "/soul/check", "/soul/history",
                     "/soul/render?audience=" + AUDIENCE):
            self.assertEqual(200, self._call("GET", path)[0], path)
        self.assertEqual(before, self._bytes())

    # —— 改完下一回合生效（同一进程，不重启） ————————————————
    def test_audience_no_longer_changes_anything(self):
        """矩阵归脑后对象档不再是魂的概念：熟人/生人/群/长辈注出来的文本一字不差。"""
        texts = {}
        for audience in ("熟人", "生人", "群", "长辈"):
            status, payload = self._call("GET", "/soul/render?audience=" + audience)
            self.assertEqual(200, status, audience)
            texts[audience] = payload["text"]
        self.assertEqual(1, len(set(texts.values())), texts)

    def test_edit_tone_changes_render_immediately(self):
        before = self._call("GET", "/soul/render?audience=" + AUDIENCE)[1]["text"]
        status, _ = self._call("POST", "/soul/edit", {
            "actor": "panel", "reason": "面板改基调", "block": "manner",
            "path": "stance", "value": "慢慢说，先听完",
        })
        self.assertEqual(200, status)
        after = self._call("GET", "/soul/render?audience=" + AUDIENCE)[1]["text"]
        self.assertIn("慢慢说，先听完", after)
        self.assertNotIn("慢慢说，先听完", before)
        self.assertEqual(1, len(self.store.history()))

    # —— 心与脑：入口得在，写方法一律 405 —————————————————————
    def test_heart_and_brain_are_readonly_pages(self):
        # 口径变更（2026-11-05 心线落地）：/heart 不再是占位页，返回七条真读数；
        # 只读语义由 HEART_ROUTES 承担。这里只钉「只读 + 心在这一页 + 六块读数都在」，
        # 每块的形状与取值由 api/tests/test_heart_api.py 逐个端点钉。
        # 心读的是自己的数据根：一律指向临时目录（root= 只收绝对路径且存在的目录）。
        heart_root = Path(self._tmp.name) / "heart"
        heart_root.mkdir()
        card = PLUGIN / "organs" / "heart" / "data" / "baseline-card.json"
        (heart_root / card.name).write_bytes(card.read_bytes())
        status, payload = api.handle("GET", "/heart", None, self.store,
                                     query={"root": str(heart_root)}, brain=self.brain)
        self.assertEqual(200, status)
        self.assertIn(payload["status"], ("已接入", "拒载"), payload)
        self.assertTrue(payload["readonly"])
        self.assertEqual("heart", payload["agent"])
        for key in ("baseline", "render", "mood", "energy", "offset", "pending"):
            self.assertIn(key, payload, "/heart 少了读数：%s" % key)
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(405, self._call(method, "/heart", {})[0], "%s /heart" % method)

        status, payload = self._call("GET", "/brain")
        self.assertEqual(200, status)
        self.assertEqual("已接入", payload["status"])
        self.assertTrue(payload["readonly"])
        self.assertEqual("brain", payload["agent"])

        for path in BRAIN_ROUTES:
            for method in ("POST", "PUT", "DELETE"):
                self.assertEqual(405, self._call(method, path, {})[0],
                                 "%s %s" % (method, path))
        self.assertEqual(404, self._call("GET", "/heart/edit")[0])
        self.assertEqual(404, self._call("POST", "/brain/x")[0])
        self.assertEqual(405, self._call("GET", "/soul/proposal")[0])
        self.assertEqual(405, self._call("POST", "/soul/render")[0])

    def test_brain_readonly_endpoints_answer(self):
        self.assertEqual(200, self._call("GET", "/brain/overview")[0])
        self.assertEqual(200, self._call("GET", "/brain/today")[0])
        self.assertEqual(200, self._call("GET", "/brain/people")[0])
        self.assertEqual(200, self._call("GET", "/brain/proposals")[0])
        self.assertEqual(200, self._call("GET", "/brain/selfnarration")[0])
        status, payload = self._call("GET", "/brain/recall?q=以前说过什么")
        self.assertEqual(200, status)
        self.assertIn("items", payload)
        self.assertEqual(400, self._call("GET", "/brain/recall")[0])

    def test_percent_encoded_audience_is_decoded(self):
        status, payload = self._call("GET", "/soul/render?audience=%E7%86%9F%E4%BA%BA")
        self.assertEqual(200, status)
        self.assertEqual(AUDIENCE, payload["audience"])


class PanelSourceTest(unittest.TestCase):
    """面板上没有改心、改脑的入口——用源码断言看住，不靠人记。"""

    def test_heart_and_brain_routes_are_get_only(self):
        # 口径变更（2026-11-05 心线落地）：READONLY_PAGES 已随「心不再是占位页」删除，
        # 只读语义改由 HEART_ROUTES 承担——七条路由全 GET，且没有第二条登记口。
        self.assertEqual(set(core.heart_api.HEART_ROUTES),
                         {path for path in api.ROUTES if path.startswith("/heart")})
        self.assertEqual({"GET"}, {api.ROUTES[path] for path in core.heart_api.HEART_ROUTES})
        self.assertEqual({}, core.page_api.PAGE_ROUTES, "占位页表已空：心不再是占位页")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        mounted = {(method, path) for method, path, _ in panel.routes(
            load(copy_fixture("valid", temp.name)), _brain_store(temp.name))}
        self.assertIn(("GET", "/heart"), mounted)
        self.assertIn(("GET", "/brain"), mounted)
        write_routes = [item for item in mounted
                        if (item[1] == "/heart" or item[1].startswith("/brain"))
                        and item[0] != "GET"]
        self.assertEqual([], write_routes, "心与脑只读：不许挂任何写方法")

    def test_route_table_partitions_into_soul_brain_and_readonly_pages(self):
        """按前缀分组断言，不写死条数：别处加端点这里不该红，写错归属才该红。

        口径变更（2026-11-05 心线落地）：心从「占位页」变成六张器官表里的一张，四组变五组——
        魂（/soul*）、脑（/brain*）、家（/home*）、心（/heart*）、其余（嘴的干跑端点）。
        READONLY_PAGES 已删，所以「其余」只剩 MOUTH_ROUTES；五组两两不相交、并集就是整张
        ROUTES——每条路由都要有明确归属，不许有一条落在分类之外。

        口径变更（2026-11-05 日子线第一波）：多第六组「日子」（/day*，DAY_ROUTES 那一张表），
        「其余」仍只有嘴的干跑端点 + 核自报身份——日子不许混进 rest，混进去就等于没归属。
        """
        soul = {path for path in api.ROUTES if path.startswith("/soul")}
        brain = {path for path in api.ROUTES if path.startswith("/brain")}
        family = {path for path in api.ROUTES if path.startswith("/home")}
        heart = {path for path in api.ROUTES if path.startswith("/heart")}
        day = {path for path in api.ROUTES if path.startswith("/day")}
        rest = set(api.ROUTES) - soul - brain - family - heart - day
        self.assertEqual(set(core.home_api.HOME_ROUTES), family,
                         "家的路由就是 HOME_ROUTES 那一张表")
        self.assertEqual({"/home/write"},
                         {path for path in family if api.ROUTES[path] == "POST"},
                         "家只有一条写路由：POST /home/write")
        self.assertEqual({"GET"},
                         {api.ROUTES[path] for path in family} - {"POST"},
                         "家其余九条只读")
        mouth = set(core.mouth_api.MOUTH_ROUTES)
        version_set = set(core.version_api.VERSION_ROUTES)
        self.assertEqual(set(core.heart_api.HEART_ROUTES), heart,
                         "心的路由就是 HEART_ROUTES 那一张表")
        self.assertEqual({"GET"}, {api.ROUTES[path] for path in heart}, "心只有只读端点")
        self.assertEqual(set(core.day_api.DAY_ROUTES), day,
                         "日子的路由就是 DAY_ROUTES 那一张表")
        self.assertEqual({"GET", "POST"}, {api.ROUTES[path] for path in day},
                         "日子只有四条只读 + 六条手工触发（写方法错了一律 405）")
        self.assertEqual(mouth | version_set, rest,
                         "魂、脑、家、心之外 = 嘴的干跑端点 + 核自报身份 %s"
                         % (sorted(mouth | version_set),))
        self.assertEqual({"GET"}, {api.ROUTES[path] for path in brain}, "脑只有只读端点")
        # 写入口六条：改值 / 换形状 / 提案 / 红线逐条加撤 / 回滚 / 验收探针。
        # 前五条是「改她的魂」；探针是「写两次再写回」的验收路径，edit 上的 probe 键已关掉。
        # 整块替换仍然没有入口（edit 动 items 整表、migrate 碰 bounds 都一律拒）。
        self.assertEqual({"/soul/edit", "/soul/migrate", "/soul/proposal",
                          "/soul/redline", "/soul/rollback", "/soul/acceptance"},
                         {path for path in soul if api.ROUTES[path] == "POST"},
                         "魂的写入口：改值 / 换形状 / 提案 / 红线逐条 / 回滚 / 验收探针")
        self.assertEqual({"GET"}, {api.ROUTES[path] for path in rest},
                         "嘴的干跑端点都只有 GET")
        # 五组必须真的分区：两两不相交，并集就是整张表——没有端点落在分类之外
        self.assertEqual(set(), soul & brain, "魂与脑有重叠")
        self.assertEqual(set(), soul & heart, "魂与心有重叠")
        self.assertEqual(set(), soul & rest, "魂与嘴有重叠")
        self.assertEqual(set(), brain & rest, "脑与嘴有重叠")
        self.assertEqual(set(), heart & rest, "心与嘴有重叠")
        self.assertEqual(set(), brain & heart, "脑与心有重叠")
        self.assertEqual(set(), family & (soul | brain | heart | rest), "家与别的组有重叠")
        self.assertEqual(set(), day & (soul | brain | family | heart | rest), "日子与别的组有重叠")
        self.assertEqual(set(api.ROUTES), soul | brain | family | heart | day | rest,
                         "路由表分不成六组：有端点没归属")
        self.assertEqual(set(REQUIRED_ROUTES),
                         {(api.ROUTES[path], path) for path in api.ROUTES},
                         "路由表就是那张关键端点表：多了少了都得在这儿说清楚")

    def test_panel_page_write_controls_are_the_named_ones(self):
        """面板的写控件逐个点名，不许多出来：本轮从两处扩到四处，各对应一条机制。

        原口径「只有两个写控件」是吸收前的世界：红线段与整档回滚都还没有入口。
        本轮加上 ① 红线逐条加（M3：整块替换没有入口，只有单条表单）与 ② 回滚（M5）。
        撤线按钮由 JS 按条生成（不在源码里数），所以 <button> 仍是四个固定提交钮。
        """
        text = PAGE.read_text(encoding="utf-8")
        self.assertEqual(1, text.count("改心"))
        self.assertEqual(1, text.count("改脑"))
        note = [line for line in text.splitlines() if "改心" in line or "改脑" in line]
        self.assertEqual(1, len(note), "改心/改脑 只许出现在那一行只读说明里")
        self.assertIn("只能看", note[0])
        self.assertIn("127.0.0.1", text)
        # 三个文本框：待人姿态、提案 JSON、新红线条目的禁令正文
        self.assertEqual(3, text.count("<textarea"), "可编辑的只有这三处")
        # 四个固定提交钮：姿态 / 提案 / 加一条红线 / 回滚；撤线按钮按条生成
        self.assertEqual(4, text.count("<button"), "只有四个固定提交按钮")
        self.assertNotIn("<form", text)
        # 五个写请求、四个端点：/soul/edit、/soul/proposal、/soul/rollback、
        # /soul/redline 两次（逐条加、逐条撤各一次——红线没有「整块写」的那一次）
        self.assertEqual(5, text.count('call("POST"'), "全页只有五个写请求")
        self.assertNotIn("/soul/migrate", text, "换形状是坏卡状态的路，不给面板入口")
        self.assertIn("/soul/render", text, "面板要显示她会被注入的样子")
        self.assertIn("/soul/revisions", text, "回滚要能列出可回到的版本")

    def test_panel_links_to_the_two_readonly_pages(self):
        """心与脑只许以只读方式出现：两条链接 + 只读说明 + 只读提案框，没有任何写口。"""
        text = PAGE.read_text(encoding="utf-8")
        self.assertEqual(2, text.count("<a "), "顶部的只读页链接：心、脑各一条")
        # 链接是相对的：壳把本页挂在 /qqagent/panel 下，"/heart" 会折成壳的 JSON 端点；
        # 页面自己按当前前缀补回面板路径（见页内那段 script）。
        self.assertIn('href="heart"', text)
        self.assertIn('href="brain"', text)
        for word in ("heart", "brain"):
            hits = [line for line in text.splitlines() if word in line.lower()]
            self.assertGreater(len(hits), 0, "%s 至少出现在链接行上" % word)
            for line in hits:
                lowered = line.lower()
                for needle in ("<button", "<form", "<textarea", "<input",
                               'call("post"', 'call("put"', 'call("delete"'):
                    self.assertNotIn(needle, lowered,
                                     "%s 出现的那行不能有写口：%s" % (word, line))

    def test_readonly_pages_have_no_write_controls(self):
        """两页一条规则：页面在、只读说明在、没有任何写控件（不钉占位文案，心/脑上线都不用改）。

        「未接入」这类实时状态由端点自己报（见 test_heart_and_brain_are_readonly_json），
        不写进源码断言：页面是活是占位，这条断言都不该红。
        """
        text = (PAGES / "heart.html").read_text(encoding="utf-8")
        self.assertIn("心 · 只读", text)
        self.assertIn("只读：人不能改心与脑", text)
        self.assertIn("§9.2", text, "只读的理由要引 architecture §9.2")
        for needle in ("button", "form", "textarea", "<input"):
            self.assertNotIn(needle, text.lower(), "heart.html 里出现了写控件：%s" % needle)
        for needle in ('"post"', '"put"', '"delete"'):
            self.assertNotIn(needle, text.lower(), "heart.html 的请求只能是 GET：%s" % needle)

        # 脑页多一条写（接受提案，写魂不写脑），所以上面那张图不能整张套到它头上；
        # 完整判据在 test_pages_brain.py：写只有 POST /soul/proposal，其余全 GET。
        brain = (PAGES / "brain.html").read_text(encoding="utf-8")
        self.assertIn("脑 · 只读", brain)
        self.assertIn("§9.2", brain, "只读的理由要引 architecture §9.2")
        for needle in ("<form", "<textarea", "<select", "contenteditable", 'call("put"',
                       'call("delete"'):
            self.assertNotIn(needle, brain.lower(), "brain.html 里出现了写控件：%s" % needle)

        # 脑已接入：这一页是活的只读视图，五条 GET 的路径直接来自脑的路由表
        brain = (PAGES / "brain.html").read_text(encoding="utf-8")
        for path in BRAIN_ROUTES:
            if path in ("/brain", "/brain/recall"):
                # /brain 是这一页自己（devserver 静态页）；回想要带 q，只在人点的时候拉
                continue
            self.assertIn('call("GET", "%s")' % path, brain, "brain.html 要拉 %s" % path)

    def test_panel_page_reads_five_blocks_and_budget(self):
        text = PAGE.read_text(encoding="utf-8")
        for label in ("名字", "形象", "三句自述", "待人姿态", "红线", "合计"):
            self.assertIn(label, text)
        self.assertIn('id="ro-appearance"', text)          # 只读展示
        self.assertNotIn("appearance-value", text)         # 本轮只读：没有编辑框


class PanelContractTest(unittest.TestCase):
    """给壳的挂载点：routes(store, brain) -> [(method, path, handler)]。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)
        self.brain = _brain_store(self._tmp.name)

    def test_routes_cover_every_registered_endpoint(self):
        """挂载表从路由表推导：条数不写死，一一对应且不重复挂。"""
        routes = panel.routes(self.store, self.brain)
        mounted = {(method, path) for method, path, _ in routes}
        self.assertEqual({(method, path) for path, method in api.ROUTES.items()}, mounted)
        self.assertEqual(len(api.ROUTES), len(routes), "同一个端点不许挂两次")
        self.assertEqual([], [pair for pair in REQUIRED_ROUTES if pair not in mounted],
                         "关键端点没挂上")

    def test_handlers_take_body_and_query_and_return_json_safe_payloads(self):
        """每条挂上的端点都能被调用，且返回 json 安全的 dict。

        分三类，谁都不许 skip：
        - 无必填参数的 GET：空 query 必须 200；
        - REQUIRED_PARAM_ROUTES 里的 GET（嘴的 /mouth/split 与 /mouth/gate 缺 text，家的
          /home/list 与 /home/read 缺 room）：空 query 必须 400，而且仍是 json 安全的 dict +
          ok:false + 一句点名缺哪个参数的 message；
        - 未接入的端点（day_api.unconnected_routes()，由懒导入失败**推导**）：必须 501 +
          message 含「未接入」。这一组不是手写名单——organs/day 的 schedule / delivery
          一落地它就自动变空，那几条路由自动回到"必须 200"，不留永久例外。
        枚举口径覆盖 api.ROUTES 里每一条 GET：没有一条落在三类之外（漏网即失败）。
        """
        seen_get = set()
        answered_unconnected = set()
        # 口径变更（2026-11-05 心线落地）：心读自己的数据根，而 mood.read / energy.read 是
        # "读时算、顺手落一笔"的设计——所以这条"把每条 GET 都走一遍"的测试必须把心指到
        # 临时目录，否则跑一次测试就往仓里的 data 留下 mood_traj.jsonl 与 energy.json。
        heart_root = Path(self._tmp.name) / "heart"
        heart_root.mkdir()
        # 口径变更（2026-11-05 日子线第一波）：日子的每条路由都会真落盘——tick 落计划账与
        # 节律状态、tick/night 跑夜落夜日志与心的结算、schedule/draw 往家里落卡——所以 /day*
        # 不分读写一律把**三个**根都指到临时目录。少指一个就是往真实的家/心写东西：
        # home_root 缺了，draw 会往 organs/home/data/工作台 落测试卡（这条踩过）。
        day_root = Path(self._tmp.name) / "day"
        day_root.mkdir()
        home_root = Path(self._tmp.name) / "home"
        home_root.mkdir()
        day_query = {"root": str(day_root), "home_root": str(home_root),
                     "heart_root": str(heart_root)}
        unconnected = set(day_api.unconnected_routes())
        self.assertEqual(set(), unconnected - set(api.ROUTES), "未接入名单里有不存在的路由")
        for method, path, handler in panel.routes(self.store, self.brain):
            self.assertTrue(callable(handler), path)
            query = dict(day_query) if path.startswith("/day") else {}
            if method == "GET":
                if path == "/brain/recall":
                    query["q"] = "以前说过什么"
                if path.startswith("/heart"):
                    query["root"] = str(heart_root)
                status, payload = handler(None, query)
                seen_get.add(path)
                if status == 501:
                    answered_unconnected.add(path)
                if path in REQUIRED_PARAM_ROUTES:
                    self.assertEqual(400, status, path)
                    self.assertIsInstance(payload, dict, path)
                    self.assertFalse(payload.get("ok"), path)
                    self.assertIn(REQUIRED_PARAM_ROUTES[path],
                                  payload.get("message", ""), path)
                elif path in unconnected:
                    self.assertEqual(501, status, path)
                    self.assertIn("未接入", payload.get("message", ""), path)
                else:
                    self.assertEqual(200, status, path)
            else:
                status, payload = handler({}, query)
                self.assertIsInstance(status, int, path)
                if path in unconnected:
                    self.assertEqual(501, status, path)
                    self.assertIn("未接入", payload.get("message", ""), path)
            json.dumps(payload, ensure_ascii=False)
        registered_get = {path for path, method in api.ROUTES.items() if method == "GET"}
        self.assertEqual(registered_get, seen_get, "有 GET 路由没被这一层枚举到")
        self.assertTrue(set(REQUIRED_PARAM_ROUTES) <= registered_get,
                        "REQUIRED_PARAM_ROUTES 里有端点不在路由表上")
        # 声明式收口：实际回 501 的 GET 集合 == unconnected_routes() ∩ GET 路由。
        # 断言随模块落地自动变严，不会留下"某条路由永远豁免"的口子。
        self.assertEqual(unconnected & registered_get, answered_unconnected,
                         "501 的 GET 与未接入名单对不上：要么有路由被永久豁免，"
                         "要么模块落地了名单没跟上")

    def test_render_handler_reads_audience_from_query(self):
        routes = dict(((method, path), handler)
                      for method, path, handler in panel.routes(self.store, self.brain))
        status, payload = routes[("GET", "/soul/render")](None, {"audience": AUDIENCE})
        self.assertEqual(200, status)
        self.assertEqual(AUDIENCE, payload["audience"])
        self.assertIn("【待人姿态】", payload["text"])

    def test_zz_heart_data_dir_stays_clean(self):
        """守卫：跑完这一轮，仓里的 heart/data 只许留 .gitignore / .gitkeep / baseline-card.json。

        上面那条"把每条 GET 都走一遍"的用例是本目录里唯一会碰心全部端点的：
        它一旦忘了把 root 指到临时目录，energy.read 的读时结算就会往这里写回
        energy.json（pending.json / mood_traj.jsonl / event_log.jsonl 同理）。
        故意把名字排到本类最后（test_zz_）：同一类里它一定最后跑，正好收这一类的尾。
        """
        present = {path.name for path in HEART_DATA.iterdir()}
        extra = sorted(present - ALLOWED_HEART_DATA)
        self.assertEqual(set(), set(extra),
                         "心的 data 目录多了运行时文件 %r：有测试拿了默认根（真实数据根）"
                         "跑心，root= 必须指到临时目录" % (extra,))
        self.assertEqual(ALLOWED_HEART_DATA, present,
                         "心的 data 目录少了卡或说明文件：%r" % (sorted(present),))

    def test_contract_module_documents_mount_point_and_imports_no_host(self):
        source = (PLUGIN / "api" / "panel.py").read_text(encoding="utf-8")
        self.assertIn("挂载点", source)
        self.assertIn("核里不 import 宿主", source)
        self.assertNotIn("import " + "ast" + "rbot", source)
        self.assertNotIn("from " + "ast" + "rbot", source)


class ShellMountTest(unittest.TestCase):
    """壳的接线：宿主给了挂载点就交 routes()，没给就跳过——两条都不许抛。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def test_no_mount_hook_is_skipped_silently(self):
        import main

        class BareContext:
            pass

        main._mount_panel(BareContext(), self.store)  # 不抛就是过

    def test_mount_hook_receives_the_route_table(self):
        import main

        received = []

        class Context:
            def mount_panel_routes(self, routes):
                received.append(routes)

        main._mount_panel(Context(), self.store)
        self.assertEqual(1, len(received))
        self.assertEqual(len(api.ROUTES), len(received[0]), "挂载条数照路由表")
        self.assertEqual({(method, path) for path, method in api.ROUTES.items()},
                         {(method, path) for method, path, _ in received[0]})

    def test_failing_hook_does_not_raise(self):
        import main

        class Context:
            def mount_panel_routes(self, routes):
                raise RuntimeError("宿主挂载点坏了")

        main._mount_panel(Context(), self.store)  # 面板挂不上也得让她起身

    def test_missing_store_skips_mounting(self):
        import main

        class Context:
            def mount_panel_routes(self, routes):
                raise AssertionError("store 为 None 时不该挂载")

        main._mount_panel(Context(), None)


class DevserverPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_dir = copy_fixture("valid", self._tmp.name)

    def test_read_page_returns_bytes_and_missing_is_none(self):
        root = Path(self._tmp.name) / "pages"
        root.mkdir()
        (root / "x.html").write_text("<!doctype html><p>x</p>", encoding="utf-8")
        self.assertEqual(b"<!doctype html><p>x</p>", devserver.read_page("x.html", root))
        self.assertIsNone(devserver.read_page("nope.html", root))

    def test_root_and_panel_serve_the_same_page(self):
        self.assertEqual({"/": "soul.html", "/panel": "soul.html",
                          "/heart": "heart.html", "/brain": "brain.html",
                          "/home": "home.html"},
                         devserver.PAGE_ROUTES)
        data = devserver.read_page("soul.html")
        self.assertIsNotNone(data)
        self.assertIn(b"<!doctype html", data)
        self.assertIn("她此刻会被注入的样子".encode("utf-8"), data)

    def test_raw_utf8_audience_from_request_line_is_recovered(self):
        """请求行按 latin-1 解出来的乱码要能还原成 UTF-8；curl 直接写中文走的就是这条。"""
        mojibake = AUDIENCE.encode("utf-8").decode("latin-1")
        target = devserver._decode_target("/soul/render?audience=" + mojibake)
        self.assertEqual("/soul/render?audience=" + AUDIENCE, target)
        status, payload = api.handle("GET", target, None, load(self.store_dir))
        self.assertEqual(200, status)
        self.assertEqual(AUDIENCE, payload["audience"])


class DevserverHttpTest(unittest.TestCase):
    """真起一个 127.0.0.1 的服务器，确认静态页与查询串接得上。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store = load(copy_fixture("valid", self._tmp.name))
        handler = type("_TestHandler", (devserver._Handler,),
                       {"store": store, "log_message": lambda *args: None})
        try:
            self.server = HTTPServer(("127.0.0.1", 0), handler)
        except OSError as exc:  # 本机不让绑端口就跳过，不影响别的断言
            self.skipTest("起不了本地端口：%s" % exc)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

        def _stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)

        self.addCleanup(_stop)
        self.port = self.server.server_address[1]

    def _get(self, path: str) -> Tuple[int, bytes, str]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            res = conn.getresponse()
            return res.status, res.read(), res.getheader("Content-Type") or ""
        finally:
            conn.close()

    def test_root_serves_the_panel_page(self):
        status, body, ctype = self._get("/")
        self.assertEqual(200, status)
        self.assertIn("text/html", ctype)
        self.assertIn(b"<!doctype html", body)

    def test_panel_path_serves_the_same_page(self):
        status, body, _ = self._get("/panel")
        self.assertEqual(200, status)
        self.assertEqual(devserver.read_page("soul.html"), body)

    def test_render_endpoint_answers_over_http(self):
        status, body, ctype = self._get("/soul/render?audience=%E7%86%9F%E4%BA%BA")
        self.assertEqual(200, status)
        self.assertIn("application/json", ctype)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(AUDIENCE, payload["audience"])
        self.assertIn("【待人姿态】", payload["text"])

    def _request(self, method: str, path: str,
                 body: str = "{}") -> Tuple[int, bytes, str]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body.encode("utf-8"),
                         headers={"Content-Type": "application/json"})
            res = conn.getresponse()
            return res.status, res.read(), res.getheader("Content-Type") or ""
        finally:
            conn.close()

    def test_heart_and_brain_serve_readonly_pages_over_http(self):
        status, body, ctype = self._get("/heart")
        self.assertEqual(200, status)
        self.assertIn("text/html", ctype)
        self.assertIn("心 · 只读".encode("utf-8"), body)
        # 口径变更（2026-11-05 心线落地，第 8 处）：心页是活视图，不再是"未接入"占位页。
        # 断真话：只读说明在、页面上没有任何写控件、也不再说自己没接入。
        self.assertIn("只读：人不能改心与脑".encode("utf-8"), body)
        self.assertNotIn(b"<button", body.lower())
        self.assertNotIn("未接入：这一层还没有代码".encode("utf-8"), body)

        status, body, ctype = self._get("/brain")
        self.assertEqual(200, status)
        self.assertIn("text/html", ctype)
        self.assertIn("脑 · 只读".encode("utf-8"), body)
        self.assertNotIn("未接入：这一层还没有代码".encode("utf-8"), body)

        for path in ("/heart", "/brain"):
            for method in ("POST", "PUT", "DELETE"):
                status, body, ctype = self._request(method, path)
                self.assertEqual(405, status, "%s %s" % (method, path))
                self.assertIn("application/json", ctype)
                self.assertIn("只读", json.loads(body.decode("utf-8"))["message"])
        self.assertEqual(404, self._get("/brain/x")[0])


class RouteRegistryTest(unittest.TestCase):
    """路由表的汇总口径：八张器官表拼一张，同名键当场炸——静默覆盖正是拆包要止住的事。"""

    def test_route_table_is_the_merge_of_five_organ_tables(self):
        """汇总表 = 八张器官表逐键拼起来，一条不少、一条不多。

        口径变更（2026-11-05 心线落地）：心的七条只读端点成了独立一张表
        （heart_api.HEART_ROUTES），占位页表 PAGE_ROUTES 留空——所以这里是六张，不是五张。

        口径变更（2026-11-05 日子线第一波）：日子成了独立一张表（day_api.DAY_ROUTES，
        十条：tick/plan/night/log/schedule/schedule-draw/pending/deliver/ack/reconcile）
        ——所以这里是八张，不是六张（核自报身份那张一直在，从没算进"器官"里）。

        八张表各自守着一条器官线：soul_api.SOUL_ROUTES（魂）/ brain_api.BRAIN_ROUTES（脑）/
        page_api.PAGE_ROUTES（占位页，已空）/ mouth_api.MOUTH_ROUTES（嘴的干跑端点）/
        home_api.HOME_ROUTES（家：九条只读 + 一条 POST /home/write）/
        heart_api.HEART_ROUTES（心：七条只读）/
        day_api.DAY_ROUTES（日子：四条只读 + 六条 POST，内容归 schedule、送达归 delivery）/
        version_api.VERSION_ROUTES（核自报身份）。
        """
        tables = (("soul_api.SOUL_ROUTES", core.soul_api.SOUL_ROUTES),
                  ("brain_api.BRAIN_ROUTES", core.brain_api.BRAIN_ROUTES),
                  ("page_api.PAGE_ROUTES", core.page_api.PAGE_ROUTES),
                  ("mouth_api.MOUTH_ROUTES", core.mouth_api.MOUTH_ROUTES),
                  ("home_api.HOME_ROUTES", core.home_api.HOME_ROUTES),
                  ("heart_api.HEART_ROUTES", core.heart_api.HEART_ROUTES),
                  ("day_api.DAY_ROUTES", core.day_api.DAY_ROUTES),
                  ("version_api.VERSION_ROUTES", core.version_api.VERSION_ROUTES))
        sources = " / ".join(name for name, _ in tables)
        merged = core._merge(*[table for _, table in tables])
        self.assertEqual(merged, core.ROUTES, "汇总表不是这六张表的拼接：%s" % sources)
        self.assertEqual(set(core.ROUTES),
                         set().union(*[set(table) for _, table in tables]),
                         "汇总表的路径不是这六张表的并集：%s" % sources)
        for name, table in tables:
            self.assertEqual([], [route for route in table if route not in core.ROUTES],
                             "%s 里的路由漏在汇总表外面了" % name)

    def test_two_organs_registering_the_same_route_raises(self):
        """两条线各自登记同一个端点：必须抛，不许后写的悄悄盖掉先写的。"""
        with self.assertRaises(ValueError):
            core._merge({"/soul": "GET"}, {"/soul": "POST"})
        with self.assertRaises(ValueError):
            core._merge({"/a": "GET"}, {"/b": "GET"}, {"/a": "GET"})
        self.assertEqual({"/a": "GET", "/b": "GET"},
                         core._merge({"/a": "GET"}, {"/b": "GET"}))

    def test_required_endpoints_are_registered_with_the_documented_method(self):
        registered = {(method, path) for path, method in core.ROUTES.items()}
        self.assertEqual([], [pair for pair in REQUIRED_ROUTES if pair not in registered],
                         "关键端点缺失或方法被改")


if __name__ == "__main__":
    unittest.main()
