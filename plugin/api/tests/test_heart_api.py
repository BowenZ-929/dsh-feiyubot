"""心的十个只读端点：形状、405 措辞、坏卡不假装、轨迹、干跑、偏移键集、唯一合成点、窗口、
睡眠账与意图排序。

判据（architecture §9.2：心与脑只能看）：
- 九条路由全是 GET，写方法一律 405，且心的措辞点名「只收 GET」；
- 卡坏了 HTTP 仍是 200，但 ok 必须是 False、issues 必须非空——页面要能显示；
  500 等于把「她没载入」藏起来，假装成功更是骗人；
- /heart/engage 是干跑：只调 decide，一个字节都不落盘（日志文件不许出现）；
- /heart/offset 每轴四个键冻结（value/decayed_value/day_delta/clamped），多出来的原样透传，
  不做任何半衰换算（offset 现在是天尺）。
- 合成只有一处（organs/heart/effective.py）：/heart/effective 是三块的原文，
  /heart/overview 的 effective 块、/heart/mood 的两个数与档位词、/heart/engage 的
  readouts.mood_now 都从那里取——三处取值必须逐字相同，只比两方不算数。

一律用临时根：mood.read 读时往轨迹账本追加一行、energy.read 读时把结算写回——这是它们的
设计（「读时算」与「重启不重复扣」就靠这个），所以拿默认根跑一次就会往仓里的 data 目录留
文件。本文件每个用例都把 root= 指到临时目录（root 只收绝对路径且存在的目录）。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from api import api
from api import heart as heart_api
from organs.heart import card as card_module
from organs.heart import energy as energy_module
from organs.heart import engage as engage_module
from organs.heart import model as heart_model
from organs.heart import mood as mood_module
from organs.heart import events as events_module
from organs.heart import gate as gate_module
from organs.heart import intent as intent_module
from organs.heart import offset as offset_module
from organs.heart import pending as pending_module
from organs.heart import proactive as proactive_module
from organs.heart import refuse as refuse_module
from organs.heart import sleep as sleep_module
from organs.heart import store as store_module

PLUGIN = Path(__file__).resolve().parents[2]
REAL_CARD = PLUGIN / "organs" / "heart" / "data" / "baseline-card.json"

HEART_ROUTES = ("/heart", "/heart/overview", "/heart/baseline", "/heart/mood",
                "/heart/energy", "/heart/offset", "/heart/engage", "/heart/effective",
                "/heart/pending", "/heart/sleep", "/heart/refusals", "/heart/proactive",
                "/heart/gate")

# 固定时刻：断言不看"现在几点"。09:00 / 23:00 分别避开与落进夜间窗口（23-8）。
T0 = "2026-11-05T09:00:00+09:00"


def _stamp(day: int, hour: int, minute: int = 0) -> str:
    """造一个带 +09:00 的 ISO 时刻；跨天、跨时段都靠它，不依赖本机时区。"""
    return "2026-11-%02dT%02d:%02d:00+09:00" % (day, hour, minute)


_START = datetime(2026, 11, 1)


def _local(day: int, hour: int = 0, minute: int = 0) -> str:
    """起点之后第 day 天的 hour:minute（本机时区，与 sleep 的锚点同一把挂钟）。

    睡眠账判熬夜用的是「醒来的那一刻往前找最近一次基准起点」的本地挂钟小时，
    所以睡眠账相关的时刻一律用本地时间造，别掺 +09:00 的固定偏移。
    """
    return heart_model.iso(_START + timedelta(days=day, hours=hour, minutes=minute))


def _night(root: Any, day: int, late_min: float = 0.0, minutes: float = 480.0) -> Any:
    """day 那天 23:00 之后第 late_min 分钟睡，睡 minutes 分钟后醒；返回醒来的那笔。"""
    onset = heart_model.parse_ts(_local(day, 23)) + timedelta(minutes=float(late_min))
    sleep_module.record(sleep_module.KIND_ASLEEP, root=root, now=heart_model.iso(onset))
    return sleep_module.record(
        sleep_module.KIND_AWAKE, root=root,
        now=heart_model.iso(onset + timedelta(minutes=float(minutes))))


class HeartApiTest(unittest.TestCase):
    """公共夹具：临时根 + 一张真卡（只读引用，绝不往仓里写）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "heart"
        self.root.mkdir()
        (self.root / card_module.CARD_FILE).write_bytes(REAL_CARD.read_bytes())

    def get(self, route: str, brain: Any = None, **params: Any) -> Any:
        """打一条 GET；root 一律指向临时根，payload 必须能 json.dumps。

        brain 是关键字参数、不进查询串：它是宿主给的那只脑（缺省 None = 心的降级路径）。
        """
        query = dict((key, str(value)) for key, value in params.items() if value is not None)
        query.setdefault("root", str(self.root))
        status, payload = api.handle("GET", route, None, None, query=query, brain=brain)
        json.dumps(payload, ensure_ascii=False)
        return status, payload


class _WarmBrain(object):
    """假脑：只提供 ports 的两条通道（people_list / today_view）。

    熟稔给 2.0（高于 ENGAGE_FAMILIARITY_LOW），好把第 4 道软门单独隔出来看——
    否则 brain=None 的降级会同时塞一条 low-familiarity，"ignored-recently 生效了没有"
    就混在别的命中里说不清。
    """

    def people_list(self, now: Any = None) -> List[Dict[str, Any]]:
        return [{"id": "主人", "display": "主人", "familiarity": 2.0}]

    def today_view(self, now: Any = None) -> Dict[str, Any]:
        return {"entries": []}


