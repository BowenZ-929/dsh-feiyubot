"""日子的 HTTP 层：十条路由各认一个方法、方法错一律 405、未接入的路由回 501。

501 这一组不是手写名单：它由 api.day.unconnected_routes() 真去试导入推导出来，
schedule / delivery 一落地就自动变空，于是"必须 200"自动接管，不留永久例外。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest import mock
from urllib.parse import urlencode

from api import api
from api.day import DAY_ROUTES, unconnected_routes
from organs.brain import BrainStore
from organs.day import model, plan, proactive_ask
from organs.day.tests.helpers import offline_night, seed_today
from organs.heart import model as heart_model
from organs.heart import proactive as heart_proactive

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
        # 夜在 03:31 跑那一条（换日点 04:00 前，算前一天）：素材要落在那一夜里
        seed_today(self.brain, at("2026-11-05T03:31:00"))
        # 第三、四步会真写抽屉、真调核：把家的根换到临时目录，把核换成假的
        # （这两层进程内直接调 night.run，没有 mind 注入点，只能 patch）
        self.home = offline_night(self)

    def _query(self, **params: Any) -> str:
        pairs = {key: value for key, value in params.items() if value is not None}
        return ("?" + urlencode(pairs)) if pairs else ""

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
              query: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        target = path + self._query(root=str(self.root), **(query or {}))
        status, payload = api.handle(method, target, body, None, brain=self.brain)
        json.dumps(payload, ensure_ascii=False)
        return status, payload

    def test_life_routes_read_configure_generate_and_adjust(self):
        from organs.day import life
        now = at(NOW)
        status, empty = self._call("GET", "/day/life", query={"now": now})
        self.assertEqual(200, status)
        self.assertIsNone(empty["plan"])
        self.assertEqual(200, self._call("POST", "/day/life/config", {"enabled": True})[0])
        self.assertEqual(400, self._call("POST", "/day/life/config", {"enabled": "true"})[0])
        self.assertEqual(400, self._call("GET", "/day/life", query={"day": "../no"})[0])
        raw = {"title": "日常", "items": [{"start": 0, "end": 720, "activity": "读书"},
                                          {"start": 720, "end": 1440, "activity": "休息"}]}
        soul = mock.Mock()
        soul.render.return_value = "喜欢看书"
        with mock.patch.object(life.llm, "complete", return_value=json.dumps(raw)):
            status, made = api.handle("POST", "/day/life/generate" + self._query(root=str(self.root), now=now),
                                     {"day": "2026-11-05"}, soul, brain=self.brain)
        self.assertEqual(200, status)
        self.assertEqual("ready", made["status"])
        status, edited = self._call("POST", "/day/life/adjust",
            {"day": "2026-11-05", "revision": 1, "start": made["items"][0]["start"], "activity": "听歌"},
            query={"now": now})
        self.assertEqual(200, status)
        self.assertEqual(2, edited["revision"])

    def test_nightly_writing_uses_current_soul_for_diary_review_and_dream(self):
        from api.tests.helpers import copy_fixture, read_json, write_json
        from organs.soul import load
        from organs.day import night
        from organs.day.tests.helpers import article
        soul_root = copy_fixture("valid", str(self.root))
        soul = load(soul_root)
        # store 已载入后改卡，确认夜间同样读热更新，不读另一份固定人设。
        identity = read_json(soul_root / "identity.json")
        identity["likes"] = "喜欢青色的小纸船，话少，慢慢想。"
        write_json(soul_root / "identity.json", identity)
        requests = []
        def complete(messages, **kwargs):
            requests.append(messages)
            return json.dumps({"text": article(), "changes": []}) if kwargs.get("purpose") == "日记事实复核" else article()
        with mock.patch.object(night, "complete", side_effect=complete):
            status, payload = api.handle("POST", "/day/tick" + self._query(root=str(self.root), now=at("2026-11-05T03:31:00")),
                {}, soul, brain=self.brain)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["night"]["executed"]))
        self.assertEqual(3, len(requests))
        for messages in requests:
            self.assertIn(identity["likes"], messages[0]["content"])
            self.assertNotIn(identity["likes"], messages[1]["content"], "性情不冒充经历素材")

    def test_day_routes_registered_with_one_method_each(self):
        self.assertEqual(24, len(DAY_ROUTES))
        self.assertEqual("POST", DAY_ROUTES["/day/proactive/enable"])
        self.assertEqual("POST", DAY_ROUTES["/day/proactive/disable"])
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
        for route in ("/day/tick", "/day/plan", "/day/night", "/day/log",
                      "/day/proactive", "/day/proactive/consider",
                      "/day/proactive/outcome", "/day/solitude",
                      "/day/solitude/consider"):
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

    def test_solitude_route_reads_without_writing(self):
        """GET /day/solitude：三档阈值 / 两个档位 / 闲置分钟的读数 + idle 账尾，纯读不落账。"""
        status, payload = self._call("GET", "/day/solitude", query={"now": at(NOW)})
        self.assertEqual(200, status)
        self.assertIn("threshold_minutes", payload)
        self.assertEqual(900, payload["awake_minutes"])
        self.assertIn("idle_minutes", payload)
        self.assertEqual([], payload["log"], "GET 不该写 idle 账")

    def test_solitude_consider_refuses_two_drafts_in_one_trigger(self):
        """一次只做一件：同一次塞两张草稿 → 400，并且说清为什么（不是默默只做第一张）。"""
        status, payload = self._call("POST", "/day/solitude/consider",
                                     {"drafts": [{"name": "草稿一", "note": "第一张"},
                                                 {"name": "草稿二", "note": "第二张"}]},
                                     query={"now": at(NOW)})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("一次只做一件", payload["message"])
        self.assertIn("2 张草稿", payload["message"])

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
        self.assertEqual(["brain_compact", "brain_decay", "home_diary", "home_dream",
                          "heart_settle"], list(payload["steps"]))
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
        self.assertEqual({"ok": 4, "stubbed": 1},
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
        # 只数夜那一条：tick 现在末尾还会动念一次，主动开口的计划另算（见下一条测试）
        night = [row for row in plan_payload["pending"] if row["kind"] == "night"]
        self.assertEqual(1, len(night))
        self.assertEqual(0, night[0]["night"]["attempts"])
        self.assertEqual([], plan_payload["consumed"], "失败的那次不许静默 consume")

    def test_plan_route_exposes_the_never_ran_column(self):
        """/day/plan 的第五栏：试满 4 次后那条进 never_ran，之后再 tick 条数不变。"""
        # 退避 1 → 5 → 15 分钟：四次尝试分别在 T / T+1 / T+6 / T+21
        for now in ("2026-11-05T03:31:00", "2026-11-05T03:32:00",
                    "2026-11-05T03:37:00", "2026-11-05T03:52:00"):
            status, _ = self._call("POST", "/day/tick",
                                   query={"now": at(now), "heart_root": str(self.heart),
                                          "break_step": 2})
            self.assertEqual(200, status)
        status, plan_payload = self._call("GET", "/day/plan")
        self.assertEqual(200, status)
        self.assertIn("never_ran", plan_payload)
        self.assertEqual([], [row for row in plan_payload["pending"]
                              if row["kind"] == "night"],
                         "夜那一条进了 never_ran，不在 pending 里了")
        self.assertEqual(1, len(plan_payload["never_ran"]))
        self.assertIn("brain_decay", plan_payload["never_ran"][0]["reason"])
        self.assertIn("4 次尝试", plan_payload["never_ran"][0]["reason"])
        status, again = self._call("POST", "/day/tick",
                                   query={"now": at("2026-11-05T03:53:00"),
                                          "heart_root": str(self.heart), "break_step": 2})
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

    def test_plan_route_shows_required_on_every_row(self):
        """/day/plan 每行带 required：人一眼看出这条受不受额度管。"""
        capped = {"day_cutover_hour": 4, "min_gap_minutes": 10, "daily_cap": 1,
                  "kinds": {"night": {"at": "03:30", "required": True},
                            "greet": {"at": "09:00"}}}
        plan.add([{"kind": "night", "at": at("2026-11-05T03:30:00")}], root=self.root,
                 config=capped)
        plan.add([{"kind": "greet", "at": at("2026-11-05T09:00:00")}], root=self.root,
                 config=capped)
        plan.add([{"kind": "greet", "at": at("2026-11-05T10:00:00")}], root=self.root,
                 config=capped)
        status, payload = self._call("GET", "/day/plan")
        self.assertEqual(200, status)
        self.assertEqual([True, False], [row["required"] for row in payload["pending"]])
        self.assertEqual(["fuse-tripped"], [row["reason"] for row in payload["blocked"]])
        self.assertEqual([False], [row["required"] for row in payload["blocked"]])

    # ---- 主动开口：三条新路由（读 / 动念 / 结算回执） ----------------------

    def test_default_proactive_routes_read_home_and_explicit_root_stays_isolated(self):
        from organs.home import cards
        from organs.day.tests.test_proactive import home_at
        ident = cards.append_cards(root=self.home, role='agent', by='agent', cards=[
            {'kind': '物件', 'room': '工作台', 'name': '五分钟后的约定', 'note': '',
             'params': {'task': True, 'due': at('2026-11-05T09:35:00')}}])['cards'][0]['card_id']
        home_at(self.root)  # 显式测试树里的家为空，不能借用环境变量中的家。
        self.brain.today_entries(at(NOW))
        env = {'DAY_ROOT': str(self.root), 'HEART_ROOT': str(self.heart), 'HOME_ROOT': str(self.home)}
        with mock.patch.dict(os.environ, env):
            before = {p: p.read_bytes() for base in (self.root, self.home)
                      for p in base.rglob('*') if p.is_file()}
            status, default = api.handle('GET', '/day/proactive' + self._query(now=at(NOW)),
                                        None, None, brain=self.brain)
            self.assertEqual(200, status)
            self.assertTrue(default['pending_task']['ok'], default['pending_task'])
            self.assertEqual(ident, default['pending_task']['fired'])
            _, isolated = self._call('GET', '/day/proactive', query={'now': at(NOW)})
            self.assertTrue(isolated['pending_task']['ok'])
            self.assertEqual([], isolated['pending_task']['cards'])
            _, explicit = self._call('GET', '/day/proactive', query={'now': at(NOW), 'home_root': str(self.home)})
            self.assertEqual(ident, explicit['pending_task']['fired'])
            self.assertEqual(before, {p: p.read_bytes() for base in (self.root, self.home)
                                     for p in base.rglob('*') if p.is_file()})
            status, considered = api.handle('POST', '/day/proactive/consider' + self._query(now=at(NOW)),
                                           {}, None, brain=self.brain)
            self.assertEqual(200, status)
            self.assertEqual('pending_task', considered['reason_code'])
            self.assertEqual(ident, considered['row']['card_id'])
            self.assertEqual(ident, considered['plan']['task']['card_id'])

    def test_proactive_reads_without_writing(self):
        """GET /day/proactive：账尾 + 此刻的读数，一个字节都不落盘（GET 不写）。"""
        before = sorted(path.name for path in self.root.iterdir())
        status, payload = self._call("GET", "/day/proactive", query={"now": at(NOW)})
        self.assertEqual(200, status)
        self.assertEqual(before, sorted(path.name for path in self.root.iterdir()))
        for key in ("who", "priority", "quota", "muted", "streak", "log", "counts"):
            self.assertIn(key, payload)
        self.assertEqual([], payload["log"])
        self.assertEqual({"before", "after"}, set(payload["priority"]["unfinished"]))

    def test_consider_makes_one_plan_that_the_plan_route_can_read(self):
        """动念 → 一行动念账 + 一条计划，priority 与 who 在 /day/plan 上读得出来。"""
        self.brain.ingest(who="qq:3425635473", text="晚上有空吗", ts=at(NOW))
        status, payload = self._call("POST", "/day/proactive/consider",
                                     query={"now": at(NOW)})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["fired"])
        self.assertEqual("qq:3425635473", payload["who"])
        self.assertEqual("planned", payload["action"])
        status, plan_payload = self._call("GET", "/day/plan")
        planned = [row for row in plan_payload["pending"] if row["kind"] == "proactive"]
        self.assertEqual(1, len(planned))
        self.assertEqual("qq:3425635473", planned[0]["who"])
        self.assertIsInstance(planned[0]["priority"], int)
        self.assertFalse(planned[0]["held"])
        status, tail = self._call("GET", "/day/proactive", query={"now": at(NOW)})
        row = tail["log"][-1]
        self.assertEqual("unfinished", row["reason_code"])
        self.assertIn("晚上有空吗", row["because"])
        self.assertEqual({"planned": 1}, tail["counts"])

    def test_consider_at_night_keeps_the_plan_but_calls_nothing(self):
        """夜里判 SILENT：计划仍然生成、标 held，核一次都不调、一条也不发。"""
        self.brain.ingest(who="qq:3425635473", text="晚上有空吗", ts=at("2026-11-05T23:00:00"))
        with mock.patch.object(proactive_ask, "ask",
                               side_effect=AssertionError("夜里不许调核")) as asked:
            status, payload = self._call("POST", "/day/proactive/consider",
                                         query={"now": at("2026-11-05T23:30:00")})
        self.assertEqual(200, status, payload)
        self.assertEqual(0, asked.call_count)
        self.assertEqual("held", payload["action"])
        self.assertEqual({"state": heart_model.STATE_SILENT,
                          "reason": heart_model.REASON_NIGHT}, payload["row"]["engage"])
        status, plan_payload = self._call("GET", "/day/plan")
        held = [row for row in plan_payload["pending"] if row["kind"] == "proactive"]
        self.assertEqual(1, len(held), "判 SILENT 仍然生成计划")
        self.assertTrue(held[0]["held"])
        status, queue = self._call("GET", "/day/pending")
        self.assertEqual([], queue["items"], "没发送")

    def test_outcome_without_result_is_400(self):
        status, payload = self._call("POST", "/day/proactive/outcome", {})
        self.assertEqual(400, status)
        self.assertIn("result", payload["message"])
        status, payload = self._call("POST", "/day/proactive/outcome", {"result": "随便"})
        self.assertEqual(400, status)
        self.assertIn("replied", payload["message"])
        self.assertFalse((self.root / "proactive_log.jsonl").exists(), "400 不许落账")

    def test_outcome_lands_cold_once_per_mute(self):
        """连三次没人接：streak=3、静音、动念账上多一行 cold；第二次不再落。"""
        for _ in range(3):
            heart_proactive.sent(self.heart, at(NOW))
            status, payload = self._call("POST", "/day/proactive/outcome",
                                        {"result": "ignored", "who": "qq:3425635473"},
                                        query={"now": at(NOW)})
            self.assertEqual(200, status, payload)
        self.assertEqual(3, payload["readings"]["streak"])
        self.assertTrue(payload["readings"]["muted"])
        self.assertIsNotNone(payload["cold"])
        status, tail = self._call("GET", "/day/proactive", query={"now": at(NOW)})
        self.assertEqual(1, tail["counts"]["cold"])
        heart_proactive.sent(self.heart, at(NOW))
        status, again = self._call("POST", "/day/proactive/outcome",
                                   {"result": "ignored"}, query={"now": at(NOW)})
        self.assertEqual(200, status)
        self.assertIsNone(again["cold"], "同一段静音只落一次 cold")
        status, tail = self._call("GET", "/day/proactive", query={"now": at(NOW)})
        self.assertEqual(1, tail["counts"]["cold"])

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
