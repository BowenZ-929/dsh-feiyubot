"""model：换日点、时区、时间戳解析与阈值读取。"""
from __future__ import annotations

import json
import unittest
from datetime import timezone

from organs import paths
from organs.day import model
from organs.day.tests.helpers import at, temp_root


class DayKeyTest(unittest.TestCase):
    def test_cutover_is_four_oclock_local(self):
        """03:59 还归昨天，04:00 就是新的一天（本地时区）。"""
        self.assertEqual("2026-11-04", model.day_key(at("2026-11-05T03:59:59")))
        self.assertEqual("2026-11-05", model.day_key(at("2026-11-05T04:00:00")))
        self.assertEqual("2026-11-05", model.day_key(at("2026-11-05T23:59:00")))
        self.assertEqual("2026-11-03", model.day_key(at("2026-11-04T00:30:00")), "凌晨 00:30 归前一天")

    def test_cutover_hour_can_be_injected(self):
        self.assertEqual("2026-11-05", model.day_key(at("2026-11-05T03:59:00"), cutover_hour=0))

    def test_day_key_follows_local_clock_not_the_written_offset(self):
        """同一时刻换个偏移写，day_key 不变：换日认本地钟点，认的是时区，不是字面。"""
        local = at("2026-11-05T03:00:00")
        utc = model.parse_ts(local).astimezone(timezone.utc).isoformat()
        self.assertNotEqual(local, utc, "两份写法必须真的不同，否则这条断言没意义")
        self.assertEqual(model.day_key(local), model.day_key(utc))
        self.assertEqual("2026-11-04", model.day_key(model.parse_ts(local)))


class TimestampTest(unittest.TestCase):
    def test_iso_collapses_one_instant_to_one_string(self):
        """09:00+08:00 与 01:00+00:00 是同一时刻 → 归一后逐字相同（id 因此也相同）。"""
        self.assertEqual(model.iso("2026-11-05T09:00:00+08:00"),
                         model.iso("2026-11-05T01:00:00+00:00"))

    def test_parse_accepts_zulu_and_trims_microseconds(self):
        stamp = model.parse_ts("2026-11-05T01:00:00Z")
        self.assertEqual("2026-11-05T01:00:00+00:00", stamp.isoformat())
        self.assertNotIn(".", model.iso("2026-11-05T09:00:00.123456+08:00"))

    def test_naive_is_read_as_local(self):
        self.assertEqual(model.local_tz(), model.parse_ts("2026-11-05T09:00:00").tzinfo)

    def test_garbage_raises_value_error(self):
        for bad in ("", "   ", "明天", None, 123):
            with self.assertRaises(ValueError):
                model.parse_ts(bad)


class LimitsTest(unittest.TestCase):
    def test_config_ships_with_the_four_thresholds(self):
        """阈值优先读 limits.json（没给 root 就是实盘 home/organs/day 那份）；文件必须真的在。"""
        self.assertTrue(model.limits_file().exists(), "%s 不见了" % model.limits_file())
        values = model.load_limits()
        self.assertEqual(60, values["tick_seconds"])
        self.assertEqual(10, values["min_gap_minutes"])
        self.assertEqual(model.DEFAULTS["daily_cap"], values["daily_cap"],
                         "这一条读的是文件里的 daily_cap，不写死数字：默认值一改它就该跟着改")
        self.assertEqual(4, values["day_cutover_hour"])
        # 自动排程表也在这份文件里：删掉它就没有自动排程，所以包内那份必须真的带着 night
        # （required: true 是这一版新加的：夜是"必须跑的"，不受额度管）
        self.assertEqual({"night": {"at": "03:30", "min_gap_minutes": 10, "daily_cap": 1,
                                    "required": True}},
                         values["kinds"])
        # 重试口径也在数据文件里：次数与退避都从这里读，代码只留兜底
        self.assertEqual(3, values["night"]["max_retries"])
        self.assertEqual([1, 5, 15], values["night"]["backoff_minutes"])

    def test_the_shipped_default_cap_is_a_fuse_not_a_rhythm(self):
        """DEFAULTS 里的 daily_cap 是保险丝：撞到是异常，所以离日常节奏很远（12 → 200）。"""
        self.assertEqual(200, model.DEFAULTS["daily_cap"])

    def test_the_data_file_and_the_defaults_never_drift_apart(self):
        """真源是文件，DEFAULTS 只是回落：两份副本不一致就是改了一半。"""
        self.assertEqual(model.DEFAULTS["daily_cap"], model.load_limits()["daily_cap"],
                         "出厂/实盘 limits.json 与 DEFAULTS 必须一致：默认值只是回落，真源是文件；"
                         "两边不一致就是改了一半")
        shipped = json.loads((model.DATA_DIR / model.LIMITS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(model.load_limits()["daily_cap"], shipped["daily_cap"],
                         "包内 data/limits.json 是实盘那份的出厂副本：两份 daily_cap 也得一致")

    def test_missing_config_falls_back_to_defaults(self):
        values = model.load_limits(temp_root(self))
        self.assertEqual(model.DEFAULTS["tick_seconds"], values["tick_seconds"])
        self.assertEqual(model.DEFAULTS["day_cutover_hour"], values["day_cutover_hour"])

    def test_bad_numbers_fall_back_one_by_one(self):
        root = temp_root(self)
        (root / model.LIMITS_FILE).write_text(
            json.dumps({"tick_seconds": "abc", "daily_cap": 5}), encoding="utf-8")
        values = model.load_limits(root)
        self.assertEqual(60, values["tick_seconds"])
        self.assertEqual(5, values["daily_cap"])

    def test_root_is_explicit_then_env_then_home(self):
        root = temp_root(self)
        self.assertEqual(root, model.day_root(root))
        # 没给参数、没有 DAY_ROOT：实盘根是 <仓库>/home/organs/day，包内 data/ 只是出厂默认参数
        self.assertEqual(paths.organ_dir("day"), model.day_root())


if __name__ == "__main__":
    unittest.main()
