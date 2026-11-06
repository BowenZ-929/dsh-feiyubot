"""源码断言：白名单恰四个动作、没有命令执行与动态加载、直接碰盘只在两个文件里、
网络只在两个文件里。

这些是**读源码**的断言，不是评审时眼睛看：改坏了就红。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import organs.hand
from organs.hand import whitelist

HAND_DIR = Path(organs.hand.__file__).resolve().parent
#: 只有这两个文件可以直接碰盘（open 写模式 / write_text / write_bytes / append_line）。
DISK_FILES = ("ledger.py", "store.py")
#: 只有这两个文件可以上网（urllib / socket）。再长出一个能上网的文件 = 改这一条断言。
NETWORK_FILES = ("channel.py", "draw.py")
#: 通用能力一个都不许长出来。
FORBIDDEN = ("subprocess", "os.system", "os.popen", "eval(", "exec(", "__import__",
             "importlib", "shutil")
#: 直接碰盘的写法。`urlopen(` 不算——那是网络，不是盘；其余一个都不许出现在别的文件里。
DISK_TOKENS = re.compile(r"(?<!url)open\(|os\.open|write_text|write_bytes|read_bytes|"
                         r"append_line|Path\.write|makedirs")
#: 网络的入口。只有 NETWORK_FILES 里能出现。
NETWORK_TOKENS = ("urllib", "socket", "http.client", "http.server", "requests", "httpx")
#: 只许 import 家与 paths：别的器官一个都不认识。
ALLOWED_ORGANS = ("home", "paths")


def sources():
    """本包里除 tests/ 之外的全部 .py。"""
    return sorted(path for path in HAND_DIR.glob("*.py"))


class WhitelistLiteralTest(unittest.TestCase):
    def test_actions_literal_holds_exactly_four_names_in_the_frozen_order(self):
        text = (HAND_DIR / "whitelist.py").read_text(encoding="utf-8")
        found = re.search(r"^ACTIONS: Tuple\[str, \.\.\.\] = \(([^)]*)\)", text, re.M)
        self.assertIsNotNone(found, "whitelist.py 里找不到 ACTIONS 那一行")
        names = tuple(part.strip().strip('"') for part in found.group(1).split(",") if part.strip())
        self.assertEqual(names, ("add_task", "complete_task", "send_image", "draw"))
        self.assertEqual(whitelist.ACTIONS, names)

    def test_whitelist_has_no_dynamic_registration(self):
        text = (HAND_DIR / "whitelist.py").read_text(encoding="utf-8")
        for bad in ("def register", "ACTIONS.append", "ACTIONS +=", "setattr("):
            self.assertNotIn(bad, text)


class NoGeneralCapabilityTest(unittest.TestCase):
    def test_no_shell_no_dynamic_loading(self):
        for path in sources():
            text = path.read_text(encoding="utf-8")
            for bad in FORBIDDEN:
                self.assertNotIn(bad, text, "%s 里出现了 %r" % (path.name, bad))

    def test_only_home_and_paths_are_imported_from_organs(self):
        pattern = re.compile(r"^\s*(?:from|import)\s+organs\.(\w+)", re.M)
        for path in sources():
            text = path.read_text(encoding="utf-8")
            for name in pattern.findall(text):
                self.assertIn(name, ALLOWED_ORGANS, "%s 里 import 了 organs.%s" % (path.name, name))
            self.assertNotRegex(text, r"from\s+\.(heart|brain|soul|mouth|day)\b",
                                "%s 里 import 了别的器官" % path.name)


class NetworkTest(unittest.TestCase):
    def test_only_the_two_named_modules_can_reach_the_network(self):
        """网络只许在这两个文件里：别处出现 urllib／socket／http 就是又开了一个对外口子。"""
        self.assertEqual(("channel.py", "draw.py"), NETWORK_FILES)
        for path in sources():
            if path.name in NETWORK_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            for bad in NETWORK_TOKENS:
                self.assertNotIn(bad, text, "%s 里出现了网络入口 %r" % (path.name, bad))

    def test_the_two_network_modules_are_the_ones_named_and_actually_use_urllib(self):
        for name in NETWORK_FILES:
            target = HAND_DIR / name
            self.assertTrue(target.is_file(), "%s 不在" % name)
            self.assertIn("urllib", target.read_text(encoding="utf-8"), name)


class DirectDiskTest(unittest.TestCase):
    def test_only_ledger_and_store_touch_the_disk(self):
        for path in sources():
            if path.name in DISK_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            found = DISK_TOKENS.search(text)
            self.assertIsNone(found, "%s 里直接碰了盘：%r" % (path.name, found and found.group(0)))

    def test_the_two_disk_files_are_the_ones_named(self):
        self.assertTrue((HAND_DIR / "ledger.py").is_file())
        self.assertTrue((HAND_DIR / "store.py").is_file())

    def test_landing_bytes_goes_through_store(self):
        """图落的盘必须在 store.py 里那一个函数：draw.py 只拿字节，不自己写文件。"""
        self.assertIn("def land_draw(", (HAND_DIR / "store.py").read_text(encoding="utf-8"))
        self.assertIn("store.land_draw(",
                      (HAND_DIR / "actions.py").read_text(encoding="utf-8"))

    def test_no_real_qq_numbers_in_examples(self):
        pattern = re.compile(r"\b[1-9]\d{8,11}\b")
        for path in sources():
            text = path.read_text(encoding="utf-8")
            for hit in pattern.findall(text):
                self.assertTrue(hit.startswith("1000"), "%s 里像真号：%s" % (path.name, hit))


if __name__ == "__main__":
    unittest.main()
