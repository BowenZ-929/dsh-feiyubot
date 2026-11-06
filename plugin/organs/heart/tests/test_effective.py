"""effective_state：全仓唯一合成点的契约，以及「三处取值逐字相同」的证据。

判据（病根见 organs/heart/effective.py 模块头）：
- 面板说档位词、engage 说原始数——两套换算一旦分叉，就会出现「面板显示还行，她却
  闭嘴」。所以本文件第一条断言就是三方相等：organ 的 effective_state、/heart/effective、
  /heart/engage 的 readouts.mood_now 必须是**同一个数**；只比两方不算数。
- 档界只在 mood / card 里：本模块一个数字都不重写，也不做任何时间衰减。
- 六轴完整（2026-11-06 人定稿）：六轴都有 inner/shown 与档位词，没有一根标灰。
- render_text 与 card.render_text 逐字相同，且输出里 0-9 一个都没有。

说明：本文件 import 了 api（organs → api 的唯一一处），因为「三处取值一致」这句判据
天生跨层——它证明的正是 organs 的合成点与 api 的两个端点没有各自算一遍。
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from api import api
from organs.heart import card as card_module
from organs.heart import effective as effective_module
from organs.heart import engage as engage_module
from organs.heart import model as heart_model
from organs.heart import mood as mood_module
from organs.heart import offset as offset_module

PLUGIN = Path(__file__).resolve().parents[3]
REAL_CARD = PLUGIN / "organs" / "heart" / "data" / "baseline-card.json"

T0 = "2026-11-05T09:00:00+09:00"
T_APPLIED = "2026-11-05T08:00:00+09:00"


class EffectiveBase(unittest.TestCase):
    """临时根 + 真卡（只读引用，绝不往仓里写）；api 一律用 root= 指过来。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "heart"
        self.root.mkdir()
        (self.root / card_module.CARD_FILE).write_bytes(REAL_CARD.read_bytes())

    def get(self, route: str, **params: Any) -> Dict[str, Any]:
        query = dict((key, str(value)) for key, value in params.items() if value is not None)
        query.setdefault("root", str(self.root))
        status, payload = api.handle("GET", route, None, None, query=query)
        self.assertEqual(200, status, payload)
        json.dumps(payload, ensure_ascii=False)
        return payload

    def card(self) -> card_module.Card:
        return card_module.load_card(self.root / card_module.CARD_FILE)

    def organ_state(self, now: str = T0, offsets: Any = None) -> Dict[str, Any]:
        """直接调合成点：输入取 mood / offset 自己的读数（与 api 的取法一致）。"""
        values = mood_module.read(root=self.root, now=now)
        return effective_module.effective_state(
            values["baseline"], values["now"],
            offset=offset_module.overview(root=self.root, now=now) if offsets is None else offsets,
            card_obj=self.card(), now=now)


class ThreeSourcesAreTheSameNumber(EffectiveBase):
    """三方相等：organ 的合成点 / /heart/effective / /heart/engage（只比两方不算数）。"""

    def setUp(self) -> None:
        super().setUp()
        # 让数字离开默认值：一次冲突 = −2 档，读书时再掉一小截（衰减在 mood.read 里）。
        mood_module.apply_event("conflict", root=self.root, now=T_APPLIED)

    def test_mood_now_is_one_number_in_three_places(self):
        organ = self.organ_state(T0)
        api_eff = self.get("/heart/effective", now=T0)
        engaged = self.get("/heart/engage", now=T0)

        self.assertAlmostEqual(44.1, organ["mood"]["now"], places=1,
                               msg="先钉住这个数，免得三方相等只是三处都空着")
        self.assertEqual(organ["mood"]["now"], api_eff["mood"]["now"],
                         "organ 的合成点与 /heart/effective 不是同一个数")
        self.assertEqual(api_eff["mood"]["now"], engaged["readouts"]["mood_now"],
                         "/heart/effective 与 /heart/engage 不是同一个数")
        self.assertEqual(organ["mood"]["now"], engaged["readouts"]["mood_now"],
                         "organ 的合成点与 /heart/engage 不是同一个数")
        self.assertEqual(organ["mood"]["baseline"], api_eff["mood"]["baseline"])

    def test_mood_route_publishes_the_same_numbers(self):
        organ = self.organ_state(T0)
        mood_route = self.get("/heart/mood", now=T0)
        self.assertEqual(organ["mood"]["now"], mood_route["now"])
        self.assertEqual(organ["mood"]["baseline"], mood_route["baseline"])

    def test_voice_tone_is_the_mood_tier_and_the_route_publishes_that_tier(self):
        organ = self.organ_state(T0)
        api_eff = self.get("/heart/effective", now=T0)
        mood_route = self.get("/heart/mood", now=T0)

        self.assertEqual(organ["mood"]["tier"], organ["voice"]["tone"],
                         "voice.tone 就是 mood 那一档的词")
        self.assertEqual(organ["mood"]["tier"], api_eff["voice"]["tone"])
        self.assertEqual(api_eff["voice"]["tone"], api_eff["mood"]["tier"])
        self.assertEqual(api_eff["voice"]["tone"], mood_route["tier"],
                         "面板从 /heart/mood 读到的档位词与语气档不是同一个词")
        self.assertEqual(api_eff["voice"]["direction"], mood_route["direction"])
        # 档位词不是默认那一档：反过来证明上面那条相等不是「都空着」凑出来的
        self.assertEqual("偏冷", api_eff["voice"]["tone"])

    def test_overview_embeds_the_same_three_blocks(self):
        overview = self.get("/heart/overview", now=T0)
        api_eff = self.get("/heart/effective", now=T0)
        self.assertIn("effective", overview, "overview 要带上合成读数")
        for key in ("mood", "axes", "voice"):
            self.assertEqual(api_eff[key], overview["effective"][key],
                             "overview 的 %s 与 /heart/effective 分叉了" % key)
        self.assertEqual(overview["mood"]["now"], api_eff["mood"]["now"])
        # 既有键一个都不许少
        for key in ("status", "ok", "readonly", "agent", "baseline", "render",
                    "mood", "energy", "offset", "pending", "issues"):
            self.assertIn(key, overview, "overview 少了既有键 %s" % key)


