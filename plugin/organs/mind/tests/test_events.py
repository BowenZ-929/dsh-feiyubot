"""核·识别的验收：五类正反例、cold 窗口、数据驱动、is_insult 同口径、账本三支。

测试一律给临时根（root=tempdir）——账本绝不落到实盘 home/organs/mind。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.mind import events

# 五类的正例：一句话 + 这一场 + （rejected 要的上/下一轮）
POSITIVE = [
    ("praise", dict(text="你真的很厉害，这次多亏你", scene="private"), "praise"),
    ("care", dict(text="记得吃饭，别累着", scene="private"), "care"),
    ("care-group", dict(text="记得吃饭，别累着", scene="group"), "care"),
    ("cold", dict(text="在吗", scene="private", recent_replies=["嗯", "哦", "好"]), "cold"),
    ("insult", dict(text="你这个废物，给我滚开", scene="group"), "insult"),
    ("rejected", dict(text="不用了，我自己来", scene="private",
                      last_reply="要不要我帮你说一句？"), "rejected"),
]

# 五类的反例：必须有话说、说的不是这一类，结果只能是 None（不许瞎猜）
NEGATIVE = [
    ("praise", dict(text="今天下午三点开会", scene="private")),
    ("care-group", dict(text="第三季度报表我改完了", scene="group")),
    ("care-private", dict(text="第三季度报表我改完了", scene="private")),
    ("cold-single", dict(text="在吗", scene="private", recent_replies=["嗯"])),
    ("cold-long", dict(text="在吗", scene="private",
                       recent_replies=["嗯", "今天真不想说话，你别问了", "哦"])),
    ("insult", dict(text="这件事我再想想吧", scene="private")),
    ("rejected-no-offer", dict(text="不用了", scene="private", last_reply="今天下雨了")),
    ("rejected-no-word", dict(text="我觉得挺好的", scene="private",
                              last_reply="要不要我帮你说一句？")),
]


class RecognizeTest(unittest.TestCase):
    """识别本身：正例各有各的 kind，反例一律 None。"""

    def test_five_kinds_have_a_positive_case(self):
        seen = set()
        for label, kwargs, want in POSITIVE:
            with self.subTest(kind=label):
                got = events.recognize(**kwargs)
                self.assertEqual(got["kind"], want, got)
                self.assertTrue(got["note"], "命中了却没留下依据：%r" % (got,))
                self.assertTrue(got["evidence"], "命中了却没留下原话片段：%r" % (got,))
                self.assertEqual(got["who"], "主人")
                seen.add(want)
        self.assertEqual(seen, {"praise", "care", "cold", "insult", "rejected"})

    def test_negatives_stay_none(self):
        for label, kwargs in NEGATIVE:
            with self.subTest(case=label):
                got = events.recognize(**kwargs)
                self.assertIsNone(got["kind"], "反例被认成了 %r：%r" % (got["kind"], got))
                self.assertEqual(got["note"], "")
                self.assertEqual(got["evidence"], "")

    def test_empty_or_punctuation_only_is_vetoed(self):
        for text in ("", "   ", "？？？", "……", "，，。"):
            for scene in ("private", "group"):
                with self.subTest(text=text, scene=scene):
                    self.assertIsNone(events.recognize(text, scene=scene)["kind"])

    def test_unknown_scene_recognizes_nothing(self):
        """场合说不清就一个都不认：unknown 不在任何 kind 的场景开关里。"""
        for label, kwargs, _want in POSITIVE:
            with self.subTest(kind=label):
                kwargs = dict(kwargs)
                kwargs["scene"] = "unknown"
                self.assertIsNone(events.recognize(**kwargs)["kind"])

    def test_cold_needs_the_whole_window(self):
        """连续 3 轮「嗯」→ cold；其中一轮是长句 → 不 cold（窗口是判据，不是单词）。"""
        terse = events.recognize("在吗", scene="private", recent_replies=["嗯", "嗯", "哦"])
        self.assertEqual(terse["kind"], "cold", terse)
        self.assertEqual(terse["evidence"], "嗯/嗯/哦")

        mixed = events.recognize(
            "在吗", scene="private",
            recent_replies=["嗯", "今天我在想那件事，还想再说两句", "哦"])
        self.assertIsNone(mixed["kind"], mixed)

        short = events.recognize("在吗", scene="private", recent_replies=["嗯", "嗯"])
        self.assertIsNone(short["kind"], short)

    def test_rejected_needs_a_question_relation(self):
        """她上一轮在问她才叫"被拒绝"；上一轮不是在问，说不清就 None。"""
        asked = events.recognize("算了，不用了", scene="private",
                                 last_reply="要不要我帮你说一句？")
        self.assertEqual(asked["kind"], "rejected", asked)
        unasked = events.recognize("算了，不用了", scene="private", last_reply="今天下雨了")
        self.assertIsNone(unasked["kind"], unasked)
        nowhere = events.recognize("算了，不用了", scene="private", last_reply="")
        self.assertIsNone(nowhere["kind"], nowhere)


class DataDrivenTest(unittest.TestCase):
    """词表 / 阈值 / 场景开关全在数据里：改数据文件，识别结果跟着变。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mind-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _copy(self, patch):
        cfg = json.loads(Path(events.data_path_factory()).read_text(encoding="utf-8"))
        patch(cfg)
        path = self.tmp / "events.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return path

    def test_word_lists_come_from_the_file(self):
        path = self._copy(lambda cfg: cfg["kinds"]["praise"]["words"].append("芝麻开门"))
        text = "芝麻开门"
        self.assertIsNone(events.recognize(text, scene="private")["kind"])
        self.assertEqual(events.recognize(text, scene="private", data_path=path)["kind"],
                         "praise")

    def test_removing_a_word_removes_the_hit(self):
        path = self._copy(lambda cfg: cfg["kinds"]["praise"].update({"words": []}))
        text = "你真的很厉害"
        self.assertEqual(events.recognize(text, scene="private")["kind"], "praise")
        self.assertIsNone(events.recognize(text, scene="private", data_path=path)["kind"])

    def test_thresholds_come_from_the_file(self):
        path = self._copy(lambda cfg: cfg["kinds"]["cold"].update({"window": 4}))
        replies = ["嗯", "嗯", "哦"]
        self.assertEqual(
            events.recognize("在吗", scene="private", recent_replies=replies)["kind"], "cold")
        self.assertIsNone(
            events.recognize("在吗", scene="private", recent_replies=replies,
                             data_path=path)["kind"])

    def test_scene_switches_come_from_the_file(self):
        path = self._copy(lambda cfg: cfg["scenes"]["care"].remove("group"))
        text = "记得吃饭"
        self.assertEqual(events.recognize(text, scene="group")["kind"], "care")
        self.assertIsNone(events.recognize(text, scene="group", data_path=path)["kind"])

    def test_broken_rules_are_named_not_swallowed(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"priority": ["praise"]}), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            events.recognize("你好", scene="private", data_path=bad)
        self.assertIn("缺", str(caught.exception))


