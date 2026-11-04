"""实盘根：门禁、补件、魂例外（api/roots.py）。

两条门禁的验收口径就写在这里：
1. 根指到 plugin/ 或 docs/（或仓库外）→ 必须拒绝启动，且报错文案人话可读；
2. home/organs/soul 空着 → 必须报「魂还没搬进来」并拒绝启动，绝不替她造一个。
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from api import roots
from organs import paths

ENV_KEYS = ("QQAGENT_HOME", "HOME_ROOT", "DAY_ROOT", "HEART_ROOT")


class RootsTest(unittest.TestCase):
    """临时根都建在 <仓库>/home 底下：门禁只认那里，测试也不能自己开后门。"""

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _home(self, name: str) -> Path:
        base = paths.HOME / name
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, base, True)
        return base

    # ---- 默认落点 ---------------------------------------------------------

    def test_default_root_is_repo_home(self) -> None:
        self.assertEqual(paths.HOME / "organs", roots.home_root())
        self.assertEqual(paths.HOME / "organs" / "soul", roots.organ_root("soul"))
        self.assertEqual(paths.HOME / "organs" / "brain", roots.organ_root("brain"))

    def test_mouth_stays_factory(self) -> None:
        mouth = roots.organ_root("mouth")
        self.assertEqual(roots.factory_root("mouth"), mouth)
        self.assertNotIn(paths.HOME, mouth.parents)

    def test_house_organs_are_the_five(self) -> None:
        self.assertEqual(("soul", "brain", "heart", "home", "day"), roots.HOUSE_ORGANS)
        for organ in roots.HOUSE_ORGANS:
            self.assertIn(paths.HOME, roots.organ_root(organ).parents)

    # ---- 门禁 -------------------------------------------------------------

    def test_rejects_plugin_path(self) -> None:
        os.environ["QQAGENT_HOME"] = str(roots.factory_root("soul"))
        with self.assertRaises(roots.RootsError) as caught:
            roots.home_root()
        message = str(caught.exception)
        self.assertIn("plugin/", message)
        self.assertIn(str(paths.HOME), message)

    def test_rejects_docs_path(self) -> None:
        os.environ["QQAGENT_HOME"] = str(paths.REPO_ROOT / "docs")
        with self.assertRaises(roots.RootsError) as caught:
            roots.home_root()
        self.assertIn("docs/", str(caught.exception))

    def test_rejects_outside_repo(self) -> None:
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        os.environ["QQAGENT_HOME"] = outside
        with self.assertRaises(roots.RootsError):
            roots.home_root()

    def test_explicit_root_also_gated(self) -> None:
        """--root 这种显式覆盖同样过门禁：兼容不等于开后门。"""
        with self.assertRaises(roots.RootsError):
            roots.organ_root("soul", roots.factory_root("soul"))

    # ---- 魂例外 -----------------------------------------------------------

    def test_empty_soul_refuses_and_does_not_create_one(self) -> None:
        home = self._home(".roots-test-empty")
        os.environ["QQAGENT_HOME"] = str(home)
        with self.assertRaises(roots.RootsError) as caught:
            roots.bootstrap()
        self.assertIn("魂还没搬进来", str(caught.exception))
        self.assertEqual([], sorted(home.iterdir()))

    def test_missing_soul_dir_refuses(self) -> None:
        home = self._home(".roots-test-nosoul")
        os.environ["QQAGENT_HOME"] = str(home)
        with self.assertRaises(roots.RootsError) as caught:
            roots.bootstrap()
        self.assertIn("魂还没搬进来", str(caught.exception))

    # ---- 补件 -------------------------------------------------------------

    def test_seed_fills_missing_only(self) -> None:
        home = self._home(".roots-test-seed")
        (home / "soul").mkdir()
        (home / "soul" / "identity.json").write_text("{}", encoding="utf-8")
        (home / "brain").mkdir()
        mine = home / "brain" / "tiers.json"
        mine.write_text('{"mine": true}', encoding="utf-8")
        os.environ["QQAGENT_HOME"] = str(home)

        info = roots.bootstrap()

        self.assertGreater(info["seeded"]["brain"], 0)
        self.assertEqual('{"mine": true}', mine.read_text(encoding="utf-8"))
        for source in roots.factory_root("brain").iterdir():
            if source.name == ".gitignore":
                continue
            self.assertTrue((home / "brain" / source.name).is_file(), source.name)
        # 魂不补
        self.assertEqual(["identity.json"],
                         sorted(item.name for item in (home / "soul").iterdir()))

    def test_seed_skips_gitignore(self) -> None:
        """出厂忽略规则别搬进实盘：它在 home 里会二次生效，把 .gitkeep 一起吃掉。"""
        home = self._home(".roots-test-ignore")
        (home / "soul").mkdir()
        (home / "soul" / "identity.json").write_text("{}", encoding="utf-8")
        os.environ["QQAGENT_HOME"] = str(home)
        roots.bootstrap()
        self.assertTrue((roots.factory_root("heart") / ".gitignore").is_file())
        self.assertFalse((home / "heart" / ".gitignore").exists())

    def test_bootstrap_points_organs_at_home(self) -> None:
        home = self._home(".roots-test-env")
        (home / "soul").mkdir()
        (home / "soul" / "identity.json").write_text("{}", encoding="utf-8")
        os.environ["QQAGENT_HOME"] = str(home)
        roots.bootstrap()
        self.assertEqual(str(home / "home"), os.environ["HOME_ROOT"])
        self.assertEqual(str(home / "heart"), os.environ["HEART_ROOT"])
        self.assertEqual(str(home / "day"), os.environ["DAY_ROOT"])


if __name__ == "__main__":
    unittest.main()