class AxesComeFromCardPlusOffset(EffectiveBase):
    def test_value_is_card_value_plus_that_axis_offset(self):
        card = self.card()
        deltas = {"服气": 1.0, "心疼": -0.5, "想黏人": 2.0, "怕失去": 0.0}
        state = effective_module.effective_state(60, 60, offset=deltas, card_obj=card)
        for axis, delta in deltas.items():
            row = state["axes"][axis]
            low, high = card.scale_min, card.scale_max
            self.assertEqual(
                card_module._bounded(card.axes[axis].inner + delta, low, high),
                row["inner_value"], axis)
            self.assertEqual(
                card_module._bounded(card.axes[axis].shown + delta, low, high),
                row["shown_value"], axis)
            self.assertEqual(delta, row["offset"], axis)
            self.assertFalse(row["greyed"], axis)

    def test_tier_and_word_come_from_card_not_from_effective(self):
        card = self.card()
        deltas = {"服气": 1.0, "心疼": -2.0, "想黏人": 3.0, "怕失去": -1.0}
        state = effective_module.effective_state(60, 60, offset=deltas, card_obj=card)
        view = card_module.view(card, deltas)
        for axis in heart_model.PAIRED_AXES:
            row, tier = state["axes"][axis], view["tiers"][axis]
            self.assertEqual(tier["inner_tier"], row["inner_tier"], axis)
            self.assertEqual(tier["shown_tier"], row["shown_tier"], axis)
            self.assertEqual(tier["inner_word"], row["inner_word"], axis)
            self.assertEqual(tier["shown_word"], row["shown_word"], axis)
            self.assertEqual(card_module.tier_word(axis, row["inner_tier"]),
                             row["inner_word"], axis)

    def test_offset_overview_shape_is_accepted_too(self):
        offsets = offset_module.overview(root=self.root, now=T0)
        state = effective_module.effective_state(60, 60, offset=offsets, card_obj=self.card())
        self.assertEqual(0.0, state["axes"]["服气"]["offset"], "空状态按 0 起算")
        offset_module.apply_daily("服气", 1.0, root=self.root, now=T0)
        offsets = offset_module.overview(root=self.root, now=T0)
        state = effective_module.effective_state(60, 60, offset=offsets, card_obj=self.card())
        self.assertAlmostEqual(1.0, state["axes"]["服气"]["offset"], places=6)
        self.assertEqual(float(self.card().axes["服气"].inner + 1.0),
                         float(state["axes"]["服气"]["inner_value"]))

    def test_missing_or_broken_offset_is_zero(self):
        card = self.card()
        for offsets in (None, {}, {"axes": None}, {"服气": "坏值"}, {"unknown": 5.0}):
            state = effective_module.effective_state(60, 60, offset=offsets, card_obj=card)
            self.assertEqual(0.0, state["axes"]["服气"]["offset"], repr(offsets))
            self.assertEqual(card.axes["服气"].inner, state["axes"]["服气"]["inner_value"])

    def test_all_six_axes_carry_numbers_and_words(self):
        state = effective_module.effective_state(60, 60, card_obj=self.card())
        self.assertEqual(set(heart_model.AXES), set(state["axes"]))
        for axis in heart_model.AXES:
            row = state["axes"][axis]
            self.assertFalse(row["greyed"], axis)
            for key in ("inner_value", "shown_value", "inner_tier", "shown_tier",
                        "inner_word", "shown_word", "offset"):
                self.assertIn(key, row, "%s 少了 %s" % (axis, key))
            self.assertIsNotNone(row["inner_word"], axis)
            self.assertIsNotNone(row["shown_word"], axis)


