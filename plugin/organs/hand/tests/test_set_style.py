"""set_style：她只能改「怎么说话」（两个旋钮），不能改「她是谁」。

判据（本批冻结的那几条）：
- 白名单恰五个动作，`set_style` 是「可撤销」（改的是家的一张偏好卡，随时能再改回来）；
- 可写的 key 恰两个（chunks_per_reply 1–3、max_chars 10–80），别的 key 一律 KEY_NOT_ALLOWED；
- utterance 走 stylelang 那张短语表，**认不出就反问**（STYLE_NOT_UNDERSTOOD），一个字都不写；
- 身份类的话（「你把自己改成别的人」）**不许从 STYLE_NOT_UNDERSTOOD 溜过去**，落 KEY_NOT_ALLOWED；
- 结果行要记清：key / value / old / old_source / card_id / who / target_who；
- 「家的盘零新增行」是这一批最要紧的一条：拒了就一格没动，读的是盘，不是回执。
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from organs.home import preference_of as home_preference_of
from organs.hand import actions, cli, permit, whitelist
from organs.hand.tests import helpers

WHO = "阿禾"


class StyleTest(unittest.TestCase):
    """公共夹具：临时实盘（手 hand/ + 家 home/ 是兄弟目录）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)

    def set_style(self, *, who: str = WHO, **kwargs: Any) -> Dict[str, Any]:
        return actions.set_style(who=who, now=helpers.NOW, root=self.root, **kwargs)

    def prefs_file(self) -> Path:
        return helpers.home_root(self.root) / "名片盒" / "prefs.jsonl"

    def pref_rows(self) -> List[Dict[str, Any]]:
        """偏好卡流水的**盘上原样**（不走被测代码）：零新增行这条只能这么验。"""
        path = self.prefs_file()
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def pref(self, who: str = WHO) -> Dict[str, Any]:
        return home_preference_of(root=helpers.home_root(self.root), who=who)


class WhitelistTest(StyleTest):
    def test_whitelist_holds_exactly_six_actions(self):
        self.assertEqual(6, len(whitelist.ACTIONS))
        self.assertEqual(("add_task", "complete_task", "send_image", "draw", "set_style",
                          "lookup"),
                         whitelist.ACTIONS)
        self.assertTrue(whitelist.allowed("set_style"))

    def test_set_style_is_reversible_and_needs_no_grant(self):
        self.assertEqual("可撤销", permit.risk_of("set_style"))
        self.assertFalse(permit.needs_grant("set_style"))

    def test_signature_is_the_frozen_one_and_takes_no_path(self):
        params = inspect.signature(actions.set_style).parameters
        self.assertEqual(["who", "key", "value", "utterance", "now", "root"], list(params))
        for name, param in params.items():
            self.assertIs(inspect.Parameter.KEYWORD_ONLY, param.kind, name)
            if name != "who":
                self.assertIsNone(param.default, name)


