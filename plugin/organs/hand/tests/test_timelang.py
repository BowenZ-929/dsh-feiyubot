"""最小时间词表：认得出的那几种、认不出就 None，还有「不补时区」。"""
from __future__ import annotations

import unittest

from organs.hand import timelang

from . import helpers

NOW = helpers.NOW


class ParseTest(unittest.TestCase):
    def test_iso_with_offset_comes_back_untouched(self):
        text = "2026-11-06T12:00:00+08:00"
        self.assertEqual(timelang.parse(text, now=NOW), text)

    def test_iso_without_offset_is_refused_not_filled_in(self):
        self.assertIsNone(timelang.parse("2026-11-06T12:00:00", now=NOW))

    def test_tomorrow_noon(self):
        self.assertEqual(timelang.parse("明天中午", now=NOW), "2026-11-07T12:00:00+09:00")

    def test_today_morning(self):
        self.assertEqual(timelang.parse("今天早上", now=NOW), "2026-11-06T08:00:00+09:00")

    def test_day_after_tomorrow_night(self):
        self.assertEqual(timelang.parse("后天夜里", now=NOW), "2026-11-08T22:00:00+09:00")

    def test_afternoon_n_oclock_shifts_by_twelve(self):
        self.assertEqual(timelang.parse("下午3点半", now=NOW), "2026-11-06T15:30:00+09:00")
        self.assertEqual(timelang.parse("明天下午3点", now=NOW), "2026-11-07T15:00:00+09:00")

    def test_bare_hour_already_past_rolls_to_tomorrow(self):
        # NOW 是 09:00，「8点」今天已经过了 → 明天 08:00
        self.assertEqual(timelang.parse("8点", now=NOW), "2026-11-07T08:00:00+09:00")

    def test_bare_hour_still_ahead_stays_today(self):
        self.assertEqual(timelang.parse("下午5点", now=NOW), "2026-11-06T17:00:00+09:00")

    def test_written_day_is_not_rolled(self):
        # 写了「今天」就照字面落，不顺延——顺延等于替她改主意
        self.assertEqual(timelang.parse("今天8点", now=NOW), "2026-11-06T08:00:00+09:00")

    def test_midnight_by_period_words(self):
        self.assertEqual(timelang.parse("晚上12点", now=NOW), "2026-11-07T00:00:00+09:00")

    def test_out_of_table_is_none(self):
        for text in ("下周再说", "开完会", "", "   ", "明天", "25点", "早上25点", None, 42):
            self.assertIsNone(timelang.parse(text, now=NOW), text)