class EightReadonlyRoutes(HeartApiTest):
    def test_route_table_has_the_eight_get_routes(self):
        for route in HEART_ROUTES:
            self.assertIn(route, api.ROUTES, "路由表里没有 %s" % route)
            self.assertEqual("GET", api.ROUTES[route], "%s 必须是 GET" % route)
        self.assertEqual(set(heart_api.HEART_ROUTES),
                         {path for path in api.ROUTES if path.startswith("/heart")})

    def test_overview_and_heart_carry_all_four_blocks(self):
        for route in ("/heart", "/heart/overview"):
            status, payload = self.get(route, now=T0)
            self.assertEqual(200, status, route)
            for key in ("status", "ok", "readonly", "agent", "baseline", "render",
                        "mood", "energy", "offset", "pending", "thoughts", "window",
                        "effective", "intents"):
                self.assertIn(key, payload, "%s 少了 %s" % (route, key))
            self.assertEqual("已接入", payload["status"])
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["readonly"])
            self.assertEqual("heart", payload["agent"])
            # 老键不换义：pending 仍是那把「要跳过注入的轴名册」，六轴完整之后它是空列表，
            # 也没有同义的第二个键；念头块叫 thoughts（外层块、块里的列表同名，两层各归各的）。
            self.assertEqual([], payload["pending"])
            self.assertNotIn("pending_axes", payload, "不许留同义的第二把钥匙")
            self.assertEqual([], payload["issues"])
            # 念头块与窗口块：与 /heart/pending、engage 的 signal 是同一份（同一个纯读）
            for key in ("config", "thoughts", "open", "at"):
                self.assertIn(key, payload["thoughts"], "%s 的 thoughts 少了 %s" % (route, key))
            self.assertEqual(payload["thoughts"]["thoughts"],
                             self.get("/heart/pending", now=T0)[1]["thoughts"])
            for key in ("weight", "consecutive_ignored", "rows", "window_min"):
                self.assertIn(key, payload["window"], "%s 的 window 少了 %s" % (route, key))

    def test_baseline_is_tier_wordsonly_over_all_six_axes(self):
        status, payload = self.get("/heart/baseline", now=T0)
        self.assertEqual(200, status)
        # /heart/baseline 的 pending 是同一把名册（老键不换义）：六轴完整之后为空，念头块不在
        # 这条端点上
        self.assertEqual([], payload["pending"])
        self.assertNotIn("thoughts", payload, "baseline 只给档位与名单，不给念头块")
        view = payload["baseline"]
        self.assertEqual([], view["pending"], "pending 这个键留着，且恒为空表")
        self.assertEqual(list(heart_model.AXES), view["loaded"])
        self.assertEqual(set(heart_model.AXES), set(view["tiers"]), "六轴都出档位词")
        for axis, row in view["tiers"].items():
            self.assertIn(axis, heart_model.AXES)
            self.assertTrue(row["inner_word"], axis)
            self.assertTrue(row["shown_word"], axis)
        # 六轴都出词：文本里不许出现 0-9，六条也一行不缺
        self.assertIsNone(re.search(r"[0-9]", payload["render"]), payload["render"])
        self.assertNotIn("None", payload["render"])
        for axis in heart_model.AXES:
            self.assertIn(axis, view["tiers"])
            self.assertIn("- %s：" % axis, payload["render"])

    def test_mood_route_returns_read_plus_trajectory(self):
        status, payload = self.get("/heart/mood", now=T0)
        self.assertEqual(200, status)
        for key in ("now", "baseline", "direction", "tier", "as_of", "updated_at"):
            self.assertIn(key, payload)
        self.assertIsInstance(payload["trajectory"], list)
        self.assertEqual(T0, payload["as_of"])

    def test_energy_route_returns_value_tier_and_settlement(self):
        status, payload = self.get("/heart/energy", now=T0)
        self.assertEqual(200, status)
        for key in ("value", "tier", "settled_from", "settled_to", "hours", "capped"):
            self.assertIn(key, payload)
        self.assertTrue(0 <= float(payload["value"]) <= 100)
        self.assertTrue(payload["tier"])
        self.assertIn("deducted", payload, "overview 要报今天已经额外扣掉多少")

    def test_write_methods_are_405_with_the_heart_wording(self):
        for route in HEART_ROUTES:
            for method in ("POST", "PUT", "DELETE"):
                status, payload = api.handle(method, route, {}, None,
                                             query={"root": str(self.root)})
                self.assertEqual(405, status, "%s %s" % (method, route))
                self.assertFalse(payload["ok"])
                self.assertIn("只读", payload["message"])
                self.assertIn("只收 GET", payload["message"])
                self.assertIn("人不能改心", payload["message"])


class BadCardIsNotASuccess(HeartApiTest):
    """坏卡：HTTP 200 但 ok=False、issues 非空——页面要能显示，不许 500、不许假装成功。"""

    def _write_bad_card(self) -> None:
        (self.root / card_module.CARD_FILE).write_text(json.dumps({
            "scale": {"min": 0, "max": 100},
            "axes": [
                {"id": "服气", "inner": 90, "shown": 20},
                {"id": "心疼", "inner": 70, "shown": 60},
                # 「想黏人」「怕失去」故意缺；耐心 / 记性 六轴完整之后照规矩带数
                {"id": "耐心", "inner": 72, "shown": 52},
                {"id": "记性", "inner": 78, "shown": 60},
            ],
        }, ensure_ascii=False), encoding="utf-8")

    def test_overview_reports_refusal_with_every_issue(self):
        self._write_bad_card()
        status, payload = self.get("/heart/overview", now=T0)
        self.assertEqual(200, status, "坏卡也走 200：页面要能显示，不许 500")
        self.assertEqual("拒载", payload["status"])
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["issues"], "拒载必须逐条报出来")
        for issue in payload["issues"]:
            self.assertEqual({"code", "axis", "message"}, set(issue))
        self.assertIn("axis-missing", {issue["code"] for issue in payload["issues"]})
        self.assertIn("想黏人", {issue["axis"] for issue in payload["issues"]})
        self.assertEqual({}, payload["baseline"], "拒载时不给档位")
        self.assertEqual("", payload["render"])

    def test_baseline_route_also_refuses(self):
        self._write_bad_card()
        status, payload = self.get("/heart/baseline", now=T0)
        self.assertEqual(200, status)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["issues"])


class MoodRoute(HeartApiTest):
    def test_same_now_reads_the_same_numbers_and_trajectory_is_not_empty(self):
        mood_module.apply_event("praise", root=self.root, now=_stamp(5, 8))
        mood_module.apply_event("praise", root=self.root, now=_stamp(5, 9))
        first = self.get("/heart/mood", now=_stamp(5, 12, 30))[1]
        second = self.get("/heart/mood", now=_stamp(5, 12, 30))[1]
        self.assertEqual(first["now"], second["now"], "同一个 now 必须读出同一个数")
        self.assertEqual(first["baseline"], second["baseline"])
        self.assertTrue(first["trajectory"], "轨迹账本该有采样点了")
        self.assertIn("baseline", first["trajectory"][-1])
        self.assertIn("tier", first["trajectory"][-1])


class EngageIsADryRun(HeartApiTest):
    """二十组输入：每次都要说得出「哪几个门命中」，没有一次 unattributed。"""

    def _combos(self) -> List[Dict[str, str]]:
        combos: List[Dict[str, str]] = []
        for index in range(10):
            # 白天被动：软门（熟稔偏低 / 今日量 / 刚被打断）里至少命中一条
            combos.append({"intent": "passive", "now": _stamp(5 + index, 9 + index % 5)})
            # 夜里主动：硬门（夜间窗口）先于软门
            combos.append({"intent": "proactive", "insulted": "0",
                           "now": _stamp(5 + index, 23)})
            # 白天主动 + 被骂当天：另一道硬门
            combos.append({"intent": "proactive", "insulted": "1",
                           "now": _stamp(5 + index, 14)})
            # 刚被打断：软门 interrupted
            combos.append({"intent": "passive", "interrupted": _stamp(5 + index, 8, 55),
                           "now": _stamp(5 + index, 9)})
        return combos[:20]

    def test_every_judgement_has_hits_and_no_unattributed(self):
        rows: List[Dict[str, Any]] = []
        states = set()
        for index, combo in enumerate(self._combos()):
            status, payload = self.get("/heart/engage", **combo)
            self.assertEqual(200, status, payload)
            self.assertTrue(payload["dry_run"], "干跑要自己说出来")
            for key in ("state", "reason", "hits", "hard", "soft", "soft_skipped",
                        "readouts", "score"):
                self.assertIn(key, payload, "判决少了 %s" % key)
            self.assertNotEqual(heart_model.STATE_SPEAK, payload["state"],
                                "第 %d 组：判了「说」就说明门没命中，输入该重挑" % index)
            self.assertTrue(payload["hits"], "第 %d 组：判了却没有任何命中门" % index)
            self.assertNotIn(heart_model.REASON_UNATTRIBUTED, payload["hits"])
            states.add(payload["state"])
            rows.append({"state": payload["state"], "reason": payload["reason"],
                         "hits": list(payload["hits"])})

        report = engage_module.attribution_report(rows)
        self.assertEqual(20, report["total"])
        self.assertEqual(0, report["unattributed"], "有判决说不清为什么：%r" % (report,))
        self.assertTrue(states <= set(heart_model.STATES), states)

    def test_dry_run_writes_no_log(self):
        for combo in self._combos():
            self.get("/heart/engage", **combo)
        self.assertFalse((self.root / engage_module.LOG_FILE).exists(),
                         "干跑不许写判决日志")

    def test_double_low_gate_fires_on_the_effective_reading(self):
        """双低门用 effective 的有效值命中：mood 恰好 29、energy 20 → 不说。

        这两个数是直接写进状态文件的（衰减为 0 的那一刻），所以门看到的就是 29 / 20，
        readouts.mood_now 必须与 /heart/effective 的 mood.now 逐字相同。
        """
        (self.root / mood_module.MOOD_FILE).write_text(json.dumps({
            "version": 1, "now": 29.0, "baseline": 60.0, "updated_at": T0,
            "day": "2026-11-05", "conflict_used": 0, "window": {},
        }), encoding="utf-8")
        (self.root / energy_module.ENERGY_FILE).write_text(json.dumps({
            "version": 1, "value": 20.0, "updated_at": T0, "day": "2026-11-05",
            "deducted": 0.0, "last_burst_at": None,
        }), encoding="utf-8")

        status, payload = self.get("/heart/engage", intent="passive", now=T0)
        self.assertEqual(200, status, payload)
        self.assertEqual(29.0, payload["readouts"]["mood_now"])
        self.assertEqual(heart_model.STATE_SILENT, payload["state"], "双低必须判「不说」")
        self.assertIn(heart_model.REASON_DOUBLE_LOW, payload["hits"])
        effective = self.get("/heart/effective", now=T0)[1]
        self.assertEqual(effective["mood"]["now"], payload["readouts"]["mood_now"])

    def test_brain_is_optional_and_degrades_to_zero(self):
        # brain=None → 熟稔 0 / 今日 0（ports 的降级），照常判，不抛
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            status, payload = self.get("/heart/engage", now=T0)
        self.assertEqual(200, status)
        self.assertEqual(0.0, payload["readouts"]["familiarity"])
        self.assertEqual(0, payload["readouts"]["today_count"])
        self.assertTrue(any("没有给脑" in str(item.message) for item in caught),
                        "降级要 WARN，不能悄悄少两个数")

    def test_unknown_intent_is_rejected_with_a_message(self):
        status, payload = self.get("/heart/engage", intent="嚷嚷", now=T0)
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("intent", payload["message"])


