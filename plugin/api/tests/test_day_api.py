"""日子的 HTTP 层：十条路由各认一个方法、方法错一律 405、未接入的路由回 501。

501 这一组不是手写名单：它由 api.day.unconnected_routes() 真去试导入推导出来，
schedule / delivery 一落地就自动变空，于是"必须 200"自动接管，不留永久例外。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

from api import api
from api.day import DAY_ROUTES, unconnected_routes
from organs.brain import BrainStore
from organs.day import model, plan

NOW = "2026-11-05T09:30:00"
# 本波只做壳的两条线：未接入的路由一定出自这两个模块（落地后 unconnected_routes() 会变空）
SUCCESSOR_MODULES = {"organs.day.schedule", "organs.day.delivery"}


def at(text: str) -> str:
    """把墙上时间钉到本机时区（与器官测试用同一套办法，换机器结论不变）。"""
    return model.iso(datetime.fromisoformat(text).replace(tzinfo=model.local_tz()))


class DayApiTest(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.heart = self.root / "heart"
        self.heart.mkdir()
        self.brain = BrainStore(self.root / "brain")

    def _query(self, **params: Any) -> str:
        pairs = {key: value for key, value in params.items() if value is not None}
        return ("?" + urlencode(pairs)) if pairs else ""

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
              query: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        target = path + self._query(root=str(self.root), **(query or {}))
        status, payload = api.handle(method, target, body, None, brain=self.brain)
        json.dumps(payload, ensure_ascii=False)
        return status, payload

    def test_ten_routes_registered_with_one_method_each(self):
        self.assertEqual(10, len(DAY_ROUTES))
        for route, method in DAY_ROUTES.items():
            self.assertIn(route, api.ROUTES, "日子线的表没并进 core.ROUTES")
            self.assertEqual(method, api.ROUTES[route], route)
        self.assertEqual({"POST", "GET"}, set(DAY_ROUTES.values()))

    def test_wrong_methods_are_405_with_day_wording(self):
        for route, method in DAY_ROUTES.items():
            for other in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                if other == method:
                    continue
                status, payload = self._call(other, route, {})
                self.assertEqual(405, status, "%s %s" % (other, route))
                self.assertIn("日子线", payload["message"])

    def test_unconnected_routes_are_derived_from_import_failure(self):
        """未接入名单由"真去试导入"推导：不是手写常量，模块落地就自动少一条。"""
        routes = unconnected_routes()
        self.assertEqual(len(routes), len(set(routes)), "名单里有重复")
        self.assertEqual(set(), set(routes) - set(DAY_ROUTES), "名单里有不存在的路由")
        for route in routes:
            method = DAY_ROUTES[route]
            status, payload = self._call(method, route,
                                         {"text": "一句话", "item_id": "m-1", "retcode": 0})
            self.assertEqual(501, status, "%s %s" % (method, route))
            self.assertIn("未接入", payload["message"])
            self.assertIn("未落地", payload["message"])
            self.assertIn(payload["module"], SUCCESSOR_MODULES)

    def test_routes_with_modules_are_never_in_the_unconnected_set(self):
        for route in ("/day/tick", "/day/plan", "/day/night", "/day/log"):
            self.assertNotIn(route, unconnected_routes())

    def test_tick_is_idempotent_over_http(self):
        first = self._call("POST", "/day/tick", query={"now": at(NOW)})
        second = self._call("POST", "/day/tick", query={"now": at(NOW)})
        self.assertEqual(200, first[0])
        self.assertEqual(1, len(first[1]["ensured"]["added"]), "第一次 tick 排当天那条 night")
        self.assertEqual([], second[1]["ensured"]["added"], "第二次一条都不新增")
        self.assertEqual(first[1]["plan_ids"], second[1]["plan_ids"])
        self.assertEqual(first[1]["counts"], second[1]["counts"])
        self.assertEqual(first[1]["rollovers"], second[1]["rollovers"])
        self.assertEqual(first[1]["next_tick_at"], second[1]["next_tick_at"])

    def test_plan_route_keeps_three_columns(self):
        status, payload = self._call("GET", "/day/plan")
        self.assertEqual(200, status)
        for key in ("pending", "consumed", "blocked", "expired"):
            self.assertEqual([], payload[key])
        self.assertEqual(0, payload["counts"]["total"])

    def test_plan_route_shows_a_blocked_candidate(self):
        plan.add([{"kind": "diary", "at": at("2026-11-05T09:00:00")}], root=self.root,
                 now_ts=at(NOW))
        plan.add([{"kind": "diary", "at": at("2026-11-05T09:03:00")}], root=self.root,
                 now_ts=at(NOW))
        status, payload = self._call("GET", "/day/plan")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["pending"]))
        self.assertEqual(["min_gap"], [row["reason"] for row in payload["blocked"]])

    def test_night_then_log_round_trip(self):
        status, payload = self._call("POST", "/day/night",
                                     query={"now": at(NOW), "heart_root": str(self.heart)})
        self.assertEqual(200, status)
        self.assertEqual(["brain_compact", "brain_decay", "home_diary", "heart_settle"],
                         list(payload["steps"]))
        self.assertEqual(1, payload["written"])
        status, tail = self._call("GET", "/day/log", query={"limit": 5})
        self.assertEqual(200, status)
        self.assertEqual(1, tail["count"])
        self.assertIn("steps", tail["lines"][0])

    def test_tick_runs_the_due_night_over_http(self):
        """到点的夜由 tick 真跑一次并 consume：HTTP 上也是同一条闭环。"""
        status, payload = self._call("POST", "/day/tick",
                                     query={"now": at("2026-11-05T03:31:00"),
                                            "heart_root": str(self.heart)})
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["night"]["executed"]))
        self.assertEqual({"ok": 2, "stubbed": 2},
                         payload["night"]["executed"][0]["summary"]["statuses"])
        status, plan_payload = self._call("GET", "/day/plan")
        self.assertEqual(1, len(plan_payload["consumed"]))
        status, tail = self._call("GET", "/day/log", query={"limit": 5})
        self.assertEqual(1, tail["count"])

    def test_night_break_step_is_reported_per_step(self):
        status, payload = self._call("POST", "/day/night",
                                     query={"now": at(NOW), "heart_root": str(self.heart),
                                            "break_step": 2})
        self.assertEqual(200, status)
        self.assertEqual("failed", payload["steps"]["brain_decay"]["status"])
        self.assertEqual("ok", payload["steps"]["heart_settle"]["status"])

    def test_tick_break_step_keeps_the_plan_pending(self):
        """验收注入 break_step 从 tick 透传：失败的那次不下账，返回体里看得见 retried。"""
        status, payload = self._call("POST", "/day/tick",
                                     query={"now": at("2026-11-05T03:31:00"),
                                            "heart_root": str(self.heart), "break_step": 2})
        self.assertEqual(200, status)
        self.assertEqual([], payload["night"]["executed"])
        self.assertEqual(1, len(payload["night"]["retried"]))
        self.assertEqual(["brain_decay"], payload["night"]["retried"][0]["failed_steps"])
        self.assertEqual([], payload["night"]["never_ran"])
        status, plan_payload = self._call("GET", "/day/plan")
        self.assertEqual(1, len(plan_payload["pending"]))
        self.assertEqual(0, plan_payload["pending"][0]["night"]["attempts"])
        self.assertEqual([], plan_payload["consumed"], "失败的那次不许静默 consume")

    def test_plan_route_exposes_the_never_ran_column(self):
        """/day/plan 的第五栏：试满 4 次后那条进 never_ran，之后再 tick 条数不变。"""
        query = {"now": at("2026-11-05T03:31:00"), "heart_root": str(self.heart),
                 "break_step": 2}
        for _ in range(4):
            status, _ = self._call("POST", "/day/tick", query=query)
            self.assertEqual(200, status)
        status, plan_payload = self._call("GET", "/day/plan")
        self.assertEqual(200, status)
        self.assertIn("never_ran", plan_payload)
        self.assertEqual([], plan_payload["pending"])
        self.assertEqual(1, len(plan_payload["never_ran"]))
        self.assertIn("brain_decay", plan_payload["never_ran"][0]["reason"])
        self.assertIn("4 次尝试", plan_payload["never_ran"][0]["reason"])
        status, again = self._call("POST", "/day/tick", query=query)
        self.assertEqual(200, status)
        self.assertEqual(plan_payload["counts"], again["counts"])
        self.assertEqual(plan_payload["plan_ids"], again["plan_ids"])
        self.assertEqual(1, len(self._call("GET", "/day/plan")[1]["never_ran"]))

    def test_schedule_takes_now_and_writes_nothing(self):
        """只读端点：?now= 跟着注入时刻走（day / slot 都变），不结算心、不落日志。"""
        before = sorted(path.name for path in self.root.iterdir())
        status, morning = self._call("GET", "/day/schedule",
                                     query={"now": at("2026-11-05T09:30:00")})
        self.assertEqual(200, status)
        status, evening = self._call("GET", "/day/schedule",
                                     query={"now": at("2026-11-05T23:30:00")})
        self.assertEqual(200, status)
        status, next_day = self._call("GET", "/day/schedule",
                                      query={"now": at("2026-11-06T05:00:00")})
        self.assertEqual(200, status)
        self.assertEqual("2026-11-05", morning["day"])
        self.assertEqual("2026-11-05", evening["day"], "23:30 还没到 04:00 换日点")
        self.assertEqual("2026-11-06", next_day["day"], "05:00 已经是新的一天")
        self.assertEqual(at("2026-11-05T09:30:00"), morning["now"])
        self.assertNotEqual(morning["slot"], evening["slot"], "slot 跟着注入时刻走")
        self.assertEqual(before, sorted(path.name for path in self.root.iterdir()),
                         "GET /day/schedule 不落任何文件")

    def test_bad_now_is_400_and_points_at_the_plus_trap(self):
        status, payload = self._call("POST", "/day/tick", query={"now": "2026-11-05T09:00:00 09:00"})
        self.assertEqual(400, status)
        self.assertIn("%2B", payload["message"])

    def test_relative_root_is_ignored_not_trusted(self):
        """root 只接受绝对路径的目录：相对路径按默认根处理（并 WARN），不写任意路径。"""
        with self.assertWarns(RuntimeWarning):
            status, payload = api.handle("GET", "/day/plan?root=不是绝对路径", None, None)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

    def test_deliver_without_text_never_writes(self):
        """缺正文的 deliver：模块没落地回 501，落地了就是 400——两种都不许落盘。"""
        before = sorted(path.name for path in self.root.iterdir())
        status, payload = self._call("POST", "/day/deliver", {})
        if "/day/deliver" in unconnected_routes():
            self.assertEqual(501, status)
            self.assertIn("未接入", payload["message"])
        else:
            self.assertEqual(400, status)
            self.assertIn("text", payload["message"])
        self.assertEqual(before, sorted(path.name for path in self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
