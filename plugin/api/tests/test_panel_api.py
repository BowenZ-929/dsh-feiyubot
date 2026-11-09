"""面板与端点：读、改待人倾向、接受提案、迁移、注入面、挂载契约、静态页。

验收口径（都被下面的断言看住）：
- 七个魂端点各有一条成功路径；提案端点成功与被拒（只两条依据、改红线）都有；
- 面板上心与脑只有只读入口：GET /heart 返回「未接入」，GET /brain 返回「已接入」，
  心与脑的路径上 POST/PUT/DELETE 一律 405（入口存在，但没有写），
  pages/heart.html 无任何写控件；pages/ 里的脑页已删除（2026-11-06，见下），
  它原来那张「只许一条写」的图随之失效——那条写（接受提案，写魂不写脑）
  搬到了 pages/soul.html 的「她提的改动」块，判据在 tests/test_pages_fetch_targets.py；
  pages/soul.html 里心与脑只以链接 + 只读说明 + 只读提案框出现；
- 只读端点无副作用：GET 前后五个数据文件字节不变；
- 面板改完这里就变：POST /soul/edit 改 tone → GET /soul/render 立刻是新句子（同一进程，不重启）。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from unittest import mock
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
# 心的数据目录：跑完一轮测试，这里只许剩这四个条目——两个出厂文件（卡 + 意图权重表）
# 加两个说明文件。mood.read / energy.read 是"读时算、顺手落一笔"的设计，谁拿默认根跑
# 一次就会往这儿丢运行时文件——守卫见 PanelContractTest.test_zz_heart_data_dir_stays_clean。
# engage_weights.json 是第四轮的出厂权重表（intent 的参数表，人可改、改完即生效）：
# 它是**出厂文件**，不是运行时落盘——intent 只读它，一个字节都不写。
# emotion_contract.json 同理：情绪的契约（五类事件、权重、账格式），report 只读它；
# 运行期那份在 store.default_root()，不是这里。
HEART_DATA = PLUGIN / "organs" / "heart" / "data"
ALLOWED_HEART_DATA = {".gitignore", ".gitkeep", "baseline-card.json",
                      "engage_weights.json", "emotion_contract.json"}
BRAIN_ROUTES = ("/brain", "/brain/overview", "/brain/today", "/brain/people",
                "/brain/recall", "/brain/selfnarration", "/brain/proposals")
# 关键端点：方法 + 路径。条数由 api.ROUTES 自己算（见 RouteRegistryTest），这里只钉"必须在"。
#: 允许「两种真话」的 GET：409 在这里**不是故障**，是系统在如实报告。
#: /home/warehouse 每次被读都现场重算指针卡的 sha256，对不上就回 409 POINTER_STALE
#: ——「改了就是改了，只报不修」正是它的判据。把它钉进"必须 200"会怎样：
#: 嘴或心每改一次数据文件这条就红一次，而红的原因恰是系统在正常工作；
#: 那种「正确行为导致测试红」会训练人去 reseed 让它变绿，等于把诚实报告当噪音。
HONEST_TWO_ANSWERS = {"/home/warehouse": (200, 409)}

REQUIRED_ROUTES = (
    ("GET", "/day/growth"), ("POST", "/day/growth/config"), ("POST", "/day/growth/profile"),
    ("GET", "/memes/list"), ("GET", "/memes/read"), ("POST", "/memes/save"), ("POST", "/memes/settings"),
    ("GET", "/phone/status"), ("POST", "/phone/connect"), ("POST", "/phone/inspect"),
    ("POST", "/phone/screen"), ("POST", "/phone/action"),
    ("GET", "/phone/browse"), ("POST", "/phone/browse/start"), ("POST", "/phone/browse/stop"), ("POST", "/phone/browse/settings"),
    ("GET", "/learning/status"), ("GET", "/learning/read"),
    ("GET", "/learning/search"), ("POST", "/learning/start"),
    ("GET", "/learning/expressions"), ("POST", "/learning/expressions/set"),
    ("GET", "/api/graph/snapshot"),
    ("GET", "/studio/strokes"), ("GET", "/studio/strokes/read"), ("POST", "/studio/strokes/start"),
    ("GET", "/studio/assets"), ("POST", "/studio/assets/save"),
    ("GET", "/studio/drafts"), ("POST", "/studio/drafts/save"), ("POST", "/studio/preview"),
    ("GET", "/observe/records"),
    ("GET", "/observe/activity"),
    ("GET", "/observe/rice"),
    ("GET", "/observe/participation"),
    ("GET", "/observe/channel"),
    ("GET", "/observe/channel-runtime"),
    ("GET", "/eye/status"), ("POST", "/eye/config"),
    ("GET", "/voice/status"), ("POST", "/voice/config"),
    ("GET", "/ear/status"), ("POST", "/ear/config"),
    ("GET", "/model/status"), ("POST", "/model/config"),
    ("GET", "/soul"), ("GET", "/soul/check"), ("GET", "/soul/history"),
    ("GET", "/soul/pin"), ("GET", "/soul/revisions"),
    ("GET", "/soul/render"), ("POST", "/soul/edit"), ("POST", "/soul/migrate"),
    # 提案落状态（2026-11-06 三次）：清单从脑读出来，拒绝只写脑里的终态。
    ("GET", "/soul/proposals"), ("POST", "/soul/proposal/reject"),
    ("POST", "/soul/proposal"), ("POST", "/soul/redline"), ("POST", "/soul/rollback"),
    ("POST", "/soul/acceptance"),
    # 口径变更（2026-11-05 心线落地）：心从占位页变成七条只读端点，全在这一张表里。
    ("GET", "/heart"), ("GET", "/heart/overview"), ("GET", "/heart/baseline"),
    ("GET", "/heart/mood"), ("GET", "/heart/energy"), ("GET", "/heart/offset"),
    ("GET", "/heart/engage"), ("GET", "/heart/changes"),
    # 2026-11-05 第二轮：/heart/effective 加入关键端点表
    ("GET", "/heart/effective"),
    # 第三轮念头层：/heart/pending 与心跳同一条只读口径（谁加路由谁登记）
    ("GET", "/heart/pending"),
    # 第四轮接线段：/heart/sleep 把睡眠账搬上端点（谁加路由谁登记）
    ("GET", "/heart/sleep"),
    # 第五轮接线段：/heart/refusals 给拒绝记录开了一条只读查询（谁加路由谁登记）
    ("GET", "/heart/refusals"),
    # 第六轮接线段：/heart/proactive 把「主动开口之后有没有人理」搬上端点（谁加路由谁登记）
    ("GET", "/heart/proactive"),
    # 第七轮接线段：/heart/gate 把出站闸的只读判读搬上端点（人级在前、群级在后）
    ("GET", "/heart/gate"),
    # 第四轮手线：手的三条只读读数（判据快照／任务与到点没做／手账尾读）
    ("GET", "/hand"), ("GET", "/hand/tasks"), ("GET", "/hand/ledger"),
    ("GET", "/brain"), ("GET", "/brain/overview"), ("GET", "/brain/today"),
    ("GET", "/brain/people"), ("GET", "/brain/recall"),
    ("GET", "/brain/selfnarration"), ("GET", "/brain/proposals"),
    ("GET", "/brain/tiers"), ("GET", "/brain/cold"), ("POST", "/brain/judge"),
    ("GET", "/mouth/split"), ("GET", "/mouth/address"),
    ("GET", "/mouth/tone"), ("GET", "/mouth/gate"),
    # 说话线（2026-11-05）：POST /chat 是核上唯一一条真会说话的写（谁加路由谁登记）
    ("POST", "/chat"),
    ("POST", "/chat/ack"),
    ("GET", "/home/rooms"), ("GET", "/home/access"), ("GET", "/home/list"),
    ("GET", "/home/read"), ("GET", "/home/audit"), ("GET", "/home/audit/verify"),
    ("GET", "/home/drawer"), ("GET", "/home/archive/borrow"), ("GET", "/home/warehouse"),
    ("GET", "/home/anchors"), ("POST", "/home/anchors/append"),
    ("POST", "/home/consume"), ("POST", "/home/progress"), ("POST", "/home/ingest"),
    ("POST", "/home/write"),
    # 核自报身份（2026-11-05）：跑的是哪一版、什么时候起的、源码有没有在它脚下被改过。
    ("GET", "/healthz"),
    # 脑线新落的两条只读端点（2026-11-05，四档列表与冷仓分页）：先在这儿认下来，
    # 脑线自己的断言到了再各自归位。
    ("GET", "/brain/tiers"), ("GET", "/brain/cold"),
    # 日子线第一波（2026-11-05）：十条 = 四条只读 + 六条手工触发。
    # 写着"手工触发"的那几条不是只读页：tick 排当天、night 跑四步、deliver 入队、ack 记回执。
    ("POST", "/day/tick"), ("GET", "/day/plan"), ("POST", "/day/night"),
    ("GET", "/day/life"), ("POST", "/day/life/config"),
    ("POST", "/day/life/adjust"), ("POST", "/day/life/generate"),
    ("GET", "/day/log"), ("GET", "/day/schedule"), ("POST", "/day/schedule/draw"),
    ("GET", "/day/pending"), ("POST", "/day/deliver"), ("POST", "/day/ack"),
    ("POST", "/day/reconcile"),
    # 日子线第五波：主动开口那一类（读账 / 动念 / 结算回执）
    ("GET", "/day/proactive"), ("POST", "/day/proactive/consider"),
    ("POST", "/day/proactive/disable"),
    ("POST", "/day/proactive/enable"),
    ("POST", "/day/proactive/outcome"),
    # 日子线第七波：独处循环（idle 账尾部 + 读数；手工触发一次）
    ("GET", "/day/solitude"), ("POST", "/day/solitude/consider"),
)
# 有必填查询参数的 GET：空 query 时必须是 400（不是 200，也不许崩）——缺哪个参数就得在 message 里点名。
# 以后加带必填参数的端点，放进这一组；别把它学成 skip（skip 掉就没人守「缺参要响」了）。
REQUIRED_PARAM_ROUTES = {"/memes/read": "表情编号", "/learning/read": "学习记录编号", "/learning/search": "搜索词",
                         "/studio/strokes/read": "画作编号", "/mouth/split": "text", "/mouth/gate": "text",
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
        # 这些用例验证基线提案；不读取正在运行的自主成长开关。
        day_root = mock.patch.dict('os.environ', {'DAY_ROOT': str(Path(self._tmp.name) / 'day')})
        day_root.start()
        self.addCleanup(day_root.stop)

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
        # 一件一套口径：七个 parts 一律四个键，不再区分 suggested/limit 两套键名
        for part in chars["parts"]:
            self.assertEqual({"name", "chars", "limit", "hard"}, set(part), part["name"])
        self.assertEqual(["名字与自称", "形象", "初始性格", "怕什么", "喜欢什么",
                          "待人姿态", "红线条目"],
                         [part["name"] for part in chars["parts"]])   # 顺序即渲染顺序
        self.assertEqual(7, len(chars["parts"]))
        self.assertEqual(sum(part["chars"] for part in chars["parts"]), chars["used"])
        self.assertEqual(1000, chars["limit"])          # 合计是硬线
        hard = [part for part in chars["parts"] if part["hard"]]
        self.assertEqual(["形象", "怕什么", "喜欢什么"],
                         [part["name"] for part in hard])   # 三件硬线
        self.assertEqual([60, 60, 80], [part["limit"] for part in hard])
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
        # 拒绝记录的查询侧也是只读：写方法一律 405，措辞沿用心专属那一句。
        for method in ("POST", "PUT", "DELETE"):
            status, payload = self._call(method, "/heart/refusals", {})
            self.assertEqual(405, status, "%s /heart/refusals" % method)
            self.assertIn("只收 GET", payload["message"])
            self.assertIn("人不能改心", payload["message"])

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
        self.assertEqual([("POST", "/brain/judge")], write_routes,
                         "心只读；脑只有 POST /brain/judge 一个写方法（名片走动态路由，不在这张表）")

    def test_proactive_route_is_registered_in_both_tables(self):
        """谁加路由谁登记：HEART_ROUTES 与 REQUIRED_ROUTES 必须同时认得 /heart/proactive。

        两张表各管一件事——前者定方法（只读、405 措辞也按它判）、后者守「关键端点
        被谁悄悄删掉」。只登一处，就等于给这条线留了半张门票。
        """
        self.assertEqual("GET", core.heart_api.HEART_ROUTES["/heart/proactive"])
        self.assertEqual("GET", api.ROUTES["/heart/proactive"])
        self.assertIn(("GET", "/heart/proactive"), REQUIRED_ROUTES)

    def test_gate_route_is_registered_in_both_tables(self):
        """谁加路由谁登记：出站闸的只读判读走同一条口径（只读、405 也按它判）。

        这条线的判据是「查询不是发生」：端点必须贴得出「调它前后两本账逐字节不变」，
        所以它只能是 GET；两张表都登记，谁也删不掉半张门票。
        """
        self.assertEqual("GET", core.heart_api.HEART_ROUTES["/heart/gate"])
        self.assertEqual("GET", api.ROUTES["/heart/gate"])
        self.assertIn(("GET", "/heart/gate"), REQUIRED_ROUTES)

    def test_route_table_partitions_into_soul_brain_and_readonly_pages(self):
        """按前缀分组断言，不写死条数：别处加端点这里不该红，写错归属才该红。

        口径变更（2026-11-05 心线落地）：心从「占位页」变成六张器官表里的一张，四组变五组——
        魂（/soul*）、脑（/brain*）、家（/home*）、心（/heart*）、其余（嘴的干跑端点）。
        READONLY_PAGES 已删，所以「其余」只剩 MOUTH_ROUTES；五组两两不相交、并集就是整张
        ROUTES——每条路由都要有明确归属，不许有一条落在分类之外。

        口径变更（2026-11-05 日子线第一波）：多第六组「日子」（/day*，DAY_ROUTES 那一张表），
        「其余」仍只有嘴的干跑端点 + 核自报身份——日子不许混进 rest，混进去就等于没归属。

        口径变更（2026-11-05 说话线）：多第七组「说话」（/chat，CHAT_ROUTES 那一张表：
        核上唯一一条真会说话的写）。它不复用嘴的干跑端点那张表——干跑只算不发，说话是真发。

        口径变更（2026-11-06 第四轮手线）：多第八组「手」（/hand*，HAND_ROUTES 那一张表，
        三条全 GET）。手是读数不是动作：记下事／标完成那两条动作没有 HTTP 入口。
        """
        soul = {path for path in api.ROUTES if path.startswith("/soul")}
        brain = {path for path in api.ROUTES if path.startswith("/brain") or path == "/api/graph/snapshot"}
        family = {path for path in api.ROUTES if path.startswith("/home")}
        heart = {path for path in api.ROUTES if path.startswith("/heart")}
        day = {path for path in api.ROUTES if path.startswith("/day")}
        chat = {path for path in api.ROUTES if path.startswith("/chat")}
        hand = {path for path in api.ROUTES if path.startswith("/hand")}
        eye = {path for path in api.ROUTES if path.startswith("/eye/")}
        self.assertEqual(set(core.eye_api.EYE_ROUTES), eye)
        voice = {path for path in api.ROUTES if path.startswith("/voice/")}
        self.assertEqual(set(core.voice_api.VOICE_ROUTES), voice)
        ear = {path for path in api.ROUTES if path.startswith("/ear/")}
        self.assertEqual(set(core.ear_api.EAR_ROUTES), ear)
        studio = {path for path in api.ROUTES if path.startswith("/studio/")}
        self.assertEqual(set(core.studio_api.STUDIO_ROUTES), studio)
        learning = {path for path in api.ROUTES if path.startswith("/learning/")}
        self.assertEqual(set(core.learning_api.LEARNING_ROUTES), learning)
        observe = {"/observe/records", "/observe/activity", "/observe/rice", "/observe/participation", "/observe/channel", "/observe/channel-runtime"}
        self.assertEqual(set(core.observe_api.OBSERVE_ROUTES), observe)
        self.assertEqual({"GET"}, {api.ROUTES[path] for path in observe})
        phone = {path for path in api.ROUTES if path.startswith("/phone/")}
        self.assertEqual(set(core.phone_api.PHONE_ROUTES), phone)
        memes = {path for path in api.ROUTES if path.startswith('/memes/')}
        self.assertEqual(set(core.memes_api.MEME_ROUTES), memes)
        model_settings = {path for path in api.ROUTES if path.startswith('/model/')}
        self.assertEqual(set(core.model_settings_api.MODEL_ROUTES), model_settings)
        rest = set(api.ROUTES) - model_settings - soul - brain - family - heart - day - chat - hand - observe - eye - voice - ear - studio - learning - phone - memes
        self.assertEqual(set(core.home_api.HOME_ROUTES), family,
                         "家的路由就是 HOME_ROUTES 那一张表")
        self.assertEqual({"/home/write", "/home/anchors/append", "/home/consume",
                          "/home/progress", "/home/ingest"},
                         {path for path in family if api.ROUTES[path] == "POST"},
                         "家四条写路由：/home/write、/home/anchors/append、/home/consume、"
                         "/home/progress")
        self.assertEqual({"GET"},
                         {api.ROUTES[path] for path in family} - {"POST"},
                         "家其余十条只读")
        mouth = set(core.mouth_api.MOUTH_ROUTES)
        version_set = set(core.version_api.VERSION_ROUTES)
        self.assertEqual(set(core.heart_api.HEART_ROUTES), heart,
                         "心的路由就是 HEART_ROUTES 那一张表")
        self.assertEqual({"GET"}, {api.ROUTES[path] for path in heart}, "心只有只读端点")
        self.assertEqual(set(core.hand_api.HAND_ROUTES), hand,
                         "手的路由就是 HAND_ROUTES 那一张表")
        self.assertEqual({"GET"}, {api.ROUTES[path] for path in hand},
                         "手只有只读读数：记下事／标完成是动作，不上 HTTP")
        self.assertEqual(set(core.day_api.DAY_ROUTES), day,
                         "日子的路由就是 DAY_ROUTES 那一张表")
        self.assertEqual({"GET", "POST"}, {api.ROUTES[path] for path in day},
                         "日子只有五条只读 + 八条手工触发（写方法错了一律 405）")
        self.assertEqual(set(core.chat_api.CHAT_ROUTES), chat,
                         "说话的路由就是 CHAT_ROUTES 那一张表")
        self.assertEqual({"POST"}, {api.ROUTES[path] for path in chat},
                         "说话只有一条写：POST /chat")
        self.assertEqual(mouth | version_set, rest,
                         "魂、脑、家、心、日子、说话之外 = 嘴的干跑端点 + 核自报身份 %s"
                         % (sorted(mouth | version_set),))
        self.assertEqual({"GET", "POST"}, {api.ROUTES[path] for path in brain},
                         "脑的端点：只读 + 唯一写端点 POST /brain/judge")
        self.assertEqual({"/brain/judge"},
                         {path for path in brain if api.ROUTES[path] == "POST"},
                         "脑只有一条写路由：POST /brain/judge")
        # 写入口七条：改值 / 换形状 / 提案 / 拒绝提案 / 红线逐条加撤 / 回滚 / 验收探针。
        # 前五条是「改她的魂」；拒绝提案只落脑里的终态（不写魂、不产生 revision）；
        # 探针是「写两次再写回」的验收路径，edit 上的 probe 键已关掉。
        # 整块替换仍然没有入口（edit 动 items 整表、migrate 碰 bounds 都一律拒）。
        self.assertEqual({"/soul/edit", "/soul/migrate", "/soul/proposal",
                          "/soul/proposal/reject", "/soul/redline", "/soul/rollback",
                          "/soul/acceptance"},
                         {path for path in soul if api.ROUTES[path] == "POST"},
                         "魂的写入口：改值 / 换形状 / 提案 / 拒绝提案 / 红线逐条 / 回滚 / 验收探针")
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
        self.assertEqual(set(), chat & (soul | brain | family | heart | day | rest),
                         "说话与别的组有重叠")
        self.assertEqual(set(api.ROUTES),
                         soul | brain | family | heart | hand | day | chat | observe | eye | voice | ear | studio | learning | phone | memes | model_settings | rest,
                         "路由表分不成八组：有端点没归属")
        self.assertEqual(set(REQUIRED_ROUTES),
                         {(api.ROUTES[path], path) for path in api.ROUTES},
                         "路由表就是那张关键端点表：多了少了都得在这儿说清楚")

    def test_panel_page_write_controls_are_the_named_ones(self):
        """面板的写控件逐个点名，不许多出来。

        口径变更（2026-11-06 前端重做，人拍板的信息架构）：魂页先只留「改一句」与
        「加一条红线」两个主操作，提案与整档回滚被砍掉——理由是没有进六项导航。

        **改回来（2026-11-06 二次）+ 判据**：那条理由是把「有没有导航位」当成了
        「要不要这个入口」，两条都不成立：
        - 「她提的改动」是她想改自己唯一的通路（她只能提、不能自己改）。没有接受入口，
          「魂只有人能改」就只堵了一半——写入口只剩人，读的却是死信；
        - 「改坏了能退」是底线，命令行能用不等于页面能用——人能点到的才叫入口。
        所以现在固定按钮三个：改这一句 / 加这条红线 / 退回上一版；写请求五个：
        /soul/edit 一次、/soul/redline 两次（逐条加、逐条撤）、/soul/proposal 一次
        （接受提案，拒绝不发请求）、/soul/rollback 一次。撤线按钮仍由 JS 按条生成。

        **2026-11-06 三次（提案落状态）**：拒绝不再是「页内说一句就散」——拒掉的提案必须
        从待办里消失，所以多一个写请求 /soul/proposal/reject（只落脑里的终态，不写魂）。
        固定按钮仍是四个：拒绝用 window.prompt 问原因，不新造按键。
        """
        text = PAGE.read_text(encoding="utf-8")
        self.assertNotIn("127.0.0.1", text, "界面上不写核的地址：那是工程细节")
        # 三个文本框：喜欢什么、待人姿态、新红线条目的禁令正文（第七件由人写、人可改）
        self.assertEqual(4, text.count("<textarea"), "初始姿态、初始喜好、红线与成长纠正四处")
        # 四个固定按钮：改这一句（喜欢什么）/ 改这一句（待人姿态）/ 加这条红线 / 退回上一版；
        # 撤线按钮与提案两钮都由 JS 生成
        self.assertEqual(9, text.count("<button"), "四个基线按钮，加成长开关、刷新、纠正、回退与只读状态刷新")
        self.assertNotIn("<form", text)
        # 七个写请求：/soul/edit 2（喜欢什么、待人姿态）、/soul/redline 2（加、撤）、
        # /soul/proposal 1、/soul/proposal/reject 1、/soul/rollback 1
        self.assertEqual(7, text.count('call("POST"'), "全页只有七个写请求")
        self.assertNotIn("/soul/migrate", text, "换形状是坏卡状态的路，不给面板入口")
        self.assertIn("/soul/proposal", text, "接受提案的入口必须在页上（没有它她改不了自己）")
        self.assertIn("/soul/rollback", text, "整档回滚的入口必须在页上（改坏了能退是底线）")
        self.assertIn("/soul/render", text, "面板要显示她会被注入的样子")
        self.assertIn("/soul/history", text, "改动记录要有入口：谁为什么改")
        self.assertIn("/soul/check", text, "自检要在这页跑")

    def test_settings_page_links_to_the_two_readonly_pages(self):
        """心与家只许以只读方式出现：两条链接 + 各自一句「为什么在这」，没有任何写口。

        口径变更（2026-11-06 前端重做）：心与家不占导航位——它们与脑是同一批只读面，
        入口收在「设置」页。魂页因此不再有 心/脑 两条链接，断言跟着挪到设置页。
        """
        text = (PAGES / "settings.html").read_text(encoding="utf-8")
        self.assertIn('href="heart"', text, "心 · 只读读数 的入口在设置页")
        self.assertIn('href="home"', text, "家 · 十间房 的入口在设置页")
        for word in ("heart", "home"):
            hits = [line for line in text.splitlines() if word in line.lower()]
            self.assertGreater(len(hits), 0, "%s 至少出现在链接行上" % word)
            for line in hits:
                lowered = line.lower()
                for needle in ("<button", "<form", "<textarea", "<input",
                               'call("post"', 'call("put"', 'call("delete"'):
                    self.assertNotIn(needle, lowered,
                                     "%s 出现的那行不能有写口：%s" % (word, line))
        # 原来那句「还没接」改成「参数开口子以后再放这里」：页面不再是占位说明。
        self.assertNotIn("还没接：这一页只有地址", text)
        self.assertIn('href="eye"', text)

    def test_readonly_pages_have_no_write_controls(self):
        """两页一条规则：页面在、只读说明在、没有任何写控件（不钉占位文案，心/脑上线都不用改）。

        「未接入」这类实时状态由端点自己报（见 test_heart_and_brain_are_readonly_json），
        不写进源码断言：页面是活是占位，这条断言都不该红。
        """
        text = (PAGES / "heart.html").read_text(encoding="utf-8")
        self.assertIn("心 · 只读", text)
        self.assertRegex(text, r'id="readonly-note"[^>]*data-readonly="true"')
        self.assertIn("§9.2", text, "只读的理由要引 architecture §9.2")
        for needle in ("<form", "<textarea"):
            self.assertNotIn(needle, text.lower(), "heart.html 里出现了写控件：%s" % needle)
        for needle in ('"post"', '"put"', '"delete"'):
            self.assertNotIn(needle, text.lower(), "heart.html 的请求只能是 GET：%s" % needle)

        # 脑页那一段删掉了（2026-11-06）：那一页已整页退役——
        # 内容被 /about（关于他）与 /memory（记忆）整块接走，第二份副本只会走味。
        # 页没了，原来那三条「脑页有几个文本域、拉哪几条 GET」的断言就没有对象可断言，
        # 跟着删；它们看住的东西没丢——搬过去的取数分别由 about.html / memory.html
        # 在 tests/test_pages_fetch_targets.py 的 MIN_TARGETS 里真打活进程验。
        self.assertNotIn("/brain", devserver.PAGE_ROUTES,
                         "脑页已退役：/brain 交回 JSON 端点，不再是一张静态页")
        self.assertNotIn("brain", {path.stem for path in PAGES.glob("*.html")},
                         "脑那页该已删除（不写死文件名，按目录现状断言）")

    def test_panel_page_reads_five_blocks_and_budget(self):
        text = PAGE.read_text(encoding="utf-8")
        for label in ("名字", "形象", "初始性格", "怕什么", "标志性喜好", "待人姿态",
                      "红线", "合计"):
            self.assertIn(label, text)
        self.assertIn('id="ro-appearance"', text)          # 只读展示
        self.assertNotIn("appearance-value", text)         # 本轮只读：没有编辑框


class PanelContractTest(unittest.TestCase):
    """给壳的挂载点：routes(store, brain) -> [(method, path, handler)]。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 路由合同不依赖真实 QQ 通道是否在线；协议返回校验另有独立测试。
        channel_probe = mock.patch('api.observe.HTTPConnection')
        connection = channel_probe.start().return_value
        self.addCleanup(channel_probe.stop)
        connection.getresponse.return_value.status = 200
        def probe_request(method, path):
            data = ({'ok': True, 'channel': 'onebot-business', 'mode': 'send', 'core_enabled': False,
                     'started_at': '2026-11-09T00:00:00Z', 'core_timeout_ms': 45000,
                     'policy': None, 'gate_words': None, 'disk_matches': None}
                    if path == '/runtime' else {'channel': 'nonebot', 'bots': []})
            connection.getresponse.return_value.read.return_value = json.dumps(data).encode()
        connection.request.side_effect = probe_request
        # 枚举接口不应启动真实设备后台任务；实际会话另由手机测试验收。
        patcher = mock.patch("organs.day.phone_browse.start", return_value={"id":"contract-only", "status":"queued"})
        patcher.start()
        self.addCleanup(patcher.stop)
        # 合同检查不依赖真机连接；设备状态的成功/失败由 phone_api 测试覆盖。
        phone_status = mock.patch("organs.hand.phone.status", return_value={"ok": True, "connected": False})
        phone_status.start()
        self.addCleanup(phone_status.stop)
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
                elif path in HONEST_TWO_ANSWERS:
                    allowed = HONEST_TWO_ANSWERS[path]
                    self.assertIn(status, allowed,
                                  "%s 只许回 %s：409 是「改了就是改了」的如实回答，不是故障"
                                  % (path, allowed))
                    self.assertIsInstance(payload, dict, path)
                    for key in ("ok", "code"):
                        self.assertIn(key, payload, "%s 的两种回执都要带 %s" % (path, key))
                    if status == 409:
                        self.assertEqual("POINTER_STALE", payload.get("code"), path)
                        self.assertFalse(payload.get("ok"), path)
                        self.assertTrue(payload.get("stale"), "%s 回 409 就得说出是哪几份" % path)
                    else:
                        self.assertTrue(payload.get("ok"), path)
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

    def test_warehouse_may_honestly_answer_409(self):
        """单列：/home/warehouse 允许 200（aligned）或 409（POINTER_STALE），两种都带 ok 与 code，不许 500。

        两种结果都用临时家造出来，**不依赖实盘现状**——依赖实盘就等于让嘴或心改一次数据红一次，
        而那种红恰恰证明系统在正常工作。
        """
        base = Path(self._tmp.name) / "warehouse-case"
        root = base / "home"
        for name in ("玄关", "客厅", "名片盒", "工作台", "仓库", "信箱",
                     "卧室", "橱柜", "抽屉", "台历"):
            (root / name).mkdir(parents=True, exist_ok=True)
        target = base / "policy.json"
        target.write_text('{"v": 2}', encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        size = target.stat().st_size

        def write_pointer(sha: str, byte_count: int) -> None:
            (root / "仓库" / "index.jsonl").write_text(json.dumps({
                "card_id": "仓库-1", "room": "仓库", "kind": "借阅条", "name": target.name,
                "path": str(target), "sha256": sha, "bytes": byte_count, "chars": 0,
                "note": "指针卡", "by": "home", "role": "organ",
                "at": "2026-11-05T00:00:00+00:00", "params": {"pointer": True}},
                ensure_ascii=False) + "\n", encoding="utf-8")

        # 对齐那一支：200 + ok + code
        write_pointer(digest, size)
        status, payload = api.handle("GET", "/home/warehouse", None, str(root))
        self.assertEqual(200, status, payload)
        self.assertTrue(payload.get("ok"))
        self.assertEqual("ok", payload.get("code"))
        self.assertTrue(payload.get("aligned"))

        # 对不上那一支：409 + ok:false + code，且这两个键一个都不许少
        write_pointer("0" * 64, size + 1)
        status, payload = api.handle("GET", "/home/warehouse", None, str(root))
        self.assertEqual(409, status, payload)
        self.assertFalse(payload.get("ok"))
        self.assertEqual("POINTER_STALE", payload.get("code"))
        self.assertEqual(["policy.json"], payload.get("stale"))
        self.assertIn(digest[:12], payload.get("message", ""), "消息里要有现算的 12 位")
        self.assertIn("000000000000", payload.get("message", ""), "消息里要有卡上的 12 位")
        self.assertNotEqual(500, status)

    def test_honest_two_answers_never_grows_silently(self):
        """名单要长，得连带写出理由：只认 GET、必须在路由表上、且当前就这一条。"""
        self.assertEqual({"/home/warehouse"}, set(HONEST_TWO_ANSWERS),
                         "多一条「允许 409」的端点就是多一处诚实报告，理由要写进本文件")
        for path, allowed in HONEST_TWO_ANSWERS.items():
            self.assertEqual("GET", api.ROUTES.get(path), "%s 不是 GET 路由" % path)
            self.assertEqual((200, 409), allowed)

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
                         "心的 data 目录只许出厂文件（baseline-card.json / "
                         "engage_weights.json）；运行时落盘一律去临时根。多出来的是 %r："
                         "有测试拿了默认根（真实数据根）跑心，root= 必须指到临时目录"
                         % (extra,))
        self.assertEqual(ALLOWED_HEART_DATA, present,
                         "心的 data 目录少了出厂文件或说明文件：%r" % (sorted(present),))

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
        # 口径变更（2026-11-05 一页一席位）："/" 仍是魂页（直开核时 / 就是魂），
        # "/panel" 改成聚合页 panel.html（页里一条导航 + 一个 iframe，DSH 侧只有一个席位）。
        # 新增 /day、/settings 两张占位页（导航里点得到、打开不是 404）与 /panel-base.js
        # （MOUNT/BASE 的唯一一份推导，按 .js 发 text/javascript）。
        # 2026-11-06 信息架构重切：多 /about 与 /memory 两页（脑页里的名片与记忆各自
        # 独立成一页）。
        # 2026-11-06 脑页退场：那张静态页的登记（"/brain"）从这张表里删掉——内容已全部搬到
        # /about 与 /memory，留着就是第二份会走味的副本。地址不空着：它落回
        # api/brain.py 的 JSON 端点 GET /brain（见下面 DevserverHttpTest 那条）。
        self.assertEqual({"/": "soul.html", "/panel": "panel.html",
                          "/heart": "heart.html",
                          "/home": "home.html", "/day": "day.html",
                          "/settings": "settings.html", "/chat": "chat.html",
                          "/about": "about.html", "/memory": "memory.html",
                          "/galaxy": "galaxy.html", "/galaxy.js": "galaxy.js",
                          "/growth-panel.js": "growth-panel.js", "/model-settings.js": "model-settings.js",
                          "/observe": "observe.html",
                          "/eye": "eye.html",
                          "/voice": "voice.html",
                          "/ear": "ear.html",
                          "/studio": "studio.html",
                          "/learning": "learning.html",
                          "/expressions": "expressions.html",
                          "/memes": "memes.html",
                          "/phone": "phone.html",
                          "/panel-base.js": "panel-base.js"},
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

    def test_panel_path_serves_the_aggregation_page(self):
        # 口径变更（2026-11-05）：/panel 不再是 soul.html 的别名，是那条导航 + iframe 的
        # 聚合页（panel.html）；"/" 才是魂页。
        status, body, _ = self._get("/panel")
        self.assertEqual(200, status)
        self.assertEqual(devserver.read_page("panel.html"), body)

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
        self.assertNotIn(b'method: "POST"', body)
        self.assertIn(b'id="changes-refresh"', body)
        self.assertEqual(200, self._get("/heart/changes")[0])
        self.assertNotIn("未接入：这一层还没有代码".encode("utf-8"), body)

        # /brain 不再是一张页（2026-11-06 脑页退场）：这条地址回到 JSON 端点，
        # 断真话——回来的是 JSON 且自报 brain，不再是 HTML 页。
        status, body, ctype = self._get("/brain")
        self.assertEqual(200, status)
        self.assertIn("application/json", ctype, "/brain 该回到 JSON 端点了，不是静态页")
        self.assertEqual("brain", json.loads(body.decode("utf-8"))["agent"])
        self.assertNotIn(b"<!doctype html", body.lower(), "/brain 还在发页面字节")

        for path in ("/heart", "/brain"):
            for method in ("POST", "PUT", "DELETE"):
                status, body, ctype = self._request(method, path)
                self.assertEqual(405, status, "%s %s" % (method, path))
                self.assertIn("application/json", ctype)
                self.assertIn("只读", json.loads(body.decode("utf-8"))["message"])
        self.assertEqual(404, self._get("/brain/x")[0])


class RouteRegistryTest(unittest.TestCase):
    """路由表的汇总口径：九张器官表拼一张，同名键当场炸——静默覆盖正是拆包要止住的事。"""

    def test_route_table_is_the_merge_of_five_organ_tables(self):
        """汇总表 = 九张器官表逐键拼起来，一条不少、一条不多。

        口径变更（2026-11-05 心线落地）：心的七条只读端点成了独立一张表
        （heart_api.HEART_ROUTES），占位页表 PAGE_ROUTES 留空——所以这里是六张，不是五张。

        口径变更（2026-11-05 日子线第一波）：日子成了独立一张表（day_api.DAY_ROUTES，
        十条：tick/plan/night/log/schedule/schedule-draw/pending/deliver/ack/reconcile）
        ——所以这里是八张，不是六张（核自报身份那张一直在，从没算进"器官"里）。

        口径变更（2026-11-05 说话线）：说话成了独立一张表（chat_api.CHAT_ROUTES，
        一条 POST /chat）——所以这里是九张，不是八张。

        九张表各自守着一条器官线：soul_api.SOUL_ROUTES（魂）/ brain_api.BRAIN_ROUTES（脑）/
        page_api.PAGE_ROUTES（占位页，已空）/ mouth_api.MOUTH_ROUTES（嘴的干跑端点）/
        home_api.HOME_ROUTES（家：九条只读 + 一条 POST /home/write）/
        heart_api.HEART_ROUTES（心：七条只读）/
        day_api.DAY_ROUTES（日子：四条只读 + 六条 POST，内容归 schedule、送达归 delivery）/
        version_api.VERSION_ROUTES（核自报身份）/ chat_api.CHAT_ROUTES（说话：POST /chat）/
        hand_api.HAND_ROUTES（手：三条只读读数，记下事／标完成那两条动作没有 HTTP 入口）。
        """
        tables = (("soul_api.SOUL_ROUTES", core.soul_api.SOUL_ROUTES),
                  ("brain_api.BRAIN_ROUTES", core.brain_api.BRAIN_ROUTES),
                  ("page_api.PAGE_ROUTES", core.page_api.PAGE_ROUTES),
                  ("mouth_api.MOUTH_ROUTES", core.mouth_api.MOUTH_ROUTES),
                  ("home_api.HOME_ROUTES", core.home_api.HOME_ROUTES),
                  ("heart_api.HEART_ROUTES", core.heart_api.HEART_ROUTES),
                  ("day_api.DAY_ROUTES", core.day_api.DAY_ROUTES),
                  ("version_api.VERSION_ROUTES", core.version_api.VERSION_ROUTES),
                  ("chat_api.CHAT_ROUTES", core.chat_api.CHAT_ROUTES),
                  ("hand_api.HAND_ROUTES", core.hand_api.HAND_ROUTES),
                  ("observe_api.OBSERVE_ROUTES", core.observe_api.OBSERVE_ROUTES),
                  ("eye_api.EYE_ROUTES", core.eye_api.EYE_ROUTES),
                  ("voice_api.VOICE_ROUTES", core.voice_api.VOICE_ROUTES),
                  ("ear_api.EAR_ROUTES", core.ear_api.EAR_ROUTES),
                  ("studio_api.STUDIO_ROUTES", core.studio_api.STUDIO_ROUTES),
                  ("memes_api.MEME_ROUTES", core.memes_api.MEME_ROUTES),
                  ("model_settings_api.MODEL_ROUTES", core.model_settings_api.MODEL_ROUTES),
                  ("learning_api.LEARNING_ROUTES", core.learning_api.LEARNING_ROUTES),
                  ("phone_api.PHONE_ROUTES", core.phone_api.PHONE_ROUTES))
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