class UtteranceTest(StyleTest):
    def test_one_sentence_utterance_lands_on_the_card(self):
        """「一句话说完，别分开发」→ chunks_per_reply=1，而且卡上真读得到。"""
        out = self.set_style(utterance="一句话说完，别分开发")
        self.assertEqual("done", out["state"], out)
        self.assertEqual("OK", out["code"])
        self.assertEqual("set_style", out["action"])
        card = self.pref()
        self.assertEqual(1, card["chunks_per_reply"])
        self.assertEqual(WHO, card["who"])
        self.assertTrue(card["card_id"].startswith("偏好-"), card["card_id"])
        self.assertEqual([card["card_id"]], [row["card_id"] for row in self.pref_rows()])

    def test_once_no_more_than_twenty_chars(self):
        out = self.set_style(utterance="一次别超 20 字")
        self.assertEqual("done", out["state"], out)
        self.assertEqual("max_chars", out["detail"]["key"])
        self.assertEqual(20, out["detail"]["value"])
        self.assertEqual(20, self.pref()["max_chars"])

    def test_say_shorter_means_the_short_length_tier(self):
        out = self.set_style(utterance="说短点")
        self.assertEqual("done", out["state"], out)
        self.assertEqual(20, self.pref()["max_chars"])

    def test_unreadable_words_get_a_question_and_write_nothing(self):
        out = self.set_style(utterance="温柔一点嘛")
        self.assertEqual("refused", out["state"])
        self.assertEqual("STYLE_NOT_UNDERSTOOD", out["code"])
        self.assertIn("怎么说话", out["say"])
        self.assertEqual([], self.pref_rows())
        self.assertEqual(["reserved", "refused"],
                         [row["state"] for row in helpers.ledger_lines(self.root)])

    def test_two_different_wishes_in_one_sentence_are_not_guessed(self):
        """一段话里两件不同的事：不挑一件办，反问。"""
        out = self.set_style(utterance="说短点，一句话说完")
        self.assertEqual("STYLE_NOT_UNDERSTOOD", out["code"])
        self.assertEqual([], self.pref_rows())

    def test_identity_words_never_slip_through_as_not_understood(self):
        """「你把自己改成别的人」听起来像在改口吻，其实是改身份：走 KEY_NOT_ALLOWED。"""
        out = self.set_style(utterance="你把自己改成别的人")
        self.assertEqual("refused", out["state"])
        self.assertEqual("KEY_NOT_ALLOWED", out["code"])
        self.assertIn("改她是谁", out["say"])
        self.assertIn("chunks_per_reply", out["say"])
        self.assertIn("max_chars", out["say"])
        self.assertEqual("identity", out["detail"]["reason"])
        self.assertEqual([], self.pref_rows(), "卡上一格没动")
        self.assertFalse(self.prefs_file().exists(), "家的盘上零新增行")


class KeyTest(StyleTest):
    def test_any_key_outside_the_two_knobs_is_refused(self):
        for key in ("identity", "改魂", "人设", "appearance", "style"):
            out = self.set_style(key=key, value=1)
            self.assertEqual("refused", out["state"], key)
            self.assertEqual("KEY_NOT_ALLOWED", out["code"], key)
            self.assertIn("只能改这两样", out["say"])
            self.assertIn("改她是谁", out["say"])
        self.assertEqual([], self.pref_rows())

    def test_both_knobs_are_writable_on_the_same_card(self):
        self.assertEqual("done", self.set_style(key="chunks_per_reply", value=2)["state"])
        self.assertEqual("done", self.set_style(key="max_chars", value=40)["state"])
        card = self.pref()
        self.assertEqual(2, card["chunks_per_reply"])
        self.assertEqual(40, card["max_chars"])
        self.assertEqual(2, len(self.pref_rows()), "一人一卡：写两次就是追加两行")


class RangeTest(StyleTest):
    def test_out_of_range_values_name_the_range_and_change_nothing(self):
        for key, value, (low, high) in (("max_chars", 5, (10, 80)),
                                        ("chunks_per_reply", 9, (1, 3)),
                                        ("max_chars", 200, (10, 80)),
                                        ("chunks_per_reply", 0, (1, 3))):
            out = self.set_style(key=key, value=value)
            self.assertEqual("refused", out["state"], (key, value))
            self.assertEqual("VALUE_OUT_OF_RANGE", out["code"], (key, value))
            self.assertIn("%d–%d" % (low, high), out["say"], (key, value))
            self.assertIn("今天没改", out["say"], (key, value))
        self.assertEqual([], self.pref_rows())

    def test_digit_strings_convert_but_bools_floats_and_words_do_not(self):
        """命令行只能给字符串：纯数字串无损转 int；bool／浮点／汉字数一律拒（不猜）。"""
        ok = self.set_style(key="max_chars", value="30")
        self.assertEqual("done", ok["state"], ok)
        self.assertEqual(30, ok["detail"]["value"])
        self.assertEqual(30, self.pref()["max_chars"])
        for bad in (True, False, 20.5, "二十", "", None):
            out = self.set_style(key="max_chars", value=bad)
            self.assertEqual("VALUE_OUT_OF_RANGE", out["code"], repr(bad))
            self.assertEqual("not_an_int", out["detail"]["reason"], repr(bad))
        self.assertEqual(30, self.pref()["max_chars"], "拒的那几次一个字都没写")


