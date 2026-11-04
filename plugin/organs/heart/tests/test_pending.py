"""念头层：只派生、只投递、只回吐——永不进证据链。

钉死的三件事，改之前先读：
1. settle 只由「嘴 / 脑投递」触发；本模块只把事件交给 mood.apply_event，
   永远不碰心情那本落盘文件——源码里连那个文件名都不许出现（见 forbidden 用例）。
2. TTL / attempts 全部读时现算，不挂定时器：这里所有「傍晚 / 明天」都靠传 now 造，
   没有一条用例在等真实时间。
3. 每条用例只落 helpers.make_root 的临时目录，绝不写仓里真 data。
"""
from __future__ import annotations

import hashlib
import inspect
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict
from unittest import mock

from organs.heart import energy, events, model, mood, offset, pending, store
from organs.heart.tests import helpers

T0 = "2026-11-03T12:00:00+08:00"


def at(minutes: float) -> str:
    """T0 之后第几分钟（测试里的「现在」全靠传，不靠等）。"""
    return model.iso(model.parse_ts(T0) + timedelta(minutes=float(minutes)))


def sha256_of(path: Any) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ignore(root: Any, times: int = 3, minute: float = 0.0) -> None:
    for _ in range(times):
        events.deliver("ignored", root=root, now=at(minute))


def seed_offsets(root: Any, values: Dict[str, float], when: str = T0) -> None:
    """直接造 offset.json：本用例只关心「轴值够不够大」，不走日事件封顶。"""
    axes = {
        axis: {
            "value": float(value),
            "day_delta": 0.0,
            "day": model.day_key(when),
            "updated_at": when,
            "idle_anchor_at": when,
        }
        for axis, value in values.items()
    }
    store.write_json(
        Path(root) / offset.OFFSET_FILE,
        {"version": offset.STATE_VERSION, "week": "", "axes": axes},
    )


def seed_offset(root: Any, axis: str, value: float, when: str = T0) -> None:
    seed_offsets(root, {axis: value}, when)


def seed_energy(root: Any, value: float, when: str = T0) -> None:
    store.write_json(
        Path(root) / energy.ENERGY_FILE,
        {
            "version": model.VERSION,
            "value": float(value),
            "updated_at": when,
            "day": model.day_key(when),
            "deducted": 0.0,
            "last_burst_at": None,
        },
    )


def axis_value(root: Any, axis: str, when: str = T0) -> float:
    return offset.overview(root=root, now=when)["axes"][axis]["value"]


class PendingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)
        self.file = Path(self.root) / pending.PENDING_FILE

    # ---- 1) 派生：三个来源，只派生不新建维度 -----------------------------

    def test_ignored_window_derives_wanted_praise(self):
        ignore(self.root, 3)
        self.assertEqual(len(pending.derive(root=self.root, now=T0)["created"]), 1)
        rows = pending.open_thoughts(root=self.root, now=T0)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["kind"], pending.KIND_PRAISE)
        self.assertEqual(row["source"], pending.SOURCE_IGNORED)
        self.assertIsNone(row["axis"])
        self.assertEqual(row["state"], pending.STATE_OPEN)
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["expires_at"], at(180))
        self.assertAlmostEqual(row["ttl_left_min"], 180.0)

    def test_two_ignored_are_not_enough(self):
        ignore(self.root, 2)
        self.assertEqual(pending.derive(root=self.root, now=T0)["created"], [])
        ignore(self.root, 1)
        self.assertEqual(len(pending.derive(root=self.root, now=T0)["created"]), 1)

    def test_a_reply_breaks_the_ignored_run(self):
        ignore(self.root, 3)
        events.deliver("replied", root=self.root, now=at(1))
        self.assertEqual(pending.derive(root=self.root, now=at(2))["created"], [])

    def test_derive_is_idempotent(self):
        ignore(self.root, 3)
        first = pending.derive(root=self.root, now=T0)
        second = pending.derive(root=self.root, now=at(5))
        self.assertEqual(second["created"], [])
        self.assertEqual(len(pending.open_thoughts(root=self.root, now=at(5))), 1)
        self.assertEqual(
            first["created"][0],
            pending.open_thoughts(root=self.root, now=at(5))[0]["id"],
        )

    def test_energy_low_derives_wanted_rest(self):
        seed_energy(self.root, 20.0)
        rows = pending.derive(root=self.root, now=T0)["open"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], pending.KIND_REST)
        self.assertEqual(rows[0]["source"], pending.SOURCE_ENERGY)
        self.assertIsNone(rows[0]["axis"])

    def test_energy_above_low_derives_nothing(self):
        seed_energy(self.root, 31.0)
        self.assertEqual(pending.derive(root=self.root, now=T0)["created"], [])

    def test_offset_high_derives_wanted_axis(self):
        seed_offset(self.root, "想黏人", 1.6)
        rows = pending.derive(root=self.root, now=T0)["open"]
        self.assertEqual(
            [(row["kind"], row["axis"], row["source"]) for row in rows],
            [(pending.KIND_AXIS, "想黏人", pending.SOURCE_OFFSET)],
        )

    def test_offset_takes_the_single_largest_axis(self):
        seed_offsets(self.root, {"想黏人": 1.6, "心疼": -1.8, "服气": 0.4})
        rows = pending.derive(root=self.root, now=T0)["open"]
        self.assertEqual([(row["kind"], row["axis"]) for row in rows], [(pending.KIND_AXIS, "心疼")])

    def test_offset_below_threshold_derives_nothing(self):
        seed_offset(self.root, "想黏人", 1.4)
        self.assertEqual(pending.derive(root=self.root, now=T0)["created"], [])

    # ---- 2) settle：只由事件触发，只投递、不写别人的文件 ------------------

    def test_settle_praise_closes_thought_and_lifts_mood(self):
        ignore(self.root, 3)
        pending.derive(root=self.root, now=T0)
        before = mood.read(root=self.root, now=T0)["now"]
        result = pending.settle("praise", root=self.root, now=T0, who="主人")
        after = mood.read(root=self.root, now=T0)["now"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], pending.STATE_SETTLED)
        self.assertTrue(result["event"]["ok"])
        self.assertEqual(result["event"]["kind"], model.EVENT_PRAISE)
        self.assertGreater(after, before, "settle 必须把事件真的投给 mood")
        self.assertEqual(pending.open_thoughts(root=self.root, now=T0), [])

    def test_settle_by_thought_id(self):
        ignore(self.root, 3)
        pending.derive(root=self.root, now=T0)
        thought_id = pending.open_thoughts(root=self.root, now=T0)[0]["id"]
        result = pending.settle(thought_id, root=self.root, now=T0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], thought_id)

    def test_settle_rest_only_marks_settled(self):
        seed_energy(self.root, 20.0)
        pending.derive(root=self.root, now=T0)
        result = pending.settle("rest", root=self.root, now=T0)
        self.assertEqual(result["state"], pending.STATE_SETTLED)
        self.assertIsNone(result["event"], "rest 不投心情事件")
        self.assertFalse((Path(self.root) / mood.MOOD_FILE).exists(), "rest 一个字节都不该碰心情")

    def test_settle_by_axis_name(self):
        seed_offset(self.root, "想黏人", 1.6)
        pending.derive(root=self.root, now=T0)
        result = pending.settle("想黏人", root=self.root, now=T0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], pending.KIND_AXIS)
        self.assertEqual(result["axis"], "想黏人")
        self.assertEqual(result["state"], pending.STATE_SETTLED)
        self.assertFalse((Path(self.root) / mood.MOOD_FILE).exists(), "轴念头不投心情事件")

    def test_settle_without_open_thought_is_skipped(self):
        result = pending.settle("praise", root=self.root, now=T0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["skipped"], "no-open")

    def test_settle_never_writes_the_mood_file_itself(self):
        ignore(self.root, 3)
        pending.derive(root=self.root, now=T0)
        calls = []
        original = store.write_json

        def spy(path: Any, data: Any, *args: Any, **kwargs: Any) -> None:
            calls.append((Path(path).name, sys._getframe(1).f_globals.get("__name__")))
            return original(path, data, *args, **kwargs)

        with mock.patch.object(store, "write_json", spy):
            pending.settle("praise", root=self.root, now=T0)

        mood_writers = sorted({who for name, who in calls if name == mood.MOOD_FILE})
        self.assertEqual(mood_writers, ["organs.heart.mood"], "只许 mood 自己写心情")
        pending_writers = {who for name, who in calls if name == pending.PENDING_FILE}
        self.assertIn("organs.heart.pending", pending_writers)

        source = inspect.getsource(pending)
        for needle in ("mood.json", "MOOD_FILE"):
            self.assertNotIn(needle, source, "pending 源码里不许出现心情文件的字样")

    def test_settle_tells_mood_but_keeps_the_thought_record(self):
        ignore(self.root, 3)
        pending.derive(root=self.root, now=T0)
        thought_id = pending.open_thoughts(root=self.root, now=T0)[0]["id"]
        pending.settle("praise", root=self.root, now=T0)
        stored = helpers.read_json_file(self.file)["thoughts"][0]
        self.assertEqual(stored["id"], thought_id)
        self.assertEqual(stored["state"], pending.STATE_SETTLED)
        self.assertEqual(stored["settled_at"], T0)
        self.assertIsNone(stored["faded_at"])

    # ---- 3) fade：TTL 到 / attempts 超限，回吐幅度读 config ----------------

    def test_ttl_expiry_fades_and_releases_axis(self):
        seed_offset(self.root, "想黏人", 1.6)
        pending.derive(root=self.root, now=T0)
        self.assertEqual(len(pending.open_thoughts(root=self.root, now=T0)), 1)
        before = axis_value(self.root, "想黏人", T0)
        self.assertAlmostEqual(before, 1.6, places=6)

        rows = pending.open_thoughts(root=self.root, now=at(181))
        self.assertEqual(rows, [])
        row = pending.overview(root=self.root, now=at(181))["thoughts"][0]
        self.assertEqual(row["state"], pending.STATE_FADED)
        self.assertEqual(row["faded_at"], at(181))
        self.assertAlmostEqual(axis_value(self.root, "想黏人", at(181)), before - 0.5, places=6)

    def test_ttl_expiry_without_axis_does_not_touch_offset(self):
        ignore(self.root, 3)
        pending.derive(root=self.root, now=T0)
        pending.open_thoughts(root=self.root, now=at(181))
        self.assertFalse((Path(self.root) / offset.OFFSET_FILE).exists(), "没轴就不许碰 offset")

    def test_negative_axis_release_climbs_back_toward_zero(self):
        seed_offset(self.root, "记性", -1.6)
        pending.derive(root=self.root, now=T0)
        pending.open_thoughts(root=self.root, now=at(181))
        self.assertAlmostEqual(axis_value(self.root, "记性", at(181)), -1.1, places=6)

    def test_attempt_counts_up_to_the_limit(self):
        seed_offset(self.root, "服气", 1.6)
        pending.derive(root=self.root, now=T0)
        thought_id = pending.open_thoughts(root=self.root, now=T0)[0]["id"]
        for expected in (1, 2, 3):
            result = pending.attempt(thought_id, root=self.root, now=T0)
            self.assertTrue(result["ok"])
            self.assertFalse(result["faded"], "到 max_attempts 这一下还不 fade")
            self.assertEqual(result["state"], pending.STATE_OPEN)
            self.assertEqual(result["attempts"], expected)

    def test_attempt_over_the_limit_fades_and_releases(self):
        seed_offset(self.root, "服气", 1.6)
        pending.derive(root=self.root, now=T0)
        thought_id = pending.open_thoughts(root=self.root, now=T0)[0]["id"]
        for _ in range(pending.CONFIG_DEFAULTS["max_attempts"]):
            pending.attempt(thought_id, root=self.root, now=T0)
        result = pending.attempt(thought_id, root=self.root, now=T0)
        self.assertTrue(result["faded"])
        self.assertEqual(result["attempts"], 4)
        self.assertEqual(result["state"], pending.STATE_FADED)
        self.assertIsNotNone(result["release"])
        self.assertAlmostEqual(axis_value(self.root, "服气", T0), 1.1, places=6)
        self.assertEqual(pending.open_thoughts(root=self.root, now=T0), [])

    def test_attempt_unknown_id_is_skipped(self):
        result = pending.attempt("p-zzzz", root=self.root, now=T0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["skipped"], "no-open")

    def test_max_attempts_read_from_config_file(self):
        seed_offset(self.root, "心疼", 1.6)
        pending.derive(root=self.root, now=T0)
        thought_id = pending.open_thoughts(root=self.root, now=T0)[0]["id"]
        set_config(self.file, max_attempts=1)
        self.assertFalse(pending.attempt(thought_id, root=self.root, now=T0)["faded"])
        self.assertTrue(pending.attempt(thought_id, root=self.root, now=T0)["faded"])

    # ---- 4) 最多 max_open 个：先到先得 -----------------------------------

    def test_max_open_keeps_the_earliest_three(self):
        ignore(self.root, 3)
        seed_energy(self.root, 20.0)
        seed_offset(self.root, "想黏人", 1.6)
        first = pending.derive(root=self.root, now=T0)
        self.assertEqual(len(first["open"]), 3)
        first_ids = [row["id"] for row in first["open"]]

        seed_offsets(self.root, {"想黏人": 1.6, "怕失去": 1.9})
        second = pending.derive(root=self.root, now=at(60))
        self.assertEqual(second["created"], [])
        self.assertEqual([(row["kind"], row["axis"]) for row in second["dropped"]],
                         [(pending.KIND_AXIS, "怕失去")])
        self.assertEqual(second["dropped"][0]["reason"], "max-open")
        self.assertEqual([row["id"] for row in second["open"]], first_ids)
        kinds = sorted(row["kind"] for row in second["open"])
        self.assertEqual(kinds, sorted([pending.KIND_PRAISE, pending.KIND_REST, pending.KIND_AXIS]))

    # ---- 5) config 落盘、可改、改了就生效 --------------------------------

    def test_config_lands_on_disk_with_defaults(self):
        ignore(self.root, 3)
        pending.derive(root=self.root, now=T0)
        data = helpers.read_json_file(self.file)
        self.assertEqual(data["version"], pending.VERSION)
        self.assertEqual(data["config"], pending.CONFIG_DEFAULTS)

    def test_release_zero_from_config_disables_the_release(self):
        seed_offset(self.root, "想黏人", 1.6)
        pending.derive(root=self.root, now=T0)
        set_config(self.file, release=0)
        pending.open_thoughts(root=self.root, now=at(181))
        self.assertAlmostEqual(axis_value(self.root, "想黏人", at(181)), 1.6, places=6)
        self.assertEqual(pending.overview(root=self.root, now=at(181))["thoughts"][0]["state"],
                         pending.STATE_FADED)

    def test_release_amount_read_from_config_file(self):
        seed_offset(self.root, "想黏人", 1.6)
        pending.derive(root=self.root, now=T0)
        set_config(self.file, release=0.25)
        pending.open_thoughts(root=self.root, now=at(181))
        self.assertAlmostEqual(axis_value(self.root, "想黏人", at(181)), 1.35, places=6)

    def test_ttl_from_config_file_applies_to_new_thoughts(self):
        ignore(self.root, 3)
        pending.derive(root=self.root, now=T0)
        set_config(self.file, ttl_min=10)
        seed_energy(self.root, 20.0)
        pending.derive(root=self.root, now=T0)
        rows = pending.open_thoughts(root=self.root, now=T0)
        rest = [row for row in rows if row["kind"] == pending.KIND_REST][0]
        self.assertEqual(rest["expires_at"], at(10))
        praise = [row for row in rows if row["kind"] == pending.KIND_PRAISE][0]
        self.assertEqual(praise["expires_at"], at(180), "旧念头的 expires_at 不被改写")

    # ---- 6) 读时现算、面板、坏数据 ---------------------------------------

    def test_ttl_left_shrinks_as_now_moves(self):
        ignore(self.root, 3)
        pending.derive(root=self.root, now=T0)
        self.assertAlmostEqual(
            pending.open_thoughts(root=self.root, now=at(60))[0]["ttl_left_min"], 120.0
        )

    def test_overview_reports_config_ttl_attempts_state(self):
        ignore(self.root, 3)
        pending.derive(root=self.root, now=T0)
        snap = pending.overview(root=self.root, now=T0)
        self.assertEqual(snap["config"]["release"], 0.5)
        self.assertEqual(snap["open"], 1)
        row = snap["thoughts"][0]
        self.assertEqual(row["state"], pending.STATE_OPEN)
        self.assertEqual(row["attempts"], 0)
        self.assertAlmostEqual(row["ttl_left_min"], 180.0)
        self.assertFalse(row["expired"])

    def test_overview_is_a_pure_read(self):
        seed_offset(self.root, "想黏人", 1.6)
        pending.derive(root=self.root, now=T0)
        before = sha256_of(self.file)
        snap = pending.overview(root=self.root, now=at(181))
        self.assertEqual(sha256_of(self.file), before, "面板读数不许落笔")
        self.assertEqual(snap["thoughts"][0]["state"], pending.STATE_OPEN)
        self.assertTrue(snap["thoughts"][0]["expired"])
        self.assertAlmostEqual(snap["thoughts"][0]["ttl_left_min"], 0.0)
        self.assertEqual(snap["open"], 0)

    def test_open_thoughts_fades_expired_rows_only(self):
        seed_energy(self.root, 20.0)
        pending.derive(root=self.root, now=T0)
        rows = pending.open_thoughts(root=self.root, now=at(30))
        self.assertEqual(len(rows), 1)
        self.assertEqual(pending.open_thoughts(root=self.root, now=at(181)), [])
        self.assertEqual(pending.overview(root=self.root, now=at(181))["thoughts"][0]["state"],
                         pending.STATE_FADED)

    def test_bad_json_falls_back_to_empty_state(self):
        helpers.write_raw(self.file, "{not json")
        ignore(self.root, 3)
        self.assertEqual(len(pending.derive(root=self.root, now=T0)["created"]), 1)

    def test_bad_expires_at_is_not_treated_as_expired(self):
        helpers.write_raw(
            self.file,
            '{"version": 1, "config": {}, "thoughts": [{"id": "p-bad1", '
            '"kind": "wanted_praise", "axis": null, "created_at": "' + T0 + '", '
            '"expires_at": "???", "attempts": 0, "state": "open", '
            '"settled_at": null, "faded_at": null, "source": "ignored-window"}]}',
        )
        rows = pending.open_thoughts(root=self.root, now=at(9999))
        self.assertEqual(len(rows), 1, "坏 expires_at 不猜、不当过期")
        self.assertIsNone(rows[0]["ttl_left_min"])

    def test_unknown_kind_row_is_dropped_on_load(self):
        helpers.write_raw(
            self.file,
            '{"version": 1, "config": {}, "thoughts": [{"id": "p-x1", '
            '"kind": "wanted_moon", "state": "open", "attempts": 0}]}',
        )
        self.assertEqual(pending.overview(root=self.root, now=T0)["thoughts"], [])

    # ---- 7) 边界：不进证据链、不挂定时器 --------------------------------

    def test_source_has_no_brain_import_and_no_timers(self):
        source = inspect.getsource(pending)
        for needle in ("organs.brain", "import brain", "import threading",
                       "threading.", "Timer(", "sched.", "schedule."):
            self.assertNotIn(needle, source)

    def test_public_surface_is_only_the_frozen_five(self):
        public = {
            name
            for name, value in vars(pending).items()
            if inspect.isfunction(value) and not name.startswith("_")
        }
        self.assertEqual(
            public, {"derive", "open_thoughts", "settle", "attempt", "overview"},
            "念头层不许提供任何「写进记忆 / 证据」的入口",
        )


def set_config(path: Any, **values: Any) -> None:
    """人手改文件：直接改 pending.json 的 config，不改代码、不重启。"""
    data = helpers.read_json_file(path)
    data["config"].update(values)
    store.write_json(path, data)
