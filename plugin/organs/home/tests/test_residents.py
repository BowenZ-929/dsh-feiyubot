"""三样特殊住户：抽屉只读、借阅条缺读数不报 0、仓库指针卡现场重算 sha256。

长相不在家：本文件只有一条反向断言（源码里不出现那句定性描述），没有任何长相字段。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.home import access, audit, cards, residents, rooms

ORGAN_DATA = Path(__file__).resolve().parents[1] / "data"
MOUTH_DATA = Path(__file__).resolve().parents[2] / "mouth" / "data"
HOME_SOURCES = ("home/cards.py", "home/residents.py", "home/templates/drawer-card.md")
# 码点写法：源码里连这串字都不留，测试照样钉得住「家里不复制它」（真源在魂的 identity.json）
APPEARANCE_TEXT = "\u84dd\u53d1\u5782\u8170"


class ResidentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.root = self.work / "home"
        self.root.mkdir()
        (self.root / "access.json").write_text(
            (ORGAN_DATA / "access.json").read_text(encoding="utf-8"), encoding="utf-8")

    # -------------------------------------------------------------- 1. 抽屉

    def test_drawer_missing_says_not_connected_with_template(self):
        """抽屉没接入：回 status=未接入 + 模板路径，不回空正文、不报 0 字。"""
        got = residents.read_drawer(root=self.root)
        self.assertEqual(got["status"], "未接入")
        self.assertTrue(got["readonly"])
        self.assertTrue(got["template"].endswith("templates/drawer-card.md"))
        self.assertIn("抽屉", got["path"])
        self.assertIsNone(got["body"])
        self.assertNotIn("chars", got)

    def test_drawer_reads_human_edited_file_without_cache(self):
        """人直接改盘，下一次调用就生效：只读、不校验、不缓存。"""
        path = self.root / "抽屉" / "她的资料卡.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 她的资料卡\n## 吃的\n面条\n", encoding="utf-8")
        first = residents.read_drawer(root=self.root)
        self.assertEqual(first["status"], "已接入")
        self.assertIn("面条", first["body"])
        self.assertGreater(first["chars"], 0)

        path.write_text("# 她的资料卡\n## 吃的\n米饭\n", encoding="utf-8")
        second = residents.read_drawer(root=self.root)
        self.assertIn("米饭", second["body"])
        self.assertNotIn("面条", second["body"])

    def test_drawer_is_refused_for_every_role(self):
        """抽屉只读对三个角色一律成立：结构门（十间分成可写五间／只读五间）先于白名单门，
        人改抽屉是盘上直接改文件，不走 API。

        白名单门只管 agent（plan.md「她能写哪几间」）；抽屉这一条属于结构门，
        所以 organ 与 human 也必须被拒。
        """
        whitelist = access.Whitelist(writable=frozenset(rooms.DEFAULT_WRITABLE), source="test")
        for role in ("agent", "organ", "human"):
            with self.subTest(role=role):
                with self.assertRaises(access.AccessDenied):
                    access.check_write(role=role, room="抽屉", name="她的资料卡.md", chars=10,
                                       root=self.root, whitelist=whitelist)

        # 对照：同一份白名单下，工作台（默认可写）对三个角色都能过
        for role in ("agent", "organ", "human"):
            access.check_write(role=role, room="工作台", name="x.md", chars=10,
                               root=self.root, whitelist=whitelist)

        with self.assertRaises(access.AccessDenied):
            cards.append_cards(root=self.root, role="organ", by="organ:eye", cards=[
                {"room": "抽屉", "kind": "物件", "name": "x.md", "chars": 3}])
        self.assertFalse((self.root / "抽屉" / "index.jsonl").exists())

    def test_artwork_and_asset_write_through_the_access_gate(self):
        """自画像与素材不许绕过门直接落盘：两张卡都要经过 access.check_write。"""
        art = self.work / "自画像.png"
        art.write_bytes(b"fake-png-bytes")
        real = access.check_write
        seen = []

        def spy(**kwargs):
            seen.append(kwargs.get("room"))
            return real(**kwargs)

        with mock.patch.object(access, "check_write", spy):
            residents.register_artwork(root=self.root, by="agent", params={"seed": 7}, path=str(art))
            residents.register_asset(root=self.root, by="human", path=str(art), note="画风")
        self.assertEqual(seen, ["工作台", "仓库"])

    def test_borrow_lands_in_card_box_and_creates_no_eleventh_room(self):
        """借阅条落名片盒（十间之内），而且没有长出第十一间「档案」。"""
        outcome = residents.register_borrow(
            root=self.root, role="human", by="主人", source="/外面/词库.json",
            version="v1", total=5, sha256="d" * 64, note="借来的")
        self.assertEqual(outcome["audit"].room, "名片盒")
        self.assertEqual(outcome["borrow"]["borrow_id"], "借-1")
        path = self.root / "名片盒" / "borrow.jsonl"
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)
        # 一条断言钉死「没有第十一间房」
        self.assertFalse((self.root / "档案").exists())
        # 名片盒的 index.jsonl 仍只放人卡：借阅条走独立的 borrow.jsonl
        self.assertFalse((self.root / "名片盒" / "index.jsonl").exists())
        self.assertEqual(residents.borrow_ledger(root=self.root)["room"], "名片盒")

    def test_borrow_agent_is_held_by_the_whitelist_door(self):
        """借阅条真的过房级门：白名单空集时 agent 被门二拒；同一份空表下 organ 放行。"""
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": []}), encoding="utf-8")
        args = {"root": self.root, "source": "/外面/词库.json", "version": "v1",
                "total": 5, "sha256": "d" * 64, "note": "借来的"}

        with self.assertRaises(access.AccessDenied) as caught:
            residents.register_borrow(role="agent", by="agent", **args)
        self.assertEqual(caught.exception.code, "ROOM_READONLY")
        self.assertEqual(caught.exception.detail["door"], "whitelist")
        self.assertEqual(caught.exception.detail["room"], "名片盒")
        self.assertFalse((self.root / "名片盒" / "borrow.jsonl").exists())

        organ = residents.register_borrow(role="organ", by="organ:eye", **args)
        self.assertEqual(organ["audit"].room, "名片盒")
        self.assertEqual(len((self.root / "名片盒" / "borrow.jsonl")
                             .read_text(encoding="utf-8").splitlines()), 1)
        self.assertFalse((self.root / "档案").exists())

    def test_borrow_char_limit_comes_from_the_gate_only(self):
        """字数上限只有门一处判定：这里不再手搓一遍，超了由 access 抛 CHARS_OVER。"""
        residents.register_borrow(root=self.root, role="human", by="主人",
                                  source="/外面/词库.json", version="v1", total=5,
                                  sha256="d" * 64, note="借来的")
        with self.assertRaises(access.AccessDenied) as caught:
            residents.register_borrow(root=self.root, role="human", by="主人",
                                      source="/外面/词库.json", version="v1", total=5,
                                      sha256="d" * 64,
                                      note="字" * (access.MAX_CHARS_PER_WRITE + 1))
        self.assertEqual(caught.exception.code, "CHARS_OVER")
        self.assertEqual(len((self.root / "名片盒" / "borrow.jsonl")
                             .read_text(encoding="utf-8").splitlines()), 1)

    def test_residents_has_no_code_path_that_writes_the_drawer(self):
        """residents.py 里凡是提到抽屉的行，都不许是写盘那几句；cards.py 根本不提抽屉。"""
        home = Path(__file__).resolve().parents[1]
        source = (home / "residents.py").read_text(encoding="utf-8")
        drawer_lines = [line for line in source.splitlines() if "DRAWER" in line]
        self.assertTrue(drawer_lines)
        for line in drawer_lines:
            for writer in ("append_line", "atomic_write_text", "append_cards", "write_text",
                           "os.replace"):
                self.assertNotIn(writer, line, line)
        self.assertNotIn("抽屉", (home / "cards.py").read_text(encoding="utf-8"))

    def test_nothing_written_into_drawer_by_residents(self):
        """把所有会写的动作跑一遍：抽屉里不该长出任何文件（资料卡只有人去放）。"""
        art = self.work / "自画像.png"
        art.write_bytes(b"fake-png-bytes")
        residents.register_artwork(root=self.root, by="agent", params={"seed": 7}, path=str(art))
        residents.register_asset(root=self.root, by="human", path=str(art), note="画风")
        residents.register_borrow(root=self.root, role="human", by="主人", source="/外面/词库.json",
                                  version="v1", total=10, sha256="c" * 64)
        residents.read_drawer(root=self.root)
        self.assertFalse((self.root / "抽屉").exists())

    def test_template_has_three_sections(self):
        """包里只放模板：模板不是房里那份文件，三节各留占位。"""
        template = Path(__file__).resolve().parents[1] / "templates" / "drawer-card.md"
        text = template.read_text(encoding="utf-8")
        for section in ("## 吃的", "## 怕的", "## 以前的事"):
            self.assertIn(section, text)
        self.assertNotIn(APPEARANCE_TEXT, text)

    # -------------------------------------------------------------- 2. 借阅条

    def test_borrow_without_readout_is_null_not_zero(self):
        """登记一条借阅：learned 为 null、readout_status 未接入；人塞一个数进去就读回那个数。"""
        outcome = residents.register_borrow(
            root=self.root, role="human", by="主人", source="/外面/黑话词库.json",
            version="v1", total=120, sha256="a" * 64, note="借来的词库")
        self.assertEqual(outcome["borrow"]["borrow_id"], "借-1")
        self.assertEqual(outcome["audit"].room, "名片盒")
        self.assertTrue((self.root / "名片盒" / "borrow.jsonl").is_file())

        ledger = residents.borrow_ledger(root=self.root)
        self.assertEqual(len(ledger["borrows"]), 1)
        self.assertIsNone(ledger["borrows"][0]["learned"])
        self.assertEqual(ledger["borrows"][0]["readout_status"], "未接入")
        self.assertEqual(ledger["borrows"][0]["total"], 120)
        self.assertIn("未接入", ledger["note"])
        # 借阅条不占 home-objects 的单件身份
        self.assertFalse((self.root / "仓库" / "index.jsonl").exists())
        self.assertFalse((self.root / "工作台" / "index.jsonl").exists())

        readout = self.root / "名片盒" / "借阅读数.json"
        readout.write_text(json.dumps(
            {"version": 1, "readouts": {"借-1": {"learned": 42, "at": "2026-11-03T00:00:00+08:00", "by": "主人"}}},
            ensure_ascii=False), encoding="utf-8")
        ledger = residents.borrow_ledger(root=self.root)
        self.assertEqual(ledger["borrows"][0]["learned"], 42)
        self.assertEqual(ledger["borrows"][0]["readout_status"], "已接入")
        self.assertEqual(residents.read_learned(root=self.root)["readouts"]["借-1"]["learned"], 42)

    def test_borrow_ledger_missing_everything(self):
        """柜子里一条借阅都没有：status=未接入，不出一个假 0。"""
        ledger = residents.borrow_ledger(root=self.root)
        self.assertEqual(ledger["status"], "未接入")
        self.assertEqual(ledger["borrows"], [])
        self.assertEqual(residents.read_learned(root=self.root)["status"], "未接入")

    def test_broken_readout_is_loud(self):
        """读数单坏了要响：文件在但读不出来，不许折成「没有读数」。"""
        card_box = self.root / "名片盒"
        card_box.mkdir(parents=True, exist_ok=True)
        (card_box / "借阅读数.json").write_text("{坏掉的", encoding="utf-8")
        with self.assertRaises(cards.CardError) as caught:
            residents.borrow_ledger(root=self.root)
        self.assertEqual(caught.exception.code, "READOUT_UNREADABLE")

    # —— 写读数：record_readout（这一轮加的）——————————————

    def _one_borrow(self) -> str:
        outcome = residents.register_borrow(
            root=self.root, role="organ", by="mouth", source="clauses.json", version="v1",
            total=8, sha256="b" * 64, note="嘴借去用的条款清单")
        return outcome["borrow"]["borrow_id"]

    def test_a_written_readout_is_read_back_and_flips_to_connected(self):
        """写一条读数之后：read_learned 读得到，borrow_ledger 那条从未接入变已接入。"""
        borrow_id = self._one_borrow()
        self.assertEqual("未接入", residents.borrow_ledger(root=self.root)["borrows"][0]["readout_status"])

        outcome = residents.record_readout(borrow_id, 3, role="organ", by="mouth",
                                           note="这一轮真用掉三条", root=self.root)
        self.assertEqual(borrow_id, outcome["readout"]["borrow_id"])
        self.assertEqual(3, outcome["readout"]["learned"])
        self.assertEqual("名片盒", outcome["audit"].room)
        self.assertEqual("借阅读数.json", outcome["audit"].name)

        # 落盘长这样：readouts[borrow_id] = {learned, note, at, by}
        raw = json.loads((self.root / "名片盒" / "借阅读数.json").read_text(encoding="utf-8"))
        self.assertEqual({"learned", "note", "at", "by"}, set(raw["readouts"][borrow_id]))
        self.assertEqual(3, raw["readouts"][borrow_id]["learned"])
        self.assertEqual("mouth", raw["readouts"][borrow_id]["by"])

        learned = residents.read_learned(root=self.root)
        self.assertEqual("已接入", learned["status"])
        self.assertEqual(3, learned["readouts"][borrow_id]["learned"])
        row = residents.borrow_ledger(root=self.root)["borrows"][0]
        self.assertEqual(3, row["learned"])
        self.assertEqual("已接入", row["readout_status"])

    def test_zero_is_a_real_readout_and_merges_instead_of_overwriting(self):
        """0 照写（0 是真读数，不是缺失），且只动自己那一条：表里别人的读数一个字节不变。"""
        first = self._one_borrow()
        second = residents.register_borrow(
            root=self.root, role="organ", by="mouth", source="policy.json", version="v1",
            total=4, sha256="c" * 64)["borrow"]["borrow_id"]
        residents.record_readout(first, 5, role="organ", by="mouth", root=self.root)
        residents.record_readout(second, 0, role="organ", by="mouth", root=self.root)

        readouts = residents.read_learned(root=self.root)["readouts"]
        self.assertEqual(5, readouts[first]["learned"])
        self.assertEqual(0, readouts[second]["learned"])
        rows = {row["borrow_id"]: row for row in residents.borrow_ledger(root=self.root)["borrows"]}
        self.assertEqual(0, rows[second]["learned"])
        self.assertEqual("已接入", rows[second]["readout_status"], "0 也算已接入，不许读成未接入")

    def test_bad_learned_or_empty_borrow_id_is_loud_and_names_the_field(self):
        """不合法当场抛、点名是哪一格，且一个字节都没写：不许静默改成 0。"""
        borrow_id = self._one_borrow()
        audit_before = len(audit.read_tail(root=self.root))
        bad_inputs = [("", 1), ("   ", 1), (borrow_id, True), (borrow_id, "3"),
                      (borrow_id, -1), (borrow_id, 1.5), (borrow_id, None)]
        for given_id, learned in bad_inputs:
            with self.assertRaises(cards.CardError) as caught:
                residents.record_readout(given_id, learned, role="organ", by="mouth",
                                         root=self.root)
            self.assertEqual("BAD_ARGUMENT", caught.exception.code, (given_id, learned))
            field = "learned" if str(given_id).strip() else "borrow_id"
            self.assertIn(field, caught.exception.detail, (given_id, learned))
        self.assertFalse((self.root / "名片盒" / "借阅读数.json").exists(), "抛了就不许留下半份读数")
        self.assertEqual(audit_before, len(audit.read_tail(root=self.root)),
                         "抛在不合法入参上，账也不该多记一行")

    def test_a_broken_readout_file_raises_instead_of_being_overwritten(self):
        """读数单坏了照既有口径响亮抛：不当作「还没有读数」，免得把一屋子读数覆盖成一条。"""
        borrow_id = self._one_borrow()
        card_box = self.root / "名片盒"
        (card_box / "借阅读数.json").write_text("{坏掉的", encoding="utf-8")
        with self.assertRaises(cards.CardError) as caught:
            residents.record_readout(borrow_id, 1, role="organ", by="mouth", root=self.root)
        self.assertEqual("READOUT_UNREADABLE", caught.exception.code)
        self.assertEqual("{坏掉的",
                         (card_box / "借阅读数.json").read_text(encoding="utf-8"))

    def test_writing_a_readout_goes_through_the_same_door_as_registering(self):
        """写权与 register_borrow 同一口径：门二把 agent 拦在外面，organ 放行。"""
        borrow_id = self._one_borrow()
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": []}), encoding="utf-8")
        with self.assertRaises(access.AccessDenied) as caught:
            residents.record_readout(borrow_id, 2, role="agent", by="agent", root=self.root)
        self.assertEqual("ROOM_READONLY", caught.exception.code)
        self.assertEqual("名片盒", caught.exception.detail["room"])
        self.assertFalse((self.root / "名片盒" / "借阅读数.json").exists())

        residents.record_readout(borrow_id, 2, role="organ", by="mouth", root=self.root)
        self.assertEqual(2, residents.read_learned(root=self.root)["readouts"][borrow_id]["learned"])

    # -------------------------------------------------------------- 3. 仓库指针卡

    def _mouth_copy(self) -> Path:
        target = self.work / "mouth"
        target.mkdir(exist_ok=True)
        for name in residents.MOUTH_FILES:
            shutil.copy2(str(MOUTH_DATA / name), str(target / name))
        return target

    def test_pointers_recompute_sha_and_report_stale(self):
        """三张指针卡各带 sha256；改一个字节后那一条 aligned=False + POINTER_STALE。"""
        mouth = self._mouth_copy()
        before_real = {name: hashlib.sha256((MOUTH_DATA / name).read_bytes()).hexdigest()
                       for name in residents.MOUTH_FILES}
        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(mouth)}):
            seeded = residents.seed_mouth_pointers(root=self.root)
            self.assertTrue(seeded["ok"])
            self.assertEqual(seeded["missing"], [])
            self.assertEqual(len(seeded["seeded"]), 3)

            healthy = residents.warehouse_pointers(root=self.root)
            self.assertEqual(healthy["total"], 3)
            self.assertTrue(healthy["aligned"])
            for pointer in healthy["pointers"]:
                self.assertEqual(len(pointer["sha256"]), 64)
                self.assertEqual(pointer["aligned"], True)
                self.assertTrue(pointer["path"].startswith(str(mouth)))

            target = mouth / "clauses.json"
            raw = bytearray(target.read_bytes())
            raw[-2] = raw[-2] ^ 0x01          # 只动一个字节，长度不变
            target.write_bytes(bytes(raw))

            stale = residents.warehouse_pointers(root=self.root)
            self.assertFalse(stale["aligned"])
            changed = [pointer for pointer in stale["pointers"] if pointer["code"] == "POINTER_STALE"]
            self.assertEqual([pointer["name"] for pointer in changed], ["clauses.json"])
            self.assertEqual(changed[0]["aligned"], False)

        # 真数据没被碰过
        for name in residents.MOUTH_FILES:
            self.assertEqual(hashlib.sha256((MOUTH_DATA / name).read_bytes()).hexdigest(),
                             before_real[name])

    def test_seed_reports_missing_files(self):
        """三份缺任一份：如实报缺哪份，不静默跳过。"""
        mouth = self._mouth_copy()
        (mouth / "policy.json").unlink()
        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(mouth)}):
            seeded = residents.seed_mouth_pointers(root=self.root)
        self.assertFalse(seeded["ok"])
        self.assertEqual(seeded["missing"], ["policy.json"])
        self.assertIn("policy.json", seeded["note"])
        self.assertEqual(len(seeded["seeded"]), 2)

    # -------------------------------------------------------------- 4. 自画像与素材

    def test_artwork_lands_in_desk_and_asset_in_warehouse(self):
        """画出来的图 → 工作台（记谁画的、什么参数）；画她的素材 → 仓库。"""
        art = self.work / "自画像-7.png"
        art.write_bytes(b"fake-png-bytes")
        outcome = residents.register_artwork(
            root=self.root, by="agent", params={"model": "m", "seed": 7, "size": "512x512"},
            path=str(art), note="第七号自画像")
        self.assertEqual(outcome["card"]["room"], "工作台")
        self.assertEqual(outcome["card"]["kind"], "自画像")
        self.assertEqual(outcome["card"]["by"], "agent")
        self.assertEqual(outcome["card"]["params"]["seed"], 7)
        self.assertEqual(outcome["card"]["path"], str(art))
        self.assertEqual([item["card_id"] for item in
                          cards.list_room(root=self.root, room="工作台")["items"]], ["工作台-1"])

        style = self.work / "画风参考.png"
        style.write_bytes(b"style-ref")
        asset = residents.register_asset(root=self.root, by="human", path=str(style), note="画风")
        self.assertEqual(asset["card"]["room"], "仓库")
        self.assertEqual(asset["card"]["kind"], "素材")
        self.assertEqual([item["kind"] for item in
                          cards.list_room(root=self.root, room="仓库")["items"]], ["素材"])

    def test_appearance_is_not_home_business(self):
        """长相不在家：params 或关键字里出现 appearance/look/长相，整条拒。"""
        art = self.work / "自画像.png"
        art.write_bytes(b"fake-png-bytes")
        with self.assertRaises(cards.CardError) as caught:
            residents.register_artwork(root=self.root, by="agent", params={"长相": "某个样子"},
                                       path=str(art))
        self.assertEqual(caught.exception.code, "NOT_HOME_BUSINESS")

        with self.assertRaises(cards.CardError) as caught:
            residents.register_artwork(root=self.root, by="agent", params={}, path=str(art),
                                       appearance="某个样子")
        self.assertEqual(caught.exception.code, "NOT_HOME_BUSINESS")
        self.assertFalse((self.root / "工作台" / "index.jsonl").exists())

    def test_missing_original_is_refused(self):
        """外面那张图不在就不登记：没有指纹的卡没意义。"""
        with self.assertRaises(cards.CardError) as caught:
            residents.register_asset(root=self.root, by="human", path=str(self.work / "没有.png"))
        self.assertEqual(caught.exception.code, "ASSET_MISSING")

    # -------------------------------------------------------------- 长相不在家

    def test_no_appearance_description_in_sources(self):
        """家的代码与模板都不许复制那句定性描述：长相的真源在魂。"""
        organs = Path(__file__).resolve().parents[2]
        for relative in HOME_SOURCES:
            text = (organs / relative).read_text(encoding="utf-8")
            self.assertNotIn(APPEARANCE_TEXT, text, relative)


if __name__ == "__main__":
    unittest.main()