class EffectiveIsTheSingleSource(HeartApiTest):
    """三块合成读数：形状、只读、坏卡不假装；api 侧的三处取值一致。

    organs 侧的同一条判据（effective_state 本体）在 organs/heart/tests/test_effective.py。
    """

    def test_payload_carries_the_three_blocks_and_the_envelope(self):
        status, payload = self.get("/heart/effective", now=T0)
        self.assertEqual(200, status)
        for key in ("ok", "readonly", "agent", "as_of", "status", "mood", "axes", "voice"):
            self.assertIn(key, payload, "/heart/effective 少了 %s" % key)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readonly"])
        self.assertEqual("heart", payload["agent"])
        self.assertEqual("已接入", payload["status"])
        self.assertEqual(T0, payload["as_of"])
        self.assertNotIn("dry_run", payload, "effective 是读数，不是干跑")

        self.assertEqual({"now", "baseline", "tier", "direction"}, set(payload["mood"]))
        self.assertEqual({"tone", "direction", "bans"}, set(payload["voice"]))
        self.assertEqual(payload["mood"]["tier"], payload["voice"]["tone"],
                         "语气档就是 mood 那一档的词")
        self.assertEqual(payload["mood"]["direction"], payload["voice"]["direction"])
        self.assertIsInstance(payload["voice"]["bans"], list)

        self.assertEqual(set(heart_model.AXES), set(payload["axes"]), "六轴都要在")
        for axis in heart_model.AXES:
            self.assertEqual(
                {"inner_value", "shown_value", "inner_tier", "shown_tier",
                 "inner_word", "shown_word", "offset", "greyed"},
                set(payload["axes"][axis]), axis)
            self.assertFalse(payload["axes"][axis]["greyed"], axis)

    def test_three_places_publish_one_number(self):
        mood_module.apply_event("rejected", root=self.root, now=_stamp(5, 8))
        effective = self.get("/heart/effective", now=T0)[1]
        engaged = self.get("/heart/engage", now=T0)[1]
        mood_route = self.get("/heart/mood", now=T0)[1]
        self.assertEqual(effective["mood"]["now"], engaged["readouts"]["mood_now"],
                         "/heart/effective 与 /heart/engage 不是同一个数")
        self.assertEqual(effective["mood"]["now"], mood_route["now"],
                         "/heart/effective 与 /heart/mood 不是同一个数")
        self.assertEqual(effective["voice"]["tone"], mood_route["tier"],
                         "面板读到的档位词与语气档不是同一个词")
        self.assertEqual(effective["mood"]["baseline"], mood_route["baseline"])

    def test_overview_embeds_the_same_blocks(self):
        overview = self.get("/heart/overview", now=T0)[1]
        effective = self.get("/heart/effective", now=T0)[1]
        for key in ("mood", "axes", "voice"):
            self.assertEqual(effective[key], overview["effective"][key])

    def test_bad_card_refuses_with_status_and_issues(self):
        (self.root / card_module.CARD_FILE).write_text(json.dumps([
            {"scale": {"min": 0, "max": 100}, "axes": []},
        ], ensure_ascii=False), encoding="utf-8")
        status, payload = self.get("/heart/effective", now=T0)
        self.assertEqual(200, status, "坏卡也走 200：页面要能显示，不许 500")
        self.assertFalse(payload["ok"])
        self.assertEqual("拒载", payload["status"])
        self.assertTrue(payload["issues"], "拒载必须逐条报出来")
        for issue in payload["issues"]:
            self.assertEqual({"code", "axis", "message"}, set(issue))
        self.assertNotIn("axes", payload, "拒载时一个档位都不给")
        self.assertTrue(payload["readonly"])
        self.assertEqual(T0, payload["as_of"])

    def test_bad_card_does_not_block_the_gates(self):
        """坏卡只影响档位与语气：门只读 mood.now，判决照常干跑。"""
        (self.root / card_module.CARD_FILE).write_text("{坏卡", encoding="utf-8")
        status, payload = self.get("/heart/engage", intent="passive", now=T0)
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["readouts"]["mood_now"])
        self.assertFalse((self.root / engage_module.LOG_FILE).exists(), "干跑不写日志")


class OffsetRoute(HeartApiTest):
    def test_axes_keep_the_four_frozen_keys(self):
        status, payload = self.get("/heart/offset", now=T0)
        self.assertEqual(200, status)
        self.assertEqual({"axes", "as_of"}, set(payload))
        self.assertEqual(set(heart_model.AXES), set(payload["axes"]), "六轴都要在（六轴完整）")
        for axis, row in payload["axes"].items():
            for key in ("value", "decayed_value", "day_delta", "clamped"):
                self.assertIn(key, row, "%s 少了 %s" % (axis, key))
        self.assertEqual(T0, payload["as_of"], "as_of 用 offset 自己给的读数时刻")

    def test_day_scale_is_passed_through_untouched(self):
        """天尺：日事件 +1 当天是 1.0，满 3 天回落到 0——本层只透传，不按半衰换算。"""
        offset_module.apply_daily("服气", 1.0, root=self.root, now=_stamp(5, 9))
        same_day = self.get("/heart/offset", now=_stamp(5, 9))[1]["axes"]["服气"]
        self.assertAlmostEqual(1.0, same_day["value"], places=6)
        self.assertFalse(same_day["clamped"])
        after_three_days = self.get("/heart/offset", now=_stamp(8, 9))[1]["axes"]["服气"]
        self.assertAlmostEqual(0.0, after_three_days["value"], places=6)

    def test_root_override_only_takes_an_existing_absolute_directory(self):
        """root 白名单：相对路径 / 不存在的绝对路径一律忽略并 WARN，不会被指到任意路径。"""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertIsNone(heart_api._root({"root": "relative/path"}))
            self.assertIsNone(heart_api._root({"root": "/definitely/not/here-xyz"}))
        self.assertEqual(2, len(caught), "两次都该 WARN 出来")
        self.assertEqual(str(self.root), str(heart_api._root({"root": str(self.root)})))


