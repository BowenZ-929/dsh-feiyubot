"""check 的判据：坏卡拒载、话术拒收、占位符示警、账对不上报错、摘要按七件报字数。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from organs.soul import checks, model
from organs.soul.store import load
from organs.soul.tests.helpers import copy_fixture, edit_card

DATA = Path(__file__).resolve().parents[1] / "data"


def codes(issues) -> list:
    return [issue.code for issue in issues]


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)

    def test_clean_fixture_reports_no_issues(self):
        self.assertEqual([], codes(checks.collect(self.root, cross_check=False)))

    def test_check_returns_issue_objects(self):
        issues = checks.collect(self.root)
        for issue in issues:
            self.assertIsInstance(issue, model.Issue)
            self.assertIn(issue.level, (model.ERROR, model.WARN))

    def _manner_rejected(self, note: str) -> list:
        edit_card(self.root, "manner", lambda data: data.update({"stance": note}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        return [issue.code for issue in caught.exception.issues]

    def test_manner_script_with_punctuation_rejected(self):
        self.assertIn("MANNER_HAS_SCRIPT", self._manner_rejected("熟人来了就说「你今天回来啦」"))

    def test_manner_long_quote_rejected(self):
        self.assertIn("MANNER_HAS_SCRIPT", self._manner_rejected('对他说"这一整句话太长了不像倾向"'))

    def test_manner_short_quotes_are_allowed(self):
        edit_card(self.root, "manner", lambda data: data.update({"stance": '别把"不熟"演成"不理人"'}))
        self.assertNotIn("MANNER_HAS_SCRIPT", codes(load(self.root).snapshot().issues))

    def test_manner_imperative_phrase_rejected(self):
        self.assertIn("MANNER_HAS_SCRIPT", self._manner_rejected("被问到时就说自己的想法"))

    def test_external_red_line_is_warned_not_enforced(self):
        edit_card(self.root, "bounds", lambda data: data["items"].append({
            "id": "b03", "rule": "不刷脏话", "match": {"type": "external", "source": "家·仓库/脏话词表"},
            "action": "block", "active": True}))
        issues = checks.collect(self.root, cross_check=False)
        self.assertIn("MATCH_UNENFORCED", codes(issues))
        self.assertNotIn("error", [issue.level for issue in issues])

    def test_external_without_source_rejects_load(self):
        edit_card(self.root, "bounds", lambda data: data["items"].append({
            "id": "b03", "rule": "不刷脏话", "match": {"type": "external"},
            "action": "block", "active": True}))
        with self.assertRaises(model.SoulLoadError):
            load(self.root)

    def test_unknown_match_type_rejects_load(self):
        edit_card(self.root, "bounds", lambda data: data["items"][0]["match"].update({"type": "vibes"}))
        with self.assertRaises(model.SoulLoadError):
            load(self.root)

    def test_placeholder_is_warned_not_rejected(self):
        edit_card(self.root, "identity", lambda data: data["self_statements"].__setitem__(
            0, "我是 {{name}}"))
        issues = checks.collect(self.root, cross_check=False)
        self.assertIn("TEXT_HAS_PLACEHOLDER", codes(issues))
        self.assertNotIn("error", [issue.level for issue in issues])

    def test_placeholder_in_bounds_pattern_is_warned(self):
        edit_card(self.root, "bounds", lambda data: data["items"][1].update(
            {"match": {"type": "literal", "pattern": "{% if %}"}}))
        self.assertIn("TEXT_HAS_PLACEHOLDER", codes(checks.collect(self.root, cross_check=False)))

    def test_missing_block_rejects_load(self):
        (self.root / "bounds.json").unlink()
        with self.assertRaises(model.SoulLoadError):
            load(self.root)

    def test_unreadable_json_rejects_load(self):
        (self.root / "manner.json").write_text("{不是 JSON", encoding="utf-8")
        with self.assertRaises(model.SoulLoadError):
            load(self.root)

    def test_manner_matrix_cannot_come_back(self):
        """对象矩阵归脑：manner 再多出 matrix 就是七件之外的东西，按坏卡拒。"""
        edit_card(self.root, "manner", lambda data: data.update({"matrix": []}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        messages = "；".join(issue.message for issue in caught.exception.issues)
        self.assertIn("manner", messages)
        self.assertIn("matrix", messages)

    def test_blank_stance_is_rejected(self):
        """姿态是唯一的一句，空了就没有待人倾向可注入。"""
        edit_card(self.root, "manner", lambda data: data.update({"stance": "  "}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        self.assertIn("manner.stance", "；".join(issue.message for issue in caught.exception.issues))

    def test_duplicate_bounds_id_rejected(self):
        edit_card(self.root, "bounds", lambda data: data["items"][1].update({"id": "b01"}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        self.assertIn("BOUNDS_ID_DUPLICATE", [issue.code for issue in caught.exception.issues])

    def test_unreadable_history_is_reported(self):
        (self.root / "history.jsonl").write_text("不是账\n", encoding="utf-8")
        self.assertIn("HISTORY_UNREADABLE", codes(checks.collect(self.root)))

    def test_summary_reports_seven_items_and_total(self):
        lines = checks.summary(self.root)
        text = "\n".join(lines)
        for word in ("七件·名字与自称", "七件·形象", "七件·初始性格", "七件·怕什么",
                     "七件·喜欢什么", "七件·待人姿态", "七件·红线条目", "七件合计"):
            self.assertIn(word, text)
        self.assertIn("七件·形象 %d/60 字（硬线）" % model.char_count(
            load(self.root).snapshot().identity.appearance), text)
        self.assertIn("七件·怕什么 %d/60 字（硬线）" % model.char_count(
            load(self.root).snapshot().identity.fears), text)
        self.assertIn("七件·喜欢什么 %d/80 字（硬线；建议 40）" % model.char_count(
            load(self.root).snapshot().identity.likes), text)
        self.assertNotIn("来历", text)
        snapshot = load(self.root).snapshot()
        expected = model.body_chars(snapshot.identity, snapshot.manner, snapshot.bounds)
        self.assertIn("七件合计 %d/%d 字" % (expected, model.limits.body_total), text)

    def test_summary_carries_no_baseline_or_axis_line(self):
        text = "\n".join(checks.summary(self.root))
        for word in ("baseline", "六轴", "轴", "待定稿"):
            self.assertNotIn(word, text)

    def test_real_data_has_no_baseline_card(self):
        """真数据里那张卡已迁心：文件不在了，check 也不再报 PENDING_AXIS。"""
        self.assertFalse((DATA / "baseline.json").exists())
        found = codes(checks.collect(DATA))
        for code in ("PENDING_AXIS", "AXIS_COUNT_NOT_SIX", "AXIS_PAIR_INCOMPLETE",
                     "AXIS_OUT_OF_RANGE"):
            self.assertNotIn(code, found)

    def test_real_ledger_records_the_manual_rows_as_warn(self):
        """两处手工落盘（六轴迁心、待人矩阵改姿态）都是 manual：check 报 warn，等人复核。"""
        manual = [issue for issue in checks.collect(DATA)
                  if issue.code == "MANUAL_EDIT_IN_LEDGER"]
        self.assertTrue(manual, "manual 行没进账或没被 check 看见")
        self.assertEqual({"warn"}, {issue.level for issue in manual})
        text = "；".join(issue.message for issue in manual)
        self.assertIn("baseline", text)
        self.assertIn("(整块)", text)

    def test_missing_enforcer_is_a_warn_not_a_block(self):
        """没写执行方的线照样生效，但没人接线会永久悬空：报 warn 让人看得见。"""
        edit_card(self.root, "bounds", lambda data: data["items"][0].pop("enforced_by"))
        issues = checks.collect(self.root, cross_check=False)
        warned = [issue for issue in issues if issue.code == "BOUNDS_ENFORCER_MISSING"]
        self.assertTrue(warned)
        self.assertEqual({"warn"}, {issue.level for issue in warned})
        self.assertIn("items[0]", warned[0].message)
        self.assertEqual([], [issue for issue in issues if issue.level == model.ERROR])
        load(self.root)                     # 不拦：卡照旧载得进来

    def test_unknown_enforcer_is_rejected(self):
        """执行方只认已登记的三类：写个没人认得的名字等于这条线没人接线。"""
        edit_card(self.root, "bounds", lambda data: data["items"][0].update({"enforced_by": "嘴"}))
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        messages = "；".join(issue.message for issue in caught.exception.issues)
        self.assertIn("mouth·语义复核", messages)

    def test_unenforced_message_names_who_is_owed_the_wiring(self):
        edit_card(self.root, "bounds", lambda data: data["items"].append({
            "id": "b03", "rule": "有人难过时不开玩笑",
            "match": {"type": "review"}, "action": "block", "active": True,
            "enforced_by": "mouth·语义复核"}))
        messages = [issue.message for issue in checks.collect(self.root, cross_check=False)
                    if issue.code == "MATCH_UNENFORCED"]
        self.assertTrue(messages)
        self.assertIn("mouth·语义复核", messages[-1])
        self.assertIn("等它接线", messages[-1])

    def test_summary_refuses_when_card_is_broken(self):
        """摘要不可用只留给真正写错的卡：缺这一格是降级载入，摘要照出（那一行带 Issue）。"""
        edit_card(self.root, "manner", lambda data: data.update({"stance": "  "}))
        self.assertEqual(["有 error 级问题，摘要不可用，见下面的 Issue 列表"], checks.summary(self.root))


if __name__ == "__main__":
    unittest.main()