class VoiceBans(EffectiveBase):
    def test_bans_are_the_card_rows_hit_by_the_effective_tier(self):
        card = self.card()
        state = effective_module.effective_state(60, 60, card_obj=card)
        expected: list = []
        for axis in heart_model.PAIRED_AXES:
            row = state["axes"][axis]
            for tier in (row["inner_tier"], row["shown_tier"]):
                ban = card_module.TIER_BANS.get(axis, {}).get(tier)
                if ban and ban not in expected:
                    expected.append(ban)
        self.assertEqual(expected, state["voice"]["bans"])
        self.assertTrue(state["voice"]["bans"], "真卡至少命中一条禁令，否则这条断言是空的")
        for ban in state["voice"]["bans"]:
            self.assertIsInstance(ban, str)
            self.assertTrue(ban)
            self.assertIsNone(re.search(r"[0-9]", ban), ban)

    def test_no_ban_means_an_empty_list(self):
        """没命中就没有禁令：bans 是空表，不是 None、不是「无」。"""
        state = effective_module.effective_state(60, 60, card_obj=self.card())
        self.assertIsInstance(state["voice"]["bans"], list)
        self.assertEqual([], effective_module._all_bans({}), "六轴都没命中时不许凭空造禁令")
        # 真卡的耐心是第四档（有耐心），禁令只写 1–2 档 → 这一轴确实一条都不出
        row = effective_module.axis_rows(self.card())["耐心"]
        self.assertEqual(4, row["inner_tier"])
        self.assertEqual([], effective_module._axis_bans("耐心", row))


class BadCardStillRaises(EffectiveBase):
    def test_card_error_propagates_to_the_caller(self):
        (self.root / card_module.CARD_FILE).write_text("{不是 JSON", encoding="utf-8")
        with self.assertRaises(card_module.CardError):
            effective_module.effective_state(60, 60, offset={}, root=self.root / card_module.CARD_FILE)

class RootIsADataDirectory(EffectiveBase):
    """root 按数据目录解释；这一组只调模块，不借 api 层。

    模块层要能独立工作：端点绿不代表函数对——api/heart.py 曾经自己
    load_card(_card_path(root)) 再把 card_obj 传进来，于是"effective_state(root=数据目录)
    加载不到卡"这个洞只走端点永远看不见。
    """

    def test_module_level_call_with_a_data_directory(self):
        state = effective_module.effective_state(60.0, 29.0, root=self.root, now=T0)
        self.assertEqual({"now", "baseline", "tier", "direction"}, set(state["mood"]))
        self.assertEqual(29.0, state["mood"]["now"])
        self.assertEqual({"tone", "direction", "bans"}, set(state["voice"]))
        self.assertEqual(list(heart_model.AXES), list(state["axes"]))
        for axis in heart_model.AXES:
            self.assertFalse(state["axes"][axis]["greyed"], axis)
            self.assertTrue(state["axes"][axis]["inner_word"], axis)
            self.assertTrue(state["axes"][axis]["shown_word"], axis)
        self.assertEqual(state["mood"]["tier"], state["voice"]["tone"])

    def test_card_path_takes_the_directory_then_a_file(self):
        card_file = self.root / card_module.CARD_FILE
        self.assertEqual(card_file, effective_module.card_path(self.root), "目录 → 目录里的卡")
        self.assertEqual(card_file, effective_module.card_path(card_file), "文件路径也认")
        self.assertIsNone(effective_module.card_path(None), "None 交给 card 自己的默认路径")

    def test_empty_data_directory_raises_card_missing(self):
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(card_module.CardError) as caught:
            effective_module.effective_state(60.0, 29.0, root=empty)
        self.assertEqual(card_module.CARD_MISSING, caught.exception.code)
        self.assertEqual(card_module.CARD_MISSING, caught.exception.issues[0]["code"])
        self.assertIn(str(empty / card_module.CARD_FILE),
                      caught.exception.issues[0]["message"],
                      "报错要点名它去找的那个文件，不是那个目录")

    def test_a_lonely_card_file_path_also_works(self):
        state = effective_module.effective_state(
            60, 60, offset={}, root=self.root / card_module.CARD_FILE)
        self.assertTrue(state["axes"]["服气"]["inner_word"])


