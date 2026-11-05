"""嘴的冷却说法与禁语的回归：她说哪一句、面板那一句怎么拼，以及那三句系统腔不许再回来。

判据：

1. **冷却不是第四种拒绝**：say() 用的就是 data/refusal.json 里那三态说法之一——冷却这一路取
   data/cooldown.json 的 shape 指的那一句（现在是 refused-topic），台词一个字节都不在代码里。
   数据把 shape 改指另一句，这里就跟到另一句（断言只比「等于 refusal.say(...)」，不抄台词）。
2. **面板那一句全在数据里**：模板、两个单位（小时 / 分钟）、以及「多少分钟以上才按小时说」的分界
   都取自 data/cooldown.json。240 分钟说「4.0 小时」，30 分钟说「30.0 分钟」；把分界改到 600，
   240 分钟就该改口说分钟——断言跟着数据走，不写死一个数。
3. **缺键不补默认值**：没给数据表、表里缺一节、没在冷却却要一句拒绝，都要抛 CooldownBroken。
4. **纯**：cooldown.py 不读盘、不取时钟——用 ast 看它的 import 与调用，没有 time / datetime /
   random，也没有 open / load 这类入口（这条盯着的是「别哪天顺手在那儿读一次盘」）。
5. **禁语（跟额度那件事同源）**：plugin/ 与 docs/ 里那三句系统腔 0 命中。判据是「她自己说话的
   位置不许冒出系统腔」——所以这条扫的是我们**自己的树**，扫到就要么换成她的话、要么换成中性说法。

**为什么扫描显式排除 .scratch/refs**：那是外部参考仓（别人写的东西）的落地处，不是我们自己的树。
按我们自己的口径去扫别人的文案，只会逼着以后每次同步都去改人家的原话；排除写在代码里，不靠
「碰巧那儿没有」。docs/ 是根目录那一份（plugin/ 的上一级），所以两个根各扫一遍。

那三句禁语在源码里一律由**两截拼出来**（见 BANNED）：这一份测试文件自己也在被扫的树里，
写成一个整串就等于自己撞自己。同一条道理适用于**守卫**：organs/day/tests/test_plan.py 那条
assertNotIn 要叫得出整句禁语，字面量于是也拆成两截——源码里不留整串，运行时仍是整句；
_guarded_phrases 用 ast 把它的第一个参数算回原串，断言它没被拆弱成只禁半句。
"""
from __future__ import annotations

import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path

from organs.mouth import cooldown, refusal, warehouse

DATA = Path(__file__).resolve().parents[1] / "data"
SOURCE = Path(__file__).resolve().parents[1] / "cooldown.py"
BOOK = warehouse.load("cooldown")

PLUGIN = Path(__file__).resolve().parents[3]      # plugin/
REPO = PLUGIN.parent                              # 仓库根（docs/ 在这里）
TREES = ("plugin", "docs")
SKIP_DIRS = (".scratch", "__pycache__", ".git")   # .scratch/refs 是外部参考仓，不归我们管
TEXT_SUFFIXES = (".py", ".md", ".json", ".html", ".js", ".toml", ".txt",
                 ".yaml", ".yml", ".cfg", ".sh", ".css")

#: 三句禁语（两截拼，免得这份测试文件自己被扫描撞上）：系统在说话，不是她。
BANNED = ("额度" + "已满", "达到" + "上限", "超过" + "限制")


def _files() -> list:
    """要扫的那几棵树里的文本文件：只认自己的树，跳过缓存与外部参考仓。"""
    out = []
    for name in TREES:
        root = REPO / name
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(REPO).parts):
                continue
            out.append(path)
    return out


def _scan(paths) -> list:
    """这些文件里哪几行出现了禁语：[(路径, 行号, 那一句, 那一行原文)]，空清单才算干净。"""
    found = []
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, 1):
            for phrase in BANNED:
                if phrase in line:
                    found.append((str(path), number, phrase, line))
    return found


