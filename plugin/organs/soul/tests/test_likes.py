"""喜欢什么（第七件）：identity.likes 必填、≤80 字硬线（建议 40）、进渲染、进合计预算。

两条判据级边界，测在这里：
- **不许自动增长**：不存在让她往 likes 里追加的程序路径。她要长新喜欢，写进 selfnarration
  与家·抽屉；魂里这一格只有 edit / migrate 两个人的入口。
- **不许写「喜欢主人」这类讨好**：那条写在判据里（docs/modules/soul.md），代码不做词表检测
  ——所以这里也不许出现任何讨好词表/过滤，别自己加。
"""
from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from typing import List

from organs.soul import checks, model, render
from organs.soul import store as store_module
from organs.soul.store import load
from organs.soul.tests.helpers import (copy_fixture, edit_card, proposal, read_bytes, read_json,
                                       write_json)

# 旧形状：没有 likes。migrate 是唯一能把这张卡换成新形状的路。
OLD_IDENTITY = {
    "self_names": {"display": "【待定稿】", "synonyms": []},
    "appearance": "【待定稿】",
    "self_statements": ["【待定稿】", "【待定稿】", "【待定稿】"],
    "fears": "【待定稿】",
}
# 人给定稿，逐字写入：正文只有这一句，不改写、不润色。
SETTLED_LIKES = "喜欢的食物：大白饭，也就是 token，尤其是背着主人偷吃。"

SOUL = Path(__file__).resolve().parents[1]
SKIP_DIRS = ("tests", "__pycache__", "fixtures")
# 「写 likes」的四种形状：下标赋值、属性赋值、自增、setattr。
# 自动增长必须先写进来，而写进来只剩这四条路——一条都不许有。
_LIKES_WRITE = re.compile(
    r"""\[["']likes["']\]\s*(?:=|\+=)"""
    r"""|\.likes\s*="""
    r"""|\blikes\s*\+="""
    r"""|setattr\([^)]*likes""")
_IDENTITY_CTOR = re.compile(r"\bIdentity\(")
_DEF = re.compile(r"^\s*def\s+(\w+)")


def codes(issues) -> list:
    return [issue.code for issue in issues]


def digest(path) -> str:
    return "sha256:" + hashlib.sha256(read_bytes(path)).hexdigest()


def _sources() -> List[Path]:
    return sorted(path for path in SOUL.rglob("*.py")
                  if not any(part in SKIP_DIRS for part in path.relative_to(SOUL).parts))


def _enclosing(lines: List[str], index: int) -> str:
    for back in range(index, -1, -1):
        matched = _DEF.match(lines[back])
        if matched:
            return matched.group(1)
    return ""


def _lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _likes_write_hits() -> List[str]:
    found: List[str] = []
    for path in _sources():
        for number, line in enumerate(_lines(path)):
            if "likes" in line and _LIKES_WRITE.search(line):
                found.append("%s:%d %s" % (path.name, number + 1, line.strip()))
    return found


def _identity_construction_sites() -> List[str]:
    found: List[str] = []
    for path in _sources():
        lines = _lines(path)
        for number, line in enumerate(lines):
            if _IDENTITY_CTOR.search(line):
                found.append("%s:%s" % (path.name, _enclosing(lines, number)))
    return found


class LikesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)

    def _rejected(self, mutate) -> list:
        edit_card(self.root, "identity", mutate)
        with self.assertRaises(model.SoulLoadError) as caught:
            load(self.root)
        return codes(caught.exception.issues)

    def test_missing_likes_degrades_with_the_placeholder(self):
        """缺这一格 → 降级载入：值换成占位，报 error 级但非阻塞的 Issue（码名不变）。"""
        edit_card(self.root, "identity", lambda data: data.pop("likes"))
        store = load(self.root)                       # 不抛：缺这一格不拦载入
        self.assertEqual(model.PLACEHOLDER, store.snapshot().identity.likes)
        found = [item for item in store.check() if item.code == "IDENTITY_LIKES_MISSING"]
        self.assertEqual(1, len(found), codes(store.check()))
        self.assertEqual(model.ERROR, found[0].level)
        self.assertFalse(found[0].blocking)
        self.assertIn("降级载入", found[0].message)
        self.assertNotIn("SOUL_JSON_UNREADABLE", codes(store.check()))   # 不连带报"七件之外"
        self.assertIn("【喜欢什么】" + model.PLACEHOLDER, store.render("熟人"))
        # 摘要里这一件照报字数，并把 Issue 带上
        self.assertIn("七件·喜欢什么 %d/80 字" % model.char_count(model.PLACEHOLDER),
                      "\n".join(checks.summary(self.root)))

    def test_blank_likes_degrades_with_the_placeholder(self):
        """空串与全空白都按缺这一格降级：不许拿空格顶一件。"""
        for blank in ("", "  \u3000 "):
            edit_card(self.root, "identity", lambda data: data.update({"likes": blank}))
            found = [item for item in store_module.read_blocks(self.root)[1]
                     if item.code == "IDENTITY_LIKES_MISSING"]
            self.assertEqual(1, len(found), repr(blank))
            self.assertFalse(found[0].blocking)
            self.assertEqual(model.PLACEHOLDER, load(self.root).snapshot().identity.likes)

    def test_eighty_one_chars_is_rejected(self):
        """≤80 是硬线：超一个字就 error 拒载，不截断、不 WARN。"""
        found = self._rejected(lambda data: data.update({"likes": "喜" * 81}))
        self.assertIn("IDENTITY_LIKES_TOO_LONG", found)

    def test_eighty_chars_passes(self):
        edit_card(self.root, "identity", lambda data: data.update({"likes": "喜" * 80}))
        self.assertEqual("喜" * 80, load(self.root).snapshot().identity.likes)

    def test_forty_one_chars_is_only_warned(self):
        """建议 40：超了只 WARN（与其它建议值同口径），卡照样载得进来。"""
        edit_card(self.root, "identity", lambda data: data.update({"likes": "喜" * 41}))
        _, issues = store_module.read_blocks(self.root)
        warned = [issue for issue in issues if issue.code == "SUGGESTED_BUDGET_EXCEEDED"]
        self.assertTrue(warned, codes(issues))
        self.assertEqual({"warn"}, {issue.level for issue in warned})
        self.assertEqual([], [issue for issue in issues if issue.level == model.ERROR])
        self.assertEqual("喜" * 41, load(self.root).snapshot().identity.likes)

    def test_render_puts_likes_between_fears_and_stance(self):
        edit_card(self.root, "identity", lambda data: data.update({"likes": SETTLED_LIKES}))
        lines = load(self.root).render("熟人").splitlines()
        self.assertEqual("【喜欢什么】" + SETTLED_LIKES, lines[4])
        heads = [line.split("】")[0] + "】" for line in lines]
        self.assertEqual(["【自称】", "【形象】", "【性格底色】", "【怕什么】", "【喜欢什么】",
                          "【待人姿态】", "【红线】"], heads)

    def test_likes_counts_toward_the_total_budget(self):
        edit_card(self.root, "identity", lambda data: data.update({"likes": "喜" * 80}))
        snapshot = load(self.root).snapshot()
        expected = (model.identity_name_chars(snapshot.identity)
                    + model.identity_statement_chars(snapshot.identity)
                    + model.char_count(snapshot.identity.appearance)
                    + model.fears_chars(snapshot.identity)
                    + 80
                    + model.manner_chars(snapshot.manner)
                    + model.bounds_chars(snapshot.bounds))
        self.assertEqual(expected, model.body_chars(snapshot.identity, snapshot.manner,
                                                    snapshot.bounds))
        self.assertEqual(expected, render.budget_used(snapshot))
        text = "\n".join(checks.summary(self.root))
        self.assertIn("七件·喜欢什么 80/80 字（硬线；建议 %d）" % model.limits.likes_suggested,
                      text)
        self.assertIn("七件合计 %d/%d 字（硬线）" % (expected, model.limits.body_total), text)

    def test_migrate_adds_the_field_and_check_passes(self):
        """形状变化只有 migrate 一条路：换完 check 无 error，渲染里有了这一行。"""
        write_json(self.root / "identity.json", OLD_IDENTITY)
        # 旧形状在新 model 下是「缺这一格」：降级载入、报非阻塞 error，migrate 仍是换形状的路
        self.assertEqual(model.PLACEHOLDER, load(self.root).snapshot().identity.likes)
        new_value = dict(OLD_IDENTITY, likes=SETTLED_LIKES)
        result = store_module.migrate(
            self.root, actor="bowen",
            reason="补 identity.likes：人给定稿（第七件）；不许自动增长、不许讨好",
            block="identity", new_value=new_value,
            expect_old_digest=digest(self.root / "identity.json"))
        self.assertTrue(result["ok"], result)
        row = store_module.read_history(self.root / "history.jsonl")[-1]
        self.assertEqual("migrate", row["kind"])
        self.assertEqual("bowen", row["actor"])
        self.assertEqual([], [item for item in checks.collect(self.root)
                              if item.level == model.ERROR])
        self.assertEqual(SETTLED_LIKES, read_json(self.root / "identity.json")["likes"])
        self.assertIn("【喜欢什么】" + SETTLED_LIKES, load(self.root).render("熟人"))
        self.assertIn("七件·喜欢什么 %d/80 字（硬线；建议 40）" % model.char_count(SETTLED_LIKES),
                      "\n".join(checks.summary(self.root)))

    def test_no_second_write_path_touches_likes(self):
        """除了 edit / migrate 两个人的入口，没有任何函数写 likes。

        三层都断：静态——源码里没有一处对 likes 赋值/追加（自动增长必须先写进来），
        Identity 也只在 model._shape_identity 由卡上的原文构造；动态——其余每一条写路径
        跑完，identity.json 的字节一字不动；最后两个入口确实是「人写的那一句」。
        """
        self.assertEqual([], _likes_write_hits(),
                         "魂出现了写 likes 的程序路径：这一格只许人写（edit / migrate），"
                         "她要长新喜欢就写进 selfnarration 与家·抽屉")
        self.assertEqual(["model.py:_shape_identity"], _identity_construction_sites(),
                         "Identity 只许由卡上的原文构造：第二处凭空造一个 Identity 就是第二只写的手")

        before = read_bytes(self.root / "identity.json")
        store = load(self.root)
        added = store.add_redline(actor="bowen", reason="加一条", item={
            "id": "b03", "rule": "不刷脏话", "match": {"type": "literal", "pattern": "脏话"},
            "action": "block", "active": True, "enforced_by": "soul·字面"})
        self.assertTrue(added["ok"], added)
        retired = store.retire_redline(actor="bowen", reason="撤一条", item_id="b03")
        self.assertTrue(retired["ok"], retired)
        accepted = store.accept_proposal(actor="bowen",
                                         proposal=proposal(value="慢慢说，先听完"))
        self.assertTrue(accepted["ok"], accepted)
        probed = store.acceptance(actor="bowen", reason="探一针", block="manner", path="stance",
                                  value="探一针", restore_value="新的姿态")
        self.assertTrue(probed["ok"], probed)
        self.assertEqual(before, read_bytes(self.root / "identity.json"),
                         "有人绕过 edit / migrate 改了 identity.json")

        edited = store.edit(actor="panel", reason="改一句喜欢", block="identity",
                            path="likes", value=SETTLED_LIKES)
        self.assertTrue(edited["ok"], edited)
        self.assertEqual("edit", edited["history_line"]["kind"])
        self.assertEqual("identity", edited["history_line"]["block"])
        self.assertEqual("likes", edited["history_line"]["path"])
        self.assertEqual(SETTLED_LIKES, read_json(self.root / "identity.json")["likes"])
        self.assertNotEqual(before, read_bytes(self.root / "identity.json"))


class RealCardLikesTest(unittest.TestCase):
    """真卡里的喜欢什么是人给的定稿：逐字写入，不改写、不加料。"""

    def test_real_identity_carries_the_settled_likes(self):
        data = read_json(SOUL / "data" / "identity.json")
        self.assertEqual(SETTLED_LIKES, data["likes"])
        self.assertLessEqual(model.char_count(data["likes"]), model.limits.likes_suggested)

    def test_the_real_ledger_records_the_migration(self):
        """这一格是 migrate 补进来的：账上留着一行，人写的那句在 reason 里。"""
        rows = store_module.read_history(SOUL / "data" / "history.jsonl")
        mine = [row for row in rows if row.get("block") == "identity"
                and row.get("kind") == "migrate"
                and "identity.likes" in (row.get("reason") or "")]
        self.assertTrue(mine, "真账里没有这次补字段的 migrate 行")
        self.assertEqual("bowen", mine[-1]["actor"])


if __name__ == "__main__":
    unittest.main()
