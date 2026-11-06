"""仓库·台词卡：两档（口癖 ≤4 字 + 完整例句）——判据 docs/plan.md §3.14。

锚句管的是**口音**，不是**台词**：给模型整句示例它就照着念（参考实测复述率 22%~27%），
所以进提示词的只能是 ≤4 字的碎片，完整例句只作「她的口吻长什么样」的证据、不进提示词。

五张卡的 example 在这里是**验收基准**：这张测试就是「一个字别改」那句话的可执行形式，
所以原文只在本文件与 仓库/anchors.jsonl 两处出现，别处一律不许有
（test_verbatim_is_not_copied_elsewhere 把这条扫成断言）。
"""
from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.home import access, anchors, audit, rooms, store

FIVE = (
    {"id": "被夸", "phrase": "才知道啊", "example": "哼，才知道啊。",
     "when": "被夸，且不想直接承认高兴", "body": "尾巴翘起来晃了两下"},
    {"id": "被关心", "phrase": "行了行了", "example": "行了行了，我又不是玻璃做的。",
     "when": "被关心，且不想被当成弱者", "body": "别开头、脸微微发红"},
    {"id": "不想聊", "phrase": "以后再说", "example": "这个啊……以后再说吧。",
     "when": "不想继续聊，但要留门、不消失", "body": ""},
    {"id": "认错", "phrase": "记岔了", "example": "……是我记岔了。",
     "when": "发现自己记错了，直接认、不辩解", "body": "低头、尾巴垂下去不敢动"},
    {"id": "你要走", "phrase": "我在这儿", "example": "你忙你的，我在这儿。",
     "when": "他要走或要忙，她不拦、不索取，只留门", "body": "尾巴慢慢摆一下，乖巧的笑了笑"},
)
PACKAGE = Path(anchors.__file__).resolve().parent
PLUGIN = PACKAGE.parent.parent.parent


def make_home(base: Path) -> Path:
    root = base / "home"
    for name in rooms.ROOM_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def five() -> list:
    return [dict(card) for card in FIVE]


class AnchorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = make_home(self.base)

    # —— 两档：口癖与例句 ————————————————————————————

    def test_five_cards_read_back_verbatim(self) -> None:
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        got = anchors.read_all(root=self.root)
        self.assertEqual(5, got["total"])
        by_id = {card["id"]: card for card in got["anchors"]}
        for want in FIVE:
            have = by_id[want["id"]]
            self.assertEqual(want["example"], have["example"],
                             "example 一个字都不许改：%s" % want["id"])
            self.assertEqual(want["phrase"], have["phrase"], "phrase 也不许改")
            self.assertEqual(want["when"], have["when"])
            self.assertEqual(want["body"], have["body"])
            self.assertEqual("主人", have["who"])
            self.assertLessEqual(len(have["phrase"]), anchors.PHRASE_MAX,
                                 "进提示词的那档必须 ≤4 字")
            self.assertGreater(len(have["example"]), anchors.PHRASE_MAX,
                               "例句本来就该比口癖长；两档塌成一档就是没拆")

    def test_a_five_char_phrase_is_refused_with_its_length(self) -> None:
        """口癖超 4 字当场拒，消息里要写清字数。"""
        too_long = "才知道啊呀"
        self.assertEqual(5, len(too_long))
        with self.assertRaises(anchors.AnchorError) as caught:
            anchors.append_anchors(root=self.root, role="human", by="主人", anchors=[
                dict(FIVE[0], phrase=too_long)])
        self.assertEqual(anchors.CODE_BAD, caught.exception.code)
        self.assertIn("5 字", caught.exception.message)
        self.assertIn(str(anchors.PHRASE_MAX), caught.exception.message)
        self.assertEqual("PHRASE_TOO_LONG", caught.exception.detail.get("reason"))
        self.assertFalse(anchors.anchors_path(self.root).exists(), "拒了就不许落盘")

    def test_phrase_ceiling_is_four_not_five(self) -> None:
        for phrase in ("才知道啊", "行了行了", "记岔了", ""):
            with self.subTest(phrase=phrase):
                self.assertEqual(phrase, anchors.check_phrase(phrase))
        with self.assertRaises(anchors.AnchorError):
            anchors.check_phrase("才知道啊呀")

    def test_old_shape_row_reads_as_example_with_empty_phrase_and_a_warning(self) -> None:
        """老格式（只有一句话的行）→ example + phrase 留空 + warn。**不许猜 phrase。**"""
        path = anchors.anchors_path(self.root)
        store.ensure_dir(path.parent)
        path.write_text(json.dumps({"scene": "被夸", "say": "哼，才知道啊。",
                                    "body": "尾巴翘起来晃了两下",
                                    "who": "主人", "why": "",
                                    "ts": "2026-11-05T15:29:18+09:00"},
                                   ensure_ascii=False) + "\n", encoding="utf-8")
        got = anchors.read_all(root=self.root)
        self.assertEqual(1, got["total"])
        card = got["anchors"][0]
        self.assertEqual("哼，才知道啊。", card["example"], "整句读成 example")
        self.assertEqual("", card["phrase"], "phrase 留空——不猜口癖")
        self.assertEqual("被夸", card["id"], "没有 id 时按 when 归并")
        codes = {warning["code"] for warning in got["warnings"]}
        self.assertIn(anchors.WARN_LEGACY_SAY, codes)
        self.assertIn(anchors.WARN_NO_ID, codes)
        self.assertEqual(1, got["legacy_rows"])

    def test_old_field_names_are_taken_as_aliases_and_never_guess_the_phrase(self) -> None:
        """老字段当别名收下（别的线还在按老形状播种），但 **phrase 绝不猜**，而且不静默。"""
        outcome = anchors.append_anchors(root=self.root, role="human", by="主人", anchors=[
            {"scene": "被夸", "say": "哼，才知道啊。", "body": "尾巴翘起来晃了两下"}])
        self.assertEqual(["say", "scene"], outcome["legacy_fields"], "用了老字段要记出来")
        card = outcome["anchors"][0]
        self.assertEqual("哼，才知道啊。", card["example"], "say 落成 example")
        self.assertEqual("被夸", card["when"], "scene 落成 when")
        self.assertEqual("被夸", card["id"], "没有 id 时按 when 归并")
        self.assertEqual("", card["phrase"], "phrase 留空——不猜口癖")

    def test_new_shape_write_reports_no_legacy_fields(self) -> None:
        outcome = anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        self.assertEqual([], outcome["legacy_fields"])

    def test_id_is_the_stable_key_not_the_sentence(self) -> None:
        """改 when／改 example 都不改 id：按 id 取最新，不按句子。"""
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        moved = dict(FIVE[0], when="被夸得很直白，还是不想承认")
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=[moved])
        got = anchors.read_all(root=self.root)
        self.assertEqual(6, got["lines"], "改一次是追加一行")
        self.assertEqual(5, got["total"], "id 不变，还是五条")
        self.assertEqual("被夸得很直白，还是不想承认",
                         {card["id"]: card for card in got["anchors"]}["被夸"]["when"])

    # —— 只追加与落盘 ————————————————————————————————

    def test_change_appends_and_latest_wins(self) -> None:
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        changed = dict(FIVE[0], example=FIVE[0]["example"][:-1] + "？")
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=[changed])
        got = anchors.read_all(root=self.root)
        self.assertEqual(6, got["lines"], "改一句是追加一行，不是覆盖")
        self.assertEqual(5, got["total"], "现行台词仍是五条")
        latest = {card["id"]: card for card in got["anchors"]}["被夸"]
        self.assertEqual("哼，才知道啊？", latest["example"])
        rows, _bad = store.read_lines(anchors.anchors_path(self.root))
        self.assertEqual("哼，才知道啊。", rows[0]["example"], "旧的那句原样留着，不许被改写")

    def test_body_with_another_person_is_refused(self) -> None:
        for body in ("我给你倒了水", "看了主人一眼", "朝他点头", "脸红了"):
            with self.subTest(body=body):
                if body == "脸红了":
                    anchors.check_body(body)          # 只写自己：放行
                    continue
                with self.assertRaises(anchors.AnchorError) as caught:
                    anchors.append_anchors(root=self.root, role="human", by="主人",
                                           anchors=[dict(FIVE[0], body=body)])
                self.assertEqual(anchors.CODE_NO_SELF, caught.exception.code)
        self.assertFalse(anchors.anchors_path(self.root).exists(), "被拒的整批不许落盘")

    def test_missing_example_and_unknown_field_are_loud(self) -> None:
        card = dict(FIVE[0])
        card.pop("example")
        with self.assertRaises(anchors.AnchorError) as caught:
            anchors.append_anchors(root=self.root, role="human", by="主人", anchors=[card])
        self.assertEqual(anchors.CODE_BAD, caught.exception.code)
        bad = dict(FIVE[0], tone="撒娇")
        with self.assertRaises(anchors.AnchorError) as caught:
            anchors.append_anchors(root=self.root, role="human", by="主人", anchors=[bad])
        self.assertEqual(anchors.CODE_BAD, caught.exception.code)
        self.assertIn("tone", caught.exception.message)

    def test_audit_grows_one_line_per_card(self) -> None:
        outcome = anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        self.assertEqual(5, len(outcome["audit"]))
        entries = audit.read_tail(root=self.root, limit=50)
        self.assertEqual(5, len(entries))
        self.assertEqual([1, 2, 3, 4, 5], [entry.seq for entry in entries])
        for entry in entries:
            self.assertEqual("仓库", entry.room)
            self.assertEqual("主人", entry.by)
            self.assertTrue(entry.name.startswith("anchors.jsonl#"), entry.name)
            self.assertTrue(entry.at, "谁、哪间、何时、多大——何时不许空")
        self.assertTrue(audit.verify(root=self.root)["ok"])

    def test_whitelist_door_holds_her_back_but_not_organ(self) -> None:
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": []}), encoding="utf-8")
        with self.assertRaises(access.AccessDenied) as caught:
            anchors.append_anchors(root=self.root, role="agent", by="她", anchors=five())
        self.assertEqual("ROOM_READONLY", caught.exception.code)
        self.assertEqual("whitelist", caught.exception.detail.get("door"))
        self.assertFalse(anchors.anchors_path(self.root).exists(), "门挡下来就一行都不许落")
        outcome = anchors.append_anchors(root=self.root, role="organ", by="程序", anchors=five())
        self.assertEqual(5, outcome["count"], "organ 不过门二：她今天不能写，不等于这间放不了东西")

    def test_appending_never_rewrites_existing_lines(self) -> None:
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five()[:2])
        first = anchors.anchors_path(self.root).read_text(encoding="utf-8")
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five()[2:])
        second = anchors.anchors_path(self.root).read_text(encoding="utf-8")
        self.assertTrue(second.startswith(first), "只追加：后面那次写不许动前面一个字节")

    def test_no_new_room_is_created(self) -> None:
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        self.assertEqual(set(rooms.ROOM_NAMES), {p.name for p in self.root.iterdir() if p.is_dir()},
                         "台词卡不开新房间，房名表一字不改")
        self.assertTrue((self.root / "仓库" / "anchors.jsonl").exists())

    def test_count_only_answers_for_warehouse(self) -> None:
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        self.assertEqual(5, anchors.count(self.root, "仓库"))
        self.assertEqual(0, anchors.count(self.root, "工作台"), "别间不答，也不建空文件")

    def test_missing_file_is_not_an_error(self) -> None:
        got = anchors.read_all(root=self.root)
        self.assertFalse(got["file_exists"])
        self.assertEqual(0, got["lines"])
        self.assertEqual(0, got["total"])

    def test_seed_copy_carries_the_five(self) -> None:
        """出厂默认那份（seed 用）若在，必须是新格式的五条——防有人手改跑偏。"""
        rows, bad = anchors.read_seed()
        if not rows:
            self.skipTest("这台机器上没有出厂默认那份")
        self.assertEqual(0, bad)
        by_id = {card["id"]: card for card in anchors.latest_by_scene(rows)}
        self.assertEqual({card["id"] for card in FIVE}, set(by_id))
        for want in FIVE:
            self.assertEqual(want["example"], by_id[want["id"]]["example"])
            self.assertEqual(want["phrase"], by_id[want["id"]]["phrase"])
            self.assertLessEqual(len(by_id[want["id"]]["phrase"]), anchors.PHRASE_MAX,
                                 "出厂那份也不许带着超长口癖出厂")

    # —— 真源只有一个：实盘 ——————————————————————————————

    def test_default_read_goes_through_the_home_root(self) -> None:
        """读「她现在说什么」的路一律经 store.home_root() 解析出的根，不认任何写死的路径。"""
        self.assertEqual(store.home_root() / "仓库" / "anchors.jsonl", anchors.anchors_path())

    def test_live_copy_is_the_source_of_truth(self) -> None:
        """方向一：**改实盘** → 读跟着变；同时出厂那份一个字节都不许被碰。"""
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        seed_before = anchors.seed_path().read_bytes()
        changed = dict(FIVE[0], example=FIVE[0]["example"][:-1] + "？")
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=[changed])
        got = anchors.read_all(root=self.root)
        self.assertEqual(changed["example"],
                         {c["id"]: c for c in got["anchors"]}["被夸"]["example"],
                         "改了实盘，读出来的必须是新那句")
        self.assertEqual(seed_before, anchors.seed_path().read_bytes(),
                         "改实盘不许改到出厂那份")

    def test_seed_corruption_does_not_affect_reads(self) -> None:
        """方向二：**把出厂那份改坏** → 读实盘完全不受影响。

        不动代码树里那个文件：它被 git 跟踪，改它等于让并发的另一轮测试读到残局
        （这条踩过：一次被打断的运行把仓里的出厂 access.json 留成了空表）。
        改「出厂默认指向哪儿」证明的是同一件事——读路径根本不问 seed_path。
        """
        anchors.append_anchors(root=self.root, role="human", by="主人", anchors=five())
        broken = Path(self._tmp.name) / "broken-seed-anchors.jsonl"
        broken.write_text(json.dumps({"id": "被改坏的出厂默认", "phrase": "",
                                      "example": "这句不该被任何人读到"},
                                     ensure_ascii=False) + "\n", encoding="utf-8")
        with mock.patch.object(anchors, "seed_path", return_value=broken):
            got = anchors.read_all(root=self.root)
            self.assertEqual(5, got["total"])
            self.assertNotIn("被改坏的出厂默认", {c["id"] for c in got["anchors"]})
            self.assertEqual(FIVE[0]["example"],
                             {c["id"]: c for c in got["anchors"]}["被夸"]["example"])
            self.assertEqual(["被改坏的出厂默认"],
                             [c["id"] for c in anchors.latest_by_scene(anchors.read_seed()[0])],
                             "sanity: 被改坏的是出厂那份")

    def test_read_paths_never_touch_the_seed(self) -> None:
        """源码级：四个读函数里不许出现 seed_path／read_seed／写死的 "data"。"""
        for func in (anchors.anchors_path, anchors.read_all, anchors.latest_by_scene, anchors.count):
            source = inspect.getsource(func)
            for banned in ("seed_path", "read_seed", '"data"', "'data'"):
                self.assertNotIn(banned, source, "%s 里出现了 %s：读路径不许碰出厂那份"
                                 % (func.__name__, banned))

    def test_verbatim_is_not_copied_elsewhere(self) -> None:
        """「不复制原文到别处」：代码、模板、页面、文档里都不许出现这五句，只许留在台词库。"""
        needles = [card["example"] for card in FIVE] + [card["phrase"] for card in FIVE]
        targets = [p for p in PACKAGE.glob("*.py") if p.name != "anchors.py"]
        targets += list((PACKAGE / "templates").glob("*")) if (PACKAGE / "templates").is_dir() else []
        targets += list(PLUGIN.glob("api/*.py"))
        pages = PLUGIN / "pages"
        targets += list(pages.glob("*.html")) if pages.is_dir() else []
        docs = PLUGIN.parent / "docs"
        targets += list(docs.rglob("*.md")) if docs.is_dir() else []
        for path in targets:
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in needles:
                self.assertNotIn(needle, text, "台词原文被复制到了 %s" % path)


if __name__ == "__main__":
    unittest.main()
