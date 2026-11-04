"""round2 fixture：回归基准的形状、占位 id、覆盖项。"""
from __future__ import annotations

import re
import unittest

from organs.brain.bench.fixture import build_store, load_fixture


class FixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_fixture()

    def test_shape_and_people(self):
        self.assertEqual("brain-round2-fixture-v1", self.fixture["schema"])
        messages = self.fixture["messages"]
        self.assertGreaterEqual(len(messages), 20)
        self.assertLessEqual(len(messages), 30)
        people = self.fixture["people"]
        self.assertGreaterEqual(len(people), 3)
        roles = [p["role"] for p in people]
        self.assertEqual(1, roles.count("主人"))
        self.assertGreaterEqual(roles.count("熟人"), 1)
        self.assertGreaterEqual(roles.count("陌生"), 2)

    def test_ids_are_placeholders_no_real_qq(self):
        pattern = re.compile(r"^1000\d{3}$")
        for person in self.fixture["people"]:
            self.assertRegex(person["id"], pattern)
        for message in self.fixture["messages"]:
            self.assertRegex(message["id"], pattern)
            self.assertRegex(message["who"], pattern)

    def test_required_coverage(self):
        notes = " ".join(m.get("note", "") for m in self.fixture["messages"])
        for required in ("被夸", "被冷落", "带图", "问过去", "日程模拟", "已退役", "旧事重复"):
            self.assertIn(required, notes, "fixture 必须覆盖：%s" % required)
        scheduled = [m for m in self.fixture["messages"]
                     if m["id"] in self.fixture["scheduled_ids"]]
        self.assertEqual(1, len(scheduled))
        self.assertEqual("日程模拟", scheduled[0]["source_type"])
        retired = [m for m in self.fixture["messages"]
                   if m["id"] in self.fixture["retired_ids"]]
        self.assertEqual(1, len(retired))

    def test_questions_20_plus_6_with_refuse_questions(self):
        questions = self.fixture["questions"]
        self.assertEqual(26, len(questions))
        refuse = [q for q in questions if q.get("kind") == "refuse"]
        self.assertGreaterEqual(len(refuse), 4, "至少 4 题是「该拒答」的")
        for q in questions:
            self.assertIn(q.get("kind"), ("answer", "refuse"))
            self.assertTrue(q.get("answer"), "每道题都要有答案子串")

    def test_store_build_retires_and_keeps_rows(self):
        store, tmp = build_store(self.fixture)
        self.addCleanup(tmp.cleanup)
        # 退役行还在盘上（tombstone 在 longterm 里），但回想翻不到
        result = store.recall("我买了个新耳机", now_ts="2026-11-03T12:00:00+00:00")
        self.assertFalse(result["has_material"])


if __name__ == "__main__":
    unittest.main()