class RenderTextIsUnchanged(EffectiveBase):
    def test_effective_render_text_matches_card_render_text(self):
        card = self.card()
        for offsets in (None, {}, {"服气": -0.5, "心疼": 1.4, "想黏人": -1.5, "怕失去": 2.0},
                        {"服气": 5.0, "心疼": -5.0}):
            self.assertEqual(card_module.render_text(card, offsets),
                             effective_module.render_text(card, offsets), repr(offsets))

    def test_render_text_has_no_digits_and_all_six_lines(self):
        text = card_module.render_text(self.card())
        self.assertIsNone(re.search(r"[0-9]", text), text)
        lines = text.splitlines()
        for axis in heart_model.AXES:
            self.assertTrue(any(line.startswith("- %s：" % axis) for line in lines),
                            "%s 这一行不许缺" % axis)
        self.assertEqual(len(heart_model.AXES), text.count("\n- "), "六条一行不缺")
        self.assertNotIn("None", text, "查不到档位词就会渲成 None")


class NoDecayAndNoSecondRuler(EffectiveBase):
    """本模块不许自己算衰减，也不许重写档界——源码里看得见。"""

    def setUp(self) -> None:
        super().setUp()
        self.source = (PLUGIN / "organs" / "heart" / "effective.py").read_text(encoding="utf-8")

    def test_no_time_decay_in_this_module(self):
        self.assertNotIn("0.5 **", self.source)
        self.assertNotIn("半衰", self.source)
        self.assertNotIn("HALF_LIFE", self.source)

    def test_no_tier_boundaries_of_its_own(self):
        for needle in ("cold_edge", "MOOD_UNIT", "MOOD_LOW", "TIER_COUNT", "_TIERS"):
            self.assertNotIn(needle, self.source, "合成点里出现了第二套档界：%s" % needle)

    def test_qualified_tier_lookups_happen_only_here(self):
        """card.tier_of / mood.tier_of 的调用点只有这一处（card.py 内是定义与自用）。"""
        sources = sorted((PLUGIN / "organs" / "heart").glob("*.py"))
        sources.append(PLUGIN / "api" / "heart.py")
        offenders = []
        for path in sources:
            if path.name == "effective.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(needle in line for needle in ("card.tier_of(", "mood.tier_of(",
                                                     "card_module.tier_of(", "mood_module.tier_of(")):
                    offenders.append("%s:%d: %s" % (path.name, number, line.strip()))
        self.assertEqual([], offenders, "档界换算只许在 effective.py 里调：\n" + "\n".join(offenders))


class FromEffectiveFeedsTheGates(EffectiveBase):
    def test_from_effective_takes_mood_now(self):
        state = effective_module.effective_state(60, 29.0, offset={}, card_obj=self.card())
        inp = engage_module.EngageInput.from_effective(
            state, energy=20.0, familiarity=3.0, today_count=0,
            intent=heart_model.INTENT_PASSIVE, now=T0)
        self.assertEqual(29.0, inp.mood_now)
        judgement = engage_module.decide(inp)
        self.assertEqual(heart_model.STATE_SILENT, judgement["state"])
        self.assertIn(heart_model.REASON_DOUBLE_LOW, judgement["hits"])
        self.assertEqual(29.0, judgement["readouts"]["mood_now"])

    def test_from_effective_refuses_readings_without_mood(self):
        with self.assertRaises(ValueError):
            engage_module.EngageInput.from_effective({}, 20.0, 3.0, 0)
        with self.assertRaises(ValueError):
            engage_module.EngageInput.from_effective(None, 20.0, 3.0, 0)

    def test_gates_are_untouched_by_the_effective_wrapper(self):
        """同一组输入，直接建 vs 从 effective 建，判决必须逐字段相同。"""
        state = effective_module.effective_state(60, 44.0, offset={}, card_obj=self.card())
        direct = engage_module.EngageInput(mood_now=44.0, energy=72.0, familiarity=0.5,
                                           today_count=3, now=T0)
        wrapped = engage_module.EngageInput.from_effective(
            state, energy=72.0, familiarity=0.5, today_count=3, now=T0)
        self.assertEqual(engage_module.decide(direct), engage_module.decide(wrapped))


if __name__ == "__main__":
    unittest.main()
