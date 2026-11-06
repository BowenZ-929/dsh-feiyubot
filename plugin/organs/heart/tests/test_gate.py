"""出站闸的验收：三态真的能决定发不发、群配额按滑窗拦、账写对、只读不落笔。

判据（人定，2026-11-06）：
- SILENT → 不发（账上留态 + 理由码）；LESS → 发但更短、不带反问；SPEAK → 正常发。
- 顺序钉死：先人级（engage 三门）→ 后群级（滑窗），任一不过就不发；同时成立时
  level 必须是 "engage"。
- 群配额 6 条 / 群 / 小时（滑窗），私聊不占、不写。
- gate 每次都写账（发也写、不发也写），peek 一个字节都不写、一格额度都不占。
- gate 不重判：判决只从 engage.decide 来（本文件最后一条用例钉住这件事）。

时间一律用带显式偏移的固定串，跑在哪个时区都同一个结论；数据一律落临时目录。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from organs.heart import engage, gate as gate_module, model, store
from organs.heart.tests import helpers

# 固定时刻：14:00 在白天（夜间窗口是 23-8），23:30 落在夜里。
DAY = "2024-05-06T14:00:00+08:00"
NIGHT = "2024-05-06T23:30:00+08:00"

#: 账行冻结的九个键（顺序也钉住：人读 JSONL 时先看 send，再看是哪一层拦的）。
ROW_KEYS = ["at", "who", "group", "send", "level", "state", "reason", "hits",
            "quota_used"]


def make_input(**kw: Any) -> "engage.EngageInput":
    """全绿为底的判决输入：只改想测的那一项。"""
    base: Dict[str, Any] = {
        "mood_now": 60.0,
        "energy": 80.0,
        "familiarity": 3.0,
        "today_count": 3,
        "now": DAY,
    }
    base.update(kw)
    return engage.EngageInput(**base)


def at_minutes(minutes: float, base: str = DAY) -> str:
    """base 之后 minutes 分钟的时刻串。"""
    return model.iso(model.parse_ts(base) + timedelta(minutes=float(minutes)))


class GateCase(unittest.TestCase):
    """公共夹具：临时根 + 账本读口。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)

    def rows(self) -> List[Dict[str, Any]]:
        return store.read_jsonl(self.root / gate_module.LOG_FILE)

    def window_file(self) -> Path:
        return self.root / gate_module.WINDOW_FILE

    def digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "(无此文件)"