def _literal(node) -> str:
    """把一个「字符串常量，或若干个字符串常量相加」的表达式算成那个字符串；认不出就 None。

    只认常量与加号：不执行任何代码、也不 eval，足够读出「守卫点名的到底是哪一句」。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _guarded_phrases(path) -> list:
    """这份文件里 assertNotIn / assertNotRegex 的第一个参数点名的那些串（按出现顺序，去重）。"""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name not in ("assertNotIn", "assertNotRegex"):
            continue
        said = _literal(node.args[0])
        if said is not None and said not in out:
            out.append(said)
    return out


class SayTest(unittest.TestCase):
    def test_it_is_the_refusal_word_not_a_new_line(self):
        """冷却里那一句就是 refusal 的数据台词：嘴这边一个新文案都不造。"""
        self.assertEqual(refusal.say("refused-topic"),
                         cooldown.say("医院", {"cooling": True}, BOOK))

    def test_it_follows_the_shape_in_the_data(self):
        """数据把 shape 改指另一句，这里就跟到另一句——用哪一句是数据的事。"""
        book = copy.deepcopy(BOOK)
        book["shape"] = "refused-mood"
        self.assertEqual(refusal.say("refused-mood"),
                         cooldown.say("医院", {"cooling": True}, book))

    def test_no_cooling_means_no_refusal_line(self):
        """没在冷却却要一句拒绝，就是凭空拒绝：当场抛，不编一句顶上。"""
        with self.assertRaises(cooldown.CooldownBroken):
            cooldown.say("医院", {"cooling": False}, BOOK)
        with self.assertRaises(cooldown.CooldownBroken):
            cooldown.say("医院", False, BOOK)


class LineTest(unittest.TestCase):
    def test_hours_and_minutes_are_written_the_way_the_data_says(self):
        """240 分钟按小时说、30 分钟按分钟说；小数位与两个字都在数据的单位模板里。"""
        line = cooldown.line("医院", 240.0, "refused-topic", BOOK)
        self.assertIn("医院", line)
        self.assertIn("refused-topic", line)
        self.assertIn("4.0 小时", line)
        self.assertIn("30.0 分钟", cooldown.line("医院", 30.0, "refused-topic", BOOK))

    def test_the_threshold_comes_from_the_data(self):
        """把分界改到 600 分钟，240 分钟就该改口按分钟说——分界不是写死在代码里的 60。"""
        book = copy.deepcopy(BOOK)
        book["line"]["hours_from_min"] = 600.0
        self.assertIn("240.0 分钟", cooldown.line("医院", 240.0, "refused-topic", book))

    def test_zero_and_negative_never_read_as_negative(self):
        """心给的数只会是正的，但真来了负数就夹到 0：一句「还剩 -0.0 分钟」不能出门。"""
        self.assertIn("0.0 分钟", cooldown.line("医院", -5, "refused-topic", BOOK))

    def test_missing_bits_break_loudly(self):
        """缺表、缺节、缺话题、给的不是数：一律抛 CooldownBroken（缺键不补默认值）。"""
        with self.assertRaises(cooldown.CooldownBroken):
            cooldown.line("医院", 10.0, "refused-topic", {})
        with self.assertRaises(cooldown.CooldownBroken):
            cooldown.line("医院", 10.0, "refused-topic", {"shape": "refused-topic"})
        with self.assertRaises(cooldown.CooldownBroken):
            cooldown.line("", 10.0, "refused-topic", BOOK)
        with self.assertRaises(cooldown.CooldownBroken):
            cooldown.line("医院", None, "refused-topic", BOOK)
        with self.assertRaises(cooldown.CooldownBroken):
            cooldown.line("医院", True, "refused-topic", BOOK)


class NoteTest(unittest.TestCase):
    def test_the_three_notes_come_from_the_data(self):
        """三种情形各一句注记，原样取自数据（冷却中 / 没在冷却 / 认不出话题）。"""
        notes = BOOK["notes"]
        for key in ("cooling", "clear", "no_topic"):
            self.assertEqual(notes[key], cooldown.note(BOOK, key))
        self.assertIn("冷却", cooldown.note(BOOK, "cooling"))

    def test_an_unknown_note_is_named(self):
        """认不出的键当场抛并指名，不猜一条顶上去。"""
        with self.assertRaises(cooldown.CooldownBroken):
            cooldown.note(BOOK, "no_such_note")
        with self.assertRaises(cooldown.CooldownBroken):
            cooldown.note({"shape": "refused-topic"}, "cooling")


class PurityTest(unittest.TestCase):
    def test_it_imports_no_clock_and_no_disk(self):
        """纯函数：不 import 时钟、随机数，也不碰任何读盘入口（这一条盯着「别哪天顺手读一次盘」）。"""
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                modules.add(node.module or "")
        self.assertEqual({"__future__", "typing"}, modules,
                         "cooldown.py 只许 import 这两样（加一个大 import，纯就没了）")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            self.assertNotIn(name, ("open", "load", "read_text", "write_text", "now"),
                             "cooldown.py 里出现了 %r：本文件不读盘、也不取时钟" % (name,))


class BannedPhrasesAreGoneTest(unittest.TestCase):
    def test_the_detector_is_not_dead(self):
        """先证明扫描真的会撞上：同一个探测器对一句种下的禁语必须报出来，也要报出行号。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "planted.md"
            path.write_text("第一行干净。\n这一行里藏着：" + BANNED[0] + "。\n", encoding="utf-8")
            rows = _scan([path])
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0][1], "要报出行号，人一眼找得到")
        self.assertEqual(BANNED[0], rows[0][2], "报出来的还得是那一句本身")

    def test_the_day_guard_still_bites_with_the_phrase_spelled_from_parts(self):
        """day 那条守卫仍然生效：字面量拆成两截之后，它点名的仍**是整句**（不是两个半句）。

        这一条盯的是「拆字面量」最怕的那个后果——守卫被拆弱成只禁「额度」或只禁「已满」，
        于是拼起来的那一句反而漏过去。这里用 ast 把 assertNotIn 的第一个参数算回原串（只认
        常量与加号，不执行任何代码），要求它等于整句禁语。
        """
        path = PLUGIN / "organs" / "day" / "tests" / "test_plan.py"
        self.assertIn(BANNED[0], _guarded_phrases(path),
                      "那条守卫不再点名整句禁语了：拆字面量不许把守卫拆弱")

    def test_the_scan_reaches_our_own_tree(self):
        """扫的确实是我们的树：心那两份文件在名单里，外部参考仓不在。"""
        files = _files()
        self.assertGreater(len(files), 100, "扫到的文件太少，说明根找错了")
        self.assertIn(PLUGIN / "organs" / "heart" / "refuse.py", files)
        self.assertIn(REPO / "docs" / "modules" / "hand.md", files)
        self.assertFalse([one for one in files
                          if ".scratch" in one.relative_to(REPO).parts],
                         ".scratch 是外部参考仓，扫描必须显式排除它")

    def test_no_system_line_survives_in_our_tree(self):
        """那三句系统腔在自己的树里 0 命中——**没有一地例外**。

        真要拒，用她自己的话，或者这一次不响、只记账；换成「配额耗尽」「触及限额」这种另一种
        系统腔不算改好——判据是「她嘴里不该出现系统话」，不是「换一批系统词」。
        守卫要指名那句话，就把字面量拆成两截（organs/day/tests/test_plan.py 就是这么写的）：
        运行时仍是整句，源码里不留整串，于是这条「0 命中」说得干净。
        """
        rows = _scan(_files())
        self.assertEqual([], rows,
                         "自己的树里还有系统腔（换成她的话或中性说法）：%r" % (rows,))


if __name__ == "__main__":
    unittest.main()
