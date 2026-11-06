"""手的三个只读端点：结构、405、坏 now 的 400、limit 夹边界，以及「一个字节都不写」。

判据（第四轮手线：手这个器官已经落地，这里只把它的读数搬上只读面）：
- 三条路由全是 GET；写方法在 core.handle 就被 405 挡掉，措辞要点名「只收 GET」；
- /hand 的判据快照只从手的常量派生（actions／risk／needs_grant），额度只调
  quota.remaining——本文件不自己数额度；
- /hand/tasks 的 tasks 用家折好的卡（done 是折出来的），due 用 actions.due，
  「到点没做」的判据只在手那一处；
- /hand/ledger 的 limit 默认 50、夹在 1–200，0 与 999 都夹回边界不炸；
- 查询串里时区偏移的 '+' 必须写成 %2B：裸 '+' 被 parse_qs 解成空格，当场 400 并点名原值；
- **三个端点跑完，临时实盘一个字节都没变**（对比文件列表／大小／sha256）——这是本文件
  最要紧的一条：只读不是靠自觉，是靠这条断言看住。

一律用临时根：root= 指向临时目录里的 hand/（家的卡是它的兄弟目录 home/），
所以既不会往仓里的实盘写一个字节，也不会读实盘的那一本账。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import warnings
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlencode

from api import api
from api import core as api_core
from api import hand as hand_api
from organs.hand import actions
from organs.hand import permit as hand_permit

PLUGIN = Path(__file__).resolve().parents[2]

#: 固定的「现在」：用例不看本机时钟（与 organs/hand/tests 同一把挂钟）。
NOW = "2026-11-06T09:00:00+09:00"
#: 明天中午（还没到点）。
LATER = "2026-11-07T12:00:00+09:00"
#: 一小时前（到点没做）。
PAST = "2026-11-06T08:00:00+09:00"
WHO = "阿禾"
LIMIT = 3


def _snapshot(directory: Path) -> List[Tuple[str, int, str]]:
    """目录树的指纹：相对路径 + 字节数 + sha256（目录记成 "…/"，没有内容）。"""
    rows: List[Tuple[str, int, str]] = []
    for path in sorted(directory.rglob("*")):
        rel = path.relative_to(directory).as_posix()
        if path.is_dir():
            rows.append((rel + "/", 0, ""))
        else:
            rows.append((rel, path.stat().st_size,
                         hashlib.sha256(path.read_bytes()).hexdigest()))
    return rows


class HandApiTest(unittest.TestCase):
    """公共夹具：临时实盘（手 hand/ + 家 home/）+ 两件事 + 一份额度配置。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "hand"
        self.root.mkdir()
        (self.root / "limits.json").write_text(
            json.dumps({"version": 1, "per_person_per_day": LIMIT}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        # 造数据只经手自己的动作（卡落家的工作台、账落手的手账）——本文件不碰别的器官。
        self.add("交房租", LATER)
        self.add("取快递", PAST)

    def add(self, title: str, due: str) -> Dict[str, Any]:
        out = actions.add_task(who=WHO, title=title, due=due, now=NOW, root=self.root)
        self.assertEqual("done", out["state"], "造数据失败：%s" % out)
        return out

    def get(self, path: str, **params: Any) -> Tuple[int, Dict[str, Any]]:
        """打一条 GET；root 一律指向临时根，payload 必须能 json.dumps。"""
        query = urlencode(dict((key, str(value)) for key, value in params.items()))
        if query:
            path = "%s?%s" % (path, query)
        status, payload = api.handle("GET", path, None, None)
        json.dumps(payload, ensure_ascii=False)
        return status, payload


class RouteTableTest(HandApiTest):
    def test_three_routes_are_registered_and_all_get(self):
        """接口冻结：三条路由全 GET，且进了核的那一张表（多一条少一条都算改了接口）。"""
        self.assertEqual({"/hand": "GET", "/hand/tasks": "GET", "/hand/ledger": "GET"},
                         hand_api.HAND_ROUTES)
        for route, method in hand_api.HAND_ROUTES.items():
            self.assertIn(route, api_core.ROUTES, "路由表里没有 %s" % route)
            self.assertEqual(method, api_core.ROUTES[route], route)
        self.assertEqual(set(hand_api.HAND_ROUTES),
                         {path for path in api_core.ROUTES if path.startswith("/hand")},
                         "手的路由就是 HAND_ROUTES 那一张表")

    def test_unknown_hand_path_is_404(self):
        status, payload = api.handle("GET", "/hand/nope", None, None)
        self.assertEqual(404, status)
        self.assertFalse(payload["ok"])


class SnapshotTest(HandApiTest):
    def test_get_hand_returns_full_shape(self):
        status, payload = self.get("/hand", who=WHO, now=NOW, root=self.root)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(["add_task", "complete_task"], payload["actions"])
        self.assertEqual({"add_task", "complete_task"}, set(payload["risk"]))
        for level in payload["risk"].values():
            self.assertIn(level, hand_permit.RISK_LEVELS)
        self.assertEqual({"add_task": False, "complete_task": False}, payload["needs_grant"])
        self.assertEqual(WHO, payload["who"])
        self.assertEqual({"limit": LIMIT, "used": 2, "remaining": LIMIT - 2,
                          "source": "live"}, payload["quota"])
        self.assertTrue(payload["limits_note"])

    def test_needs_grant_and_risk_follow_the_permit_table(self):
        """快照里的风险档与授权栏都是从 permit 派生的，不是本层抄一份常量。"""
        _status, payload = self.get("/hand", root=self.root)
        for action in payload["actions"]:
            self.assertEqual(hand_permit.risk_of(action), payload["risk"][action])
            self.assertEqual(hand_permit.needs_grant(action), payload["needs_grant"][action])

    def test_who_defaults_to_empty_string_not_400(self):
        """没人点名也算得出一份额度读数：这是只读查询，缺 who 不是坏请求。"""
        status, payload = self.get("/hand", root=self.root)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual("", payload["who"])
        self.assertEqual(0, payload["quota"]["used"], "空串不该继承别人的用量")
        self.assertEqual(LIMIT, payload["quota"]["limit"])

    def test_who_is_the_query_value_verbatim(self):
        status, payload = self.get("/hand", who=" 阿禾 ", root=self.root)
        self.assertEqual(200, status)
        self.assertEqual(" 阿禾 ", payload["who"], "who 要原样回报，不做名字归一化")


class TasksTest(HandApiTest):
    def test_tasks_and_due_have_the_frozen_shape(self):
        status, payload = self.get("/hand/tasks", now=NOW, root=self.root)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(NOW, payload["now"], "now 要回显解析后的那一刻")
        self.assertEqual(["交房租", "取快递"],
                         sorted(card["name"] for card in payload["tasks"]))
        self.assertEqual(2, payload["count"])
        self.assertEqual(["取快递"], [card["name"] for card in payload["due"]])
        self.assertEqual(1, payload["due_count"])
        for card in payload["tasks"]:
            self.assertFalse(card["done"], "tasks 只要还没做完的卡")
            self.assertEqual("工作台", card["room"])
            self.assertIn("due", card["params"])

    def test_now_drives_due_without_writing_anything(self):
        """把现在往回拨一小时，那张卡就还没到点——判据只看时间戳，不看落盘。"""
        earlier = "2026-11-06T07:00:00+09:00"
        _status, payload = self.get("/hand/tasks", now=earlier, root=self.root)
        self.assertEqual([], payload["due"])

    def test_completed_task_leaves_tasks_and_due(self):
        """done 是家折出来的：标完完成，这张卡就不该再出现在「未完成」里。"""
        actions.complete_task(who=WHO, card_id="工作台-2", now=NOW, root=self.root)
        _status, payload = self.get("/hand/tasks", now=NOW, root=self.root)
        self.assertEqual(["交房租"], [card["name"] for card in payload["tasks"]])
        self.assertEqual(1, payload["count"])
        self.assertEqual([], payload["due"])
        self.assertEqual(0, payload["due_count"])


class LedgerTest(HandApiTest):
    def test_tail_is_folded_actions_not_raw_lines(self):
        """两次 add_task 在盘上是四行，折叠之后是两个动作——limit 数的是动作。"""
        status, payload = self.get("/hand/ledger", limit=5, root=self.root)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(2, payload["count"])
        self.assertEqual(len(payload["rows"]), payload["count"])
        for row in payload["rows"]:
            self.assertEqual("add_task", row["action"])
            self.assertEqual("done", row["state"])
            self.assertTrue(row["ledger_id"])

    def test_limit_is_clamped_between_one_and_two_hundred(self):
        _status, low = self.get("/hand/ledger", limit=0, root=self.root)
        self.assertEqual(1, low["limit"], "limit=0 要夹回 1")
        self.assertEqual(1, low["count"])
        _status, high = self.get("/hand/ledger", limit=999, root=self.root)
        self.assertEqual(200, high["limit"], "limit=999 要夹回 200")
        self.assertEqual(2, high["count"], "账里只有两个动作，夹到 200 也还是两个")
        _status, junk = self.get("/hand/ledger", limit="abc", root=self.root)
        self.assertEqual(hand_api.DEFAULT_LEDGER_LIMIT, junk["limit"])
        self.assertEqual(2, junk["count"])


class MethodAndNowTest(HandApiTest):
    def test_every_hand_route_rejects_post_with_a_human_message(self):
        for route in hand_api.HAND_ROUTES:
            status, payload = api.handle("POST", route, {}, None)
            self.assertEqual(405, status, route)
            self.assertFalse(payload["ok"])
            self.assertIn("只收 GET", payload["message"], route)
            self.assertIn(route, payload["message"], "405 要点名是哪一条路由")

    def test_bare_plus_in_now_is_400_and_names_the_value(self):
        """裸 '+' 被 parse_qs 解成空格：当场 400，把收到的值原样摆出来，不穿成 traceback。"""
        for route in ("/hand", "/hand/tasks"):
            status, payload = api.handle(
                "GET", route + "?now=2026-11-06T12:00:00+08:00", None, None)
            self.assertEqual(400, status, route)
            self.assertFalse(payload["ok"])
            self.assertIn("2026-11-06T12:00:00 08:00", payload["message"],
                          "坏值要原样点名（空格版）")
            self.assertIn("%2B", payload["message"], "要告诉人正确的写法")

    def test_percent_encoded_plus_in_now_is_accepted(self):
        """同一个时刻写成 %2B 就是对的：这条与上一条一起钉住「怎么改」。"""
        status, payload = api.handle(
            "GET", "/hand?now=2026-11-06T12:00:00%2B08:00&root=" + str(self.root), None, None)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

    def test_now_without_timezone_is_400_not_guessed(self):
        status, payload = api.handle("GET", "/hand/tasks?now=2026-11-06T12:00:00", None, None)
        self.assertEqual(400, status)
        self.assertIn("2026-11-06T12:00:00", payload["message"])


class RootGateTest(HandApiTest):
    def test_relative_root_is_ignored_with_a_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            status, payload = self.get("/hand", root="relative/path")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, len([item for item in caught
                                 if issubclass(item.category, RuntimeWarning)]),
                         "忽略 root 要留一行 WARN")

    def test_root_inside_the_repo_is_ignored_with_a_warning(self):
        """仓库内却不是 <仓库>/home 下：那是出厂参数目录，查询串指不进自己的盘。"""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            status, payload = self.get("/hand", root=str(PLUGIN / "organs" / "hand"))
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, len([item for item in caught
                                 if issubclass(item.category, RuntimeWarning)]))