class BrokenNowIsA400(HeartApiTest):
    """坏时间戳要响成 400（说清哪个值、怎么改），不许把 ValueError 抛穿 api.handle。

    最常见的坑：查询串里的 '+' 会被 parse_qs 解码成空格，时区偏移必须写成 %2B。
    """

    def test_bad_now_names_the_value(self):
        status, payload = api.handle("GET", "/heart/mood", None, None,
                                     query={"root": str(self.root), "now": "not-a-time"})
        self.assertEqual(400, status, "坏 now 不许抛栈，要响成 400")
        self.assertFalse(payload["ok"])
        self.assertIn("now", payload["message"])
        self.assertIn("not-a-time", payload["message"], "坏值要原样带出来")

    def test_unescaped_plus_is_a_400_that_says_percent_2B(self):
        path = "/heart/engage?intent=passive&now=2026-11-05T00:00:00+09:00&root=%s" % self.root
        status, payload = api.handle("GET", path, None, None)
        self.assertEqual(400, status)
        self.assertIn("%2B", payload["message"], "要告诉人把 '+' 写成 %2B")
        self.assertIn("2026-11-05T00:00:00 09:00", payload["message"],
                      "到手的是被解成空格的串，也要原样带出来")

    def test_escaped_plus_is_200_and_as_of_is_that_moment(self):
        path = "/heart/mood?now=2026-11-05T00:00:00%%2B09:00&root=%s" % self.root
        status, payload = api.handle("GET", path, None, None)
        self.assertEqual(200, status, payload)
        self.assertEqual("2026-11-05T00:00:00+09:00", payload["as_of"])

    def test_all_routes_refuse_a_broken_now_with_400(self):
        for route in HEART_ROUTES:
            status, payload = api.handle("GET", route, None, None,
                                         query={"root": str(self.root), "now": "不是时间"})
            self.assertEqual(400, status, route)
            self.assertIn("不是时间", payload["message"], route)


class PendingThoughtsRoute(HeartApiTest):
    """/heart/pending：念头层的只读读数（config + 每条念头的 state / attempts / 剩余 TTL）。

    两条口径：
    - 它**纯读**：GET 一次不许长出 pending.json（derive 才落笔）；
    - 它**不读卡**：卡坏了这一块照常给念头（拒载由 /heart、/heart/effective 报）。
    """

    def test_route_is_registered_and_payload_has_the_envelope(self):
        self.assertIn("/heart/pending", api.ROUTES)
        self.assertEqual("GET", api.ROUTES["/heart/pending"])
        status, payload = self.get("/heart/pending", now=T0)
        self.assertEqual(200, status, payload)
        for key in ("ok", "readonly", "agent", "status", "config", "thoughts", "open", "at"):
            self.assertIn(key, payload, "/heart/pending 少了 %s" % key)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readonly"])
        self.assertEqual("heart", payload["agent"])
        self.assertEqual("已接入", payload["status"])
        for key in ("release", "ttl_min", "max_attempts", "max_open",
                    "ignore_window_min", "ignore_threshold", "axis_threshold"):
            self.assertIn(key, payload["config"], "config 少了 %s" % key)

    def test_no_thoughts_is_an_empty_list_not_a_blank(self):
        status, payload = self.get("/heart/pending", now=T0)
        self.assertEqual(200, status)
        self.assertEqual([], payload["thoughts"], "没念头就是空列表")
        self.assertEqual(0, payload["open"])
        self.assertFalse((self.root / pending_module.PENDING_FILE).exists(),
                         "/heart/pending 是纯读：不许凭空落一个 pending.json")

    def test_three_ignored_events_grow_a_thought_and_the_route_shows_it(self):
        for minute in (50, 51, 52):
            events_module.deliver("ignored", root=self.root, now=_stamp(5, 8, minute))
        grown = pending_module.derive(root=self.root, now=T0)
        self.assertEqual(1, len(grown["created"]), grown)

        status, payload = self.get("/heart/pending", now=T0)
        self.assertEqual(200, status)
        self.assertEqual(1, payload["open"])
        self.assertEqual(1, len(payload["thoughts"]))
        row = payload["thoughts"][0]
        self.assertEqual(pending_module.KIND_PRAISE, row["kind"], "连续被冷落长出的是「想被夸」")
        self.assertEqual(pending_module.SOURCE_IGNORED, row["source"])
        self.assertEqual(pending_module.STATE_OPEN, row["state"])
        self.assertEqual(0, row["attempts"])
        self.assertGreater(row["ttl_left_min"], 0.0, "刚长出来的念头还该有剩余 TTL")
        self.assertFalse(row["expired"])

    def test_bad_card_does_not_affect_the_pending_block(self):
        (self.root / card_module.CARD_FILE).write_text("{坏卡", encoding="utf-8")
        status, payload = self.get("/heart/pending", now=T0)
        self.assertEqual(200, status, "它不读卡：坏卡不许影响念头这一块")
        self.assertTrue(payload["ok"])
        self.assertEqual("已接入", payload["status"])
        self.assertIn("thoughts", payload)
        self.assertNotIn("issues", payload, "念头块不替卡的拒载背书")


class WindowReachesTheEndpoint(HeartApiTest):
    """本轮的判据落点：第 4 道软门要能在**端点级**生效，不是只在单测里存在。

    造 3 条 ignored 投进临时根的事件账本 → GET /heart/engage 必须报
    hits 含 "ignored-recently"、state=少说；再补一条 replied → 回到「说」。
    假脑只给熟稔 2.0，把别的软门隔在外面。
    """

    def _engage(self, **params: Any) -> Any:
        query = {"root": str(self.root), "now": T0}
        query.update(dict((key, str(value)) for key, value in params.items()))
        status, payload = api.handle("GET", "/heart/engage", None, None,
                                     query=query, brain=_WarmBrain())
        return status, payload

    def test_three_ignored_events_flip_the_judgement_to_less(self):
        for minute in (50, 51, 52):
            events_module.deliver("ignored", root=self.root, now=_stamp(5, 8, minute))
        status, payload = self._engage(intent="passive")
        self.assertEqual(200, status, payload)
        self.assertEqual([heart_model.REASON_IGNORED], payload["hits"],
                         "窗口没接进端点：hits 里没有 %s" % heart_model.REASON_IGNORED)
        self.assertEqual(heart_model.REASON_IGNORED, payload["reason"])
        self.assertEqual(heart_model.STATE_LESS, payload["state"])
        self.assertEqual(3, payload["window"]["consecutive_ignored"])
        self.assertGreater(payload["window"]["weight"], 0.0)
        self.assertIn(heart_model.REASON_IGNORED, payload["soft"])

    def test_one_reply_brings_her_back_to_speaking(self):
        for minute in (50, 51, 52):
            events_module.deliver("ignored", root=self.root, now=_stamp(5, 8, minute))
        self.assertEqual(heart_model.STATE_LESS, self._engage()[1]["state"])
        events_module.deliver("replied", root=self.root, now=_stamp(5, 8, 56))
        status, payload = self._engage()
        self.assertEqual(200, status)
        self.assertEqual([], payload["hits"], "回了一句就该恢复：%r" % (payload["hits"],))
        self.assertEqual(heart_model.STATE_SPEAK, payload["state"])
        self.assertEqual(0, payload["window"]["consecutive_ignored"])
        self.assertEqual(1, payload["window"]["replied"])

    def test_overview_window_is_the_same_signal_as_the_judgement(self):
        events_module.deliver("harsh", root=self.root, now=_stamp(5, 8, 40))
        judged = self._engage()[1]["window"]
        overview = self.get("/heart/overview", now=T0)[1]["window"]
        self.assertEqual(judged, overview, "/heart/overview 的窗口与判决用的不是同一份 signal")
        self.assertEqual(1, overview["harsh"])


