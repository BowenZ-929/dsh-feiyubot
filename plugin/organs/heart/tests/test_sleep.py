"""睡眠账：三样就够——上次入睡、睡了多久、连续熬夜（相位漂移）。

钉死的四件事，改之前先读：
1. 只记三样，不引入腺苷那一套：本文件代码里没有任何清除/衰减常数（见 forbidden 用例），
   清醒时长就是两次投递的时刻相减。
2. 全部读时现算，不挂定时器：这里所有「三天前 / 明天」都靠传 now 造，不有一条用例等真实时间。
3. 熬夜判定锚在基准作息上，漂移只修正 energy 那边的窗口——否则账会自我抵消。
4. 每条用例只落 helpers.make_root 的临时目录，绝不写仓里真 data。
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from organs.heart import energy, events, model, sleep
from organs.heart.tests import helpers

# 起点用 naive datetime：model.iso 会补本机时区，测试里的钟点始终是本机挂钟的钟点。
_START = datetime(2026, 11, 1)


def ts(day: int, hour: int = 0, minute: int = 0) -> str:
    """起点之后第 day 天的 hour:minute（测试里的「现在」全靠传，不靠等）。"""
    return model.iso(_START + timedelta(days=day, hours=hour, minutes=minute))


def night_late(root: Any, day: int, late_min: float = 0.0, minutes: float = 480.0) -> Any:
    """day 那天 23:00 之后第 late_min 分钟睡，睡 minutes 分钟后醒。返回醒来的那笔。"""
    onset = model.parse_ts(ts(day, 23)) + timedelta(minutes=float(late_min))
    sleep.record(sleep.KIND_ASLEEP, root=root, now=model.iso(onset))
    return sleep.record(
        sleep.KIND_AWAKE, root=root, now=model.iso(onset + timedelta(minutes=float(minutes)))
    )


def night(root: Any, day: int, minutes: float = 480.0) -> Any:
    """按时睡、睡 minutes 分钟的一夜。"""
    return night_late(root, day, late_min=0.0, minutes=minutes)


def state(root: Any) -> Any:
    """读回 sleep.json（坏了就让它炸，好定位）。"""
    return helpers.read_json_file(Path(root) / sleep.SLEEP_FILE)


def set_config(root: Any, **values: Any) -> None:
    """人手改文件：只改 config 里的几格，不改代码、不重启。"""
    path = Path(root) / sleep.SLEEP_FILE
    data = helpers.read_json_file(path)
    data["config"].update(values)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def shift_days_back(root: Any, days: int = 3) -> None:
    """把整本账（上次入睡 / 上次醒 / 每一夜）往前推 days 天，模拟「上次入睡是三天前」。"""
    path = Path(root) / sleep.SLEEP_FILE
    data = helpers.read_json_file(path)
    delta = timedelta(days=days)
    for key in ("last_asleep_at", "last_awake_at"):
        if isinstance(data.get(key), str) and data[key]:
            data[key] = model.iso(model.parse_ts(data[key]) - delta)
    for row in data["nights"]:
        row["asleep_at"] = model.iso(model.parse_ts(row["asleep_at"]) - delta)
        row["awake_at"] = model.iso(model.parse_ts(row["awake_at"]) - delta)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class SleepTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = helpers.make_root(self._temp.name)
        self.file = Path(self.root) / sleep.SLEEP_FILE

    def tearDown(self) -> None:
        self._temp.cleanup()

    # ---- 1) 账本形状 / 落盘 ------------------------------------------------

    def test_defaults_land_on_disk_with_first_record(self) -> None:
        self.assertFalse(self.file.exists())
        sleep.record(sleep.KIND_ASLEEP, root=self.root, now=ts(1, 23))
        data = state(self.root)
        self.assertEqual(data["version"], sleep.VERSION)
        self.assertEqual(data["config"], sleep.CONFIG_DEFAULTS)
        self.assertEqual(data["last_asleep_at"], ts(1, 23))
        self.assertIsNone(data["last_awake_at"])
        self.assertIsNone(data["last_duration_min"])
        self.assertEqual(data["nights"], [])
        self.assertEqual(data["consecutive_late_nights"], 0)
        self.assertEqual(data["consecutive_short_nights"], 0)

    def test_state_keys_are_exactly_the_schema(self) -> None:
        night(self.root, 1)
        self.assertEqual(
            set(state(self.root)),
            {
                "version",
                "config",
                "last_asleep_at",
                "last_awake_at",
                "last_duration_min",
                "nights",
                "consecutive_late_nights",
                "consecutive_short_nights",
            },
        )
        self.assertEqual(
            set(state(self.root)["nights"][0]),
            {"asleep_at", "awake_at", "duration_min", "late", "enough"},
        )

    def test_asleep_lands_in_the_event_ledger_too(self) -> None:
        sleep.record(sleep.KIND_ASLEEP, root=self.root, now=ts(1, 23))
        sleep.record(sleep.KIND_AWAKE, root=self.root, now=ts(2, 7))
        rows = events.recent(self.root, now=ts(2, 8), window_min=24 * 60)
        self.assertEqual([row["kind"] for row in rows], ["asleep", "awake"])
        self.assertEqual([row["at"] for row in rows], [ts(1, 23), ts(2, 7)])

    def test_record_asleep_returns_counters_and_no_night(self) -> None:
        got = sleep.record(sleep.KIND_ASLEEP, root=self.root, now=ts(1, 23))
        self.assertEqual(got["kind"], "asleep")
        self.assertEqual(got["at"], ts(1, 23))
        self.assertIsNone(got["night"])
        self.assertEqual(got["drift_hours"], 0.0)
        self.assertEqual(got["wake_cap"], 100.0)

    def test_record_awake_returns_the_night_it_wrote(self) -> None:
        sleep.record(sleep.KIND_ASLEEP, root=self.root, now=ts(1, 23))
        got = sleep.record(sleep.KIND_AWAKE, root=self.root, now=ts(2, 7))
        self.assertEqual(got["night"]["asleep_at"], ts(1, 23))
        self.assertEqual(got["night"]["awake_at"], ts(2, 7))
        self.assertEqual(got["night"]["duration_min"], 480.0)
        self.assertFalse(got["night"]["late"])
        self.assertTrue(got["night"]["enough"])

    def test_unknown_kind_is_a_value_error(self) -> None:
        for bad in ("", "sleep", "woke", "ASLEEP"):
            with self.assertRaises(ValueError):
                sleep.record(bad, root=self.root, now=ts(1, 23))
        self.assertFalse(self.file.exists())

    # ---- 2) 配对：睡了多久 -------------------------------------------------

    def test_awake_without_a_previous_asleep_writes_no_night(self) -> None:
        got = sleep.record(sleep.KIND_AWAKE, root=self.root, now=ts(2, 7))
        self.assertIsNone(got["night"])
        data = state(self.root)
        self.assertEqual(data["nights"], [])
        self.assertEqual(data["last_awake_at"], ts(2, 7))
        self.assertIsNone(data["last_duration_min"])

    def test_repeated_awake_does_not_write_a_second_night(self) -> None:
        sleep.record(sleep.KIND_ASLEEP, root=self.root, now=ts(1, 23))
        sleep.record(sleep.KIND_AWAKE, root=self.root, now=ts(2, 7))
        sleep.record(sleep.KIND_AWAKE, root=self.root, now=ts(2, 8))
        self.assertEqual(len(state(self.root)["nights"]), 1)
        self.assertEqual(state(self.root)["last_duration_min"], 480.0)

    def test_awake_before_asleep_is_clock_skew_and_writes_no_night(self) -> None:
        sleep.record(sleep.KIND_ASLEEP, root=self.root, now=ts(2, 7))
        got = sleep.record(sleep.KIND_AWAKE, root=self.root, now=ts(1, 23))
        self.assertIsNone(got["night"])
        self.assertEqual(state(self.root)["nights"], [])

    def test_two_nights_in_a_row_are_both_kept(self) -> None:
        night(self.root, 1)
        night(self.root, 2)
        data = state(self.root)
        self.assertEqual(len(data["nights"]), 2)
        self.assertEqual(data["last_duration_min"], 480.0)
        self.assertEqual(data["nights"][1]["asleep_at"], ts(2, 23))

    def test_nights_keep_only_the_last_thirty(self) -> None:
        for day in range(1, sleep.MAX_NIGHTS + 6):
            night(self.root, day)
        data = state(self.root)
        self.assertEqual(len(data["nights"]), sleep.MAX_NIGHTS)
        self.assertEqual(data["nights"][0]["asleep_at"], ts(6, 23))
        self.assertEqual(data["nights"][-1]["asleep_at"], ts(35, 23))

    # ---- 3) 熬夜判定 / 相位漂移 --------------------------------------------

    def test_on_time_night_is_not_late(self) -> None:
        got = night(self.root, 1)
        self.assertFalse(got["night"]["late"])
        self.assertEqual(got["consecutive_late_nights"], 0)

    def test_early_night_is_not_late(self) -> None:
        got = night_late(self.root, 1, late_min=-30)
        self.assertFalse(got["night"]["late"])

    def test_late_nights_accumulate(self) -> None:
        got = night_late(self.root, 1, late_min=90)
        self.assertTrue(got["night"]["late"])
        self.assertEqual(got["consecutive_late_nights"], 1)
        got = night_late(self.root, 2, late_min=90)
        self.assertEqual(got["consecutive_late_nights"], 2)
        got = night_late(self.root, 3, late_min=90)
        self.assertEqual(got["consecutive_late_nights"], 3)

    def test_on_time_night_clears_the_streak(self) -> None:
        night_late(self.root, 1, late_min=90)
        night_late(self.root, 2, late_min=90)
        got = night(self.root, 3)
        self.assertEqual(got["consecutive_late_nights"], 0)
        self.assertEqual(sleep.drift_hours(self.root), 0.0)

    def test_tolerance_is_inclusive(self) -> None:
        # 恰好晚 late_tolerance_min 分钟就算熬夜（"以上"含本数）
        got = night_late(self.root, 1, late_min=59)
        self.assertFalse(got["night"]["late"])
        got = night_late(self.root, 2, late_min=60)
        self.assertTrue(got["night"]["late"])

    def test_afternoon_onset_counts_as_late(self) -> None:
        # 正午才睡：这一夜的起点是前一夜的 23:00，晚了 13 小时。
        got = night_late(self.root, 1, late_min=13 * 60, minutes=60)
        self.assertTrue(got["night"]["late"])
        self.assertEqual(got["consecutive_late_nights"], 1)

    def test_drift_hours_accumulate_and_cap(self) -> None:
        seen = []
        for day in range(1, 8):
            night_late(self.root, day, late_min=90)
            seen.append(sleep.drift_hours(self.root))
        self.assertEqual(seen, [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.0])

    def test_effective_window_moves_later_and_wraps(self) -> None:
        self.assertEqual(sleep.effective_window(self.root), (23, 8))
        windows = []
        for day in range(1, 4):
            night_late(self.root, day, late_min=90)
            windows.append(sleep.effective_window(self.root))
        self.assertEqual(windows, [(0, 9), (0, 9), (1, 10)])

    def test_effective_window_caps_at_three_hours(self) -> None:
        for day in range(1, 9):
            night_late(self.root, day, late_min=90)
        self.assertEqual(sleep.drift_hours(self.root), 3.0)
        self.assertEqual(sleep.effective_window(self.root), (2, 11))

    def test_effective_window_honours_a_custom_base(self) -> None:
        self.assertEqual(sleep.effective_window(self.root, base=(22, 6)), (22, 6))
        self.assertEqual(sleep.effective_window(self.root, base=[1, 5]), (1, 5))
        night_late(self.root, 1, late_min=90)
        self.assertEqual(sleep.effective_window(self.root, base=(22, 6)), (23, 7))

    def test_effective_window_rejects_a_bad_base(self) -> None:
        for bad in ((23,), (23, 8, 1), (24, 8), (-1, 8), (23, 23), ("23", 8), (True, 8), 23):
            with self.assertRaises(ValueError):
                sleep.effective_window(self.root, base=bad)

    def test_default_base_is_energy_window(self) -> None:
        self.assertEqual(sleep.effective_window(self.root), energy.DEFAULT_SLEEP_WINDOW)

    # ---- 4) 睡够判定 / 起始值下调 ------------------------------------------

    def test_short_nights_accumulate_then_enough_clears(self) -> None:
        got = night(self.root, 1, minutes=240)
        self.assertFalse(got["night"]["enough"])
        self.assertEqual(got["consecutive_short_nights"], 1)
        self.assertEqual(got["wake_cap"], 100.0)
        got = night(self.root, 2, minutes=240)
        self.assertEqual(got["consecutive_short_nights"], 2)
        self.assertEqual(got["wake_cap"], 95.0)
        got = night(self.root, 3, minutes=480)
        self.assertTrue(got["night"]["enough"])
        self.assertEqual(got["consecutive_short_nights"], 0)
        self.assertEqual(got["wake_cap"], 100.0)
        self.assertEqual(sleep.wake_cap(self.root), 100.0)

    def test_three_short_nights_stay_penalised(self) -> None:
        for day in range(1, 4):
            night(self.root, day, minutes=240)
        self.assertEqual(state(self.root)["consecutive_short_nights"], 3)
        self.assertEqual(sleep.wake_cap(self.root), 95.0)

    def test_wake_cap_is_energy_max_minus_penalty(self) -> None:
        self.assertEqual(float(model.ENERGY_MAX), 100.0)
        night(self.root, 1, minutes=60)
        night(self.root, 2, minutes=60)
        set_config(self.root, wake_penalty=7.5)
        self.assertEqual(sleep.wake_cap(self.root), 92.5)

    def test_late_and_short_are_independent(self) -> None:
        got = night_late(self.root, 1, late_min=90, minutes=480)
        self.assertEqual(got["consecutive_late_nights"], 1)
        self.assertEqual(got["consecutive_short_nights"], 0)
        got = night(self.root, 2, minutes=240)
        self.assertEqual(got["consecutive_late_nights"], 0)
        self.assertEqual(got["consecutive_short_nights"], 1)

    # ---- 5) config 落盘、可改、改了就生效 ----------------------------------

    def test_enough_min_from_config_file_flips_the_verdict(self) -> None:
        for day in (1, 2):
            night(self.root, day, minutes=330)
        self.assertEqual(state(self.root)["consecutive_short_nights"], 2)
        self.assertEqual(sleep.wake_cap(self.root), 95.0)
        set_config(self.root, enough_min=300)
        self.assertEqual(sleep.overview(self.root)["config"]["enough_min"], 300.0)
        got = night(self.root, 3, minutes=330)
        self.assertTrue(got["night"]["enough"])
        self.assertEqual(got["consecutive_short_nights"], 0)
        self.assertEqual(got["wake_cap"], 100.0)

    def test_late_tolerance_from_config_file_flips_the_verdict(self) -> None:
        got = night_late(self.root, 1, late_min=90)
        self.assertTrue(got["night"]["late"])
        set_config(self.root, late_tolerance_min=120)
        got = night_late(self.root, 2, late_min=90)
        self.assertFalse(got["night"]["late"])
        self.assertEqual(got["consecutive_late_nights"], 0)
        self.assertEqual(sleep.drift_hours(self.root), 0.0)

    def test_drift_per_night_and_cap_from_config_file(self) -> None:
        night(self.root, 0)  # 先让文件落盘
        set_config(self.root, drift_per_late_night_h=1.0, drift_cap_h=2.0)
        for day in range(1, 4):
            night_late(self.root, day, late_min=90)
        self.assertEqual(sleep.drift_hours(self.root), 2.0)
        self.assertEqual(sleep.effective_window(self.root), (1, 10))

    def test_config_extras_are_preserved(self) -> None:
        night(self.root, 1)
        set_config(self.root, mine="keep-me")
        night(self.root, 2)
        self.assertEqual(state(self.root)["config"]["mine"], "keep-me")

    # ---- 6) overview / 读时算 / 坏数据 -------------------------------------

    def test_overview_reports_every_field(self) -> None:
        for day in range(1, 8):
            night_late(self.root, day, late_min=90, minutes=300)
        snap = sleep.overview(self.root, now=ts(8, 8))
        self.assertEqual(snap["consecutive_late_nights"], 7)
        self.assertEqual(snap["consecutive_short_nights"], 7)
        self.assertEqual(snap["drift_hours"], 3.0)
        self.assertEqual(snap["effective_window"], (2, 11))
        self.assertEqual(snap["wake_cap"], 95.0)
        self.assertEqual(snap["last_asleep_at"], ts(8, 0, 30))
        self.assertEqual(snap["last_duration_min"], 300.0)
        self.assertEqual(len(snap["nights"]), 5)
        self.assertEqual(snap["nights"][-1]["asleep_at"], ts(8, 0, 30))
        self.assertEqual(snap["awake_minutes"], 150.0)
        self.assertEqual(snap["config"], sleep.CONFIG_DEFAULTS)

    def test_overview_on_an_empty_root_does_not_write(self) -> None:
        snap = sleep.overview(self.root, now=ts(1, 12))
        self.assertEqual(snap["effective_window"], (23, 8))
        self.assertEqual(snap["wake_cap"], 100.0)
        self.assertEqual(snap["drift_hours"], 0.0)
        self.assertIsNone(snap["last_asleep_at"])
        self.assertIsNone(snap["last_duration_min"])
        self.assertIsNone(snap["awake_minutes"])
        self.assertEqual(snap["nights"], [])
        self.assertFalse(self.file.exists())

    def test_awake_minutes_counts_from_last_wake(self) -> None:
        self.assertIsNone(sleep.awake_minutes(self.root, now=ts(3, 12)))
        night(self.root, 1)  # 第 2 天 07:00 醒
        self.assertEqual(sleep.awake_minutes(self.root, now=ts(2, 9)), 120.0)
        self.assertEqual(sleep.awake_minutes(self.root, now=ts(3, 7)), 24 * 60.0)
        self.assertEqual(sleep.awake_minutes(self.root, now=ts(2, 5)), 0.0)

    def test_derived_reads_never_write(self) -> None:
        night_late(self.root, 1, late_min=90, minutes=300)
        before = self.file.read_bytes()
        sleep.drift_hours(self.root, now=ts(3, 9))
        sleep.effective_window(self.root, now=ts(3, 9))
        sleep.wake_cap(self.root, now=ts(3, 9))
        sleep.awake_minutes(self.root, now=ts(3, 9))
        sleep.overview(self.root, now=ts(3, 9))
        self.assertEqual(self.file.read_bytes(), before)

    def test_bad_json_falls_back_to_defaults(self) -> None:
        helpers.write_raw(self.file, '{"version": 1, "nights": [{"asleep_at": "x"')
        snap = sleep.overview(self.root, now=ts(1, 12))
        self.assertEqual(snap["drift_hours"], 0.0)
        self.assertEqual(snap["wake_cap"], 100.0)
        self.assertEqual(snap["nights"], [])
        self.assertEqual(snap["config"], sleep.CONFIG_DEFAULTS)

    def test_bad_night_rows_are_skipped(self) -> None:
        helpers.write_raw(
            self.file,
            json.dumps(
                {
                    "version": 1,
                    "config": {"enough_min": 360},
                    "last_asleep_at": ts(1, 23),
                    "last_awake_at": ts(2, 7),
                    "last_duration_min": 480.0,
                    "nights": [
                        {"asleep_at": "坏的", "awake_at": ts(2, 7)},
                        {"asleep_at": ts(1, 23)},
                        "not-a-dict",
                        {"asleep_at": ts(1, 23), "awake_at": ts(2, 7), "late": False},
                    ],
                    "consecutive_late_nights": -3,
                    "consecutive_short_nights": "2",
                },
                ensure_ascii=False,
            ),
        )
        snap = sleep.overview(self.root, now=ts(2, 8))
        self.assertEqual(len(snap["nights"]), 1)
        self.assertEqual(snap["nights"][0]["duration_min"], 480.0)
        self.assertEqual(snap["consecutive_late_nights"], 0)
        self.assertEqual(snap["consecutive_short_nights"], 2)
        self.assertEqual(snap["wake_cap"], 95.0)

    def test_corrupt_config_values_fall_back_per_key(self) -> None:
        night(self.root, 1)
        set_config(self.root, enough_min="坏值", late_tolerance_min=None)
        cfg = sleep.overview(self.root)["config"]
        self.assertEqual(cfg["enough_min"], 360.0)
        self.assertEqual(cfg["late_tolerance_min"], 60.0)

    def test_ledger_three_days_old_still_reads_sensibly(self) -> None:
        for day in range(1, 4):
            night_late(self.root, day, late_min=90, minutes=300)
        shift_days_back(self.root, days=3)
        now = ts(7, 12)  # 最后一次醒（第 4 天 04:00 之前）再往后三天
        snap = sleep.overview(self.root, now=now)
        self.assertEqual(snap["consecutive_late_nights"], 3)
        self.assertEqual(snap["consecutive_short_nights"], 3)
        self.assertEqual(snap["drift_hours"], 1.5)
        self.assertEqual(snap["effective_window"], (1, 10))
        self.assertEqual(snap["wake_cap"], 95.0)
        self.assertGreater(snap["awake_minutes"], 3 * 24 * 60)
        self.assertEqual(sleep.awake_minutes(self.root, now=now), snap["awake_minutes"])

    # ---- 7) 禁止项：不挂定时器、不引入半衰 --------------------------------

    def test_no_timer_or_thread_symbols(self) -> None:
        source = Path(sleep.__file__).read_text(encoding="utf-8")
        for banned in ("threading", "Thread", "Timer", "asyncio", "schedule", "cron", "time.sleep"):
            self.assertNotIn(banned, source)

    def test_no_half_life_symbols(self) -> None:
        source = Path(sleep.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            re.findall(r"\b(HALF_LIFE|half_life|HALF_LIFE_H|adenosine|ADENOSINE|Adenosine)\b", source),
            [],
        )

    def test_only_three_public_facts_are_stored(self) -> None:
        # 三样：上次入睡时刻、睡了多久、连续熬夜；副产品是 nights 与另一个连续计数。
        night(self.root, 1)
        data = state(self.root)
        self.assertIn("last_asleep_at", data)
        self.assertIn("last_duration_min", data)
        self.assertIn("consecutive_late_nights", data)
        self.assertNotIn("adenosine", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
