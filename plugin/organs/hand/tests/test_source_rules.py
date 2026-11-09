"""源码断言：白名单恰五个动作、「怎么说话」恰两个旋钮且没有魂的路径、没有命令执行与动态加载、
直接碰盘只在两个文件里、网络只在登记的三个文件里、**房名与格子名只能从家来**。

这些是**读源码**的断言，不是评审时眼睛看：改坏了就红。
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import organs.hand
from organs.home import cards as home_cards
from organs.home import residents as home_residents
from organs.home import rooms as home_rooms
from organs.hand import stylelang, whitelist

HAND_DIR = Path(organs.hand.__file__).resolve().parent
#: 只有这两个文件可以直接碰盘（open 写模式 / write_text / write_bytes / append_line）。
DISK_FILES = ("ledger.py", "store.py")
#: 通道、旧生图服务与新增的固定本机 ComfyUI；其他模块不能自行联网。
NETWORK_FILES = ("channel.py", "draw.py", "comfy.py")
#: 通用能力一个都不许长出来。
FORBIDDEN = ("subprocess", "os.system", "os.popen", "eval(", "exec(", "__import__",
             "importlib", "shutil")
#: 直接碰盘的写法。`urlopen(` 不算——那是网络，不是盘；其余一个都不许出现在别的文件里。
DISK_TOKENS = re.compile(r"(?<!url)open\((?!io\.BytesIO\()|os\.open|write_text|write_bytes|read_bytes|"
                         r"append_line|Path\.write|makedirs")
#: 网络的入口。只有 NETWORK_FILES 里能出现。
NETWORK_TOKENS = ("urllib", "socket", "http.client", "http.server", "requests", "httpx")
#: 只许 import 家与 paths：别的器官一个都不认识。
ALLOWED_ORGANS = ("home", "paths")


#: 家的房名与格子名：**从家 import**，断言里不另抄一串中文常量（抄一份就是第二处真源）。
HOME_NAMES = frozenset(home_rooms.ROOM_NAMES) | frozenset(home_cards.SHELF_KINDS)
#: 写盘的调用名。assets.py 只读：这些一个都不许出现。
WRITE_CALLS = frozenset({"write_text", "write_bytes", "write", "append_line", "land_draw",
                         "mkdir", "makedirs", "unlink", "replace", "rename", "copy",
                         "copy2", "copyfile", "link", "symlink_to"})
#: `open()` 的写模式。只读的那几个（r/rb）不在里面。
WRITE_MODES = frozenset({"w", "a", "x", "w+", "a+", "x+", "r+", "rb+", "wb", "ab", "xb",
                         "wb+", "ab+", "xb+", "rt+", "wt", "at"})


def sources():
    """本包里除 tests/ 之外的全部 .py。"""
    return sorted(path for path in HAND_DIR.glob("*.py"))


def string_literals(path: Path):
    """这个文件里**不是 docstring** 的字符串字面量：→ [(行号, 值)]。

    docstring 是散文（「她自己的资料卡」「仓库里只登记指针」这类话当然会提到房名），
    判据只管**代码里当值用的那些字面量**，所以先按 AST 把 docstring 那几个节点摘出去。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, "body", [])
        first = body[0] if body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            skip.add(id(first.value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in skip]


def calls_of(path: Path):
    """这个文件里的全部调用节点。"""
    return [node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)]