class SleepAccountRoute(HeartApiTest):
    """/heart/sleep：睡眠账的只读读数，且它真的接进了 energy 的结算。

    三条口径：
    - 纯读：GET 一次不长出 sleep.json（只有 sleep.record 才落账）；
    - 派生量读时现算：连续熬夜把窗口整体后移、连续没睡够把回血封顶下调；
    - 后移的窗口**真的**落到 /heart/energy 的结算上——不是只报一个数给人看。
    """

    def test_route_is_registered_with_the_readonly_envelope(self):
        self.assertIn("/heart/sleep", api.ROUTES)
        self.assertEqual("GET", api.ROUTES["/heart/sleep"])
        status, payload = self.get("/heart/sleep", now=_local(1, 12))
        self.assertEqual(200, status, payload)
        for key in ("ok", "readonly", "agent", "status", "config", "last_asleep_at",
                    "last_awake_at", "last_duration_min", "consecutive_late_nights",
                    "consecutive_short_nights", "drift_hours", "effective_window",
                    "wake_cap", "awake_minutes", "nights"):
            self.assertIn(key, payload, "/heart/sleep 少了 %s" % key)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readonly"])
        self.assertEqual("heart", payload["agent"])
        self.assertEqual("已接入", payload["status"])
        # 空账：出厂窗口 (23, 8)，回血封顶就是 ENERGY_MAX，一个夜晚都还没落
        self.assertEqual([23, 8], payload["effective_window"])
        self.assertEqual(float(heart_model.ENERGY_MAX), payload["wake_cap"])
        self.assertEqual([], payload["nights"])
        self.assertEqual(0, payload["consecutive_late_nights"])

    def test_empty_root_stays_empty(self):
        self.get("/heart/sleep", now=_local(1, 12))
        self.assertFalse((self.root / sleep_module.SLEEP_FILE).exists(),
                         "/heart/sleep 是纯读：不许凭空落一个 sleep.json")

    def test_three_late_nights_shift_the_window_and_energy_follows(self):
        for day in (1, 2, 3):
            _night(self.root, day, late_min=90)
        status, payload = self.get("/heart/sleep", now=_local(4, 12))
        self.assertEqual(200, status)
        self.assertEqual(3, payload["consecutive_late_nights"])
        self.assertEqual(1.5, payload["drift_hours"])
        self.assertEqual([1, 10], payload["effective_window"])

        energy_payload = self.get("/heart/energy", now=_local(4, 12))[1]
        self.assertEqual([1, 10], energy_payload["sleep_window"],
                         "/heart/energy 没有把睡眠账的生效窗口接进去")
        self.assertEqual(payload["wake_cap"], energy_payload["wake_cap"])

        # 结算真的变了：两个同源子根、同一个起点与结算区间，只差窗口
        plain = self.root / "plain"
        shifted = self.root / "shifted"
        for sub in (plain, shifted):
            sub.mkdir()
            (sub / sleep_module.SLEEP_FILE).write_bytes(
                (self.root / sleep_module.SLEEP_FILE).read_bytes())
        start, later = _local(4, 12), _local(5, 12)
        energy_module.read(plain, start)
        energy_module.read(shifted, start)
        factory = energy_module.read(plain, later, sleep_window=(23, 8))
        moved = energy_module.read(shifted, later,
                                   sleep_window=sleep_module.effective_window(shifted, later))
        self.assertNotEqual(factory["value"], moved["value"],
                           "窗口后移没有改变结算：这一格根本没接上")

        shipped = self.get("/heart/energy", now=later, root=str(shifted))[1]
        self.assertEqual(moved["value"], shipped["value"],
                         "/heart/energy 算出来的值与它自报的窗口不是一回事")

    def test_two_short_nights_lower_the_wake_cap(self):
        _night(self.root, 1)
        _night(self.root, 2, minutes=240)
        first = self.get("/heart/sleep", now=_local(3, 4))[1]
        self.assertEqual(1, first["consecutive_short_nights"])
        self.assertEqual(float(heart_model.ENERGY_MAX), first["wake_cap"],
                         "只差一晚，封顶还不该动")
        _night(self.root, 3, minutes=240)
        second = self.get("/heart/sleep", now=_local(4, 4))[1]
        self.assertEqual(2, second["consecutive_short_nights"])
        self.assertEqual(95.0, second["wake_cap"], "连续两晚没睡够要下调 5")
        self.assertEqual(95.0, self.get("/heart/energy", now=_local(4, 4))[1]["wake_cap"],
                         "/heart/energy 没有把回血封顶接进去")

    def test_engage_gate_reads_the_same_energy_as_the_energy_route(self):
        """门用的精力与 /heart/energy 那一格是同一个数——冷启动也不许分叉。

        三个同源子根，账面与起点逐字相同：A 先跑一次判决、B 读 /heart/energy、
        C 故意用出厂窗口结算。判决与 /heart/energy 必须同值，而 C 必须不等于 B——
        最后这条是这条用例的牙：它证明这个场景下两本窗口算出来的数本来就不一样，
        否则"两边相等"是白捡的。
        """
        for day in (1, 2, 3):
            _night(self.root, day, late_min=90)
        now = _local(4, 12)
        onset = _local(3, 8, 10)  # 那一夜醒来的时刻：结算就从这儿算到 now
        seed = json.dumps({"version": 1, "value": 80.0, "updated_at": onset,
                           "day": onset[:10], "deducted": 0.0,
                           "last_burst_at": None})
        twins = {}
        for name in ("a", "b", "c"):
            sub = self.root / ("twin-" + name)
            sub.mkdir()
            (sub / sleep_module.SLEEP_FILE).write_bytes(
                (self.root / sleep_module.SLEEP_FILE).read_bytes())
            (sub / energy_module.ENERGY_FILE).write_text(seed, encoding="utf-8")
            twins[name] = sub

        engaged = self.get("/heart/engage", now=now, root=str(twins["a"]))[1]
        route = self.get("/heart/energy", now=now, root=str(twins["b"]))[1]
        factory = energy_module.read(twins["c"], now, sleep_window=(23, 8))

        self.assertEqual(route["value"], engaged["readouts"]["energy"],
                         "门用的精力不是按睡眠账窗口结算出来的那个数")
        self.assertNotEqual(factory["value"], route["value"],
                            "这个场景两本窗口算出来一样，上一条断言就没有牙了")

    def test_overview_energy_uses_the_same_terms(self):
        for day in (1, 2, 3):
            _night(self.root, day, late_min=90)
        now = _local(4, 12)
        energy_payload = self.get("/heart/energy", now=now)[1]
        overview = self.get("/heart/overview", now=now)[1]
        self.assertEqual(energy_payload["value"], overview["energy"]["value"],
                         "总览的精力与 /heart/energy 不是同一个数：两边窗口一定分叉了")
        self.assertEqual(energy_payload["tier"], overview["energy"]["tier"])


