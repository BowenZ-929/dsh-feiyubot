"""门禁上提成一处：根能不能用，只许在 `organs/paths.py` 判一次。

同一个错误在这个仓库里出现过三次（六轴卡、anchors、access/rooms），每次都是「两份标准各活
一阵、然后分叉」。所以这条不是风格偏好，是判据：`api/roots.py` 与 `organs/home/store.py`
只许**调用**门禁、只许把它的报错翻成自己的异常类型，不许各自再写一遍判断。

被看住的四件事：
1. 全仓只有 `organs/paths.py` 里能 `raise ... HOME_ROOT_*`；
2. 两个调用方都真的走 `paths.guard_root` / `guard_root_env`；
3. 三档错误码仍然分得开（空值 / 指着仓库内的出厂目录 / 目录不存在），没被合并成一个「根不对」；
4. 四向行为：plugin/ 拒、docs/ 拒、仓库外临时目录放行、空串拒。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from api import roots
from organs import paths
from organs.home import store
from organs.paths import HOME_ROOT_EMPTY, HOME_ROOT_IN_REPO, HOME_ROOT_MISSING

PLUGIN = Path(__file__).resolve().parents[2]


def _py_files():
    for path in PLUGIN.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        yield path


class OneGateOnly(unittest.TestCase):
    def test_root_errors_are_raised_in_one_file_only(self):
        offenders = []
        for path in _py_files():
            if path.name == "paths.py":
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "raise" in line and "HOME_ROOT_" in line:
                    offenders.append("%s:%d" % (path.relative_to(PLUGIN), i))
        self.assertEqual([], offenders, "根门禁被写了第二份：%s" % offenders)

    def test_both_callers_go_through_paths(self):
        self.assertTrue(callable(paths.guard_root))
        self.assertTrue(callable(paths.guard_root_env))
        for module in (roots, store):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertIn("paths.guard_root", source,
                          "%s 没走统一门禁" % module.__name__)

    def test_three_codes_stay_distinct(self):
        codes = {HOME_ROOT_EMPTY, HOME_ROOT_IN_REPO, HOME_ROOT_MISSING}
        self.assertEqual(3, len(codes), "三档错误被合并了：%s" % (codes,))


class FourDirections(unittest.TestCase):
    """两边各跑一遍：门禁的结论必须一致，否则还是两份标准。"""

    def _both(self, value, must_exist=False):
        results = []
        for call in (lambda: paths.guard_root(value, "t", must_exist=must_exist),
                     lambda: roots.organ_root("home", explicit=value),
                     lambda: store._guard(value, "t", must_exist=must_exist)):
            try:
                call()
                results.append("ok")
            except Exception as exc:                     # noqa: BLE001 - 只取码
                results.append(getattr(exc, "code", type(exc).__name__))
        return results

    def test_plugin_dir_is_rejected_by_both(self):
        self.assertEqual(["HOME_ROOT_IN_REPO"] * 3,
                         self._both(PLUGIN / "plugin" / "organs" / "home" / "data"))

    def test_docs_dir_is_rejected_by_both(self):
        self.assertEqual(["HOME_ROOT_IN_REPO"] * 3, self._both(PLUGIN / "docs"))

    def test_outside_repo_tmp_dir_is_allowed(self):
        """仓库外的已存在目录必须放行——家的 164 条测试与部署都靠这条。"""
        self.assertEqual(["ok"] * 3, self._both("/tmp"))

    def test_empty_env_is_rejected_by_both(self):
        original = os.environ.get(paths.ENV_HOME)
        try:
            os.environ[paths.ENV_HOME] = ""
            with self.assertRaises(paths.PathError) as caught:
                roots.home_root()
            self.assertEqual(HOME_ROOT_EMPTY, caught.exception.code)
        finally:
            if original is None:
                os.environ.pop(paths.ENV_HOME, None)
            else:
                os.environ[paths.ENV_HOME] = original

        original_root = os.environ.get(store.ENV_ROOT)
        try:
            os.environ[store.ENV_ROOT] = ""
            with self.assertRaises(store.StoreError) as caught_home:
                store.home_root()
            self.assertEqual(HOME_ROOT_EMPTY, caught_home.exception.code)
        finally:
            if original_root is None:
                os.environ.pop(store.ENV_ROOT, None)
            else:
                os.environ[store.ENV_ROOT] = original_root


if __name__ == "__main__":
    unittest.main()