class IsInsultTest(unittest.TestCase):
    """is_insult 与 recognize 的 insult 一路同口径（全仓唯一「被骂」判据）。"""

    TEXTS = ["你这个废物", "给我滚开", "你有病吧", "今天下午三点开会", "？？？", "", "谢谢你"]

    def test_same_verdict_on_both_paths(self):
        for text in self.TEXTS:
            for scene in ("private", "group"):
                with self.subTest(text=text, scene=scene):
                    by_function = events.is_insult(text)
                    by_recognize = events.recognize(text, scene=scene)["kind"] == "insult"
                    self.assertEqual(by_function, by_recognize)

    def test_vetoed_text_is_not_insult(self):
        self.assertFalse(events.is_insult(""))
        self.assertFalse(events.is_insult("。。"))
        self.assertTrue(events.is_insult("闭嘴"))


class LedgerTest(unittest.TestCase):
    """账本：每轮一行，applied / reason 一律照实说。"""

    AT = "2024-05-01T09:00:00+08:00"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mind-events-log-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "mind"

    def _lines(self):
        path = events.log_path(self.root)
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_applied_row(self):
        calls = []

        def report(kind, who, at=None, note=""):
            calls.append((kind, who, at, note))
            return {"applied": True, "reason": None}

        row = events.record("你真的很厉害", scene="private", at=self.AT,
                            root=self.root, report=report)
        self.assertEqual(row, {
            "at": self.AT, "kind": "praise", "who": "主人", "scene": "private",
            "mode": "passive", "applied": True, "reason": "",
            "note": "夸：词表命中 厉害",
        })
        self.assertEqual(calls, [("praise", "主人", self.AT, "夸：词表命中 厉害")])
        self.assertEqual(self._lines(), [row])

    def test_no_kind_still_writes_a_row(self):
        row = events.record("今天下午三点开会", scene="private", at=self.AT, root=self.root)
        self.assertEqual(row["kind"], "")
        self.assertFalse(row["applied"])
        self.assertEqual(row["reason"], "没识别出事件")
        self.assertEqual(self._lines(), [row])

    def test_missing_entry_is_not_faked(self):
        with mock.patch.object(events, "_default_report", side_effect=AttributeError("没有")):
            row = events.record("你真的很厉害", scene="private", at=self.AT, root=self.root)
        self.assertFalse(row["applied"])
        self.assertEqual(row["reason"], "heart.report_event 未落地")
        self.assertEqual(self._lines(), [row])

    def test_heart_saying_not_applied_is_written_as_is(self):
        row = events.record(
            "你真的很厉害", scene="private", at=self.AT, root=self.root,
            report=lambda kind, who, at=None, note="": {"applied": False, "reason": "cooldown"})
        self.assertFalse(row["applied"])
        self.assertEqual(row["reason"], "cooldown")
        self.assertEqual(self._lines(), [row])

    def test_entry_without_applied_field_is_not_a_success(self):
        row = events.record(
            "你真的很厉害", scene="private", at=self.AT, root=self.root,
            report=lambda kind, who, at=None, note="": {"ok": True})
        self.assertFalse(row["applied"])
        self.assertEqual(row["reason"], events.REASON_NO_APPLIED)

    def test_no_delivery_at_all_when_nothing_recognized(self):
        calls = []

        def report(kind, who, at=None, note=""):
            calls.append(kind)
            return {"applied": True}

        row = events.record("今天下午三点开会", scene="private", at=self.AT,
                            root=self.root, report=report)
        self.assertEqual(calls, [], "没识别出事件却投递了")
        self.assertFalse(row["applied"])

    def test_mode_defaults_to_passive(self):
        row = events.record("你真的很厉害", scene="private", at=self.AT, root=self.root,
                            report=lambda kind, who, at=None, note="": {"applied": True})
        self.assertEqual(row["mode"], "passive")
        other = events.record("你真的很厉害", scene="private", at=self.AT, root=self.root,
                              mode="proactive",
                              report=lambda kind, who, at=None, note="": {"applied": True})
        self.assertEqual(other["mode"], "proactive")
        wrong = events.record("你真的很厉害", scene="private", at=self.AT, root=self.root,
                              mode="随便", report=lambda kind, who, at=None, note="": {"applied": True})
        self.assertEqual(wrong["mode"], "passive")

    def test_ledger_only_appends_and_skips_bad_lines(self):
        events.record("你真的很厉害", scene="private", at=self.AT, root=self.root,
                      report=lambda kind, who, at=None, note="": {"applied": True})
        events.record("今天下午三点开会", scene="private", at=self.AT, root=self.root)
        path = events.log_path(self.root)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("这不是 JSON\n")
            handle.write('["数组不算对象"]\n')
        kept = events.rows(self.root)
        self.assertEqual([row["kind"] for row in kept], ["praise", ""])
        self.assertEqual(len(kept), 2)

    def test_empty_ledger_reads_empty(self):
        self.assertEqual(events.rows(self.root), [])


if __name__ == "__main__":
    unittest.main()
