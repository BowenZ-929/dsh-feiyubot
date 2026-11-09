"""六轴卡：真卡能载、坏卡逐类拒载、文本里只许有档位词。"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from organs.heart import card, model
from organs.heart.tests import helpers


def good_card() -> Dict[str, Any]:
    """一张合规的最小六轴卡（刻度与轴义照真卡的形状）。"""
    return {
        "scale": {"min": 0, "max": 100},
        "axes": [
            {"id": "服气", "inner": 92, "shown": 18},
            {"id": "心疼", "inner": 85, "shown": 40},
            {"id": "想黏人", "inner": 70, "shown": 30},
            {"id": "怕失去", "inner": 78, "shown": 15},
            {"id": "耐心", "inner": 72, "shown": 52},
            {"id": "记性", "inner": 78, "shown": 60},
        ],
    }


def axis_entry(data: Dict[str, Any], axis_id: str) -> Dict[str, Any]:
    for entry in data["axes"]:
        if entry.get("id") == axis_id:
            return entry
    raise AssertionError("卡里没有这根轴：%s" % axis_id)


def sha256_of(path: Any) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CardLoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)

    def _write(self, data: Any) -> Path:
        path = self.root / card.CARD_FILE
        if isinstance(data, str):
            helpers.write_raw(path, data)
        else:
            helpers.write_raw(path, json.dumps(data, ensure_ascii=False, indent=2))
        return path

    # ---- 真卡 ---------------------------------------------------------

    def test_real_card_loads_with_all_six_paired(self):
        real = card.load_card(card.SEED_CARD_PATH)
        self.assertEqual(model.AXES, model.PAIRED_AXES, "六轴都成对（2026-11-06 人定稿）")
        self.assertEqual((), model.PENDING_AXES)
        self.assertEqual(real.paired, model.PAIRED_AXES)
        self.assertEqual(real.pending, ())
        self.assertEqual((real.scale_min, real.scale_max), (0, 100))
        for axis_id in model.AXES:
            axis = real.axes[axis_id]
            self.assertFalse(axis.pending, axis_id)
            self.assertIsInstance(axis.inner, int, axis_id)
            self.assertIsInstance(axis.shown, int, axis_id)
            self.assertTrue(0 <= axis.inner <= 100, axis_id)
            self.assertTrue(0 <= axis.shown <= 100, axis_id)
        self.assertEqual((72, 52), (real.axes["耐心"].inner, real.axes["耐心"].shown))
        self.assertEqual((78, 60), (real.axes["记性"].inner, real.axes["记性"].shown))

    def test_real_card_keeps_handoff_block_and_axes_order_of_docs(self):
        """整卡迁入的证据：_handoff 段还在、轴序没变；数可以随人定稿变，结构不行。

        docs 那份是 2026-11-03 迁入那一刻的历史快照，**不再逐字节跟随**——
        2026-11-06 人定稿把耐心/记性补了数，两份的 sha 本来就该分叉。
        """
        data = helpers.read_json_file(card.SEED_CARD_PATH)
        docs = Path(card.__file__).resolve().parents[3] / "docs" / "organs" / "soul" / "baseline-card-handoff.json"
        original = helpers.read_json_file(docs)
        self.assertIn("_handoff", data, "整卡迁入：_handoff 段要原样留着")
        self.assertEqual(set(original["_handoff"]), set(data["_handoff"]), "_handoff 的键不许丢")
        self.assertEqual([], data["_handoff"]["pending"], "_handoff.pending 已清成空表")
        self.assertEqual([entry["id"] for entry in original["axes"]],
                         [entry["id"] for entry in data["axes"]],
                         "轴序是迁入时定下的，不许改")
        self.assertEqual(list(model.AXES), [entry["id"] for entry in data["axes"]])

    def test_view_covers_all_six_axes_and_pending_is_empty(self):
        loaded = card.load_card(card.SEED_CARD_PATH)
        view = card.view(loaded)
        self.assertIn("pending", view, "pending 这个键不许删：面板与既有读法都读它")
        self.assertEqual([], view["pending"], "六轴完整之后没有一根轴落在 pending 里")
        self.assertEqual(set(view["tiers"]), set(model.AXES))
        self.assertEqual(set(view["loaded"]), set(model.AXES))
        for axis_id in model.AXES:
            self.assertIsNotNone(view["tiers"][axis_id]["inner_word"], axis_id)
            self.assertIsNotNone(view["tiers"][axis_id]["shown_word"], axis_id)
        self.assertEqual(view["scale"], {"min": 0, "max": 100})
        for axis_id, row in view["tiers"].items():
            self.assertTrue(1 <= row["inner_tier"] <= model.TIER_COUNT, axis_id)
            self.assertTrue(1 <= row["shown_tier"] <= model.TIER_COUNT, axis_id)
            self.assertIn(row["inner_word"], card.TIER_WORDS[axis_id])
            self.assertIn(row["shown_word"], card.TIER_WORDS[axis_id])
        self.assertEqual(view["tiers"]["服气"]["inner_word"], "特别服")
        self.assertEqual(view["tiers"]["服气"]["shown_word"], "有自己的判断")
        self.assertEqual(view["tiers"]["想黏人"]["shown_word"], "淡着")

    def test_tier_words_five_each_for_all_six_axes(self):
        for axis_id in model.AXES:
            words = card.TIER_WORDS[axis_id]
            self.assertEqual(len(words), model.TIER_COUNT, axis_id)
            self.assertEqual(len(set(words)), model.TIER_COUNT, "档位词不许重复")
            self.assertEqual("一般", words[2], "%s 的中间档必须是「一般」" % axis_id)
            self.assertEqual(card.tier_word(axis_id, 1), words[0])
            self.assertEqual(card.tier_word(axis_id, 5), words[4])
        self.assertEqual(card.TIER_WORDS["想黏人"][1], "淡着")
        self.assertEqual(card.TIER_WORDS["耐心"], ("没耐性", "有点急", "一般", "有耐心", "特别有耐心"))
        self.assertEqual(card.TIER_WORDS["记性"], ("转身就忘", "记不住", "一般", "记得住", "过目不忘"))
        self.assertIsNone(card.tier_word("不存在的轴", 1))
        self.assertIsNone(card.tier_word("服气", 0))
        self.assertIsNone(card.tier_word("服气", model.TIER_COUNT + 1))

    def test_tier_bans_cover_every_axis_and_are_adjustable(self):
        self.assertEqual(card.TIER_BANS["想黏人"][1], "黏人低档时不许连发三条以上（默认值，可调）")
        self.assertEqual(card.TIER_BANS["想黏人"][2], "黏人低档时不许连发三条以上（默认值，可调）")
        for axis_id in model.AXES:
            bans = card.TIER_BANS[axis_id]
            self.assertTrue(bans, "%s 要有一条禁令" % axis_id)
            for tier, text in bans.items():
                self.assertTrue(1 <= tier <= model.TIER_COUNT, axis_id)
                self.assertIn("默认值，可调", text)
        self.assertEqual(card.TIER_BANS["耐心"][1], "没耐性时不许连问三次以上（默认值，可调）")
        self.assertEqual(card.TIER_BANS["记性"][1],
                         "记性低档时不许硬说「我记得」——问一遍比编一句强（默认值，可调）")

    def test_tier_of_splits_scale_evenly(self):
        self.assertEqual(card.tier_of(0, 0, 100), 1)
        self.assertEqual(card.tier_of(19, 0, 100), 1)
        self.assertEqual(card.tier_of(20, 0, 100), 2)
        self.assertEqual(card.tier_of(50, 0, 100), 3)
        self.assertEqual(card.tier_of(80, 0, 100), 5)
        self.assertEqual(card.tier_of(100, 0, 100), model.TIER_COUNT)
        self.assertEqual(card.tier_of(-5, 0, 100), 1)
        self.assertEqual(card.tier_of(0, 1, 5), 1)
        self.assertEqual(card.tier_of(5, 1, 5), model.TIER_COUNT)

    def test_render_text_has_no_arabic_digits(self):
        loaded = card.load_card(card.SEED_CARD_PATH)
        text = card.render_text(loaded)
        self.assertFalse(any(ch.isdigit() for ch in text), "渲染文本里不许出现 0-9：%r" % text)
        self.assertIn("特别服", text)
        self.assertIn("有自己的判断", text)
        hits = [word for words in card.TIER_WORDS.values() for word in words if word in text]
        self.assertGreaterEqual(len(set(hits)), 2, "至少两个档位词")

    def test_render_text_with_offsets_stays_word_only(self):
        loaded = card.load_card(card.SEED_CARD_PATH)
        text = card.render_text(loaded, {"想黏人": 1.5, "服气": -0.5})
        self.assertFalse(any(ch.isdigit() for ch in text))
        self.assertIn("黏人低档时不许连发三条以上", text)
        for axis_id in model.AXES:
            self.assertIn("- %s：" % axis_id, text, "六轴一起渲染，加了偏移也不许掉轴")

    # ---- 坏卡 ---------------------------------------------------------

    def test_missing_file_raises_card_missing_and_creates_nothing(self):
        target = self.root / "nope" / card.CARD_FILE
        with self.assertRaises(card.CardError) as caught:
            card.load_card(target)
        self.assertEqual(caught.exception.code, card.CARD_MISSING)
        self.assertFalse(target.exists(), "读不到就是读不到，不许补一张默认卡")
        self.assertFalse(target.parent.exists())

    def test_bad_cards_are_rejected_by_code_and_file_left_alone(self):
        cases = []

        missing_axis = good_card()
        missing_axis["axes"] = [e for e in missing_axis["axes"] if e["id"] != "记性"]
        cases.append(("缺轴", missing_axis, card.AXIS_MISSING))

        missing_shown = good_card()
        missing_shown["axes"][0].pop("shown")
        cases.append(("缺 shown", missing_shown, card.AXIS_UNPAIRED))

        not_int = good_card()
        not_int["axes"][1]["inner"] = "85"
        cases.append(("值非 int", not_int, card.AXIS_NOT_INT))

        bool_value = good_card()
        bool_value["axes"][1]["inner"] = True
        cases.append(("值是 bool", bool_value, card.AXIS_NOT_INT))

        out_of_range = good_card()
        out_of_range["axes"][3]["shown"] = 200
        cases.append(("超 scale", out_of_range, card.AXIS_OUT_OF_RANGE))

        duplicate = good_card()
        duplicate["axes"].append({"id": "服气", "inner": 10, "shown": 10})
        cases.append(("轴重复", duplicate, card.AXIS_DUPLICATE))

        unknown = good_card()
        unknown["axes"].append({"id": "瞎眼", "inner": 10, "shown": 10})
        cases.append(("未知轴", unknown, card.AXIS_UNKNOWN))

        paired_pending = good_card()
        paired_pending["axes"][0] = {"id": "服气", "pending": True}
        cases.append(("轴标了 pending", paired_pending, card.AXIS_PAIRED_BUT_PENDING))

        # 耐心 / 记性 已经补齐：有人把轴标回 pending（哪怕数还在）就是坏卡，不是「跳过注入」。
        back_to_pending = good_card()
        back_to_pending["axes"][4]["pending"] = True
        cases.append(("补齐的耐心标回 pending", back_to_pending, card.AXIS_PAIRED_BUT_PENDING))

        missing_patience_shown = good_card()
        missing_patience_shown["axes"][4].pop("shown")
        cases.append(("耐心缺 shown", missing_patience_shown, card.AXIS_UNPAIRED))

        no_scale = good_card()
        no_scale.pop("scale")
        cases.append(("缺 scale", no_scale, card.SCALE_INVALID))

        bad_scale_type = good_card()
        bad_scale_type["scale"] = {"min": "0", "max": 100}
        cases.append(("scale 不是 int", bad_scale_type, card.SCALE_INVALID))

        inverted_scale = good_card()
        inverted_scale["scale"] = {"min": 100, "max": 100}
        cases.append(("min >= max", inverted_scale, card.SCALE_INVALID))

        axes_not_list = good_card()
        axes_not_list["axes"] = {"服气": 1}
        cases.append(("axes 不是 list", axes_not_list, card.AXES_INVALID))

        entry_not_dict = good_card()
        entry_not_dict["axes"].append("服气")
        cases.append(("axis 项不是 dict", entry_not_dict, card.AXES_INVALID))

        id_not_str = good_card()
        id_not_str["axes"].append({"id": 3, "inner": 10, "shown": 10})
        cases.append(("id 不是字符串", id_not_str, card.AXES_INVALID))

        for name, data, expected in cases:
            with self.subTest(case=name):
                path = self._write(data)
                before = sha256_of(path)
                with self.assertRaises(card.CardError) as caught:
                    card.load_card(path)
                error = caught.exception
                self.assertEqual(error.code, expected, "%s 的码不对：%s" % (name, error.issues))
                self.assertTrue(error.issues)
                for issue in error.issues:
                    self.assertEqual(set(issue), {"code", "axis", "message"})
                    self.assertTrue(issue["message"])
                self.assertTrue(any(issue["code"] == expected for issue in error.issues))
                self.assertEqual(sha256_of(path), before, "%s：坏卡一个字节都不许改" % name)
                self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                                 [card.CARD_FILE], "%s：除了原卡不许落任何默认卡" % name)

    def test_corrupt_json_and_non_dict_are_card_corrupt(self):
        for name, raw in (("坏 JSON", "{not json"), ("顶层是 list", "[1, 2, 3]"),
                          ("顶层是字符串", '"六轴"'), ("空文件", "")):
            with self.subTest(case=name):
                path = self._write(raw)
                before = sha256_of(path)
                with self.assertRaises(card.CardError) as caught:
                    card.load_card(path)
                self.assertEqual(caught.exception.code, card.CARD_CORRUPT, name)
                self.assertEqual(sha256_of(path), before)

    def test_card_error_carries_code_and_issues(self):
        error = card.CardError(card.CARD_MISSING, [{"code": card.CARD_MISSING, "axis": "",
                                                    "message": "读不到"}])
        self.assertEqual(error.code, "card-missing")
        self.assertEqual(error.issues[0]["code"], "card-missing")
        self.assertIn("读不到", str(error))

    def test_good_temp_card_loads_clean(self):
        path = self._write(good_card())
        loaded = card.load_card(path)
        self.assertEqual(set(loaded.axes), set(model.AXES))
        self.assertEqual(loaded.axes["想黏人"].inner, 70)
        self.assertEqual(loaded.axes["想黏人"].shown, 30)
        self.assertEqual((), loaded.pending)
        self.assertEqual(6, card.render_text(loaded).count("\n- "))


class SixAxesAreWhole(unittest.TestCase):
    """六轴完整（2026-11-06 人定稿）的机器守卫：谁少写一轴，这里当场红。

    这两条是补上的正面断言——原来只有「pending 轴标灰」那一侧的断言，于是
    `render_text` 里出现「心里None，面上None」都没人管：查不到词就是 None，
    而 None 不是「没有断言失败」，它是一行贴到面板上的假话。
    """

    def setUp(self) -> None:
        self.card = card.load_card(card.SEED_CARD_PATH)

    def test_render_text_has_all_six_axis_lines_and_no_none(self):
        text = card.render_text(self.card)
        lines = text.splitlines()
        for axis_id in model.AXES:
            self.assertTrue(any(line.startswith("- %s：" % axis_id) for line in lines),
                            "%s 这一行不许缺，也不许折在别人的行里" % axis_id)
        self.assertEqual(len(model.AXES), text.count("\n- "), "六条一行不缺、也不许多")
        self.assertNotIn("None", text, "档位词查不到就会渲成 None：词表少了一轴")

    def test_tier_words_and_bans_cover_every_axis(self):
        self.assertEqual(set(model.AXES), set(card.TIER_WORDS), "词表少了一轴")
        self.assertEqual(set(model.AXES), set(card.TIER_BANS), "禁令表少了一轴")
        for axis_id in model.AXES:
            words = card.TIER_WORDS[axis_id]
            self.assertEqual(model.TIER_COUNT, len(words), "%s 的档位词不是五个" % axis_id)
            self.assertEqual("一般", words[2], "%s 的中间档不是「一般」" % axis_id)
            for tier in range(1, model.TIER_COUNT + 1):
                self.assertIsNotNone(card.tier_word(axis_id, tier),
                                     "%s 第 %s 档没有词" % (axis_id, tier))
            self.assertTrue(card.TIER_BANS[axis_id], "%s 一条禁令都没有" % axis_id)