class IntentRankingReachesTheEndpoints(HeartApiTest):
    """第四轮接线：意图排序要能从端点上看见，且 pass 真的会翻。

    - /heart/overview 带 intents 块（order / top / score / threshold / clarity / pass）；
    - /heart/engage 把同一份 rank 送进第 5 道软门，返回体原样带回；
    - 阈值从**临时根**里的权重表读（出厂文件不动）：调高阈值，pass 从 True 变 False，
      判决随之多一道 intent-below。
    """

    def _put_weights(self, **overrides: Any) -> None:
        """把出厂权重表复制到临时根再改几格：人改文件即生效，不动出厂那份。"""
        table = intent_module.load_weights()
        table.update(overrides)
        (self.root / intent_module.WEIGHTS_FILE).write_text(
            json.dumps(table, ensure_ascii=False), encoding="utf-8")

    def _talked_just_now(self) -> None:
        """09:00 前 4 分钟刚回过一句：没聊完，30 分钟窗口内、之后没有收尾 normal。"""
        events_module.deliver("replied", root=self.root, now=_stamp(5, 8, 56))

    def test_overview_carries_the_ranking_block(self):
        self._put_weights()
        status, payload = self.get("/heart/overview", now=T0, brain=_WarmBrain())
        self.assertEqual(200, status)
        block = payload["intents"]
        for key in ("order", "top", "score", "threshold", "clarity", "pass"):
            self.assertIn(key, block, "intents 块少了 %s" % key)
        self.assertEqual(len(intent_module.INTENT_ORDER), len(block["order"]))
        for row in block["order"]:
            self.assertEqual({"intent", "weight", "hit", "why"}, set(row))
        weights = [row["weight"] for row in block["order"]]
        self.assertEqual(sorted(weights, reverse=True), weights, "order 要按权重降序")
        self.assertEqual(intent_module.DEFAULT_THRESHOLD, block["threshold"])

    def test_engage_carries_the_same_rank_as_the_overview(self):
        self._put_weights()
        self._talked_just_now()
        overview = self.get("/heart/overview", now=T0, brain=_WarmBrain())[1]
        engaged = self.get("/heart/engage", now=T0, brain=_WarmBrain())[1]
        self.assertEqual(overview["intents"], engaged["intents"],
                         "总览与判决读到的排序不是同一份")
        self.assertIsNotNone(engaged["intents"]["top"])
        self.assertIn(engaged["intents"]["top"]["intent"],
                      [row["intent"] for row in engaged["intents"]["order"]])

    def test_raising_the_threshold_flips_pass_and_adds_the_gate(self):
        self._put_weights()
        self._talked_just_now()
        passed = self.get("/heart/engage", now=T0, brain=_WarmBrain())[1]
        self.assertTrue(passed["intents"]["pass"], passed["intents"])
        self.assertNotIn(heart_model.REASON_INTENT_BELOW, passed["hits"],
                         "pass=True 时第 5 道软门一个字都不该加")
        self.assertEqual(heart_model.STATE_SPEAK, passed["state"])

        self._put_weights(threshold=0.95)
        blocked = self.get("/heart/engage", now=T0, brain=_WarmBrain())[1]
        self.assertFalse(blocked["intents"]["pass"])
        self.assertLess(blocked["intents"]["score"], blocked["intents"]["threshold"])
        self.assertIn(heart_model.REASON_INTENT_BELOW, blocked["hits"],
                      "pass=False 时第 5 道软门必须命中")
        self.assertEqual(heart_model.STATE_LESS, blocked["state"])
        # 阈值只改了「值不值得开口」那条线，排序与权重一个字不动
        self.assertEqual([row["weight"] for row in passed["intents"]["order"]],
                         [row["weight"] for row in blocked["intents"]["order"]])


class RefusalsRoute(HeartApiTest):
    """第五轮接线：拒绝记录开了一条只读查询（GET /heart/refusals）+ 总览里的一块。

    三件事一起看住：
    1. 空 topics 时 items=[]，但 cooling 与 quota 照给（额度不是"查了才有"的东西）；
    2. 冷却中 / 没冷却各一次：cooling 只比 until 与 now 的先后，remaining_min 就是差的那几分钟；
    3. 纯读：GET 前后 refusals.json 一个字节不变（额度与冷却都只读出来，不写回去）。
    词表指到临时那份：真词表人会补词，测试不拿它当判据。
    """

    QUOTA = int(refuse_module.DEFAULT_CONFIG["daily_quota"])

    def setUp(self) -> None:
        super().setUp()
        self.topics = Path(self._tmp.name) / "topics.json"
        self.topics.write_text(json.dumps({"医院": [], "猫": [], "工作": []},
                                          ensure_ascii=False), encoding="utf-8")
        old = os.environ.get(refuse_module.ENV_TOPICS_FILE)
        os.environ[refuse_module.ENV_TOPICS_FILE] = str(self.topics)
        self.addCleanup(self._restore_topics, old)

    def _restore_topics(self, old: Any) -> None:
        if old is None:
            os.environ.pop(refuse_module.ENV_TOPICS_FILE, None)
        else:
            os.environ[refuse_module.ENV_TOPICS_FILE] = old

    def _refuse(self, topic: str, now: str) -> Dict[str, Any]:
        return refuse_module.ingest(topic, "refused-topic", root=self.root, now=now)

    def _state_bytes(self) -> bytes:
        return (self.root / refuse_module.REFUSALS_FILE).read_bytes()

    def test_empty_topics_gives_quota_and_no_items(self):
        status, payload = self.get("/heart/refusals", now=T0)
        self.assertEqual(200, status)
        for key in ("as_of", "items", "cooling", "quota", "ok", "readonly", "agent",
                    "status"):
            self.assertIn(key, payload, "/heart/refusals 少了 %s" % key)
        self.assertEqual([], payload["items"], "空 topics 就不查任何话题")
        self.assertEqual([], payload["cooling"])
        self.assertEqual({"used_today": 0, "limit": self.QUOTA, "left": self.QUOTA},
                         payload["quota"], "额度与查不查话题无关")
        self.assertEqual(T0, payload["as_of"])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readonly"])
        self.assertEqual("heart", payload["agent"])
        self.assertEqual("已接入", payload["status"])

    def test_cooling_and_not_cooling_topics(self):
        self._refuse("医院", T0)
        status, payload = self.get("/heart/refusals", topics="医院,猫", now=T0)
        self.assertEqual(200, status)
        self.assertEqual(["医院", "猫"], [item["topic"] for item in payload["items"]],
                         "顺序照人给的")
        for item in payload["items"]:
            self.assertEqual({"topic", "known", "cooling", "remaining_min", "reason",
                              "count"}, set(item))
        hot, cold = payload["items"]
        self.assertTrue(hot["cooling"])
        self.assertEqual(720.0, hot["remaining_min"], "冷却 12 小时就是 720 分钟")
        self.assertEqual(1, hot["count"])
        self.assertEqual("refused-topic", hot["reason"])
        self.assertTrue(hot["known"], "医院在共用词表里")
        self.assertFalse(cold["cooling"], "没拒过的猫不在冷却里")
        self.assertEqual(0.0, cold["remaining_min"])
        self.assertEqual(0, cold["count"])
        self.assertIsNone(cold["reason"])
        self.assertEqual(["医院"], payload["cooling"])
        self.assertEqual({"used_today": 1, "limit": self.QUOTA,
                          "left": self.QUOTA - 1}, payload["quota"])

    def test_cooling_is_released_by_time_and_nothing_else(self):
        until = self._refuse("医院", T0)["until"]
        before = heart_model.iso(heart_model.parse_ts(until) - timedelta(minutes=1))
        inside = self.get("/heart/refusals", topics="医院", now=before)[1]["items"][0]
        self.assertTrue(inside["cooling"], "差一分钟还在冷却里")
        self.assertEqual(1.0, inside["remaining_min"])
        on_the_dot = self.get("/heart/refusals", topics="医院", now=until)[1]["items"][0]
        self.assertFalse(on_the_dot["cooling"], "到点那一刻就解除（不早不晚）")
        self.assertEqual(0.0, on_the_dot["remaining_min"])
        self.assertEqual(1, on_the_dot["count"], "解除的是冷却，不是记录：次数照旧留着")

    def test_unknown_topic_is_reported_as_unknown(self):
        item = self.get("/heart/refusals", topics="月亮", now=T0)[1]["items"][0]
        self.assertFalse(item["known"], "词表里没有的话题要如实说不认识")
        self.assertFalse(item["cooling"])
        self.assertEqual(0, item["count"])

    def test_topics_are_trimmed_deduped_and_full_width_commas_accepted(self):
        items = self.get("/heart/refusals", topics=" 医院 ，猫,,医院 ", now=T0)[1]["items"]
        self.assertEqual(["医院", "猫"], [item["topic"] for item in items])

    def test_get_writes_nothing(self):
        self._refuse("医院", T0)
        before = self._state_bytes()
        for _ in range(3):
            self.get("/heart/refusals", topics="医院,猫", now=T0)
        self.assertEqual(before, self._state_bytes(), "查询侧一个字节都不写盘")

    def test_overview_carries_refusals_block_with_remaining(self):
        self._refuse("医院", T0)
        status, payload = self.get("/heart/overview", now=T0)
        self.assertEqual(200, status)
        block = payload["refusals"]
        self.assertEqual({"quota", "cooling", "items"}, set(block),
                         "refusals 块只有这三个键：额度 / 冷却中 / 逐条剩余")
        self.assertEqual(["医院"], block["cooling"])
        self.assertEqual(1, len(block["items"]), "只列冷却中的那几条，不倒整本记录")
        item = block["items"][0]
        self.assertEqual({"topic", "known", "cooling", "remaining_min", "reason",
                          "count"}, set(item), "整本记录一个字段（since / until）都不搬上来")
        self.assertEqual(720.0, item["remaining_min"])
        self.assertEqual({"used_today": 1, "limit": self.QUOTA,
                          "left": self.QUOTA - 1}, block["quota"])

    def test_overview_leaves_expired_topics_out_of_the_block(self):
        until = self._refuse("医院", T0)["until"]
        later = heart_model.iso(heart_model.parse_ts(until) + timedelta(minutes=1))
        block = self.get("/heart/overview", now=later)[1]["refusals"]
        self.assertEqual([], block["cooling"], "过了 until 就不在冷却里")
        self.assertEqual([], block["items"])


