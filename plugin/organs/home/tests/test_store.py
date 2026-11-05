"""落盘底座：根目录三级、原子写与 0600/0700、坏行计数、encodeURIComponent 等价与 160 字截断。"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs import paths
from organs.home import store
from organs.home.store import (
    StoreError, append_line, atomic_write_text, encode_name, ensure_dir, home_root,
    line_count, read_lines, sha256_file,
)


class HomeRootTest(unittest.TestCase):
    def test_explicit_root_wins_over_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_root = Path(tmp) / "env-home"
            env_root.mkdir()
            with mock.patch.dict(os.environ, {"HOME_ROOT": str(env_root)}, clear=False):
                self.assertEqual(Path("/tmp/given"), home_root("/tmp/given"))
                self.assertEqual(env_root, home_root())

    def test_default_is_the_live_organ_dir(self):
        """没给参数、没有 HOME_ROOT：实盘根是 <仓库>/home/organs/home，包内 data/ 只是出厂默认参数。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(paths.organ_dir("home"), home_root())

    # —— 三条门禁：不留退路，缺了就响 ————————————————————

    def test_empty_env_is_loud_instead_of_falling_back(self):
        """HOME_ROOT 设成空串 ≠ 没设：不许悄悄换成默认根（那正是第二条真源的路）。"""
        with mock.patch.dict(os.environ, {"HOME_ROOT": "   "}, clear=False):
            with self.assertRaises(StoreError) as caught:
                home_root()
        self.assertEqual("HOME_ROOT_EMPTY", caught.exception.code)
        self.assertIn("别设", caught.exception.detail, "拒的话要说人话：要么给实盘，要么别设")

    def test_env_pointing_into_the_repo_is_refused(self):
        """指到包内出厂默认目录 → 门禁拒。这是「改出厂等于改实盘」那条路的入口。"""
        with mock.patch.dict(os.environ, {"HOME_ROOT": str(paths.factory_dir("home"))}, clear=False):
            with self.assertRaises(StoreError) as caught:
                home_root()
        self.assertEqual("HOME_ROOT_IN_REPO", caught.exception.code)

    def test_explicit_root_into_the_repo_is_refused_too(self):
        """显式参数不查存在，但门禁照样过——否则绕过门禁只要加个参数。"""
        with self.assertRaises(StoreError) as caught:
            home_root(str(paths.factory_dir("home")))
        self.assertEqual("HOME_ROOT_IN_REPO", caught.exception.code)

    def test_env_pointing_at_a_missing_dir_is_refused(self):
        """要存在：不替她造一个根，猜出来的根迟早和真源分叉。"""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            with mock.patch.dict(os.environ, {"HOME_ROOT": str(missing)}, clear=False):
                with self.assertRaises(StoreError) as caught:
                    home_root()
            self.assertEqual("HOME_ROOT_MISSING", caught.exception.code)
            self.assertFalse(missing.exists(), "拒了就不许顺手把它建出来")

    def test_there_is_no_factory_fallback_left(self):
        """默认根只认 <仓库>/home/organs/home：不许落回包内 data/。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            live = home_root()
        self.assertEqual(paths.organ_dir("home"), live)
        self.assertNotEqual(paths.factory_dir("home"), live)


class AtomicWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_write_is_private_and_dir_is_0700(self):
        target = self.root / "屋子" / "卡.json"
        atomic_write_text(target, '{"a": 1}\n')
        self.assertEqual('{"a": 1}\n', target.read_text(encoding="utf-8"))
        self.assertEqual(0o600, target.stat().st_mode & 0o777)
        self.assertEqual(0o700, target.parent.stat().st_mode & 0o777)

    def test_write_leaves_no_temp_file(self):
        target = self.root / "卡.json"
        atomic_write_text(target, "一\n")
        atomic_write_text(target, "二\n")
        self.assertEqual(["卡.json"], sorted(path.name for path in self.root.iterdir()))
        self.assertEqual("二\n", target.read_text(encoding="utf-8"))

    def test_failed_write_keeps_old_content(self):
        target = self.root / "卡.json"
        atomic_write_text(target, "旧\n")
        with self.assertRaises(TypeError):
            atomic_write_text(target, None)  # type: ignore[arg-type]
        self.assertEqual("旧\n", target.read_text(encoding="utf-8"))
        self.assertEqual(["卡.json"], sorted(path.name for path in self.root.iterdir()))


class LineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "账.jsonl"

    def test_append_returns_total_lines(self):
        self.assertEqual(1, append_line(self.path, {"a": 1}))
        self.assertEqual(2, append_line(self.path, {"a": 2}))
        self.assertEqual(2, line_count(self.path))
        rows, bad = read_lines(self.path)
        self.assertEqual([{"a": 1}, {"a": 2}], rows)
        self.assertEqual(0, bad)

    def test_bad_lines_are_counted_not_swallowed(self):
        self.path.write_text('{"a": 1}\n不是 json\n[1]\n\n{"b": 2}\n', encoding="utf-8")
        rows, bad = read_lines(self.path)
        self.assertEqual([{"a": 1}, {"b": 2}], rows)
        self.assertEqual(2, bad)

    def test_blank_lines_are_not_records(self):
        self.path.write_text('{"a": 1}\n\n\n{"a": 2}\n', encoding="utf-8")
        self.assertEqual(2, line_count(self.path))

    def test_missing_file_is_empty_not_error(self):
        self.assertEqual(([], 0), read_lines(self.path))
        self.assertEqual(0, line_count(self.path))


class EncodeNameTest(unittest.TestCase):
    def test_matches_encode_uri_component(self):
        self.assertEqual("%E6%8A%BD%E5%B1%89", encode_name("抽屉"))
        self.assertEqual("a%20b%2Fc", encode_name("a b/c"))
        self.assertEqual("A-z_0.!~*'()", encode_name("A-z_0.!~*'()"))
        self.assertEqual("100%25", encode_name("100%"))

    def test_no_collision_with_a_literal_lookalike(self):
        self.assertEqual("%C3%A1_ue1", encode_name("á_ue1"))
        self.assertEqual("ue1", encode_name("ue1"))
        self.assertNotEqual(encode_name("á_ue1"), encode_name("ue1"))

    def test_truncates_at_160_without_half_escape(self):
        out = encode_name("抽" * 200)
        self.assertLessEqual(len(out), 160)
        self.assertIsNone(re.search(r"%[0-9A-F]?$", out))
        self.assertTrue(out.startswith("%E6%8A%BD"))

    def test_non_text_is_loud(self):
        with self.assertRaises(StoreError) as caught:
            encode_name(123)  # type: ignore[arg-type]
        self.assertEqual("NAME_NOT_TEXT", caught.exception.code)


class Sha256Test(unittest.TestCase):
    def test_hash_and_missing_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "一件"
        path.write_bytes(b"abc")
        self.assertEqual(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            sha256_file(path),
        )
        with self.assertRaises(StoreError) as caught:
            sha256_file(Path(tmp.name) / "不在")
        self.assertEqual("FILE_MISSING", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
