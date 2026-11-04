"""心的八条只读端点：形状、405 措辞、坏卡不假装、轨迹、干跑、偏移键集、唯一合成点。

判据（architecture §9.2：心与脑只能看）：
- 八条路由全是 GET，写方法一律 405，且心的措辞点名「只收 GET」；
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
import re
import tempfile
import unittest
import warnings
from pathlib import Path
from typing import Any, Dict, List

from api import api
from api import heart as heart_api
from organs.heart import card as card_module
from organs.heart import energy as energy_module
from organs.heart import engage as engage_module
from organs.heart import model as heart_model
from organs.heart import mood as mood_module
from organs.heart import offset as offset_module

PLUGIN = Path(__file__).resolve().parents[2]
REAL_CARD = PLUGIN / "organs" / "heart" / "data" / "baseline-card.json"

HEART_ROUTES = ("/heart", "/heart/overview", "/heart/baseline", "/heart/mood",
                "/heart/energy", "/heart/offset", "/heart/engage", "/heart/effective")

# 固定时刻：断言不看"现在几点"。09:00 / 23:00 分别避开与落进夜间窗口（23-8）。
T0 = "2026-11-05T09:00:00+09:00"


def _stamp(day: int, hour: int, minute: int = 0) -> str:
    """造一个带 +09:00 的 ISO 时刻；跨天、跨时段都靠它，不依赖本机时区。"""
    return "2026-11-%02dT%02d:%02d:00+09:00" % (day, hour, minute)


class HeartApiTest(unittest.TestCase):
    """公共夹具：临时根 + 一张真卡（只读引用，绝不往仓里写）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "heart"
        self.root.mkdir()
        (self.root / card_module.CARD_FILE).write_bytes(REAL_CARD.read_bytes())

    def get(self, route: str, **params: Any) -> Any:
        """打一条 GET；root 一律指向临时根，payload 必须能 json.dumps。"""
        query = dict((key, str(value)) for key, value in params.items() if value is not None)
        query.setdefault("root", str(self.root))
        status, payload = api.handle("GET", route, None, None, query=query)
        json.dumps(payload, ensure_ascii=False)
        return status, payload


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
                        "mood", "energy", "offset", "pending", "effective"):
                self.assertIn(key, payload, "%s 少了 %s" % (route, key))
            self.assertEqual("已接入", payload["status"])
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["readonly"])
            self.assertEqual("heart", payload["agent"])
            self.assertEqual(["耐心", "记性"], payload["pending"])
            self.assertEqual([], payload["issues"])

    def test_baseline_is_tier_wordsonly_with_pending_axes_greyed(self):
        status, payload = self.get("/heart/baseline", now=T0)
        self.assertEqual(200, status)
        view = payload["baseline"]
        self.assertEqual(list(heart_model.PAIRED_AXES), view["loaded"])
        self.assertEqual([{"axis": axis, "greyed": True} for axis in heart_model.PENDING_AXES],
                         view["pending"])
        for axis, row in view["tiers"].items():
            self.assertIn(axis, heart_model.PAIRED_AXES)
            self.assertTrue(row["inner_word"])
            self.assertTrue(row["shown_word"])
        # 待定轴标灰、跳过注入：文本里不许出现 0-9，也不许给它们编档位词
        self.assertIsNone(re.search(r"[0-9]", payload["render"]), payload["render"])
        for axis in heart_model.PENDING_AXES:
            self.assertNotIn(axis, view["tiers"])
            self.assertIn(axis, payload["render"])

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
                # 「想黏人」「怕失去」故意缺；待定两轴照规矩写 pending
                {"id": "耐心", "pending": True},
                {"id": "记性", "pending": True},
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
        for axis in heart_model.PENDING_AXES:
            self.assertEqual({"greyed": True}, payload["axes"][axis],
                             "待定轴标灰且不含数值")
        for axis in heart_model.PAIRED_AXES:
            self.assertEqual(
                {"inner_value", "shown_value", "inner_tier", "shown_tier",
                 "inner_word", "shown_word", "offset", "greyed"},
                set(payload["axes"][axis]), axis)
            self.assertFalse(payload["axes"][axis]["greyed"], axis)

    def test_three_places_publish_one_number(self):
        mood_module.apply_event("conflict", root=self.root, now=_stamp(5, 8))
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
        self.assertEqual(set(heart_model.AXES), set(payload["axes"]), "六轴都要在（含待定两轴）")
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


if __name__ == "__main__":
    unittest.main()