class ProactiveRoute(HeartApiTest):
    """/heart/proactive：主动开口的反馈闭环（config + streak + 静音 + 最近 5 条 history）。

    三条口径：
    - 它是**纯读**：GET 一次不许长出 proactive.json（sent / outcome 才落笔）；
    - 静音到期在端点里现算：同一个文件、now 推过 muted_until → muted=False；
    - 与 /heart/engage 第 6 道软门读的是同一份读数：静音时 proactive 判不说、
      passive 照常判，两个对照必须同时成立。
    """

    MUTED_UNTIL = "2026-11-05T11:00:00+09:00"

    def _mute(self) -> None:
        """造一份"连续 3 次没人理、静音到 11:00"的账（T0 是 09:00）。"""
        store_module.write_json(
            self.root / proactive_module.PROACTIVE_FILE,
            {"version": 1, "config": {"mute_after": 3, "mute_hours": 12}, "streak": 3,
             "muted_until": self.MUTED_UNTIL, "last_sent_at": _stamp(5, 8, 30),
             "history": [{"at": _stamp(5, 8, 0), "outcome": "ignored"},
                         {"at": _stamp(5, 8, 15), "outcome": "ignored"},
                         {"at": _stamp(5, 8, 30), "outcome": "ignored"}]},
        )

    def test_route_is_a_get_with_the_readonly_envelope(self) -> None:
        self.assertEqual("GET", api.ROUTES["/heart/proactive"])
        status, payload = self.get("/heart/proactive", now=T0)
        self.assertEqual(200, status, payload)
        for key in ("ok", "readonly", "agent", "status", "config", "streak", "muted",
                    "remaining_min", "muted_until", "last_sent_at", "pending", "history"):
            self.assertIn(key, payload, "/heart/proactive 少了 %s" % key)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readonly"])
        self.assertEqual("heart", payload["agent"])
        self.assertEqual("已接入", payload["status"])
        self.assertEqual({"mute_after": 3, "mute_hours": 12}, payload["config"])
        self.assertFalse(payload["muted"])

    def test_it_is_a_pure_read(self) -> None:
        status, payload = self.get("/heart/proactive", now=T0)
        self.assertEqual(200, status)
        self.assertEqual([], payload["history"])
        self.assertFalse((self.root / proactive_module.PROACTIVE_FILE).exists(),
                         "/heart/proactive 是纯读：不许凭空落一个 proactive.json")

    def test_the_mute_is_visible_and_expires_by_the_clock(self) -> None:
        self._mute()
        status, payload = self.get("/heart/proactive", now=T0)
        self.assertEqual(200, status)
        self.assertTrue(payload["muted"])
        self.assertEqual(120.0, payload["remaining_min"])
        self.assertEqual(3, payload["streak"])
        later, expired = self.get("/heart/proactive", now="2026-11-05T11:00:01+09:00")
        self.assertEqual(200, later)
        self.assertFalse(expired["muted"])
        self.assertEqual(0.0, expired["remaining_min"])
        self.assertIsNone(expired["muted_until"])

    def test_overview_carries_the_same_block(self) -> None:
        self._mute()
        status, payload = self.get("/heart/overview", now=T0)
        self.assertEqual(200, status)
        self.assertIn("proactive", payload, "overview 要带 proactive 这一块")
        status, route = self.get("/heart/proactive", now=T0)
        self.assertEqual(200, status)
        for key in ("config", "streak", "muted", "remaining_min", "muted_until",
                    "last_sent_at", "pending", "history", "at"):
            self.assertEqual(route[key], payload["proactive"][key],
                             "overview 的 proactive 块与 /heart/proactive 不许分叉：%s" % key)

    def test_engage_gate_sees_the_same_mute_and_passive_is_untouched(self) -> None:
        brain = _WarmBrain()
        status, before = self.get("/heart/engage", intent="passive", now=T0, brain=brain)
        self.assertEqual(200, status, before)
        self._mute()
        status, muted_proactive = self.get("/heart/engage", intent="proactive", now=T0,
                                          brain=brain)
        self.assertEqual(200, status, muted_proactive)
        self.assertEqual("不说", muted_proactive["state"])
        self.assertIn(heart_model.REASON_PROACTIVE_MUTED, muted_proactive["hits"])
        status, muted_passive = self.get("/heart/engage", intent="passive", now=T0, brain=brain)
        self.assertEqual(200, status, muted_passive)
        self.assertEqual(before["state"], muted_passive["state"], "被叫了就得答：静音不动被动")
        self.assertEqual(before["hits"], muted_passive["hits"])
        self.assertNotIn(heart_model.REASON_PROACTIVE_MUTED, muted_passive["hits"])
        self.assertTrue(muted_passive["dry_run"])

    def test_engage_without_a_mute_judges_proactive_as_usual(self) -> None:
        brain = _WarmBrain()
        status, proactive_read = self.get("/heart/engage", intent="proactive", now=T0,
                                         brain=brain)
        self.assertEqual(200, status, proactive_read)
        status, passive_read = self.get("/heart/engage", intent="passive", now=T0, brain=brain)
        self.assertEqual(200, status, passive_read)
        self.assertEqual(passive_read["state"], proactive_read["state"],
                         "没有静音时，主动与被动过的是同一套门")
        self.assertNotIn(heart_model.REASON_PROACTIVE_MUTED, proactive_read["hits"])

    def test_engage_is_still_a_dry_run_over_the_proactive_file(self) -> None:
        self._mute()
        before = (self.root / proactive_module.PROACTIVE_FILE).read_bytes()
        status, payload = self.get("/heart/engage", intent="proactive", now=T0)
        self.assertEqual(200, status)
        self.assertEqual(before, (self.root / proactive_module.PROACTIVE_FILE).read_bytes(),
                         "干跑不许动 proactive 账本")
        self.assertFalse((self.root / engage_module.LOG_FILE).exists(),
                         "干跑不写判决日志")

    def test_write_methods_are_405_with_the_heart_wording(self) -> None:
        for method in ("POST", "PUT", "DELETE"):
            status, payload = api.handle(method, "/heart/proactive", {}, None)
            self.assertEqual(405, status, method)
            self.assertIn("只收 GET", payload["message"])
            self.assertIn("/heart/proactive", payload["message"])
            self.assertIn("人不能改心", payload["message"])


