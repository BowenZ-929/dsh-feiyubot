"""§9.5 硬约束：核不许 import 宿主——这条约束由测试看住，不靠人记。

architecture.md §9.5 的原话：硬约束只有一条，核（`organs/<器官>`）不许 import 宿主；
宿主只经三条接口进来（入站、出站、面板）。本文件把它变成两条断言：

1. 核源码（`organs/`、`api/`）里不出现任何宿主字样（AstrBot / DSH 的包名）；
2. 宿主 import 只许出现在壳文件 `main.py`，别处出现即失败。

扫描跳过各包的 `tests/`：测试要探"宿主装没装好"来决定 skip 还是跑（例如
`api/tests/test_web_api.py` 里对 AstrBot 包名做的 import 探针），那一处不是壳的接线。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import Iterable, List

PLUGIN = Path(__file__).resolve().parents[1]
KERNEL_DIRS = ("organs", "api")
SHELL = PLUGIN / "main.py"
TESTS_DIR = "tests"

# 拆开写，免得本文件自己成了下面两条扫描的命中项
_ASTRBOT = "ast" + "rbot"
_CORDIS = "cor" + "dis"
HOST_TOKENS = (_ASTRBOT, "@deepseek-ai/", _CORDIS)
HOST_IMPORTS = ("import " + _ASTRBOT, "from " + _ASTRBOT)


def _py_files(root: Path) -> List[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _is_test(path: Path) -> bool:
    return TESTS_DIR in path.relative_to(PLUGIN).parts


def _rel(path: Path) -> str:
    return path.relative_to(PLUGIN).as_posix()


def _hits(paths: Iterable[Path], needles: Iterable[str]) -> List[str]:
    """返回 "文件:行号: 该行" 形式的命中清单。"""
    found: List[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if any(needle in line for needle in needles):
                found.append("%s:%d: %s" % (_rel(path), number, line.strip()))
    return found


class KernelBoundaryTest(unittest.TestCase):
    def test_section_9_5_kernel_sources_carry_no_host_tokens(self):
        """核（organs/、api/）源码里不出现宿主字样；命中即失败。"""
        files = [path for name in KERNEL_DIRS
                 for path in _py_files(PLUGIN / name)
                 if not _is_test(path)]
        self.assertTrue(files, "没扫到任何核文件，扫描范围写错了")
        hits = _hits(files, HOST_TOKENS)
        self.assertEqual([], hits,
                         "§9.5 硬约束被破坏：核源码里出现了宿主字样（核不许 import 宿主，"
                         "宿主只经入站/出站/面板三条接口进来）：\n" + "\n".join(hits))

    def test_section_9_5_host_import_only_in_shell_main_py(self):
        """宿主 import 只许出现在壳文件 main.py；organ 或 api 里出现即失败。"""
        shell = _rel(SHELL)
        offenders = [_rel(path) for path in _py_files(PLUGIN)
                     if not _is_test(path) and _rel(path) != shell
                     and _hits([path], HOST_IMPORTS)]
        self.assertEqual([], offenders,
                         "§9.5 硬约束被破坏：宿主 import 只许出现在壳文件 %s，实际还出现在：%s"
                         % (shell, offenders))
        self.assertTrue(_hits([SHELL], HOST_IMPORTS),
                        "壳文件 %s 反而没有宿主 import——扫描口径或壳文件变了" % shell)


if __name__ == "__main__":
    unittest.main()
