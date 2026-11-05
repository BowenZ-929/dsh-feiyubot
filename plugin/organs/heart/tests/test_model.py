"""换日收敛：心的 day_key 不再自己实现一份，一律调日子线那一份（显式 cutover_hour=0）。

判据（改之前先读）：
1. 全仓 `def day_key` 只许一处真源——organs/day/model.py。心这一份在本轮删掉；
   脑那一份由脑线自己的轮次收敛成 import，本线只读断言、一个字节都不改。
2. 心的换日口径是**纯日期**（本地时区当天），而日子线的默认口径带换日点
   （limits()["day_cutover_hour"] 默认 4，凌晨那几个钟头算前一天）。所以每个调用点
   都必须**显式写 cutover_hour=0**：显式 0 才等于心的旧口径，旧数据复算才可能 0 处差异。
   省掉这个 0 不会报错，只会在 00:00–03:59 静悄悄地换错一天——这条护栏比"没异常"值钱。
3. 不许在 organs/heart 里再包一层转发的 `def day_key`：那等于第二处实现，第 1 条会红。
"""
from __future__ import annotations

import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from organs.brain import model as brain_model
from organs.day import model as day_model
from organs.heart import energy, model as heart_model
from organs.heart import mood, offset

PLUGIN_ROOT = Path(__file__).resolve().parents[3]  # <repo>/plugin
HEART_ROOT = Path(__file__).resolve().parents[1]  # <repo>/plugin/organs/heart

# 只认"行首就是 def day_key(" 的那种定义：脑的回归用例里那句
# assertNotIn("def day_key", ...) 是字面串，不算定义。
DEF_DAY_KEY = re.compile(r"^\s*def\s+day_key\s*\(", re.MULTILINE)
# 这些模块是心的换日调用点，每个都必须是 day_model.day_key(..., cutover_hour=0)
CALLER_FILES = ("energy.py", "mood.py", "offset.py")


def _defining_files(root: Path):
    """root 底下所有定义了自己的 day_key 的文件（相对 posix 路径，排好序）。"""
    found = []
    for path in sorted(root.rglob("*.py")):
        if DEF_DAY_KEY.search(path.read_text(encoding="utf-8")):
            found.append(path.relative_to(root).as_posix())
    return found


def _local(hour: int, minute: int = 0, second: int = 0, day: int = 5) -> datetime:
    """本机时区的一个固定时刻：2026-11-<day> <hour>:<minute>。"""
    tz = datetime.now().astimezone().tzinfo
    return datetime(2026, 11, day, hour, minute, second, tzinfo=tz)


class DayKeyConvergenceTest(unittest.TestCase):
    def test_whole_repo_has_exactly_one_day_key(self):
        """全仓唯一的换日实现：organs/day/model.py；心的那份已经没了。"""
        defining = _defining_files(PLUGIN_ROOT)
        self.assertEqual(
            ["organs/day/model.py"],
            defining,
            "全仓唯一的换日实现只许是 organs/day/model.py，实得 %r" % (defining,),
        )

    def test_heart_has_no_day_key_of_its_own(self):
        """心一处都没有：organs/heart 连同它自己的 tests 都不许再定义 day_key。"""
        offenders = _defining_files(HEART_ROOT) + [
            "tests/" + name for name in _defining_files(HEART_ROOT / "tests")
        ]
        self.assertEqual(
            [], offenders, "organs/heart 里不许再有 def day_key（含转发），实得 %r" % (offenders,)
        )
        self.assertFalse(hasattr(heart_model, "day_key"), "心不再提供 day_key 这个名字")

    def test_brain_only_re_exports_the_one(self):
        """脑那份属脑线、由它自己的轮次收敛：本线只读断言，不碰脑的文件一个字。"""
        self.assertIs(
            brain_model.day_key,
            day_model.day_key,
            "脑的 day_key 必须就是 organs/day/model 那一份（只读断言，本线不改脑）",
        )
        self.assertNotIn(
            "def day_key",
            Path(brain_model.__file__).read_text(encoding="utf-8"),
            "脑那份属脑线，本线不改：它在另一轮已经换成了 import",
        )

    def test_heart_call_sites_pass_cutover_hour_zero(self):
        """心的每个调用点都显式 cutover_hour=0，且都走日子线那一份（不许省这个 0）。"""
        for name in CALLER_FILES:
            source = (HEART_ROOT / name).read_text(encoding="utf-8")
            calls = re.findall(r"[A-Za-z_][\w.]*\.day_key\([^)]*\)", source)
            self.assertTrue(calls, "%s 应当有一处换日调用" % name)
            for call in calls:
                self.assertTrue(
                    call.startswith("day_model.day_key("),
                    "%s 的换日必须调日子线那一份，实得 %r" % (name, call),
                )
                self.assertIn(
                    "cutover_hour=0",
                    call,
                    "%s 的换日必须显式 cutover_hour=0（默认口径带换日点，会换错一天）：%r"
                    % (name, call),
                )
        for module in (energy, mood, offset):
            self.assertIs(
                getattr(module, "day_model").day_key,
                day_model.day_key,
                "%s 用的必须是 organs.day.model.day_key" % module.__name__,
            )

    def test_heart_day_key_matches_plain_date(self):
        """换日点附近：心的换日 == day_model.day_key(cutover_hour=0) == 纯本地日期。"""
        cases = [
            _local(0, 30),          # 刚过午夜：换日点前，日子线默认口径会算成前一天
            _local(3, 59, 59),      # 换日点前一秒
            _local(4, 0),           # 换日点整
            _local(23, 59),         # 当天最后一分钟
            _local(0, 0),           # 午夜整
        ]
        for stamp in cases:
            written = stamp.isoformat()
            plain = stamp.date().isoformat()
            heart_side = heart_model.parse_ts(written).astimezone(
                day_model.local_tz()
            ).date().isoformat()
            self.assertEqual(plain, heart_side, "心的旧口径就是本地纯日期")
            self.assertEqual(plain, day_model.day_key(written, cutover_hour=0))
            self.assertEqual(
                day_model.day_key(written, cutover_hour=0),
                day_model.day_key(stamp.astimezone(timezone.utc).isoformat(), cutover_hour=0),
                "同一时刻换个偏移写，换日结果不变",
            )

    def test_cutover_hour_zero_is_what_preserves_the_old_semantics(self):
        """默认口径（换日点 4）在凌晨会把日期算成前一天：这就是必须显式传 0 的理由。"""
        at_0030 = _local(0, 30).isoformat()
        at_0359 = _local(3, 59, 59).isoformat()
        at_0400 = _local(4, 0).isoformat()
        self.assertEqual("2026-11-04", day_model.day_key(at_0030, cutover_hour=4))
        self.assertEqual("2026-11-05", day_model.day_key(at_0030, cutover_hour=0))
        self.assertEqual("2026-11-04", day_model.day_key(at_0359, cutover_hour=4))
        self.assertEqual("2026-11-05", day_model.day_key(at_0359, cutover_hour=0))
        self.assertEqual(
            day_model.day_key(at_0400, cutover_hour=4),
            day_model.day_key(at_0400, cutover_hour=0),
            "换日点整这一天，两种口径必须给出同一天",
        )
