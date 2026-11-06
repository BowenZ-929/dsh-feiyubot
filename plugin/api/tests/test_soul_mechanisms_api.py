"""新机制的端点：pin、版本、回滚、红线逐条加/撤、验收探针，以及拒因回到面板。

口径：写端点只转调 store 的方法，不改结构；只读端点一个字节都不写（含整档归档）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from typing import Any, Dict, Optional, Tuple

from api import api
from api.tests.helpers import copy_fixture, read_bytes
from organs.soul import load, store as soul_store

BLOCK_FILES = ("identity.json", "manner.json", "bounds.json", "history.jsonl")


class SoulMechanismsHttpTest(unittest.TestCase):
    """真起一个 127.0.0.1 的服务器：新端点也得在 devserver 上通得过，不只在 handle() 里。"""

    def setUp(self) -> None:
        import threading
        from http.client import HTTPConnection
        from http.server import HTTPServer
        from api import devserver

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store = load(copy_fixture("valid", self._tmp.name))
        handler = type("_TestHandler", (devserver._Handler,),
                       {"store": store, "log_message": lambda *args: None})
        try:
            self.server = HTTPServer(("127.0.0.1", 0), handler)
        except OSError as exc:
            self.skipTest("起不了本地端口：%s" % exc)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

        def _stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)

        self.addCleanup(_stop)
        self.port = self.server.server_address[1]
        self.connection = HTTPConnection
        self.store = store

    def _request(self, method: str, path: str, body: Optional[str] = None):
        conn = self.connection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=(body or "").encode("utf-8"),
                         headers={"Content-Type": "application/json"})
            res = conn.getresponse()
            return res.status, json.loads(res.read().decode("utf-8"))
        finally:
            conn.close()

    def test_pin_and_revisions_answer_over_http(self):
        status, payload = self._request("GET", "/soul/pin")
        self.assertEqual(200, status)
        self.assertLessEqual(payload["bytes"], payload["limit"])
        status, payload = self._request("GET", "/soul/revisions")
        self.assertEqual(200, status)
        self.assertEqual(0, payload["current_revision"])

    def test_redline_add_over_http_writes_one_line(self):
        body = json.dumps({"actor": "panel", "reason": "补一条", "op": "add",
                           "item": {"id": "b09", "rule": "不在群里念别人的账号",
                                    "match": {"type": "literal",
                                              "pattern": "不在群里念别人的账号"},
                                    "action": "block", "active": True,
                                    "enforced_by": "soul·字面"}}, ensure_ascii=False)
        status, payload = self._request("POST", "/soul/redline", body)
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(3, len(self.store.snapshot().bounds.items))

class SoulMechanismsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def _call(self, method: str, path: str,
              body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        status, payload = api.handle(method, path, body, self.store)
        json.dumps(payload, ensure_ascii=False)
        return status, payload

    def _bytes(self) -> Dict[str, bytes]:
        return dict((name, read_bytes(self.root / name)) for name in BLOCK_FILES)

    def _stance(self) -> str:
        return json.loads((self.root / "manner.json").read_text(encoding="utf-8"))["stance"]

    def _seed_probe_rows(self, count: int) -> None:
        """往账里塞当天的 probe 行：日限额按「当天 probe 行数 / 2」算对。"""
        stamp = soul_store.now_iso()
        with open(str(self.root / "history.jsonl"), "a", encoding="utf-8") as handle:
            for _ in range(count):
                handle.write(json.dumps({"ts": stamp, "kind": "probe", "actor": "壳线-验收",
                                         "block": "manner", "path": "stance",
                                         "reason": "先前的探针"},
                                        ensure_ascii=False) + "\n")

    def _acceptance(self, **overrides) -> Tuple[int, Dict[str, Any]]:
        body = {"actor": "壳线-验收", "reason": "验证写路径", "block": "manner",
                "path": "stance", "value": "探一针", "restore_value": self._stance()}
        body.update(overrides)
        return self._call("POST", "/soul/acceptance", body)

    def test_pin_endpoint_gives_the_pin_and_its_budget(self):
        status, payload = self._call("GET", "/soul/pin")
        self.assertEqual(200, status)
        self.assertLessEqual(payload["bytes"], payload["limit"])
        self.assertEqual(1200, payload["limit"])
        self.assertEqual(2, payload["items"])
        self.assertIn("b01｜", payload["pin"])
        self.assertIn("压缩产物首段", payload["note"])

    def test_revisions_endpoint_lists_versions_and_current(self):
        status, payload = self._call("GET", "/soul/revisions")
        self.assertEqual(200, status)
        self.assertEqual(0, payload["current_revision"])
        self.assertEqual([], payload["revisions"])
        self.assertIn("回到哪一版", payload["note"])
        self.assertEqual(200, self._call("POST", "/soul/edit", {
            "actor": "panel", "reason": "改一句", "block": "manner",
            "path": "stance", "value": "慢慢说，先听完"})[0])
        status, payload = self._call("GET", "/soul/revisions")
        self.assertEqual(1, payload["current_revision"])
        self.assertEqual([0], [row["revision"] for row in payload["revisions"]])

    def test_rollback_endpoint_restores_and_logs(self):
        before = read_bytes(self.root / "manner.json")
        self.assertEqual(200, self._call("POST", "/soul/edit", {
            "actor": "panel", "reason": "改一句", "block": "manner",
            "path": "stance", "value": "慢慢说，先听完"})[0])
        status, payload = self._call("POST", "/soul/rollback",
                                     {"actor": "panel", "reason": "改坏了", "revision": 0})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(before, read_bytes(self.root / "manner.json"))
        self.assertEqual("rollback", self.store.history()[-1]["kind"])

    def test_rollback_accepts_a_digit_string_and_rejects_junk(self):
        self._call("POST", "/soul/edit", {"actor": "panel", "reason": "改一句",
                                          "block": "manner", "path": "stance",
                                          "value": "慢慢说，先听完"})
        status, payload = self._call("POST", "/soul/rollback",
                                     {"actor": "panel", "reason": "回退", "revision": "0"})
        self.assertEqual(200, status, payload)
        bad = self._call("POST", "/soul/rollback",
                         {"actor": "panel", "reason": "回退", "revision": "第一版"})
        self.assertEqual(400, bad[0])
        self.assertIn("revision", bad[1]["message"])

    def test_rollback_requires_every_field(self):
        status, payload = self._call("POST", "/soul/rollback", {"actor": "panel"})
        self.assertEqual(400, status)
        self.assertIn("缺字段", payload["message"])

    def test_redline_add_and_retire_are_separate_writes(self):
        status, payload = self._call("POST", "/soul/redline", {
            "actor": "panel", "reason": "补一条", "op": "add",
            "item": {"id": "b03", "rule": "不在群里念别人的账号",
                     "match": {"type": "literal", "pattern": "不在群里念别人的账号"},
                     "action": "block", "active": True, "enforced_by": "soul·字面"}})
        self.assertEqual(200, status, payload)
        self.assertEqual(3, len(self.store.snapshot().bounds.items))
        status, payload = self._call("POST", "/soul/redline", {
            "actor": "panel", "reason": "撤这条", "op": "retire", "id": "b03"})
        self.assertEqual(200, status, payload)
        items = self.store.snapshot().bounds.items
        self.assertEqual(3, len(items))
        self.assertFalse(items[-1].active)

    def test_redline_refuses_a_whole_block_write(self):
        before = self._bytes()
        status, payload = self._call("POST", "/soul/redline", {
            "actor": "panel", "reason": "整段重写", "op": "replace",
            "items": []})
        self.assertEqual(400, status)
        self.assertIn("整块替换", payload["message"])
        self.assertEqual(before, self._bytes())

    def test_acceptance_writes_then_restores_and_logs_two_probe_rows(self):
        before = self._stance()
        status, payload = self._acceptance()
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(2, len(payload["history_lines"]))
        self.assertEqual(["probe", "probe"], [line["kind"] for line in payload["history_lines"]])
        self.assertEqual(before, self._stance())                      # 写回去了
        self.assertEqual(["probe", "probe"], [row["kind"] for row in self.store.history()])

    def test_acceptance_requires_every_field(self):
        status, payload = self._call("POST", "/soul/acceptance", {"actor": "panel"})
        self.assertEqual(400, status)
        self.assertIn("缺字段", payload["message"])
        self.assertIn("restore_value", payload["message"])

    def test_acceptance_refuses_the_bounds_block(self):
        before = self._bytes()
        status, payload = self._acceptance(block="bounds", path="items[0].rule",
                                           value="探一针", restore_value="【待定稿】")
        self.assertEqual(400, status)
        self.assertEqual("PROBE_ON_BOUNDS_FORBIDDEN", payload["issues"][0]["code"])
        self.assertEqual(before, self._bytes())
        self.assertEqual([], self.store.history())

    def test_acceptance_daily_limit_counts_probe_pairs(self):
        """每天 ≤3 对：账里已有 3 对（6 行 probe）就拒。"""
        self._seed_probe_rows(6)
        status, payload = self._acceptance()
        self.assertEqual(400, status)
        self.assertEqual("PROBE_DAILY_LIMIT", payload["issues"][0]["code"])
        self.assertIn("3 对", payload["issues"][0]["message"])

    def test_acceptance_allows_the_third_pair_of_the_day(self):
        self._seed_probe_rows(4)          # 已经 2 对，这一对是今天的第 3 对
        status, payload = self._acceptance()
        self.assertEqual(200, status, payload)
        probes = [row for row in self.store.history() if row["kind"] == "probe"]
        self.assertEqual(6, len(probes))

    def test_redline_endpoint_refuses_probe(self):
        """红线不许探：/soul/redline 带 probe 就 400，一个字节都不写。"""
        before = self._bytes()
        status, payload = self._call("POST", "/soul/redline", {
            "actor": "panel", "reason": "探一针", "op": "retire", "id": "b02", "probe": True})
        self.assertEqual(400, status)
        self.assertEqual("PROBE_ON_BOUNDS_FORBIDDEN", payload["issues"][0]["code"])
        self.assertEqual(before, self._bytes())
        self.assertEqual([], self.store.history())

    def test_redline_without_reason_is_rejected(self):
        status, payload = self._call("POST", "/soul/redline", {
            "actor": "panel", "reason": "  ", "op": "retire", "id": "b02"})
        self.assertEqual(400, status)
        self.assertEqual("REDLINE_REASON_REQUIRED", payload["issues"][0]["code"])
        self.assertTrue(self.store.snapshot().bounds.items[1].active)

    def test_edit_that_breaks_the_redline_is_rejected_with_the_reason(self):
        """面板拿到的就是拒因：message 里有软话词，issues 里有 REDLINE_SOFT_WORD。"""
        before = self._bytes()
        status, payload = self._call("POST", "/soul/edit", {
            "actor": "panel", "reason": "加一句软话", "block": "bounds",
            "path": "items[0].rule", "value": "尽量避免嘲讽"})
        self.assertEqual(400, status)
        self.assertEqual("REDLINE_SOFT_WORD", payload["issues"][0]["code"])
        self.assertEqual(before, self._bytes())

    def test_check_reports_the_redline_assertion(self):
        payload = self._call("GET", "/soul/check")[1]
        # 这份卡是从 fixtures/valid 拷的：红线 id 与出厂本来就不是同一份，只剩那条对照
        self.assertEqual(["FACTORY_LIVE_ID_DRIFT"], [issue["code"] for issue in payload["issues"]])
        path = self.root / "bounds.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["items"][0]["rule"] = "尽量避免嘲讽"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        codes = [issue["code"] for issue in self._call("GET", "/soul/check")[1]["issues"]]
        self.assertIn("REDLINE_SOFT_WORD", codes)

    def test_readonly_endpoints_write_nothing(self):
        before = self._bytes()
        for route in ("/soul/pin", "/soul/revisions"):
            self.assertEqual(200, self._call("GET", route)[0], route)
        self.assertEqual(before, self._bytes())
        self.assertFalse((self.root / "previous.json").exists())


if __name__ == "__main__":
    unittest.main()
