"""拒绝记录的验收：只拒这个话题、只由时间解除、额度是刹车不是许可。

判据：下文所有数都从 DEFAULT_CONFIG 派生（冷却 12h、增长 1.5、封顶 168h、
保留 7 天、额度 3），改默认值时期望跟着改。
词表一律指到临时那份 HEART_TOPICS_FILE：真词表人会补词，测试不拿它当判据；
只有词表那一组用例显式清掉环境变量，去读仓库里真的那份，钉住"读得到真实键"。
所有用例都在 helpers.make_root 的临时目录里跑，绝不写真 data。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import tempfile
import unittest
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict

from organs.day import model as day_model
from organs.heart import model, mood, refuse, store
from organs.heart.tests import helpers

BASE_HOUR = 14


def _local(hour: int = BASE_HOUR) -> datetime:
    """本机时区的一个固定时刻：2025-01-15 <hour>:00。"""
    tz = datetime.now().astimezone().tzinfo
    return datetime(2025, 1, 15, hour, 0, 0, tzinfo=tz)


def _at(hours: float = 0.0) -> str:
    return model.iso(_local() + timedelta(hours=hours))


def _day_of(stamp: str) -> str:
    return day_model.day_key(stamp, cutover_hour=0)


class RefuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)
        self.state_path = self.root / refuse.REFUSALS_FILE

        # 临时词表：真词表人可以随时补词，测试不拿它当判据。
        self.topics = Path(self.tmp.name) / "topics.json"
        self._write_topics({
            "医院": ["住院", "医院", "病"],
            "工作": ["工作", "入职"],
            "猫": ["猫", "年糕"],
        })
        self._old_env = os.environ.get(refuse.ENV_TOPICS_FILE)
        os.environ[refuse.ENV_TOPICS_FILE] = str(self.topics)
        self.addCleanup(self._restore_env)

    # ---- 小工具 ----------------------------------------------------------

    def _restore_env(self) -> None:
        if self._old_env is None:
            os.environ.pop(refuse.ENV_TOPICS_FILE, None)
        else:
            os.environ[refuse.ENV_TOPICS_FILE] = self._old_env

    def _write_topics(self, table: Dict[str, Any]) -> None:
        helpers.write_raw(self.topics, json.dumps(table, ensure_ascii=False))

    def _seed(self, config: Any = None, rows: Any = None,
              day: Any = None, used: int = 0) -> None:
        """往临时根里放一份状态文件（伪造坏配置 / 老行用）。"""
        helpers.write_raw(self.state_path, json.dumps({
            "version": refuse.VERSION,
            "config": dict(refuse.DEFAULT_CONFIG) if config is None else config,
            "day": _day_of(_at(0)) if day is None else day,
            "used_today": used,
            "refusals": [] if rows is None else rows,
        }, ensure_ascii=False))

    def _sha(self) -> str:
        return hashlib.sha256(self.state_path.read_bytes()).hexdigest()

    # ---- 1. 冷却 12 小时，只由时间解除 -----------------------------------

    def test_ingest_cools_twelve_hours_then_time_lifts_it(self) -> None:
        out = refuse.ingest("医院", "refused-topic", root=self.root, now=_at(0))
        self.assertTrue(out["ok"])
        self.assertEqual(out["topic"], "医院")
        self.assertEqual(out["reason"], "refused-topic")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["remaining_min"], 720.0)
        self.assertEqual(out["used_today"], 1)
        self.assertEqual(out["quota"], 3)
        self.assertEqual(out["violations"], [])

        got = refuse.check(["医院"], root=self.root, now=_at(0))
        self.assertEqual(got["as_of"], _at(0))
        self.assertEqual(got["cooling"], ["医院"])
        item = got["items"][0]
        self.assertTrue(item["known"])
        self.assertTrue(item["cooling"])
        self.assertEqual(item["remaining_min"], 720.0)
        self.assertEqual(item["reason"], "refused-topic")
        self.assertEqual(item["count"], 1)
        self.assertEqual(got["quota"], {"used_today": 1, "limit": 3, "left": 2})

        # +13h：越过 until，不用任何人拍板，时间到了就解除。
        after = refuse.check(["医院"], root=self.root, now=_at(13))
        self.assertEqual(after["cooling"], [])
        self.assertFalse(after["items"][0]["cooling"])
        self.assertEqual(after["items"][0]["remaining_min"], 0.0)
        # 但"她记得自己拒过"：过期的行仍带着理由与次数。
        self.assertEqual(after["items"][0]["reason"], "refused-topic")
        self.assertEqual(after["items"][0]["count"], 1)

    # ---- 2. 连拒三次：12 / 18 / 27 小时，since 不动 -----------------------

    def test_three_refusals_grow_twelve_eighteen_twentyseven(self) -> None:
        a = refuse.ingest("医院", "refused-topic", root=self.root, now=_at(0))
        b = refuse.ingest("医院", "refused-topic", root=self.root, now=_at(1))
        c = refuse.ingest("医院", "refused-topic", root=self.root, now=_at(2))

        self.assertEqual([a["count"], b["count"], c["count"]], [1, 2, 3])
        self.assertEqual(a["since"], _at(0))
        self.assertEqual([a["since"], b["since"], c["since"]],
                         [_at(0), _at(0), _at(0)])
        hours = [round(model.hours_between(x["since"], x["until"]), 3)
                 for x in (a, b, c)]
        self.assertEqual(hours, [12.0, 18.0, 27.0])
        # 冷却延长不许越过封顶。
        cap = float(refuse.DEFAULT_CONFIG["max_cooldown_h"])
        for out in (a, b, c):
            span = model.hours_between(out["since"], out["until"])
            self.assertLessEqual(span, cap)
            self.assertAlmostEqual(out["remaining_min"],
                                   round(model.hours_between(_at(out["count"] - 1),
                                                             out["until"]) * 60, 1))
        self.assertEqual(c["used_today"], 3)
        self.assertEqual(c["quota"], 3)
        self.assertEqual(c["remaining_min"], round(27.0 * 60 - 120, 1))

    def test_cooldown_stops_at_max(self) -> None:
        """封顶即止：max_cooldown_h=13 时第二次 18 → 13，第三次 27 → 仍是 13。"""
        self._seed(config=dict(refuse.DEFAULT_CONFIG, max_cooldown_h=13.0))
        a = refuse.ingest("医院", "refused-topic", root=self.root, now=_at(0))
        b = refuse.ingest("医院", "refused-topic", root=self.root, now=_at(1))
        c = refuse.ingest("医院", "refused-topic", root=self.root, now=_at(2))
        spans = [model.hours_between(x["since"], x["until"]) for x in (a, b, c)]
        self.assertEqual(spans, [12.0, 13.0, 13.0])
        self.assertEqual([a["count"], b["count"], c["count"]], [1, 2, 3])

    # ---- 3. 额度：用完不落库，换一天又能拒 -------------------------------

    def test_fourth_refusal_is_blocked_and_writes_nothing(self) -> None:
        for hour in (0, 1, 2):
            out = refuse.ingest("医院", "refused-topic", root=self.root,
                                now=_at(hour))
            self.assertTrue(out["ok"])
        before = self._sha()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            blocked = refuse.ingest("医院", "refused-topic", root=self.root,
                                    now=_at(3))
        self.assertEqual(blocked, {
            "ok": False,
            "code": "quota-exhausted",
            "message": "今日额度已满（3/3）",
            "remaining_min": None,
        })
        self.assertTrue([w for w in caught if "额度" in str(w.message)],
                        "额度用完不许静默吞，要留一行 WARN")
        self.assertEqual(before, self._sha(), "额度用完时一个字节都不该写盘")

        # 换一天：额度归零，又能拒（冷却还在就继续在同一行上加次数）。
        later = refuse.ingest("医院", "refused-topic", root=self.root, now=_at(24))
        self.assertTrue(later["ok"])
        self.assertEqual(later["used_today"], 1)
        self.assertEqual(later["count"], 4)
        self.assertEqual(_day_of(_at(24)), helpers.read_json_file(
            self.state_path)["day"])

    # ---- 4. mood 升两档也不解除；冷却只由时间解除 ------------------------

    def test_mood_rising_does_not_lift_cooling(self) -> None:
        refuse.ingest("医院", "refused-topic", root=self.root, now=_at(0))
        was = refuse.check(["医院"], root=self.root, now=_at(0))
        self.assertEqual(was["cooling"], ["医院"])

        first = mood.apply_event("praise", root=self.root, now=_at(0))
        second = mood.apply_event("praise", root=self.root, now=_at(2))
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertGreater(second["after"], first["before"], "mood 得真的涨了两档")

        warm = refuse.check(["医院"], root=self.root, now=_at(2))
        self.assertEqual(warm["cooling"], ["医院"], "心情好了不等于话题不疼了")
        self.assertGreater(warm["items"][0]["remaining_min"], 0.0)

        lifted = refuse.check(["医院"], root=self.root, now=_at(13))
        self.assertEqual(lifted["cooling"], [], "只有 now 越过 until 才解除")

    def test_refuse_source_imports_neither_mood_nor_energy(self) -> None:
        """源码断言：没有 import mood / energy，"心情好了就提前解除"就没有入口。"""
        tree = ast.parse(Path(refuse.__file__).read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names.append(module)
                names.extend("%s.%s" % (module, alias.name)
                             for alias in node.names)
        offenders = [n for n in names if n.split(".")[-1] in ("mood", "energy")]
        self.assertEqual([], offenders,
                         "refuse.py 不许 import mood / energy，实得 %r" % (offenders,))

    # ---- 5. validate_config：默认干净，冷却越过保留期必须报 -----------------

    def test_validate_config_default_is_clean(self) -> None:
        self.assertEqual([], refuse.validate_config(refuse.DEFAULT_CONFIG))
        self.assertEqual([], refuse.validate_config({}))

    def test_validate_config_flags_cooldown_over_retention(self) -> None:
        """冷却 8 天 > 保留 7 天：记录还没到期就已经被清掉了，必须报出来。"""
        bad = dict(refuse.DEFAULT_CONFIG, cooldown_h=8 * 24.0)
        self.assertIn(refuse.BAD_COOLDOWN_OVER_RETENTION,
                      refuse.validate_config(bad))
        capped = dict(refuse.DEFAULT_CONFIG, max_cooldown_h=8 * 24.0)
        self.assertIn(refuse.BAD_MAX_COOLDOWN_OVER_RETENTION,
                      refuse.validate_config(capped))
        none = dict(refuse.DEFAULT_CONFIG, daily_quota=0)
        self.assertIn(refuse.BAD_QUOTA, refuse.validate_config(none))
        slow = dict(refuse.DEFAULT_CONFIG, growth=0.5)
        self.assertIn(refuse.BAD_GROWTH, refuse.validate_config(slow))

    def test_bad_config_is_clamped_to_retention_and_warned(self) -> None:
        self._seed(config=dict(refuse.DEFAULT_CONFIG, cooldown_h=8 * 24.0))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            out = refuse.ingest("医院", "refused-topic", root=self.root, now=_at(0))
        self.assertIn(refuse.BAD_COOLDOWN_OVER_RETENTION, out["violations"])
        self.assertTrue([w for w in caught if "夹回" in str(w.message)])
        stored = helpers.read_json_file(self.state_path)
        self.assertEqual(stored["config"]["cooldown_h"],
                         float(refuse.DEFAULT_CONFIG["retention_days"]) * 24.0)
        # 夹回之后冷却落在保留期边界上：这条记录不会先于它的冷却被清掉。
        self.assertEqual(out["remaining_min"], 168.0 * 60.0)
        self.assertLessEqual(out["remaining_min"],
                             float(refuse.DEFAULT_CONFIG["retention_days"]) * 24 * 60)

    # ---- 6. 共用词表 ----------------------------------------------------

    def test_vocabulary_keeps_file_order_and_top_level_keys_only(self) -> None:
        names = refuse.vocabulary()
        self.assertEqual(names, ["医院", "工作", "猫"])
        for keyword in ("住院", "入职", "年糕"):
            self.assertNotIn(keyword, names, "词表只取顶层键，关键词是脑的事")

    def test_unknown_topic_is_refused_with_a_clear_message(self) -> None:
        with self.assertRaises(ValueError) as caught:
            refuse.ingest("不存在的话题", "refused-topic", root=self.root,
                          now=_at(0))
        self.assertIn("话题必须取自脑的话题词表", str(caught.exception))
        self.assertFalse(self.state_path.exists(), "造分类被拒时不该落盘")

    def test_bad_reason_raises_and_names_the_bad_value(self) -> None:
        with self.assertRaises(ValueError) as caught:
            refuse.ingest("医院", "拒绝", root=self.root, now=_at(0))
        self.assertIn("拒绝", str(caught.exception))
        self.assertFalse(self.state_path.exists())

    def test_missing_topics_file_gives_empty_table_and_warns(self) -> None:
        os.environ[refuse.ENV_TOPICS_FILE] = str(
            Path(self.tmp.name) / "nope" / "topics.json")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual([], refuse.vocabulary())
        self.assertTrue([w for w in caught if "词表" in str(w.message)],
                        "读不到词表要留一行 WARN，且不许抛")

    def test_new_word_in_shared_table_works_immediately(self) -> None:
        with self.assertRaises(ValueError):
            refuse.ingest("海边", "refused-nobody", root=self.root, now=_at(0))
        self._write_topics({"医院": ["住院"], "海边": ["聚会"]})
        out = refuse.ingest("海边", "refused-nobody", root=self.root, now=_at(0))
        self.assertTrue(out["ok"])
        self.assertEqual(out["reason"], "refused-nobody")

    def test_vocabulary_reads_the_real_table(self) -> None:
        """清掉覆盖变量：真词表里得有话题，且顺序 = 文件里的顺序。"""
        os.environ.pop(refuse.ENV_TOPICS_FILE, None)
        names = refuse.vocabulary()
        self.assertTrue(names, "仓库里的真词表不该是空的")
        self.assertEqual(len(names), len(set(names)))
        real = None
        for candidate in (refuse._topic_paths()):
            try:
                real = json.loads(Path(candidate).read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                continue
        self.assertIsInstance(real, dict)
        self.assertEqual(names, [key for key in real])
        self.assertIn("医院", names)

    # ---- 只读的 check、落盘形状、清理 ------------------------------------

    def test_check_never_writes(self) -> None:
        self.assertFalse(self.state_path.exists())
        empty = refuse.check(["医院"], root=self.root, now=_at(0))
        self.assertFalse(self.state_path.exists(), "check 不许造文件")
        self.assertEqual(empty["items"][0]["cooling"], False)
        self.assertEqual(empty["items"][0]["count"], 0)
        self.assertIsNone(empty["items"][0]["reason"])
        self.assertEqual(empty["quota"], {"used_today": 0, "limit": 3, "left": 3})

        refuse.ingest("医院", "refused-topic", root=self.root, now=_at(0))
        payload = self.state_path.read_bytes()
        mtime = self.state_path.stat().st_mtime_ns
        got = refuse.check(["医院", "工作", "外人"], root=self.root, now=_at(1))
        self.assertEqual(payload, self.state_path.read_bytes(), "check 一个字节都不写")
        self.assertEqual(mtime, self.state_path.stat().st_mtime_ns)
        by_topic = {item["topic"]: item for item in got["items"]}
        self.assertTrue(by_topic["工作"]["known"])
        self.assertFalse(by_topic["工作"]["cooling"])
        self.assertFalse(by_topic["外人"]["known"])
        self.assertEqual(got["cooling"], ["医院"])

    def test_state_file_shape(self) -> None:
        refuse.ingest("医院", "refused-topic", root=self.root, now=_at(0))
        data = helpers.read_json_file(self.state_path)
        self.assertEqual(sorted(data),
                         sorted(["version", "config", "day", "used_today",
                                 "refusals"]))
        self.assertEqual(data["version"], refuse.VERSION)
        self.assertEqual(data["day"], _day_of(_at(0)))
        self.assertEqual(data["used_today"], 1)
        self.assertEqual(data["config"], refuse.DEFAULT_CONFIG)
        row = data["refusals"][0]
        self.assertEqual(sorted(row),
                         sorted(["topic", "since", "until", "reason", "count"]))
        self.assertEqual(row["topic"], "医院")
        self.assertEqual(row["reason"], "refused-topic")
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["since"], _at(0))
        self.assertEqual(row["until"], _at(12))

    def test_expired_row_restarts_at_count_one(self) -> None:
        refuse.ingest("医院", "refused-topic", root=self.root, now=_at(0))
        out = refuse.ingest("医院", "refused-mood", root=self.root, now=_at(13))
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["since"], _at(13))
        self.assertEqual(out["until"], _at(25))
        self.assertEqual(out["reason"], "refused-mood")

    def test_old_rows_are_pruned_by_retention(self) -> None:
        stale = {
            "topic": "工作",
            "since": _at(-8 * 24),
            "until": _at(-8 * 24 + 12),
            "reason": "refused-topic",
            "count": 1,
        }
        self._seed(rows=[stale])
        refuse.ingest("医院", "refused-topic", root=self.root, now=_at(0))
        data = helpers.read_json_file(self.state_path)
        self.assertEqual([row["topic"] for row in data["refusals"]], ["医院"])
        got = refuse.check(["工作"], root=self.root, now=_at(0))
        self.assertEqual(got["cooling"], [])
        self.assertIsNone(got["items"][0]["reason"])

    def test_broken_state_is_read_as_defaults(self) -> None:
        helpers.write_raw(self.state_path, "{ 坏掉的 json")
        out = refuse.ingest("医院", "refused-topic", root=self.root, now=_at(0))
        self.assertTrue(out["ok"], "读入永不抛：坏文件按默认重来")
        self.assertEqual(out["count"], 1)
        self.assertEqual(helpers.read_json_file(self.state_path)["used_today"], 1)

    def test_reasons_are_the_three_enum_values(self) -> None:
        self.assertEqual(refuse.REASONS,
                         ("refused-topic", "refused-mood", "refused-nobody"))
