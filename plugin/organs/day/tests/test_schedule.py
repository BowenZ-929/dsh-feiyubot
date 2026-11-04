"""schedule：参数全在数据文件、抽法可复现、一次抽两条痕迹、来源标记可核。

测试一律用临时根（绝不写 organs/day/data 与 organs/home/data 的真实文件），
时刻一律用注入的 now_ts（绝不依赖真实时钟）。
"""
from __future__ import annotations

import json
import os
import random
import unittest
from pathlib import Path
from unittest import mock

from organs.day import schedule
from organs.day.tests.helpers import at, brain, temp_root

MORNING = at("2026-11-06T05:00:00")
DAY = at("2026-11-06T15:00:00")
NIGHT = at("2026-11-06T22:00:00")

HOME = "工作台"


def home_case(case, writable=("工作台",)):
    """一间临时家：白名单是家的配置文件（不是内部数据），按需点名她能写哪几间。"""
    root = temp_root(case)
    (root / "access.json").write_text(
        json.dumps({"version": 1, "writable": list(writable)}, ensure_ascii=False),
        encoding="utf-8")
    return root


def read_lines(path):
    if not path.exists():
        return []
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines() if text.strip()]


class ConfigTest(unittest.TestCase):
    """参数只住在数据文件里：本模块的源码里不许出现任何条目名。"""

    def test_no_item_or_slot_name_in_the_module_source(self):
        cfg = schedule.load_config()
        source = Path(schedule.__file__).read_text(encoding="utf-8")
        for name in list(cfg["items"]) + [row["name"] for row in cfg["slots"]]:
            self.assertFalse(name in source, "条目／时段名不许硬编码在代码里：%r" % name)

    def test_three_bands_and_all_heart_tier_words_are_mapped(self):
        cfg = schedule.load_config()
        self.assertEqual({"low", "mid", "high"}, set(cfg["tier_to_band"].values()))
        # 心的四个档词（organs/heart/energy.py 的 _ENERGY_TIERS + 充沛）都必须有映射
        self.assertEqual({"累", "偏累", "一般", "充沛"}, set(cfg["tier_to_band"]))

    def test_every_band_slot_company_has_a_weight_table(self):
        cfg = schedule.load_config()
        bands = set(cfg["tier_to_band"].values())
        for band in bands:
            for row in cfg["slots"]:
                for key in cfg["company_keys"]:
                    table = cfg["weights"][band][row["name"]][key]
                    self.assertTrue(table, "%s/%s/%s 的权重表是空的" % (band, row["name"], key))
                    for name in table:
                        self.assertIn(name, cfg["items"])

    def test_config_is_validated_not_trusted(self):
        broken = json.loads(json.dumps(schedule.load_config()))
        del broken["weights"]["mid"]["午后"]["no"]
        with self.assertRaises(schedule.ScheduleError):
            schedule.load_config(broken)


class SlotTest(unittest.TestCase):
    def test_slots_cover_the_whole_day(self):
        cfg = schedule.load_config()
        names = [row["name"] for row in cfg["slots"]]
        seen = {hour: schedule.slot_of(at("2026-11-06T%02d:00:00" % hour), cfg)
                for hour in range(24)}
        self.assertEqual(set(names), set(seen.values()), "24 小时都要落进表里的某一段")
        self.assertNotEqual(seen[3], seen[5], "跨零点的段要判得对（夜里与清晨不是同一段）")

    def test_slot_boundaries_follow_the_data_file(self):
        cfg = schedule.load_config()
        for row in cfg["slots"]:
            stamp = at("2026-11-06T%02d:00:00" % int(row["from"]))
            self.assertEqual(row["name"], schedule.slot_of(stamp, cfg))

    def test_company_default_comes_from_the_slot(self):
        cfg = schedule.load_config()
        morning, _ = schedule.company_of("清晨", cfg)
        night, source = schedule.company_of("夜", cfg)
        self.assertFalse(morning)
        self.assertTrue(night)
        self.assertEqual("slot", source)


