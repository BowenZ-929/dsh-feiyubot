"""两边不许各改一边：出厂与实盘比红线 id 集合、identity 键集合。

CLI 曾经只读出厂（organs/soul/data），实盘少一条线也一片绿——这一组盯的就是"读错一边还报绿"：
根三级（--root > SOUL_ROOT > 实盘默认）解析得对，两边的骨架分叉当场是 error。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from organs import paths
from organs.soul import checks, cli, model

BLOCK_FILES = ("identity.json", "manner.json", "bounds.json")


def factory_copy(target: Path) -> Path:
    """把出厂那份拷成一个临时"实盘"：先有一份两边同源的干净对照。"""
    target.mkdir(parents=True, exist_ok=True)
    for name in BLOCK_FILES:
        shutil.copy2(str(checks.FACTORY_ROOT / name), str(target / name))
    (target / "history.jsonl").write_text("", encoding="utf-8")
    return target


def append_item(path: Path, item_id: str) -> None:
    """往某一侧的红线里加一条：id 与禁令都换掉，别让"重复的禁令"把这次的读数搅混。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["items"].append(dict(data["items"][0], id=item_id,
                              rule="只在这一侧的一条线：%s" % item_id))
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def codes(issues) -> list:
    return [issue.code for issue in issues]


class RootResolutionTest(unittest.TestCase):
    """CLI 的根三级：--root > SOUL_ROOT > 实盘默认（<仓库>/home/organs/soul）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _run(self, argv):
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = cli.main(list(argv))
        return code, buffer.getvalue()

    def test_default_root_is_the_live_one_not_the_factory(self):
        default = cli.resolve_root()
        self.assertEqual(paths.organ_dir("soul"), default)
        self.assertEqual(("home", "organs", "soul"), default.parts[-3:],
                         "默认根该是实盘：%s" % default)
        self.assertNotEqual(checks.FACTORY_ROOT, default, "默认根还指出厂：check 又开始骗人")

    def test_soul_root_env_picks_a_temp_root(self):
        live = factory_copy(self.tmp / "live")
        with mock.patch.dict(os.environ, {cli.ENV_ROOT: str(live)}):
            self.assertEqual(live, cli.resolve_root())
            code, out = self._run(["check"])
        self.assertEqual(0, code, out)
        self.assertIn("魂根 %s" % live, out, "check 没说自己读的是哪一侧")

    def test_explicit_root_beats_the_env(self):
        live = factory_copy(self.tmp / "live")
        explicit = factory_copy(self.tmp / "explicit")
        with mock.patch.dict(os.environ, {cli.ENV_ROOT: str(live)}):
            self.assertEqual(explicit, cli.resolve_root(str(explicit)))


class CrossCheckTest(unittest.TestCase):
    """两边骨架分叉：只比集合，不比顺序、措辞、字数。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _run(self, argv):
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = cli.main(list(argv))
        return code, buffer.getvalue()

    def test_both_sides_agreeing_report_nothing(self):
        live = factory_copy(self.tmp / "live")
        self.assertEqual([], checks.cross_check_issues(live))
        self.assertNotIn(checks.CROSS_CHECK_DRIFT, codes(checks.collect(live)))

    def test_an_id_only_in_the_factory_is_an_error(self):
        factory = factory_copy(self.tmp / "factory")
        live = factory_copy(self.tmp / "live")
        append_item(factory / "bounds.json", "bounds.only-in-factory")
        issues = checks.cross_check_issues(live, factory)
        self.assertEqual([checks.CROSS_CHECK_DRIFT], codes(issues))
        self.assertEqual(model.ERROR, issues[0].level)
        self.assertIn("两边 id 集合不一致", issues[0].message)
        self.assertIn("只在一侧 = ['bounds.only-in-factory']", issues[0].message)

    def test_an_id_only_in_the_live_side_is_an_error(self):
        factory = factory_copy(self.tmp / "factory")
        live = factory_copy(self.tmp / "live")
        append_item(live / "bounds.json", "bounds.only-in-live")
        issues = checks.cross_check_issues(live, factory)
        self.assertEqual([checks.CROSS_CHECK_DRIFT], codes(issues))
        self.assertIn("只在一侧 = ['bounds.only-in-live']", issues[0].message)
        # 走真出厂那份也一样响：这条断言不是只在注入的对照上成立
        self.assertIn(checks.CROSS_CHECK_DRIFT, codes(checks.collect(live)))

    def test_identity_key_drift_is_an_error_too(self):
        factory = factory_copy(self.tmp / "factory")
        live = factory_copy(self.tmp / "live")
        data = json.loads((live / "identity.json").read_text(encoding="utf-8"))
        data.pop("likes")
        (live / "identity.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
        issues = checks.cross_check_issues(live, factory)
        self.assertEqual([checks.CROSS_CHECK_DRIFT], codes(issues))
        self.assertIn("identity 键集合", issues[0].message)
        self.assertIn("likes", issues[0].message)

    def test_order_and_wording_do_not_count(self):
        live = factory_copy(self.tmp / "live")
        data = json.loads((live / "bounds.json").read_text(encoding="utf-8"))
        data["items"].reverse()
        data["items"][0]["rule"] = "换个说法：不因自称就认亲，谁是谁由号定"
        (live / "bounds.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
        self.assertEqual([], checks.cross_check_issues(live))

    def test_a_missing_side_is_skipped(self):
        live = factory_copy(self.tmp / "live")
        self.assertEqual([], checks.cross_check_issues(self.tmp / "no-such-side"))
        self.assertEqual([], checks.cross_check_issues(live, self.tmp / "no-such-factory"))

    def test_cli_check_fails_on_drift_and_no_cross_check_turns_it_off(self):
        live = factory_copy(self.tmp / "live")
        append_item(live / "bounds.json", "bounds.only-in-live")
        code, out = self._run(["check", "--root", str(live)])
        self.assertEqual(1, code, out)
        self.assertIn("FACTORY_LIVE_ID_DRIFT", out)
        self.assertIn("两边 id 集合不一致", out)
        code, out = self._run(["check", "--root", str(live), "--no-cross-check"])
        self.assertEqual(0, code, out)
        self.assertNotIn("FACTORY_LIVE_ID_DRIFT", out)


if __name__ == "__main__":
    unittest.main()