def callee(node: ast.Call) -> str:
    """被调用的名字：`open` 取 id，`path.write_text` 取最后一段。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def open_mode(node: ast.Call) -> str:
    """`open` 的模式：不给就是 "r"（读）。"""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        return str(node.args[1].value)
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return "r"


class WhitelistLiteralTest(unittest.TestCase):
    def test_actions_literal_holds_exactly_five_names_in_the_frozen_order(self):
        text = (HAND_DIR / "whitelist.py").read_text(encoding="utf-8")
        found = re.search(r"^ACTIONS: Tuple\[str, \.\.\.\] = \(([^)]*)\)", text, re.M)
        self.assertIsNotNone(found, "whitelist.py 里找不到 ACTIONS 那一行")
        names = tuple(part.strip().strip('"') for part in found.group(1).split(",") if part.strip())
        self.assertEqual(names,
                         ("add_task", "complete_task", "send_image", "draw", "set_style"))
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
    def test_comfy_requests_use_the_fixed_local_service(self):
        from unittest.mock import patch
        from organs.hand import comfy
        with patch.object(comfy.request, 'urlopen') as send:
            send.return_value.__enter__.return_value.read.return_value = b'{}'
            comfy._api('/queue')
        self.assertEqual('http://127.0.0.1:8188/queue', send.call_args.args[0].full_url)

    def test_only_registered_modules_can_reach_the_network(self):
        """ComfyUI 已接入；仍不允许未登记模块另开网络入口。"""
        self.assertEqual(("channel.py", "draw.py", "comfy.py"), NETWORK_FILES)
        for path in sources():
            if path.name in NETWORK_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            for bad in NETWORK_TOKENS:
                self.assertNotIn(bad, text, "%s 里出现了网络入口 %r" % (path.name, bad))

    def test_network_modules_are_the_ones_named_and_actually_use_urllib(self):
        for name in NETWORK_FILES:
            target = HAND_DIR / name
            self.assertTrue(target.is_file(), "%s 不在" % name)
            self.assertIn("urllib", target.read_text(encoding="utf-8"), name)


class DirectDiskTest(unittest.TestCase):
    def test_image_decoding_from_bytes_is_not_a_disk_open(self):
        self.assertIsNone(DISK_TOKENS.search('Image.open(io.BytesIO(raw))'))
        for source in ('Image.open(path)', 'open(path)', 'path.open("wb")', 'path.write_bytes(raw)'):
            self.assertIsNotNone(DISK_TOKENS.search(source))

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


class StyleSurfaceTest(unittest.TestCase):
    """`set_style` 只许改「怎么说话」：两个旋钮、没有魂的路径、没有通用配置写。

    这一组是**读源码**的断言：改坏了就红，不靠评审时眼睛看。
    """

    def test_style_keys_literal_is_exactly_the_two_frozen_knobs(self):
        text = (HAND_DIR / "whitelist.py").read_text(encoding="utf-8")
        self.assertIsNotNone(
            re.search(r"^STYLE_KEYS: Dict\[str, Tuple\[int, int\]\] = \{", text, re.M),
            "whitelist.py 里找不到 STYLE_KEYS 那一行")
        self.assertEqual({"chunks_per_reply": (1, 3), "max_chars": (10, 80)},
                         whitelist.STYLE_KEYS)
        self.assertEqual(2, len(whitelist.STYLE_KEYS), "可写的旋钮恰两个，多一个就是开了新口子")

    def test_stylelang_only_knows_the_two_knobs_and_identity_is_not_writable(self):
        self.assertEqual(set(whitelist.STYLE_KEYS),
                         {stylelang.CHUNKS_KEY, stylelang.CHARS_KEY})
        low, high = whitelist.STYLE_KEYS[stylelang.CHARS_KEY]
        self.assertTrue(low <= stylelang.SHORT_MAX_CHARS <= high, "「说短点」的档得落在范围里")
        self.assertNotIn(stylelang.IDENTITY_KEY, whitelist.STYLE_KEYS,
                         "身份不是可写的旋钮：它只能是一个「别往这边走」的信号")

    def test_no_spirit_paths_and_no_generic_config_writing(self):
        """魂的目录名、身份文件名、给它拼出来的路径——手里一个都不许出现。"""
        for path in sources():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("identity.json", text, "%s 里出现了魂的身份文件" % path.name)
            self.assertNotRegex(text, r"\bsoul\b", "%s 里出现了魂的目录名" % path.name)
        actions_text = (HAND_DIR / "actions.py").read_text(encoding="utf-8")
        self.assertIn("home_add_preference(", actions_text,
                      "改口吻只能写家的偏好卡这一条路")
        self.assertIn("home_preference_of(", actions_text,
                      "旧值只能从家的偏好卡读，不许自己再算一份")


class HomeNameLiteralsTest(unittest.TestCase):
    """取图的范围由**家的房表与格子表**决定：它们的名字只能从家 import，手里不许抄。

    这是走出**代码里的字符串字面量**（AST，跳过 docstring）的断言：有人把「抽屉」「自画像」
    当常量写进手里，这条就红——不是「我们会注意」。
    """

    def test_no_source_literal_is_a_room_name_or_a_shelf_name(self):
        self.assertTrue(HOME_NAMES, "家的房表／格子表读不出来，这条断言就是空转")
        for path in sources():
            for lineno, text in string_literals(path):
                self.assertNotIn(text, HOME_NAMES,
                                 "%s:%d 里出现了家的房名／格子名字面量 %r：要从家 import，"
                                 "不许在手里另立一份" % (path.name, lineno, text))

    def test_the_names_are_actually_imported_from_home(self):
        """光「没写字面量」还不够：那一层得真的**用**家的常量，否则只是碰巧没撞上。"""
        text = (HAND_DIR / "assets.py").read_text(encoding="utf-8")
        for needed in ("home_residents.WAREHOUSE_ROOM", "home_cards.SHELF_ROOM",
                       "home_cards.SHELF_KINDS"):
            self.assertIn(needed, text, "assets.py 取图要靠家的常量：%s" % needed)

    def test_the_shelf_room_is_home_s_own_drawer(self):
        """格子只长在抽屉里：那条「哪一间」也得是家定的那个值。"""
        self.assertEqual(home_cards.SHELF_ROOM, home_residents.DRAWER_ROOM)
        self.assertIn(home_residents.ARTWORK_KIND, home_cards.SHELF_KINDS,
                      "自画像得真的是抽屉的一格（否则「给我看自画像」没有落点）")


class AssetsReadOnlyTest(unittest.TestCase):
    """assets.py 只读：翻家、算指纹，**一个字节都不写**（不 copy、不落临时件）。"""

    def test_assets_never_writes_a_file(self):
        calls = calls_of(HAND_DIR / "assets.py")
        self.assertTrue(calls, "assets.py 里一个调用都没有？那这条断言没看住任何东西")
        for node in calls:
            name = callee(node)
            self.assertNotIn(name, WRITE_CALLS, "assets.py 只读：不许调用 %s" % name)
            if name == "open":
                mode = open_mode(node)
                self.assertNotIn(mode, WRITE_MODES,
                                 "assets.py 只读：不许 open(..., %r)" % mode)


if __name__ == "__main__":
    unittest.main()