class PickTest(unittest.TestCase):
    def test_same_key_draws_the_same_item(self):
        first = schedule.pick(DAY, energy={"tier": "充沛", "value": 90}, seed=7)
        second = schedule.pick(DAY, energy={"tier": "充沛", "value": 90}, seed=7)
        self.assertTrue(first["ok"])
        self.assertEqual(first["item"]["name"], second["item"]["name"])
        self.assertEqual(first["source"], second["source"])

    def test_rng_can_be_injected(self):
        a = schedule.pick(DAY, energy={"tier": "充沛"}, company=False, rng=random.Random(3))
        b = schedule.pick(DAY, energy={"tier": "充沛"}, company=False, rng=random.Random(3))
        self.assertEqual(a["item"]["name"], b["item"]["name"])

    def test_ten_combos_are_not_always_the_same_item(self):
        combos = [
            (MORNING, "充沛", False, 1), (at("2026-11-06T09:00:00"), "一般", False, 2),
            (DAY, "充沛", True, 3), (DAY, "累", False, 4), (NIGHT, "一般", True, 5),
            (NIGHT, "充沛", False, 6), (at("2026-11-06T18:00:00"), "偏累", True, 7),
            (at("2026-11-06T13:00:00"), "一般", False, 8), (at("2026-11-06T11:00:00"), "充沛", True, 9),
            (at("2026-11-06T21:00:00"), "累", True, 10),
        ]
        names = [schedule.pick(now, energy={"tier": tier}, company=company, seed=seed)["item"]["name"]
                 for now, tier, company, seed in combos]
        self.assertGreaterEqual(len(set(names)), 3, names)

    def test_morning_window_is_fallback(self):
        cfg = schedule.load_config()
        decision = schedule.pick(MORNING, energy={"tier": "充沛", "value": 90})
        self.assertEqual(cfg["markers"]["fallback"], decision["source"])
        self.assertEqual(cfg["fallback"]["reasons"]["slot"], decision["reason"])

    def test_daytime_high_energy_is_plan(self):
        cfg = schedule.load_config()
        decision = schedule.pick(DAY, energy={"tier": "充沛", "value": 90}, seed=1)
        self.assertEqual(cfg["markers"]["plan"], decision["source"])
        self.assertEqual("", decision["reason"])
        self.assertEqual("high", decision["band"])

    def test_unreadable_energy_falls_back(self):
        cfg = schedule.load_config()
        decision = schedule.pick(DAY, energy={"tier": None, "value": None}, seed=1)
        self.assertEqual(cfg["markers"]["fallback"], decision["source"])
        self.assertEqual(cfg["fallback"]["reasons"]["energy"], decision["reason"])
        self.assertEqual(cfg["energy"]["unreadable"], decision["energy"]["tier"])

    def test_weights_all_zero_falls_back(self):
        cfg = json.loads(json.dumps(schedule.load_config()))
        for name in cfg["weights"]["high"]["午后"]["no"]:
            cfg["weights"]["high"]["午后"]["no"][name] = 0
        decision = schedule.pick(DAY, energy={"tier": "充沛"}, company=False, config=cfg)
        self.assertEqual(cfg["markers"]["fallback"], decision["source"])
        self.assertEqual(cfg["fallback"]["reasons"]["weights"], decision["reason"])

    def test_items_are_only_about_herself(self):
        cfg = schedule.load_config()
        for name, text in cfg["items"].items():
            self.assertNotIn("主人", text, "日程不许生成关于主人的事实：%r" % name)
        for name in cfg["fallback"]["items"]:
            self.assertIn(name, cfg["items"])

    def test_bad_now_is_reported_not_raised(self):
        payload = schedule.pick("不是时间")
        self.assertFalse(payload["ok"])
        self.assertEqual("BAD_NOW", payload["code"])