class ReadOnlyTest(HandApiTest):
    def test_three_endpoints_write_nothing_at_all(self):
        """三个端点跑完，临时实盘一个字节都没变（列表／大小／sha256 逐项对比）。"""
        before = _snapshot(self.tmp)
        for path in ("/hand", "/hand/tasks", "/hand/ledger"):
            status, payload = self.get(path, now=NOW, who=WHO, limit=200, root=self.root)
            self.assertEqual(200, status, path)
            self.assertTrue(payload["ok"], path)
        after = _snapshot(self.tmp)
        self.assertEqual(before, after, "只读端点改动了盘上的东西")

    def test_endpoints_do_not_add_ledger_rows(self):
        """手账行数也不许变：三个端点各打三次，盘上还是四条（两动作 × reserved+done）。"""
        ledger = self.root / "ledger.jsonl"
        before = ledger.read_text(encoding="utf-8").splitlines()
        for _round in range(3):
            for path in ("/hand", "/hand/tasks", "/hand/ledger"):
                self.get(path, now=NOW, who=WHO, root=self.root)
        self.assertEqual(before, ledger.read_text(encoding="utf-8").splitlines())

    def test_endpoints_do_not_create_the_home_room(self):
        """读 /hand/tasks 不会顺手建家的目录：缺件就是缺件，读不造根。"""
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        bare = Path(other.name) / "hand"
        bare.mkdir()
        status, payload = self.get("/hand/tasks", now=NOW, root=bare)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual([], payload["tasks"])
        self.assertEqual([], payload["due"])
        self.assertFalse((Path(other.name) / "home").exists(), "读端点不许替她建家的目录")


if __name__ == "__main__":
    unittest.main()
