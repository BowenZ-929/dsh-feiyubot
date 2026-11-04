"""事件账本的验收：只追加、按 now 现算窗口、坏行跳过、坏 kind 抛。

这本账是"她最近遇到了什么"的唯一投递管：嘴/脑投递，心只读。
所有用例都在 helpers.make_root 的临时目录里跑，绝不写真 data。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List

from organs.heart import events, model
from organs.heart.tests import helpers

BASE_HOUR = 10


def _local(hour: int = BASE_HOUR) -> datetime:
    """本机时区的一个固定时刻：2025-01-15 <hour>:00。"""
    tz = datetime.now().astimezone().tzinfo
    return datetime(2025, 1, 15, hour, 0, 0, tzinfo=tz)


def _at_min(minutes: float = 0.0, base: Any = None) -> str:
    return model.iso((base or _local()) + timedelta(minutes=minutes))


class EventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)

    def _raw(self) -> str:
        path = Path(self.root) / events.EVENTS_FILE
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _lines(self) -> List[str]:
        return [line for line in self._raw().splitlines() if line.strip()]

    # ---- 空账 ------------------------------------------------------------

    def test_missing_file_reads_as_empty(self) -> None:
        """文件不存在 → 空账；读不许顺手把文件造出来。"""
        self.assertEqual([], events.recent(self.root, now=_at_min()))
        self.assertEqual(0, events.same_kind_since("normal", self.root, now=_at_min()))
        self.assertFalse((Path(self.root) / events.EVENTS_FILE).exists())

    def test_empty_file_reads_as_empty(self) -> None:
        """空文件（0 字节）也是空账，不是坏账。"""
        helpers.write_raw(Path(self.root) / events.EVENTS_FILE, "")
        self.assertEqual([], events.recent(self.root, now=_at_min()))

    # ---- deliver ---------------------------------------------------------

    def test_deliver_appends_one_row(self) -> None:
        """嘴/脑投递的行：at / kind / who 打头，note 有才写。"""
        at = _at_min()
        row = events.deliver(events.KIND_IGNORED, "嘴", root=self.root, now=at, note="说了没人回")
        self.assertEqual(at, row["at"])
        self.assertEqual("ignored", row["kind"])
        self.assertEqual("嘴", row["who"])
        self.assertEqual("说了没人回", row["note"])
        rows = events.recent(self.root, now=at)
        self.assertEqual([row], rows)

    def test_deliver_defaults_and_no_empty_note(self) -> None:
        """who 缺省是主人，空 who 回主人；note 空就不写这一格。"""
        row = events.deliver(events.KIND_REPLIED, "", root=self.root, now=_at_min())
        self.assertEqual("主人", row["who"])
        self.assertNotIn("note", row)

    def test_deliver_rejects_unknown_kind(self) -> None:
        """坏 kind 抛 ValueError，坏值原样带出，也不许写成 normal。"""
        with self.assertRaises(ValueError) as caught:
            events.deliver("ignored-silently", root=self.root, now=_at_min())
        self.assertIn("ignored-silently", str(caught.exception))
        self.assertEqual("", self._raw(), "坏 kind 还是落账了")

    def test_all_deliver_kinds_are_accepted(self) -> None:
        """四个投递口都认，且都在账本里。"""
        at = _at_min()
        for kind in events.DELIVER_KINDS:
            events.deliver(kind, root=self.root, now=at)
        self.assertEqual(list(events.DELIVER_KINDS), [row["kind"] for row in events.recent(self.root, now=at)])

    # ---- append ----------------------------------------------------------

    def test_append_takes_mood_kinds_and_extra(self) -> None:
        """内部口子：mood 的四类事件也落同一本账，delta / after 这类附加格原样留。"""
        at = _at_min()
        events.append("praise", "主人", root=self.root, now=at, delta=4.0, after=64.0)
        rows = events.recent(self.root, now=at, kinds=("praise",))
        self.assertEqual(1, len(rows))
        self.assertEqual(4.0, rows[0]["delta"])
        self.assertEqual(64.0, rows[0]["after"])

    def test_append_rejects_empty_kind(self) -> None:
        """空事件名的行读不出意义，直接抛。"""
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                events.append(bad, root=self.root, now=_at_min())  # type: ignore[arg-type]
        self.assertEqual("", self._raw())

    # ---- 只追加 ----------------------------------------------------------

    def test_ledger_is_append_only(self) -> None:
        """只追加、不改写：写第二行时第一行一个字节都不动。"""
        base = _local()
        events.deliver(events.KIND_NORMAL, root=self.root, now=model.iso(base))
        first = self._lines()[0]
        events.deliver(events.KIND_HARSH, root=self.root, now=_at_min(1, base))
        lines = self._lines()
        self.assertEqual(2, len(lines))
        self.assertEqual(first, lines[0])
        self.assertEqual("harsh", json.loads(lines[1])["kind"])

    def test_bad_lines_are_skipped(self) -> None:
        """坏行跳过：半截 JSON 与不是对象的行都不算账。"""
        at = _at_min()
        good = json.dumps({"at": at, "kind": "normal", "who": "主人"}, ensure_ascii=False)
        helpers.write_raw(
            Path(self.root) / events.EVENTS_FILE,
            "{半截 JSON\n[1, 2, 3]\n" + good + "\n\n",
        )
        rows = events.recent(self.root, now=at)
        self.assertEqual(1, len(rows))
        self.assertEqual("normal", rows[0]["kind"])

    def test_rows_with_bad_timestamp_are_skipped(self) -> None:
        """没有 at / at 解析不了的行走不进窗口，也不算次数。"""
        at = _at_min()
        helpers.write_raw(
            Path(self.root) / events.EVENTS_FILE,
            json.dumps({"kind": "normal"}) + "\n" + json.dumps({"at": "昨天", "kind": "normal"}) + "\n",
        )
        self.assertEqual([], events.recent(self.root, now=at))
        self.assertEqual(0, events.same_kind_since("normal", self.root, now=at))

    # ---- 窗口按 now 现算 -------------------------------------------------

    def test_window_is_computed_from_now(self) -> None:
        """同一份数据，now 不同条数就不同——窗口是现算的，不是落盘时定的。"""
        base = _local()
        events.deliver(events.KIND_NORMAL, root=self.root, now=model.iso(base))
        events.deliver(events.KIND_REPLIED, root=self.root, now=_at_min(60, base))
        events.deliver(events.KIND_HARSH, root=self.root, now=_at_min(120, base))

        at_t = _at_min(150, base)
        self.assertEqual(2, len(events.recent(self.root, now=at_t, window_min=120.0)))
        self.assertEqual(
            ["replied", "harsh"],
            [row["kind"] for row in events.recent(self.root, now=at_t, window_min=120.0)],
        )
        # T + 2h：整段老账都出了窗口
        self.assertEqual(0, len(events.recent(self.root, now=_at_min(270, base), window_min=120.0)))
        # 窗口拉长又能看见
        self.assertEqual(3, len(events.recent(self.root, now=at_t, window_min=180.0)))

    def test_window_is_half_open_at_the_left_edge(self) -> None:
        """左边界算在窗口里（(now − window, now]），右边界之后的行不算。"""
        base = _local()
        events.deliver(events.KIND_NORMAL, root=self.root, now=model.iso(base))
        events.deliver(events.KIND_NORMAL, root=self.root, now=_at_min(120, base))
        self.assertEqual(
            2, len(events.recent(self.root, now=_at_min(120, base), window_min=120.0)),
            "正好落在左边界的行被丢了",
        )
        self.assertEqual(
            1, len(events.recent(self.root, now=_at_min(60, base), window_min=120.0)),
            "还没发生的行被算进来了",
        )

    def test_same_kind_since_counts_only_that_kind(self) -> None:
        """同类型、同窗口才算：别的 kind 与出了窗口的同 kind 都不算。"""
        base = _local()
        events.deliver(events.KIND_NORMAL, root=self.root, now=model.iso(base))
        events.deliver(events.KIND_NORMAL, root=self.root, now=_at_min(1, base))
        events.deliver(events.KIND_REPLIED, root=self.root, now=_at_min(2, base))
        events.deliver(events.KIND_NORMAL, root=self.root, now=_at_min(11, base))

        self.assertEqual(2, events.same_kind_since("normal", self.root, now=_at_min(3, base)))
        self.assertEqual(1, events.same_kind_since("normal", self.root, now=_at_min(12, base)))
        self.assertEqual(0, events.same_kind_since("harsh", self.root, now=_at_min(12, base)))

    def test_kinds_filter_and_limit_take_the_tail(self) -> None:
        """kinds 过滤；limit 取最后几条（面板只看最近）。"""
        base = _local()
        for index in range(5):
            kind = events.KIND_NORMAL if index % 2 == 0 else events.KIND_REPLIED
            events.deliver(kind, root=self.root, now=_at_min(index, base))
        rows = events.recent(self.root, now=_at_min(5, base), kinds=(events.KIND_REPLIED,))
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["kind"] == "replied" for row in rows))
        tail = events.recent(self.root, now=_at_min(5, base), limit=2)
        self.assertEqual([_at_min(3, base), _at_min(4, base)], [row["at"] for row in tail])
        self.assertEqual([], events.recent(self.root, now=_at_min(5, base), limit=0))

    def test_rows_come_back_in_time_order(self) -> None:
        """返回按时刻升序，哪怕写入顺序是反的。"""
        base = _local()
        events.deliver(events.KIND_HARSH, root=self.root, now=_at_min(10, base))
        events.deliver(events.KIND_NORMAL, root=self.root, now=model.iso(base))
        rows = events.recent(self.root, now=_at_min(20, base))
        self.assertEqual(["normal", "harsh"], [row["kind"] for row in rows])

    # ---- 模块边界 --------------------------------------------------------

    def test_no_decay_constants_live_here(self) -> None:
        """本模块不持有任何时间衰减常数：窗口由调用方传进来。"""
        names = [name for name in vars(events) if name.isupper()]
        for name in names:
            self.assertNotIn("HALF_LIFE", name, "账本里不该有半衰")
            self.assertNotIn("DECAY", name, "账本里不该有衰减")
            self.assertFalse(
                name.endswith(("_MIN", "_H", "_HOURS")), "%s 是窗口尺，该由调用方传" % name
            )
        self.assertFalse(any(isinstance(getattr(events, name), float) for name in names))


if __name__ == "__main__":
    unittest.main()