class GateRouteIsAReadOnlyJudgement(HeartApiTest):
    """出站闸的只读判读：与 /heart/engage 同一份门输入，两本账一个字节都不动。

    判据（人定）：端点只回答「现在真发，门会怎么判」——查询不是发生。所以它必须
    贴得出「调它前后 gate_log.jsonl 与 group_window.json 逐字节不变」，也不能替
    真正的 gate() 占掉一格群额度。
    """

    def _seed_a_send(self, group: str = "群A", now: str = T0) -> None:
        """先用真的 gate() 造一条「她说了」的账：门的写路径只走它自己。"""
        gate_module.gate(
            engage_module.EngageInput(
                mood_now=60.0, energy=80.0, familiarity=2.0, today_count=0,
                intent=heart_model.INTENT_PASSIVE, now=now),
            group=group, root=self.root, now=now)

    def test_gate_returns_peek_plus_the_readonly_shell(self) -> None:
        status, payload = self.get("/heart/gate", group="群A", now=T0)
        self.assertEqual(200, status, payload)
        for key in ("send", "state", "reason", "hits", "level", "limits", "quota",
                    "window_min", "ok", "readonly", "agent", "status"):
            self.assertIn(key, payload, "出站闸少了 %s" % key)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readonly"])
        self.assertEqual("heart", payload["agent"])
        self.assertEqual("已接入", payload["status"])
        self.assertIn(payload["state"], heart_model.STATES)

    def test_gate_writes_neither_ledger(self) -> None:
        self._seed_a_send()
        log = self.root / gate_module.LOG_FILE
        window = self.root / gate_module.WINDOW_FILE
        before = (log.read_bytes(), window.read_bytes())
        for _ in range(3):
            status, payload = self.get("/heart/gate", group="群A", now=T0)
            self.assertEqual(200, status, payload)
            self.assertTrue(payload["send"], "群窗里只有 1/6，还发得出去")
            # peek 报的是「这一判真发之后」的用量（1 + 这一格 = 2），与 gate 的返回同一个数；
            # 但那一格并没有真占：群账在下面逐字节不变，连调三次也还是 2。
            self.assertEqual(2, payload["quota"]["used"], "只读口不占额度")
        self.assertEqual(before, (log.read_bytes(), window.read_bytes()),
                         "调它前后 gate_log 与 group_window 逐字节不变（sha 不变）")
        self.assertEqual(1, len(store_module.read_jsonl(log)), "只读口不写账")
        self.assertEqual(1, len(json.loads(window.read_text(encoding="utf-8"))["群A"]),
                         "群窗里还是只有真正发过的那一格")

    def test_gate_reports_a_full_group_without_adding_a_stamp(self) -> None:
        for index in range(6):
            self._seed_a_send(now=_stamp(5, 9, index))
        window = self.root / gate_module.WINDOW_FILE
        before = window.read_bytes()
        status, payload = self.get("/heart/gate", group="群A", now=_stamp(5, 9, 10))
        self.assertEqual(200, status, payload)
        self.assertFalse(payload["send"])
        self.assertEqual("group", payload["level"], "放行的是人级，拦下的是群级")
        self.assertEqual(gate_module.REASON_GROUP_QUOTA, payload["reason"])
        self.assertEqual({"used": 6, "limit": 6, "left": 0}, payload["quota"])
        self.assertEqual(before, window.read_bytes(), "只读口不占额度")

    def test_gate_at_night_proactive_is_silent_and_writes_nothing(self) -> None:
        status, payload = self.get("/heart/gate", intent="proactive", now=_stamp(5, 23))
        self.assertEqual(200, status, payload)
        self.assertFalse(payload["send"], "夜里主动开口：这一轮不发")
        self.assertEqual("engage", payload["level"])
        self.assertEqual(heart_model.REASON_NIGHT, payload["reason"])
        self.assertIn(heart_model.REASON_NIGHT, payload["hits"])
        self.assertFalse((self.root / gate_module.LOG_FILE).exists(),
                         "只读口不写账：账是发生了才写的")

    def test_gate_without_a_group_reports_no_quota_usage(self) -> None:
        status, payload = self.get("/heart/gate", now=T0)
        self.assertEqual(200, status, payload)
        self.assertEqual({"used": 0, "limit": 6, "left": 6}, payload["quota"],
                         "私聊不占配额：used 恒为 0，limit 照给")
        self.assertFalse((self.root / gate_module.WINDOW_FILE).exists(),
                         "私聊连群账都不写")

    def test_gate_judges_with_the_same_input_as_engage(self) -> None:
        brain = _WarmBrain()
        combos = ({"intent": "passive", "now": T0},
                  {"intent": "proactive", "now": _stamp(5, 23)},
                  {"intent": "proactive", "insulted": "1", "now": T0})
        for combo in combos:
            status, verdict = self.get("/heart/engage", brain=brain, **combo)
            self.assertEqual(200, status, verdict)
            status, read = self.get("/heart/gate", brain=brain, **combo)
            self.assertEqual(200, status, read)
            self.assertEqual(verdict["state"], read["state"],
                             "干跑与出站闸的门输入必须是同一个数：%r" % (combo,))
            self.assertEqual(verdict["reason"], read["reason"])
            self.assertEqual(list(verdict["hits"]), read["hits"])

    def test_gate_rejects_an_unknown_intent(self) -> None:
        status, payload = self.get("/heart/gate", intent="shout", now=T0)
        self.assertEqual(400, status, payload)
        self.assertIn("intent 只认", payload["message"])
        self.assertFalse(payload["ok"])

    def test_write_methods_are_405_with_the_heart_wording(self) -> None:
        for method in ("POST", "PUT", "DELETE"):
            status, payload = api.handle(method, "/heart/gate", {}, None)
            self.assertEqual(405, status, method)
            self.assertIn("只收 GET", payload["message"])
            self.assertIn("/heart/gate", payload["message"])
            self.assertIn("人不能改心", payload["message"])


if __name__ == "__main__":
    unittest.main()
