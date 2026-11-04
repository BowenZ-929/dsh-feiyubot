"""零写路径：魂的程序侧没有写盘，写只出现在 store 的原子写与追账两个点上。

扫源码，不靠人记：一旦有人给魂新加一条程序写路径，这条测试就红。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import List, Tuple

SOUL = Path(__file__).resolve().parents[1]
SKIP_DIRS = ("tests", "__pycache__", "fixtures")
WRITE_HINTS = ("open(", ".write_text(", ".write_bytes(", "os.replace(", "os.remove(",
               "os.unlink(", "os.rename(", "shutil.", "tempfile.mkstemp", "os.fsync(",
               ".mkdir(", ".touch(")
# 唯一允许的写点：(文件, 函数名)
WRITE_POINTS = {("store.py", "_atomic_write"), ("store.py", "append_history")}
_DEF = re.compile(r"^\s*def\s+(\w+)")


def _sources() -> List[Path]:
    return sorted(path for path in SOUL.rglob("*.py")
                  if not any(part in SKIP_DIRS for part in path.relative_to(SOUL).parts))


def _enclosing(lines: List[str], index: int) -> str:
    for back in range(index, -1, -1):
        matched = _DEF.match(lines[back])
        if matched:
            return matched.group(1)
    return ""


def _hits() -> List[Tuple[str, int, str, str]]:
    found: List[Tuple[str, int, str, str]] = []
    for path in _sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if any(hint in line for hint in WRITE_HINTS):
                found.append((path.name, number + 1, _enclosing(lines, number), line.strip()))
    return found


class ZeroWritePathTest(unittest.TestCase):
    def test_sources_are_scanned(self):
        names = {path.name for path in _sources()}
        self.assertIn("store.py", names)
        self.assertIn("model.py", names)

    def test_only_the_two_store_write_points_touch_disk(self):
        offenders = ["%s:%d %s() %s" % hit for hit in _hits()
                     if (hit[0], hit[2]) not in WRITE_POINTS]
        self.assertEqual([], offenders,
                         "魂出现了程序写路径：唯一写入口是人的手（edit / accept_proposal），"
                         "写盘只许在 store._atomic_write 与 store.append_history：\n"
                         + "\n".join(offenders))

    def test_both_write_points_are_really_there(self):
        used = {(hit[0], hit[2]) for hit in _hits()}
        self.assertEqual(WRITE_POINTS, used & WRITE_POINTS)

    def test_no_module_writes_outside_store(self):
        others = [hit for hit in _hits() if hit[0] != "store.py"]
        self.assertEqual([], others)


if __name__ == "__main__":
    unittest.main()

