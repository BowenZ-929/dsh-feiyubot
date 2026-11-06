"""守卫：心的测试调用写型函数，必须自己给 root=。

为什么这条守卫要存在（2026-11-06 真实发生过一次）：
store.default_root() 的默认值就是**实盘** <repo>/home/organs/heart。测试里少写一个
root= 不会报错、不会变慢，只会安安静静把真人数据覆盖成假数据——那天 mood.json 与
proactive.json 就是这么被写成 2025-01-15 的测试态的。这种事不能靠"下次注意"兜：
守卫扫的是**源码**，不是运行结果，所以写成 root=None 显式传空也会被抓到。

扫什么：organans/heart/tests/*.py 里对下列写型函数的调用（注释与文档串天然不算，
因为扫的是语法树）：
    report_event report_reaction apply_event apply_daily settle_week deliver
    record gate sent outcome ingest attempt derive
判据分两步，缺一不可：
1. 被调函数真的收 root 形参（inspect.signature 现读，不是拿名单猜）——名单里哪个
   口子将来去掉 root 形参，这里自动不再要求，不会逼人写没用的关键字；
2. 这次调用既没给 root= 关键字，也没在 root 那个**位置**上给参数
   （apply_event(kind, self.root, …) 这种位置写法是合规的，不许误报）。
说不清的（*args / **kwargs 转发）不误报，但单独数出来：数不为 0 就红，
免得有人"转发一层"就绕过守卫。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TESTS_DIR = Path(__file__).resolve().parent

#: 写型函数名单（会往盘上写东西、且收 root 的那些口子）。
WRITE_CALLS = (
    "report_event", "report_reaction", "apply_event", "apply_daily", "settle_week",
    "deliver", "record", "gate", "sent", "outcome", "ingest", "attempt", "derive",
)
POSITIONAL_KINDS = (inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD)

MESSAGE = ("心的测试不许调用写型函数而不给 root=：默认根是实盘 "
           "<repo>/home/organs/heart，漏一次就覆盖真人数据（2026-11-06 已发生过一次）")


# ---- 扫描器 --------------------------------------------------------------


def _callee_name(node: ast.Call) -> Optional[str]:
    """调用目标的名字：module.func → func；裸名 → 名字本身。"""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _resolve(node: ast.Call, namespace: Dict[str, Any]) -> Optional[Any]:
    """把 module.func / from-import 的裸名解析成真对象；解析不了返回 None。"""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        base = namespace.get(func.value.id)
        return getattr(base, func.attr, None) if base is not None else None
    if isinstance(func, ast.Name):
        return namespace.get(func.id)
    return None


def _root_slot(callee: Any) -> Optional[int]:
    """root 在位置参数里排第几；被调对象不收 root（或读不出签名）→ None。"""
    try:
        params = list(inspect.signature(callee).parameters.values())
    except (TypeError, ValueError):
        return None
    positional = [param.name for param in params if param.kind in POSITIONAL_KINDS]
    return positional.index("root") if "root" in positional else None


def _unsure(node: ast.Call) -> bool:
    """*args / **kwargs 转发：这一刻说不清根落在哪儿，就不误报（由用例单独计数）。"""
    if any(isinstance(argument, ast.Starred) for argument in node.args):
        return True
    return any(keyword.arg is None for keyword in node.keywords)


def scan_source(text: str, filename: str,
                namespace: Optional[Dict[str, Any]] = None
                ) -> Tuple[List[Dict[str, Any]], int, int]:
    """扫一段源码，返回 (违规列表, 说不清的调用数, 真正检查过的写型调用数)。

    自测与真扫走同一条路径：守卫自己也得能被验，不然"扫了个寂寞"也是绿。
    """
    tree = ast.parse(text, filename)
    space: Dict[str, Any] = namespace or {}
    violations: List[Dict[str, Any]] = []
    unsure = 0
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        if name not in WRITE_CALLS:
            continue
        slot = _root_slot(_resolve(node, space))
        if slot is None:
            continue  # 这个口子此刻不收 root：不要求（名单不是猜的）
        checked += 1
        if _unsure(node):
            unsure += 1
            continue
        if any(keyword.arg == "root" for keyword in node.keywords):
            continue
        if len(node.args) > slot:
            continue  # root 走了位置参数，合规
        violations.append({
            "file": Path(filename).name,
            "line": node.lineno,
            "code": (ast.get_source_segment(text, node) or name).strip(),
            "why": "第 %d 个位置参数是 root，既没给 root= 也没给位置参数" % (slot + 1),
        })
    return violations, unsure, checked


def _namespace_for(path: Path) -> Dict[str, Any]:
    """测试模块的全局量：把 mood / sleep_module 这类别名解析成真对象。"""
    module_name = "organs.heart.tests." + path.stem
    try:
        return dict(importlib.import_module(module_name).__dict__)
    except Exception:  # noqa: BLE001 - 坏模块由"说不清"那一栏兜底
        return {}


def scan_tests() -> Tuple[List[Dict[str, Any]], int, int, int]:
    """扫全部测试文件：返回 (违规, 说不清, 文件数, 真正检查过的写型调用数)。"""
    violations: List[Dict[str, Any]] = []
    unsure = 0
    checked = 0
    files = sorted(TESTS_DIR.glob("*.py"))
    for path in files:
        text = path.read_text(encoding="utf-8")
        if not any(name + "(" in text for name in WRITE_CALLS):
            continue
        found, odd, seen = scan_source(text, str(path), _namespace_for(path))
        violations.extend(found)
        unsure += odd
        checked += seen
    return violations, unsure, len(files), checked


# ---- 用例 ----------------------------------------------------------------


class RootDisciplineTest(unittest.TestCase):
    """写型调用必须带 root=；守卫本身也要证明它抓得住、也放过合规写法。"""

    def test_no_write_call_without_root(self) -> None:
        violations, unsure, _files, checked = scan_tests()
        detail = "\n".join(
            "  %(file)s:%(line)d  %(code)s   → %(why)s" % row for row in violations)
        self.assertEqual([], violations, MESSAGE + "\n" + detail)
        self.assertEqual(
            0, unsure,
            "有 %d 处写型调用用 *args / **kwargs 转发，守卫看不清根落在哪儿——"
            "把 root= 明写出来" % unsure)
        self.assertGreater(checked, 50,
                           "只检查到 %d 条写型调用：守卫在空转，等于没有" % checked)

    def test_tests_are_actually_scanned(self) -> None:
        _violations, _unsure, files, checked = scan_tests()
        self.assertGreaterEqual(files, 15, "只扫到 %d 个测试文件，路径是不是错了" % files)
        self.assertGreater(checked, 0, "一个写型调用都没解析出来：签名解析坏了")

    def test_scanner_catches_a_missing_root(self) -> None:
        """守卫自证：不带 root 的写法必须被抓（这就是那天事故的写法）。"""
        from organs.heart import mood
        violations, unsure, checked = scan_source(
            'mood.apply_event("praise")\n'
            'mood.apply_event("praise", root=None)\n',
            "自测", {"mood": mood})
        self.assertEqual(2, checked)
        self.assertEqual(0, unsure)
        self.assertEqual(1, len(violations), violations)
        self.assertEqual(1, violations[0]["line"])
        self.assertIn("root", violations[0]["why"])

    def test_scanner_accepts_keyword_and_positional_root(self) -> None:
        """合规写法一个都不许误报：root= 关键字与位置写法都算给了根。"""
        from organs.heart import mood, sleep
        violations, unsure, checked = scan_source(
            'mood.apply_event("praise", root=somewhere)\n'
            'mood.apply_event("praise", self.root, now=at)\n'
            'sleep.record("asleep", somewhere, now=at)\n',
            "自测", {"mood": mood, "sleep": sleep})
        self.assertEqual(3, checked)
        self.assertEqual([], violations)
        self.assertEqual(0, unsure)

    def test_scanner_ignores_comments_docstrings_and_pure_calls(self) -> None:
        """注释、文档串、纯判决函数都不算：注释里写一句调用不该让人红。"""
        from organs.heart import engage, mood
        violations, unsure, checked = scan_source(
            '"""示例：mood.apply_event("praise") 这样是错的（这里只是文档串）。"""\n'
            "# mood.apply_event(\"praise\") 这行是注释\n"
            'engage.decide(judgement)\n',
            "自测", {"mood": mood, "engage": engage})
        self.assertEqual(0, checked, "注释与文档串里那句调用被当真了")
        self.assertEqual([], violations)
        self.assertEqual(0, unsure)

    def test_the_guard_notices_the_function_loses_its_root(self) -> None:
        """名单不是猜的：被调函数不收 root 就不再要求（免得逼人写没用的关键字）。"""
        from organs.heart import mood
        violations, _unsure, checked = scan_source(
            'mood.tier_of(58.0)\n', "自测", {"mood": mood})
        self.assertEqual(0, checked, "tier_of 不收 root，不该被算进名单")
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
