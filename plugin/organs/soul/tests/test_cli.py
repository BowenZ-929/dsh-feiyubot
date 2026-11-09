"""CLI 是人的另一只手：check / show / render / edit / accept-proposal / acceptance。

edit 不再有 --probe：探针只有 acceptance 一个入口（写值再写回原值，两行都记 probe）。
"""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout

from organs.soul import cli, checks, store as store_module
from organs.soul.tests.helpers import (append_row, copy_fixture, edit_card, proposal,
                                       write_json)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)

    def _run(self, argv):
        """--root 显式点名这份样例卡：CLI 的默认根是实盘，不再是出厂，别再靠模块常量。"""
        argv = [argv[0], "--root", str(self.root)] + list(argv[1:])
        if argv[0] == "check":
            # 样例卡只有 2 条线，本就和出厂那 10 条不是同一份：只考这一侧
            argv.append("--no-cross-check")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(argv)
        return code, buffer.getvalue()

    def test_check_exits_zero_on_warn_only(self):
        code, out = self._run(["check"])
        self.assertEqual(0, code)
        self.assertIn("七件·名字与自称", out)
        self.assertIn("七件·形象 %d/60 字（硬线）" % len("【待定稿】"), out)
        self.assertIn("七件·怕什么 %d/60 字（硬线）" % len("【待定稿】"), out)
        self.assertIn("七件·喜欢什么 %d/80 字（硬线；建议 40）" % len("【待定稿】"), out)
        self.assertIn("七件合计", out)

    def test_check_exits_one_on_error(self):
        edit_card(self.root, "bounds", lambda data: data["items"][0].update({"rule": "禁" * 1000}))
        code, out = self._run(["check"])
        self.assertEqual(1, code)
        self.assertIn("BUDGET_OVER", out)
        self.assertIn("合计是硬线", out)

    def test_check_warns_single_item_over_suggested(self):
        """单件超建议值只 warn：退出码 0，卡照样载入。"""
        edit_card(self.root, "identity", lambda data: data["self_names"].update({"display": "鱼" * 41}))
        code, out = self._run(["check"])
        self.assertEqual(0, code)
        self.assertIn("SUGGESTED_BUDGET_EXCEEDED", out)
        self.assertIn("建议值", out)

    def test_show_prints_digest(self):
        code, out = self._run(["show"])
        self.assertEqual(0, code)
        self.assertIn("digest sha256:", out)

    def test_render_prints_text_and_budget(self):
        code, out = self._run(["render", "--audience", "熟人"])
        self.assertEqual(0, code)
        self.assertIn("【待人姿态】", out)
        self.assertIn("预算（七件）", out)

    def test_render_ignores_retired_audience(self):
        """矩阵归脑后对象档不再是魂的概念：长辈也不再是错，只是不再有任何区别。"""
        code, out = self._run(["render", "--audience", "长辈"])
        self.assertEqual(0, code, out)
        self.assertEqual(self._run(["render", "--audience", "熟人"])[1], out)

    def test_render_group_audience_matches_the_others(self):
        self.assertEqual(self._run(["render", "--audience", "熟人"])[1],
                         self._run(["render", "--audience", "群"])[1])

    def test_edit_ok_prints_json(self):
        code, out = self._run(["edit", "--block", "manner", "--path", "stance", "--value", "新基调",
                               "--actor", "bowen", "--reason", "面板改一句"])
        self.assertEqual(0, code)
        self.assertTrue(json.loads(out)["ok"])

    def test_edit_numeric_value_is_parsed_as_json(self):
        code, out = self._run(["edit", "--block", "bounds", "--path", "items[0].active",
                               "--value", "false", "--actor", "bowen", "--reason", "撤一条线"])
        self.assertEqual(0, code, out)
        data = json.loads((self.root / "bounds.json").read_text(encoding="utf-8"))
        self.assertFalse(data["items"][0]["active"])

    def test_edit_rejected_prints_json_issue(self):
        code, out = self._run(["edit", "--block", "bounds", "--path", "items[0]",
                               "--value", "null", "--actor", "bowen", "--reason", "想删红线"])
        self.assertEqual(1, code)
        self.assertFalse(json.loads(out)["ok"])

    def test_baseline_edit_via_cli_is_an_unknown_block(self):
        code, out = self._run(["edit", "--block", "baseline", "--path", "axes[0].inner",
                               "--value", "55", "--actor", "bowen", "--reason", "想调基线"])
        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("block 只能是 identity/manner/bounds", payload["message"])

    def test_accept_proposal_via_cli(self):
        body = json.dumps(proposal(target="manner.stance", value="慢慢说，先听完"),
                          ensure_ascii=False)
        code, out = self._run(["accept-proposal", "--proposal", body, "--actor", "bowen",
                               "--now", "2026-03-10T09:00:00+00:00"])
        self.assertEqual(0, code, out)
        self.assertTrue(json.loads(out)["ok"])
        data = json.loads((self.root / "manner.json").read_text(encoding="utf-8"))
        self.assertEqual("慢慢说，先听完", data["stance"])

    def test_acceptance_subcommand_logs_two_probe_rows(self):
        before = json.loads((self.root / "manner.json").read_text(encoding="utf-8"))["stance"]
        code, out = self._run(["acceptance", "--block", "manner", "--path", "stance",
                               "--value", "探一针", "--restore-value", before,
                               "--actor", "壳线-验收", "--reason", "验证写路径"])
        self.assertEqual(0, code, out)
        payload = json.loads(out)
        self.assertEqual(["probe", "probe"], [line["kind"] for line in payload["history_lines"]])
        rows = store_module.read_history(self.root / "history.jsonl")
        self.assertEqual(["probe", "probe"], [row["kind"] for row in rows])
        self.assertEqual("壳线-验收", rows[-1]["actor"])
        self.assertEqual(before, json.loads(
            (self.root / "manner.json").read_text(encoding="utf-8"))["stance"])

    def test_acceptance_subcommand_refuses_the_bounds_block(self):
        code, out = self._run(["acceptance", "--block", "bounds", "--path", "items[0].rule",
                               "--value", "探一针", "--restore-value", "【待定稿】",
                               "--actor", "壳线-验收", "--reason", "探一针红线"])
        self.assertEqual(1, code)
        self.assertEqual("PROBE_ON_BOUNDS_FORBIDDEN", json.loads(out)["issues"][0]["code"])

    def test_migrate_runs_before_load_so_a_bad_card_can_be_replaced(self):
        """旧形状在新 model 下是坏卡：migrate 不过 load()，坏卡状态也能换形状。"""
        write_json(self.root / "manner.json", {"tone": "旧形状", "matrix": {}})
        expect = hashlib.sha256((self.root / "manner.json").read_bytes()).hexdigest()
        code, out = self._run(["migrate", "--block", "manner",
                               "--value", json.dumps({"stance": "换完的姿态"}, ensure_ascii=False),
                               "--actor", "bowen", "--reason", "manner 收成 stance",
                               "--expect-digest", expect])
        self.assertEqual(0, code, out)
        self.assertEqual({"stance": "换完的姿态"},
                         json.loads((self.root / "manner.json").read_text(encoding="utf-8")))
        rows = store_module.read_history(self.root / "history.jsonl")
        self.assertEqual("migrate", rows[-1]["kind"])

    def test_migrate_with_a_stale_digest_exits_one(self):
        before = (self.root / "manner.json").read_bytes()
        code, out = self._run(["migrate", "--block", "manner", "--value", '{"stance": "不该落盘"}',
                               "--actor", "bowen", "--reason", "换形状",
                               "--expect-digest", "sha256:" + "0" * 64])
        self.assertEqual(1, code)
        self.assertFalse(json.loads(out)["ok"])
        self.assertEqual(before, (self.root / "manner.json").read_bytes())

    def test_check_output_has_no_baseline_or_axis_line(self):
        code, out = self._run(["check"])
        self.assertEqual(0, code)
        for word in ("baseline", "六轴", "PENDING_AXIS"):
            self.assertNotIn(word, out)

    def test_check_reports_a_manual_row_as_warn(self):
        append_row(self.root, {"ts": store_module.now_iso(), "kind": "manual", "actor": "bowen",
                               "block": "baseline", "path": "(整块)", "reason": "六轴迁心"})
        code, out = self._run(["check"])
        self.assertEqual(0, code, "manual 是 warn，不该让退出码变 1")
        self.assertIn("MANUAL_EDIT_IN_LEDGER", out)


if __name__ == "__main__":
    unittest.main()