class DrawTest(unittest.TestCase):
    def setUp(self):
        self.root = temp_root(self)
        self.home = home_case(self)
        self.brain = brain(self.root)

    def draw(self, now=DAY, **kwargs):
        params = {"brain": self.brain, "home_root": self.home,
                  "energy": {"tier": "充沛", "value": 90}, "seed": 7, "root": self.root}
        params.update(kwargs)
        return schedule.draw_and_land(now, **params)

    def test_one_draw_lands_both_traces(self):
        index = self.home / HOME / "index.jsonl"
        before_rows, before_entries = len(read_lines(index)), len(self.brain.today_entries(DAY))
        payload = self.draw()
        self.assertTrue(payload["ok"], payload)
        after_rows, after_entries = len(read_lines(index)), len(self.brain.today_entries(DAY))

        self.assertEqual(before_entries + 1, after_entries, "脑今日条目要 +1")
        entry = self.brain.today_entries(DAY)[-1]
        self.assertEqual("她自己", entry["who"])
        self.assertEqual("日程模拟", entry["source_type"])
        self.assertEqual("", entry["evidence_key"], "日程模拟不许留证据出处")

        self.assertEqual(before_rows + 1, after_rows, "家的工作台要 +1 张卡")
        card = read_lines(index)[-1]
        self.assertEqual(HOME, card["room"])
        self.assertEqual("agent", card["role"])
        self.assertEqual("day", card["by"])
        self.assertIn(card["kind"], ("物件", "痕迹", "名片", "借阅条", "素材", "自画像", "提到"))
        self.assertEqual("来源：plan", card["note"].splitlines()[0])
        self.assertEqual(entry["text"], payload["item"]["text"])
        self.assertEqual(1, len(read_lines(self.root / "schedule_log.jsonl")))

    def test_log_line_carries_the_eight_fields(self):
        self.draw()
        line = read_lines(self.root / "schedule_log.jsonl")[-1]
        self.assertEqual({"ts", "day", "item", "source", "energy_tier", "slot", "company", "seed"},
                         set(line))
        self.assertEqual("plan", line["source"])
        self.assertEqual("充沛", line["energy_tier"])

    def test_morning_draw_is_marked_fallback_on_both_traces(self):
        payload = self.draw(now=MORNING)
        self.assertEqual("fallback", payload["source"])
        card = read_lines(self.home / HOME / "index.jsonl")[-1]
        self.assertEqual("来源：fallback", card["note"].splitlines()[0])
        self.assertEqual("fallback", read_lines(self.root / "schedule_log.jsonl")[-1]["source"])

    def test_no_brain_lands_nothing(self):
        payload = schedule.draw_and_land(DAY, brain=None, home_root=self.home,
                                         energy={"tier": "充沛"}, root=self.root)
        self.assertFalse(payload["ok"])
        self.assertEqual("BRAIN_MISSING", payload["code"])
        self.assertFalse((self.home / HOME / "index.jsonl").exists(), "缺脑时家那张卡也不许写")
        self.assertEqual([], read_lines(self.root / "schedule_log.jsonl"))

    def test_home_refusal_is_surfaced_not_swallowed(self):
        closed = home_case(self, writable=())
        payload = schedule.draw_and_land(DAY, brain=self.brain, home_root=closed,
                                         energy={"tier": "充沛"}, root=self.root)
        self.assertFalse(payload["ok"])
        self.assertEqual("HOME_WRITE_REJECTED", payload["code"])
        self.assertEqual(403, payload["http_status"], payload)
        self.assertEqual([], read_lines(self.root / "schedule_log.jsonl"))
        self.assertEqual([], self.brain.today_entries(DAY), "家拒了就不许进脑")

    def test_default_home_root_follows_the_env(self):
        with mock.patch.dict(os.environ, {"HOME_ROOT": str(self.home)}):
            payload = schedule.draw_and_land(DAY, brain=self.brain,
                                             energy={"tier": "充沛"}, root=self.root)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(1, len(read_lines(self.home / HOME / "index.jsonl")))

    def test_energy_comes_from_heart_when_not_injected(self):
        heart = self.root / "heart"
        heart.mkdir()
        (heart / "energy.json").write_text(json.dumps({
            "version": 1, "value": 88.0, "updated_at": DAY,
            "day": DAY[:10], "deducted": 0.0, "last_burst_at": None}), encoding="utf-8")
        payload = self.draw(heart_root=heart, energy=None)
        self.assertEqual("heart", payload["energy"]["source"])
        self.assertEqual("充沛", payload["energy"]["tier"])
        self.assertEqual("high", payload["band"])
        self.assertEqual("plan", payload["source"])

    def test_unreadable_energy_is_marked_in_both_traces(self):
        payload = self.draw(energy={"tier": None})
        self.assertEqual("fallback", payload["source"])
        cfg = schedule.load_config()
        card = read_lines(self.home / HOME / "index.jsonl")[-1]
        self.assertEqual("来源：fallback", card["note"].splitlines()[0])
        self.assertIn(cfg["energy"]["unreadable"], card["note"])
        self.assertEqual(cfg["energy"]["unreadable"],
                         read_lines(self.root / "schedule_log.jsonl")[-1]["energy_tier"])

    def test_three_roots_stay_in_their_own_dirs(self):
        """root / home_root / heart_root 各管一处：日志进日子线那个根，卡进家那个根，精力从心那个根读。"""
        dayline = self.root / "dayline"
        home = self.root / "home-elsewhere"
        heart = self.root / "heart-elsewhere"
        home.mkdir()
        heart.mkdir()
        (home / "access.json").write_text(
            json.dumps({"version": 1, "writable": [HOME]}, ensure_ascii=False), encoding="utf-8")
        (heart / "energy.json").write_text(json.dumps({
            "version": 1, "value": 12.0, "updated_at": DAY, "day": DAY[:10],
            "deducted": 0.0, "last_burst_at": None}), encoding="utf-8")

        payload = schedule.draw_and_land(DAY, brain=self.brain, root=dayline,
                                         home_root=home, heart_root=heart, seed=21)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(str(dayline / "schedule_log.jsonl"), payload["log_path"])
        self.assertEqual(1, len(read_lines(dayline / "schedule_log.jsonl")))
        self.assertEqual(1, len(read_lines(home / HOME / "index.jsonl")))
        self.assertEqual("heart", payload["energy"]["source"])
        self.assertEqual(12.0, payload["energy"]["value"])
        self.assertEqual("low", payload["band"])
        self.assertFalse((self.home / HOME / "index.jsonl").exists(), "另一个家的根一行都不许动")

    def test_two_draws_with_the_same_key_are_the_same_item(self):
        first, second = self.draw(), self.draw()
        self.assertEqual(first["item"]["name"], second["item"]["name"])
        self.assertEqual(2, len(read_lines(self.home / HOME / "index.jsonl")))


class TodayTest(unittest.TestCase):
    def test_today_needs_no_arguments_and_is_json_safe(self):
        root = temp_root(self)
        payload = schedule.today(root=root)
        self.assertTrue(payload["ok"], payload)
        json.dumps(payload)
        self.assertIn("slot", payload)
        self.assertIsNone(payload["last"])

    def test_today_reports_the_fallback_slot(self):
        payload = schedule.today(MORNING)
        self.assertTrue(payload["ok"])
        self.assertEqual("fallback", payload["source_hint"])

    def test_today_does_not_settle_heart(self):
        heart = temp_root(self)
        schedule.today(DAY, root=heart)
        self.assertFalse((heart / "energy.json").exists(), "GET 不许结算心（那是写动作）")


if __name__ == "__main__":
    unittest.main()
