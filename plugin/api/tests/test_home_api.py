"""家（/home*）的 HTTP 层：十条路由、两道门的拒绝如实翻成 403、审计变少只报不修。

覆盖 api/home.py 与 api/core.py 的接线：路由注册表、错误码映射、未接入、405、
纯说明卡与缺件的如实答案、以及两条硬约束的源码自查（本层零文件权限判断、不补审计）。

每个用例自己造一个临时家（十间齐 + 一份白名单），真 data/ 一律不碰。
白名单经 HOME_ACCESS_FILE 指过去的那条路径也在这里被验：改文件立刻生效（不缓存）。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from api import api
from api import core as api_core
from api import home as home_api
from organs.home import access, audit, cli, residents, rooms, store

# 可写哪几间**从房表派生**，不写字面量：加一间房不该改这里。
FIVE = tuple(room.name for room in rooms.ROOMS if room.writable_default)
READONLY = ("客厅", "卧室", "台历", "橱柜", "抽屉")
SOURCE = Path(home_api.__file__).read_text(encoding="utf-8")
DRAWER_CARD = "她的资料卡.md"


def make_home(base: Path) -> Path:
    """造一个十间齐全的空家（不写白名单：由用例自己决定）。"""
    root = base / "home"
    for name in rooms.ROOM_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def write_access(path: Path, writable) -> Path:
    """写一份白名单配置；version 固定 1（access.ACCESS_VERSION）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "writable": list(writable), "note": "测试用"}
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


class HomeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = make_home(self.base)
        write_access(self.root / "access.json", FIVE)
        # 环境变量是进程级的：进来什么样子，出去还原成什么样子（别的测试不该被这份白名单影响）
        self._env = os.environ.get(access.ENV_ACCESS)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._env is None:
            os.environ.pop(access.ENV_ACCESS, None)
        else:
            os.environ[access.ENV_ACCESS] = self._env

    # —— 小工具 ————————————————————————————————————————

    def call(self, method: str, path: str, body=None, query=None):
        """经 core.handle 打一条路由（家挂在 handle 上，不是直接调 dispatch）。"""
        return api.handle(method, path, body, self.root, query=query)

    def write_card(self, role: str = "agent", room: str = "工作台", name: str = "一张画.md",
                   **extra):
        """往一间房登记一张卡；顶层 room 会落到卡上。"""
        card = {"kind": "物件", "name": name, "chars": 0}
        card.update(extra)
        return self.call("POST", "/home/write",
                         {"role": role, "by": "测试", "room": room, "cards": [card]})

    # —— 1 路由注册 ————————————————————————————————————

    def test_ten_routes_registered_with_expected_methods(self):
        """十条路由都进了 ROUTES，方法与表一致（核心表是唯一真源）。"""
        home = {route: method for route, method in api_core.ROUTES.items()
                if route.startswith("/home")}
        self.assertEqual(home, home_api.HOME_ROUTES)
        self.assertEqual(len(home), 15)
        for route in ("/home/write", "/home/anchors/append", "/home/consume", "/home/progress",
                      "/home/ingest"):
            self.assertEqual(home[route], "POST")
        for route in ("/home/rooms", "/home/access", "/home/list", "/home/read", "/home/audit",
                      "/home/audit/verify", "/home/drawer", "/home/archive/borrow",
                      "/home/warehouse", "/home/anchors"):
            self.assertEqual(home[route], "GET")

    # —— 2 房表与白名单 ————————————————————————————————

    def test_rooms_answers_structure_and_whitelist_in_two_columns(self):
        """结构可写与她现在可写分两栏：默认白名单是五间全集，只读五间两栏都是 false。"""
        status, payload = self.call("GET", "/home/rooms")
        self.assertEqual(status, 200, payload)
        rows = {row["name"]: row for row in payload["rooms"]}
        self.assertEqual(len(rows), len(rooms.ROOMS))
        self.assertEqual([name for name, row in rows.items() if row["writable_by_structure"]],
                         list(FIVE))
        self.assertEqual([name for name, row in rows.items() if row["writable_now"]], list(FIVE))
        for name in READONLY:
            self.assertFalse(rows[name]["writable_by_structure"])
            self.assertFalse(rows[name]["writable_now"])
        self.assertTrue(payload["writable_now_source"].startswith("root:"))
        self.assertIsNone(rows["玄关"]["cap"])
        self.assertEqual(rows["仓库"]["cap"], access.ROOM_CAP["仓库"])
        self.assertEqual(rows["仓库"]["headroom"], access.ROOM_CAP["仓库"])

    def test_access_default_is_five_rooms_and_says_which_level(self):
        """默认配置：五间全集、source 说得出是 home_root() 那一级、default 与只读五间都在。"""
        status, payload = self.call("GET", "/home/access")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["writable"], sorted(FIVE))
        self.assertEqual(payload["default"], sorted(FIVE))
        self.assertEqual(payload["readonly_rooms"], sorted(READONLY))
        self.assertEqual(payload["source_kind"], "root")
        self.assertEqual(payload["file"], str(self.root / "access.json"))
        self.assertEqual(payload["env"], access.ENV_ACCESS)

    def test_access_file_env_override_is_read_every_call(self):
        """HOME_ACCESS_FILE 指过去就换：/home/access 立刻变（不缓存），房表的结构那一栏不动。"""
        env_file = write_access(self.base / "only-desk.json", ["工作台"])
        os.environ[access.ENV_ACCESS] = str(env_file)

        status, payload = self.call("GET", "/home/access")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["writable"], ["工作台"])
        self.assertEqual(payload["source_kind"], "env")
        self.assertEqual(payload["file"], str(env_file))

        status, table = self.call("GET", "/home/rooms")
        rows = {row["name"]: row for row in table["rooms"]}
        self.assertEqual([name for name, row in rows.items() if row["writable_by_structure"]],
                         list(FIVE))
        self.assertEqual([name for name, row in rows.items() if row["writable_now"]], ["工作台"])

        write_access(env_file, [])            # 同一份文件改写一遍：下一次请求立刻照新的算
        status, payload = self.call("GET", "/home/access")
        self.assertEqual(payload["writable"], [])

    # —— 3 写 → 审计 ———————————————————————————————————

    def test_write_appends_one_audit_line_and_verify_stays_ok(self):
        """往工作台放一张卡：200、审计多一行、verify ok=true。"""
        before = audit.verify(self.root)
        status, payload = self.write_card(name="画.md")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["rooms"], ["工作台"])
        self.assertEqual([row["room"] for row in payload["audit"]], ["工作台"])

        after = audit.verify(self.root)
        self.assertEqual(after["lines"], before["lines"] + 1)
        self.assertTrue(after["ok"])

        status, tail = self.call("GET", "/home/audit")
        self.assertEqual(status, 200, tail)
        self.assertEqual(tail["count"], 1)
        self.assertEqual(tail["entries"][0]["room"], "工作台")
        self.assertEqual(tail["entries"][0]["seq"], 1)

        status, checked = self.call("GET", "/home/audit/verify")
        self.assertEqual(status, 200, checked)
        self.assertTrue(checked["ok"])
        self.assertFalse(checked["repaired"])

    def test_audit_shrunk_is_reported_not_repaired(self):
        """手工截掉最后一行：verify 回 409 AUDIT_SHRUNK，账与高水位一个字节都没被改写。"""
        self.assertEqual(self.write_card()[0], 200)
        path = self.root / audit.AUDIT_NAME
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-1]) + ("\n" if len(lines) > 1 else ""),
                        encoding="utf-8")
        kept = path.read_text(encoding="utf-8")
        mark_path = self.root / audit.HIGH_WATER_NAME
        mark_before = mark_path.read_text(encoding="utf-8")

        status, payload = self.call("GET", "/home/audit/verify")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], audit.CODE_SHRUNK)
        self.assertIn("只报不修", payload["message"])
        self.assertFalse(payload["repaired"])
        self.assertEqual(path.read_text(encoding="utf-8"), kept)      # 没有被补回
        self.assertEqual(mark_path.read_text(encoding="utf-8"), mark_before)

    def test_audit_tail_limit_defaults_and_caps(self):
        """审计尾：默认 20 条，上限 200 条（多报的夹到 200，不报错）。"""
        for index in range(3):
            self.assertEqual(self.write_card(name="第%d张.md" % index)[0], 200)
        status, tail = self.call("GET", "/home/audit")
        self.assertEqual(tail["limit"], 20)
        self.assertEqual(tail["count"], 3)
        status, tail = self.call("GET", "/home/audit", query={"limit": "600"})
        self.assertEqual(tail["limit"], 200)
        status, payload = self.call("GET", "/home/audit", query={"limit": "x"})
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "BAD_ARGUMENT")

    # —— 4 门一：结构门 ————————————————————————————————

    def test_drawer_write_is_refused_for_every_role_by_structure(self):
        """只读五间对三个角色一律拒，detail.door 是 structure（这间房从不放东西）。"""
        for role in access.ROLES:
            status, payload = self.write_card(role=role, room="抽屉", name="资料卡.md")
            self.assertEqual(status, 403, (role, payload))
            self.assertEqual(payload["code"], "ROOM_READONLY")
            self.assertEqual(payload["detail"]["door"], "structure")
            self.assertEqual(payload["door"], "structure")
        self.assertFalse((self.root / "抽屉" / "index.jsonl").exists())

    # —— 5 房名 ————————————————————————————————————————

    def test_unknown_room_is_400_with_nearest(self):
        """房名给「厨房」：400 ROOM_MISS，body 带一间真实房名（人要能直接点开那一间）。"""
        status, payload = self.write_card(room="厨房", name="x.md")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "ROOM_MISS")
        self.assertIn(payload["nearest"], rooms.ROOM_NAMES)
        self.assertEqual(payload["nearest"], payload["detail"]["nearest"])

        status, payload = self.call("GET", "/home/list", query={"room": "厨房"})
        self.assertEqual(status, 400, payload)
        self.assertIn(payload["nearest"], rooms.ROOM_NAMES)

    # —— 6 门二：白名单门 ——————————————————————————————

    def test_whitelist_door_refuses_agent_but_not_organ(self):
        """白名单空集：agent 写工作台 403（door=whitelist），同一批 organ 写 200。"""
        write_access(self.root / "access.json", [])
        status, payload = self.write_card(role="agent", name="画.md")
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "ROOM_READONLY")
        self.assertEqual(payload["detail"]["door"], "whitelist")
        self.assertEqual(payload["detail"]["source"], "root:" + str(self.root / "access.json"))

        status, payload = self.write_card(role="organ", name="画.md")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["count"], 1)

    def test_role_unknown_is_403(self):
        """角色不认识 → 403 ROLE_UNKNOWN（门二只认那三个角色）。"""
        status, payload = self.write_card(role="system", name="画.md")
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "ROLE_UNKNOWN")

    # —— 7 抽屉 ————————————————————————————————————————

    def test_drawer_without_card_reports_unconnected_and_never_zero_chars(self):
        """抽屉缺件：200 + status=未接入 + template，body=null，不报 0 字、不编内容。"""
        status, payload = self.call("GET", "/home/drawer")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "未接入")
        self.assertIsNone(payload["body"])
        self.assertEqual(payload["template"], residents.DRAWER_TEMPLATE)
        self.assertNotIn("chars", payload)
        self.assertTrue(payload["readonly"])
        self.assertFalse((self.root / "抽屉" / DRAWER_CARD).exists())

    def test_drawer_with_card_reports_body_and_chars(self):
        """抽屉里有卡：如实回状态、正文与字数（程序不写它，是人直接放的）。"""
        target = self.root / "抽屉" / DRAWER_CARD
        target.write_text("# 她的资料卡\n\n怕黑\n", encoding="utf-8")
        status, payload = self.call("GET", "/home/drawer")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "已接入")
        self.assertEqual(payload["body"], "# 她的资料卡\n\n怕黑\n")
        self.assertEqual(payload["chars"], len(payload["body"]))

    # —— 8 借阅条 ——————————————————————————————————————

    def test_borrow_ledger_names_the_card_room_and_keeps_learned_null(self):
        """借阅条账：room 是名片盒；缺读数 learned=null、readout_status=未接入（不报 0）。"""
        status, payload = self.call("GET", "/home/archive/borrow")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["room"], "名片盒")
        self.assertEqual(payload["status"], "未接入")
        self.assertEqual(payload["borrows"], [])

        residents.register_borrow(root=self.root, role="human", by="测试", source="黑话词库",
                                  version="v1", total=120, sha256="0" * 64)
        status, payload = self.call("GET", "/home/archive/borrow")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["total"], 1)
        self.assertIsNone(payload["borrows"][0]["learned"])
        self.assertEqual(payload["borrows"][0]["readout_status"], "未接入")
        self.assertEqual(payload["readout_status"], "未接入")

    # —— 9 方法不对 ————————————————————————————————————

    def test_wrong_method_is_405_with_home_wording(self):
        """POST /home/rooms 与 GET /home/write 都是 405，措辞说清「只有一条写路由」。"""
        status, payload = self.call("POST", "/home/rooms", {})
        self.assertEqual(status, 405, payload)
        self.assertIn("POST /home/write", payload["message"])

        status, payload = self.call("GET", "/home/write")
        self.assertEqual(status, 405, payload)
        self.assertIn("只收 POST", payload["message"])

    # —— 10 未载入 ————————————————————————————————————

    def test_store_none_reports_unconnected_without_writing(self):
        """家未载入（store=None）：200 + status=未接入；写的那条另说一个字都没写。"""
        status, payload = api.handle("GET", "/home/rooms", None, None)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "未接入")
        self.assertNotIn("rooms", payload)

        status, payload = api.handle("POST", "/home/write",
                                     {"role": "agent", "by": "测试", "room": "工作台",
                                      "cards": [{"kind": "物件", "name": "x.md"}]}, None)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "未接入")
        self.assertEqual(payload["wrote"], 0)
        self.assertFalse((self.root / "工作台" / "index.jsonl").exists())

    # —— 11 读一间 / 读一件 ————————————————————————————

    def test_list_folds_cards_and_reports_truncation(self):
        """读一间：折叠后的视角（含 mentions），limit 截了就如实回 truncated 与 total。"""
        for index in range(3):
            self.assertEqual(self.write_card(name="第%d张.md" % index)[0], 200)
        status, payload = self.call("GET", "/home/list", query={"room": "工作台"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["lines"], 3)
        self.assertFalse(payload["truncated"])
        self.assertEqual([item["mentions"] for item in payload["items"]], [0, 0, 0])

        status, payload = self.call("GET", "/home/list",
                                    query={"room": "工作台", "limit": "1"})
        self.assertEqual(payload["lines"], 1)
        self.assertEqual(payload["total"], 3)
        self.assertTrue(payload["truncated"])

        status, payload = self.call("GET", "/home/list", query={"offset": "0"})
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "BAD_ARGUMENT")

    def test_read_plain_card_returns_no_body(self):
        """纯说明卡（无外部原件）：body=null、external=true、verified=false，不编内容。"""
        self.assertEqual(self.write_card(name="纯说明卡.md")[0], 200)
        status, payload = self.call("GET", "/home/read",
                                    query={"room": "工作台", "name": "纯说明卡.md"})
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["body"])
        self.assertTrue(payload["external"])
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["chars"], 0)

    def test_read_card_mismatch_with_disk_is_409(self):
        """卡上记的 sha256／字节数跟盘上现算对不上 → 409 CARD_DISK_MISMATCH。"""
        original = self.base / "原件.md"
        original.write_text("ab", encoding="utf-8")
        digest = store.sha256_file(original)
        status, payload = self.write_card(name="原件.md", path=str(original), sha256=digest,
                                          bytes=2)
        self.assertEqual(status, 200, payload)
        status, payload = self.call("GET", "/home/read",
                                    query={"room": "工作台", "name": "原件.md"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["body"], "ab")

        original.write_text("abc", encoding="utf-8")     # 改了就是改了
        status, payload = self.call("GET", "/home/read",
                                    query={"room": "工作台", "name": "原件.md"})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "CARD_DISK_MISMATCH")
        self.assertIn("卡与盘对不上", payload["message"])

    def test_read_unknown_card_is_404(self):
        """房名对、件不在 → 404 CARD_NOT_FOUND（不是 400，也不是空正文）。"""
        status, payload = self.call("GET", "/home/read",
                                    query={"room": "工作台", "name": "没有这张.md"})
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["code"], "CARD_NOT_FOUND")

    # —— 12 仓库指针卡 ————————————————————————————————

    def test_warehouse_unconnected_then_aligned_then_stale(self):
        """没有指针卡 → 未接入；登记一张对齐的 → aligned=true；改了原件 → 409 POINTER_STALE。"""
        status, payload = self.call("GET", "/home/warehouse")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "未接入")
        self.assertEqual(payload["total"], 0)

        source = self.base / "clauses.json"
        source.write_text('{"a": 1}\n', encoding="utf-8")
        digest = store.sha256_file(source)
        status, _ = self.call("POST", "/home/write",
                              {"role": "organ", "by": "home", "room": "仓库",
                               "cards": [{"kind": residents.POINTER_KIND, "name": "clauses.json",
                                          "path": str(source), "sha256": digest,
                                          "bytes": source.stat().st_size,
                                          "params": {"pointer": True}}]})
        self.assertEqual(status, 200)
        status, payload = self.call("GET", "/home/warehouse")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["aligned"])
        self.assertEqual(payload["total"], 1)

        source.write_text('{"a": 2}\n', encoding="utf-8")
        status, payload = self.call("GET", "/home/warehouse")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "POINTER_STALE")
        self.assertIn("clauses.json", payload["stale"])

    def test_rooms_survives_a_broken_whitelist_but_access_does_not(self):
        """白名单文件不在：房表照答（她那一栏 null + whitelist_error），/home/access 回 500。

        读不到不等于「谁都能写」，也不等于「谁都不能写」——两种情况都不许折叠成空表。
        """
        (self.root / "access.json").unlink()
        status, payload = self.call("GET", "/home/rooms")
        self.assertEqual(status, 200, payload)
        rows = {row["name"]: row for row in payload["rooms"]}
        self.assertEqual([name for name, row in rows.items() if row["writable_by_structure"]],
                         list(FIVE))
        for name in FIVE:
            self.assertIsNone(rows[name]["writable_now"])
        for name in READONLY:
            self.assertFalse(rows[name]["writable_now"])
        self.assertEqual(payload["whitelist_error"]["code"], "ACCESS_FILE_MISSING")
        self.assertIsNone(payload["writable_now"])

        status, payload = self.call("GET", "/home/access")
        self.assertEqual(status, 500, payload)
        self.assertEqual(payload["code"], "ACCESS_FILE_MISSING")
        self.assertFalse(payload["ok"])

    def test_path_shape_errors_are_400(self):
        """名字里带上级目录符号：400 PATH_TRAVERSAL（门在 access 里，本层照搬它的码）。"""
        status, payload = self.write_card(name="../跑出去.md")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "PATH_TRAVERSAL")

    # —— 13 硬约束自查 ————————————————————————————————

    def test_home_api_layer_carries_no_permission_judgement(self):
        """API 层零文件权限判断：不出现门与结构可写的那两个名字，也不自己调 check_write。"""
        self.assertNotIn("ROOM_" + "READONLY", SOURCE)
        self.assertNotIn("DEFAULT_" + "WRITABLE", SOURCE)
        self.assertNotIn("check_" + "write", SOURCE)
        # 白名单只在一处拿原样返回值（/home/access 与房表那一栏共用），不拿它判读写
        self.assertEqual(SOURCE.count("load_" + "whitelist"), 1)

    def test_home_api_layer_carries_no_soul_portrait_words(self):
        """家的 HTTP 层不写她的长相：定性描述只有魂的 identity.json 一处。"""
        self.assertNotIn("\u84dd\u53d1\u5782\u8170", SOURCE)


class HomeCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = make_home(self.base)
        write_access(self.root / "access.json", ["工作台"])

    def _run(self, argv):
        """跑一条子命令，把打印出来的话收起来（退出码与日志文字要能对着看）。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv + ["--root", str(self.root)])
        return code, out.getvalue()

    def test_rooms_prints_ten_rows_and_whitelist(self):
        code, text = self._run(["rooms"])
        self.assertEqual(code, 0)
        for name in rooms.ROOM_NAMES:
            self.assertIn(name, text)
        self.assertIn("工作台", text)
        self.assertIn("只读五间", text)
        self.assertIn("退出码 0", text)

    def test_verify_exit_code_matches_its_log(self):
        """账目正常：退出码 0 且日志说 0；截掉最后一行：退出码非 0 且日志说「只报不修」。"""
        code, text = self._run(["verify"])
        self.assertEqual(code, 0)
        self.assertIn("退出码 0", text)

        residents.register_borrow(root=self.root, role="human", by="测试", source="黑话词库",
                                  version="v1", total=10, sha256="0" * 64)
        path = self.root / audit.AUDIT_NAME
        path.write_text("", encoding="utf-8")            # 账被截空
        code, text = self._run(["verify"])
        self.assertNotEqual(code, 0)
        self.assertIn(audit.CODE_SHRUNK, text)
        self.assertIn("只报不修", text)
        self.assertIn("退出码 %d" % code, text)

    def test_seed_registers_pointers_and_never_writes_the_drawer(self):
        """seed：三份都在时登记成仓库指针卡，打印抽屉模板路径，抽屉目录一个文件都不多。"""
        code, text = self._run(["seed"])
        self.assertEqual(code, 0)
        self.assertIn(residents.DRAWER_TEMPLATE, text)
        self.assertIn("一字未写", text)
        self.assertEqual(list((self.root / "抽屉").iterdir()), [])
        pointers = residents.warehouse_pointers(self.root)
        self.assertEqual(pointers["total"], len(residents.MOUTH_FILES))
        self.assertTrue(pointers["aligned"])


if __name__ == "__main__":
    unittest.main()