class LedgerTest(StyleTest):
    def test_old_and_old_source_and_card_id_are_all_recorded(self):
        first = self.set_style(key="max_chars", value=30)
        self.assertEqual("done", first["state"], first)
        params = helpers.ledger_lines(self.root)[-1]["params"]
        self.assertEqual("max_chars", params["key"])
        self.assertEqual(30, params["value"])
        self.assertIsNone(params.get("old"), "第一次改：原来没有这一格")
        self.assertEqual("default", params["old_source"])
        self.assertEqual(WHO, params["who"])
        self.assertEqual(WHO, params["target_who"])
        self.assertTrue(str(params["card_id"]).startswith("偏好-"), params["card_id"])

        second = self.set_style(key="max_chars", value=40)
        self.assertEqual("done", second["state"], second)
        params = helpers.ledger_lines(self.root)[-1]["params"]
        self.assertEqual(30, params["old"], "第二次改：原来是什么就是从卡上读来的那个数")
        self.assertEqual("live", params["old_source"])
        self.assertEqual(40, params["value"])
        self.assertEqual(self.pref()["card_id"], params["card_id"],
                         "card_id 是这一行的卡：再追加一行就是同一人的下一张")
        self.assertNotEqual(first["card_id"], params["card_id"], "两次改写是两行账，不改旧行")

    def test_unwritable_reserved_row_means_nothing_happens(self):
        (self.root / "ledger.jsonl").mkdir()      # 账变成目录：追加必然失败
        out = self.set_style(key="max_chars", value=30)
        self.assertEqual("failed", out["state"])
        self.assertEqual("LEDGER_WRITE_FAILED", out["code"])
        self.assertEqual("reserved", out["detail"]["stage"])
        self.assertFalse(self.prefs_file().exists(), "账没记上就不动手")
        self.assertFalse((helpers.home_root(self.root) / "名片盒").exists())

    def test_each_wish_is_two_rows_and_the_refusal_keeps_its_evidence(self):
        self.set_style(key="max_chars", value=30)
        states = [row["state"] for row in helpers.ledger_lines(self.root)]
        self.assertEqual(["reserved", "done"], states)
        for row in helpers.ledger_lines(self.root):
            self.assertEqual("set_style", row["action"])
            self.assertEqual(WHO, row["who"])


class QuotaTest(StyleTest):
    def test_all_five_actions_share_one_daily_quota(self):
        helpers.write_limits(self.root, 2)
        self.assertEqual("done", self.set_style(key="max_chars", value=30)["state"])
        added = actions.add_task(who=WHO, title="交房租", due=helpers.TOMORROW_NOON,
                                 now=helpers.NOW, root=self.root)
        self.assertEqual("done", added["state"], added)
        third = self.set_style(key="max_chars", value=40)
        self.assertEqual("refused", third["state"])
        self.assertEqual("QUOTA_EXCEEDED", third["code"])
        self.assertEqual(0, third["detail"]["remaining"])
        self.assertEqual(30, self.pref()["max_chars"], "额度拦下的那次一个字节都没写")


class CliTest(StyleTest):
    def test_set_style_prints_one_json_line_with_say_and_code(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            status = cli.main(["set-style", "--utterance", "说短点", "--who", WHO,
                               "--root", str(self.root), "--at", helpers.NOW])
        self.assertEqual(0, status)
        lines = buffer.getvalue().strip().splitlines()
        self.assertEqual(1, len(lines), "每条命令只打一行 JSON")
        payload = json.loads(lines[0])
        self.assertEqual({"state", "action", "code", "say", "who", "card_id", "detail",
                          "ledger_id"}, set(payload))
        self.assertEqual("done", payload["state"])
        self.assertEqual("OK", payload["code"])
        self.assertTrue(payload["say"])
        self.assertEqual(20, self.pref()["max_chars"])

    def test_cli_takes_key_and_value_as_strings(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.main(["set-style", "--key", "chunks_per_reply", "--value", "2", "--who", WHO,
                      "--root", str(self.root), "--at", helpers.NOW])
        payload = json.loads(buffer.getvalue().strip())
        self.assertEqual("done", payload["state"], payload)
        self.assertEqual(2, self.pref()["chunks_per_reply"])


if __name__ == "__main__":
    unittest.main()