class SilentMeansNoSend(GateCase):
    """SILENT 真的不发：账上写清「她这轮不想说」+ 理由码。"""

    def test_night_proactive_is_silent_and_logged(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            readout = gate_module.gate(
                make_input(intent=model.INTENT_PROACTIVE, now=NIGHT),
                group="群A", root=self.root, now=NIGHT)
        self.assertFalse(readout["send"], "夜里主动开口：这一轮不发")
        self.assertEqual("engage", readout["level"], "人级拦的")
        self.assertEqual(model.STATE_SILENT, readout["state"])
        self.assertEqual(model.REASON_NIGHT, readout["reason"])
        self.assertIn(model.REASON_NIGHT, readout["hits"])
        self.assertEqual({}, readout["limits"], "都不发了，没有说话限制可谈")
        text = " ".join(str(item.message) for item in caught)
        self.assertIn("人级", text, text)
        self.assertIn("night", text, text)
        self.assertIn("不想说", text, text)
        rows = self.rows()
        self.assertEqual(1, len(rows), "不发也要落账")
        self.assertEqual(ROW_KEYS, list(rows[0].keys()), "账行的九个键冻结")
        self.assertIs(False, rows[0]["send"])
        self.assertEqual("不说", rows[0]["state"])
        self.assertEqual("night", rows[0]["reason"])
        self.assertEqual("engage", rows[0]["level"])
        self.assertEqual("群A", rows[0]["group"])
        self.assertEqual(0, rows[0]["quota_used"], "人级拦下时不占群额度")

    def test_double_low_is_silent(self) -> None:
        readout = gate_module.gate(
            make_input(mood_now=29.0, energy=20.0), group=None,
            root=self.root, now=DAY)
        self.assertFalse(readout["send"])
        self.assertEqual("engage", readout["level"])
        self.assertEqual(model.REASON_DOUBLE_LOW, readout["reason"])
        self.assertEqual(model.STATE_SILENT, readout["state"])

    def test_silent_does_not_touch_the_group_window(self) -> None:
        gate_module.gate(make_input(intent=model.INTENT_PROACTIVE, now=NIGHT),
                         group="群A", root=self.root, now=NIGHT)
        self.assertFalse(self.window_file().exists(),
                         "不想说的那一轮不许吃掉一格群额度")


class LessMeansShorterAndNoQuestion(GateCase):
    """LESS 发，但更短、且不递话头。"""

    def test_today_volume_judges_less(self) -> None:
        readout = gate_module.gate(make_input(today_count=20), group=None,
                                   root=self.root, now=DAY)
        self.assertTrue(readout["send"], "少说不是不说")
        self.assertIsNone(readout["level"], "没拦就不报层级")
        self.assertEqual(model.STATE_LESS, readout["state"])
        self.assertEqual(model.REASON_TODAY_VOLUME, readout["reason"])
        self.assertEqual({"max_chars": 48, "no_question": True}, readout["limits"])
        rows = self.rows()
        self.assertEqual(1, len(rows))
        self.assertIs(True, rows[0]["send"])
        self.assertIsNone(rows[0]["level"])
        self.assertEqual("少说", rows[0]["state"])
        self.assertEqual("today-volume", rows[0]["reason"])

    def test_less_caps_come_from_the_file(self) -> None:
        helpers.write_raw(self.root / gate_module.CONFIG_FILE,
                          json.dumps({"less_max_chars": 20}))
        readout = gate_module.gate(make_input(today_count=20), group=None,
                                   root=self.root, now=DAY)
        self.assertEqual({"max_chars": 20, "no_question": True}, readout["limits"],
                         "数值进数据文件可改")

    def test_less_still_takes_a_group_slot(self) -> None:
        readout = gate_module.gate(make_input(today_count=20), group="群A",
                                   root=self.root, now=DAY)
        self.assertTrue(readout["send"])
        self.assertEqual(1, readout["quota"]["used"], "少说也是发，也占一格")
        self.assertEqual({"群A": [DAY]}, helpers.read_json_file(self.window_file()))


class SpeakMeansNormal(GateCase):
    """SPEAK 正常发：没有任何限制。"""

    def test_speak_has_no_limits(self) -> None:
        readout = gate_module.gate(make_input(), group=None, root=self.root, now=DAY)
        self.assertTrue(readout["send"])
        self.assertIsNone(readout["level"])
        self.assertEqual(model.STATE_SPEAK, readout["state"])
        self.assertIsNone(readout["reason"], "判「说」就没有主因码")
        self.assertEqual([], readout["hits"])
        self.assertEqual({}, readout["limits"])
        self.assertEqual({"used": 0, "limit": 6, "left": 6}, readout["quota"],
                         "私聊不占：used 恒为 0，limit 照给")
        self.assertEqual(60, readout["window_min"])

    def test_log_row_carries_who_and_the_nine_keys(self) -> None:
        gate_module.gate(make_input(), root=self.root, now=DAY)
        row = self.rows()[0]
        self.assertEqual(ROW_KEYS, list(row.keys()))
        self.assertEqual("主人", row["who"])
        self.assertIsNone(row["group"], "私聊的 group 是 null")
        self.assertTrue(row["send"])


class GroupQuotaIsSoftAndPerGroup(GateCase):
    """群配额 6 条 / 群 / 小时：第 7 次拦，换群照常，私聊不占。"""

    def test_seventh_send_in_an_hour_is_blocked(self) -> None:
        lefts: List[int] = []
        for index in range(6):
            readout = gate_module.gate(make_input(now=at_minutes(index)),
                                       group="群A", root=self.root,
                                       now=at_minutes(index))
            self.assertTrue(readout["send"], "第 %d 次该放行" % (index + 1))
            lefts.append(readout["quota"]["left"])
        self.assertEqual([5, 4, 3, 2, 1, 0], lefts, "quota.left 逐次递减")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            blocked = gate_module.gate(make_input(now=at_minutes(7)),
                                       group="群A", root=self.root,
                                       now=at_minutes(7))
        self.assertFalse(blocked["send"], "第 7 次必须拦")
        self.assertEqual("group", blocked["level"], "这次是群级拦的")
        self.assertEqual(gate_module.REASON_GROUP_QUOTA, blocked["reason"])
        self.assertEqual(model.STATE_SPEAK, blocked["state"], "她本人还是想说的")
        self.assertEqual({"used": 6, "limit": 6, "left": 0}, blocked["quota"])
        text = " ".join(str(item.message) for item in caught)
        self.assertIn("群级", text, text)
        self.assertIn("group-quota", text, text)
        rows = self.rows()
        self.assertEqual(7, len(rows), "被拦的这一次也要落账")
        self.assertIs(False, rows[6]["send"])
        self.assertEqual("group", rows[6]["level"])
        self.assertEqual("group-quota", rows[6]["reason"])
        self.assertEqual(6, rows[6]["quota_used"])

    def test_another_group_starts_with_a_fresh_window(self) -> None:
        for index in range(6):
            gate_module.gate(make_input(now=at_minutes(index)), group="群A",
                             root=self.root, now=at_minutes(index))
        readout = gate_module.gate(make_input(now=at_minutes(7)), group="群B",
                                   root=self.root, now=at_minutes(7))
        self.assertTrue(readout["send"], "换群立刻能发：冷却按群算")
        self.assertEqual(1, readout["quota"]["used"])
        self.assertEqual(5, readout["quota"]["left"])
        window = helpers.read_json_file(self.window_file())
        self.assertEqual({"群A", "群B"}, set(window), "两个群的账各记各的")
        self.assertEqual(6, len(window["群A"]))
        self.assertEqual(1, len(window["群B"]))

    def test_private_chat_sends_ten_times_and_never_occupies(self) -> None:
        for index in range(10):
            readout = gate_module.gate(make_input(now=at_minutes(index)),
                                       group=None, root=self.root,
                                       now=at_minutes(index))
            self.assertTrue(readout["send"], "私聊第 %d 次也该发" % (index + 1))
            self.assertEqual({"used": 0, "limit": 6, "left": 6}, readout["quota"])
        self.assertFalse(self.window_file().exists(),
                         "私聊连一次群账都不该写：%s" % self.window_file())

    def test_private_chat_leaves_an_existing_group_ledger_alone(self) -> None:
        gate_module.gate(make_input(now=DAY), group="群A", root=self.root, now=DAY)
        before = self.window_file().read_bytes()
        for index in range(10):
            gate_module.gate(make_input(now=at_minutes(index + 1)), group="",
                             root=self.root, now=at_minutes(index + 1))
        self.assertEqual(before, self.window_file().read_bytes(),
                         "私聊不写、不占：群账逐字节不变")

    def test_window_slides_an_hour_later(self) -> None:
        for index in range(6):
            gate_module.gate(make_input(now=at_minutes(index)), group="群A",
                             root=self.root, now=at_minutes(index))
        # 最后一次是 +5 分钟；再往后 61 分钟 = +66 时，六条都已经超过 60 分钟窗口。
        readout = gate_module.gate(make_input(now=at_minutes(66)), group="群A",
                                   root=self.root, now=at_minutes(66))
        self.assertTrue(readout["send"], "一小时前的 6 条已经滑出窗口")
        self.assertEqual(1, readout["quota"]["used"])
        self.assertEqual([at_minutes(66)],
                         helpers.read_json_file(self.window_file())["群A"])

    def test_expired_stamps_are_not_counted(self) -> None:
        stale = [at_minutes(-120 - index) for index in range(6)]
        helpers.write_raw(self.window_file(), json.dumps({"群A": stale}))
        readout = gate_module.gate(make_input(now=DAY), group="群A",
                                   root=self.root, now=DAY)
        self.assertTrue(readout["send"], "两小时前的旧账不算数")
        self.assertEqual(1, readout["quota"]["used"])
        self.assertEqual([DAY], helpers.read_json_file(self.window_file())["群A"],
                         "占格时顺手把过期的剪掉，账里只留窗口内的")

    def test_quota_limit_comes_from_the_file(self) -> None:
        helpers.write_raw(self.root / gate_module.CONFIG_FILE,
                          json.dumps({"group_quota_per_hour": 2}))
        gate_module.gate(make_input(now=at_minutes(0)), group="群A", root=self.root,
                         now=at_minutes(0))
        gate_module.gate(make_input(now=at_minutes(1)), group="群A", root=self.root,
                         now=at_minutes(1))
        blocked = gate_module.gate(make_input(now=at_minutes(2)), group="群A",
                                   root=self.root, now=at_minutes(2))
        self.assertFalse(blocked["send"])
        self.assertEqual(2, blocked["quota"]["limit"])


class EngageLayerRunsFirst(GateCase):
    """先人级后群级：两层同时不过时，level 必须是 engage。"""

    def test_silent_wins_over_a_full_group(self) -> None:
        for index in range(6):
            gate_module.gate(make_input(now=at_minutes(index)), group="群A",
                             root=self.root, now=at_minutes(index))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            readout = gate_module.gate(
                make_input(intent=model.INTENT_PROACTIVE, now=NIGHT),
                group="群A", root=self.root, now=at_minutes(8))
        self.assertFalse(readout["send"])
        self.assertEqual("engage", readout["level"], "人级在前：这条不许报成 group")
        self.assertEqual(model.REASON_NIGHT, readout["reason"],
                         "理由码是人级的 night，不是 group-quota")
        self.assertEqual(6, readout["quota"]["used"], "只读报数，不再占格")
        self.assertEqual(6, len(helpers.read_json_file(self.window_file())["群A"]))
        text = " ".join(str(item.message) for item in caught)
        self.assertIn("人级", text, text)
        self.assertNotIn("群级", text, text)

    def test_a_blocked_person_still_cannot_evade_the_group_layer_later(self) -> None:
        for index in range(6):
            gate_module.gate(make_input(now=at_minutes(index)), group="群A",
                             root=self.root, now=at_minutes(index))
        blocked = gate_module.gate(make_input(now=at_minutes(9)), group="群A",
                                   root=self.root, now=at_minutes(9))
        self.assertEqual("group", blocked["level"])


class PeekIsReadOnly(GateCase):
    """peek：同样的判断，不写账、不占额度（只读端点靠它）。"""

    def test_peek_returns_the_same_judgement(self) -> None:
        inp = make_input(now=DAY)
        peeked = gate_module.peek(inp, group="群A", root=self.root, now=DAY)
        sent = gate_module.gate(inp, group="群A", root=self.root, now=DAY)
        self.assertEqual(sent, peeked, "peek 与真发必须是同一个判断")

    def test_peek_writes_nothing_and_occupies_nothing(self) -> None:
        gate_module.gate(make_input(now=DAY), group="群A", root=self.root, now=DAY)
        log_before = self.digest(self.root / gate_module.LOG_FILE)
        window_before = self.digest(self.window_file())
        for index in range(10):
            gate_module.peek(make_input(now=at_minutes(index + 1)), group="群A",
                             root=self.root, now=at_minutes(index + 1))
        self.assertEqual(log_before, self.digest(self.root / gate_module.LOG_FILE),
                         "peek 不写账")
        self.assertEqual(window_before, self.digest(self.window_file()),
                         "peek 不占额度（账逐字节不变）")

    def test_peek_reports_a_full_group_without_adding_a_stamp(self) -> None:
        for index in range(6):
            gate_module.gate(make_input(now=at_minutes(index)), group="群A",
                             root=self.root, now=at_minutes(index))
        before = self.window_file().read_bytes()
        readout = gate_module.peek(make_input(now=at_minutes(7)), group="群A",
                                   root=self.root, now=at_minutes(7))
        self.assertFalse(readout["send"])
        self.assertEqual("group", readout["level"])
        self.assertEqual(gate_module.REASON_GROUP_QUOTA, readout["reason"])
        self.assertEqual(before, self.window_file().read_bytes())

    def test_peek_creates_no_config_file(self) -> None:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            gate_module.peek(make_input(), group=None, root=self.root, now=DAY)
        self.assertFalse((self.root / gate_module.CONFIG_FILE).exists(),
                         "只读口不落配置：落盘是 gate 的事")


class ConfigFile(GateCase):
    """gate_config.json：落盘、人可改、读不懂就默认 + WARN、永不抛。"""

    def test_first_gate_writes_the_defaults(self) -> None:
        gate_module.gate(make_input(), root=self.root, now=DAY)
        written = helpers.read_json_file(self.root / gate_module.CONFIG_FILE)
        self.assertEqual({"group_quota_per_hour": 6, "group_window_min": 60,
                          "less_max_chars": 48}, written)
        self.assertEqual(gate_module.CONFIG_DEFAULTS, written)

    def test_broken_config_falls_back_to_defaults_with_a_warning(self) -> None:
        helpers.write_raw(self.root / gate_module.CONFIG_FILE, "{ 这不是 JSON")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            readout = gate_module.gate(make_input(), group="群A", root=self.root,
                                       now=DAY)
        self.assertTrue(readout["send"], "坏配置不影响判决")
        self.assertEqual(6, readout["quota"]["limit"])
        text = " ".join(str(item.message) for item in caught)
        self.assertIn("读不懂", text, text)
        self.assertIn("内置默认", text, text)
        self.assertEqual("{ 这不是 JSON",
                         (self.root / gate_module.CONFIG_FILE).read_text(
                             encoding="utf-8"),
                         "读不懂的那份不覆盖，等人自己修")

    def test_missing_config_only_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            readout = gate_module.peek(make_input(), root=self.root, now=DAY)
        self.assertTrue(readout["send"])
        self.assertEqual(6, readout["quota"]["limit"])
        self.assertTrue(any("还没有" in str(item.message) for item in caught),
                        "读不到要 WARN：%r" % [str(i.message) for i in caught])

    def test_zero_values_are_clamped_to_sane_ones(self) -> None:
        helpers.write_raw(self.root / gate_module.CONFIG_FILE, json.dumps({
            "group_quota_per_hour": 0, "group_window_min": 0, "less_max_chars": 0}))
        snapshot = gate_module.overview(self.root, DAY)
        self.assertEqual(1, snapshot["config"]["group_quota_per_hour"],
                         "0 条不是「关掉配额」，是「出口彻底关死」")
        self.assertEqual(1, snapshot["config"]["group_window_min"])
        self.assertEqual(1, snapshot["config"]["less_max_chars"])


class OverviewIsReadOnly(GateCase):
    """overview：config + 每个群当前用量，纯读。"""

    def test_overview_counts_each_group(self) -> None:
        gate_module.gate(make_input(now=at_minutes(0)), group="群A", root=self.root,
                         now=at_minutes(0))
        gate_module.gate(make_input(now=at_minutes(1)), group="群A", root=self.root,
                         now=at_minutes(1))
        gate_module.gate(make_input(now=at_minutes(2)), group="群B", root=self.root,
                         now=at_minutes(2))
        log_before = self.digest(self.root / gate_module.LOG_FILE)
        snapshot = gate_module.overview(self.root, at_minutes(3))
        self.assertEqual({"group_quota_per_hour": 6, "group_window_min": 60,
                          "less_max_chars": 48}, snapshot["config"])
        self.assertEqual(60, snapshot["window_min"])
        self.assertEqual({"used": 2, "limit": 6, "left": 4},
                         snapshot["groups"]["群A"])
        self.assertEqual({"used": 1, "limit": 6, "left": 5},
                         snapshot["groups"]["群B"])
        self.assertEqual(log_before, self.digest(self.root / gate_module.LOG_FILE),
                         "总览不写账")

    def test_overview_drops_expired_stamps_without_rewriting_the_file(self) -> None:
        helpers.write_raw(self.window_file(), json.dumps(
            {"群A": [at_minutes(-180), DAY]}))
        before = self.window_file().read_bytes()
        snapshot = gate_module.overview(self.root, DAY)
        self.assertEqual(1, snapshot["groups"]["群A"]["used"], "三小时前的那条不算")
        self.assertEqual(before, self.window_file().read_bytes(),
                         "只读口不剪文件")


class GateDelegatesTheJudgement(GateCase):
    """gate 不重判：态只从 engage.decide 来（判据一个字都不许在本文件里复制）。"""

    def test_state_comes_from_engage_decide(self) -> None:
        crafted = {"state": model.STATE_SPEAK, "reason": None, "hits": [], "hard": [],
                   "soft": [], "soft_skipped": False, "readouts": {}, "score": 0.0,
                   "window": None, "intents": None}
        seen: List[Any] = []

        def fake_decide(inp: Any) -> Dict[str, Any]:
            seen.append(inp)
            return dict(crafted)

        with mock.patch.object(engage, "decide", side_effect=fake_decide):
            readout = gate_module.gate(make_input(mood_now=0.0, energy=0.0, now=NIGHT),
                                       group=None, root=self.root, now=NIGHT)
        self.assertEqual(1, len(seen), "gate 必须调 engage.decide，且只调一次")
        self.assertTrue(readout["send"], "判决说「说」，门就放行——门不自己重判")
        self.assertEqual(model.STATE_SPEAK, readout["state"])
        self.assertFalse(engage._hard_hits(seen[0], NIGHT) == [],
                         "输入本身是夜间硬门（证明门没在自己的代码里重判）")

    def test_engage_module_is_untouched(self) -> None:
        """门的判决与直接调 decide 逐字一致（态 / 理由 / 命中集）。"""
        for inp in (make_input(intent=model.INTENT_PROACTIVE, now=NIGHT),
                    make_input(today_count=20),
                    make_input(mood_now=29.0, energy=20.0),
                    make_input()):
            direct = engage.decide(inp)
            readout = gate_module.peek(inp, root=self.root, now=inp.now)
            self.assertEqual(direct["state"], readout["state"])
            self.assertEqual(direct["reason"], readout["reason"])
            self.assertEqual(list(direct["hits"]), readout["hits"])


if __name__ == "__main__":
    unittest.main()
