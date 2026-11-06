"""魂的 HTTP 层：四端点、只读无副作用、返回值可直接 json.dumps、无 astrbot 也能 import main。"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from typing import Any, Dict, Optional, Tuple

from organs.soul import load
from api import api
from api.tests.helpers import copy_fixture, read_bytes, read_json, write_json

BLOCK_FILES = ("identity.json", "manner.json", "bounds.json", "history.jsonl")


class WebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def _call(self, method: str, path: str,
              body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        status, payload = api.handle(method, path, body, self.store)
        json.dumps(payload, ensure_ascii=False)  # 序列化不抛就是合格返回值
        return status, payload

    def _snapshot_bytes(self) -> Dict[str, bytes]:
        return {name: read_bytes(self.root / name) for name in BLOCK_FILES}

    def test_get_soul_gives_three_blocks_digest_and_issues(self):
        status, payload = self._call("GET", "/soul")
        self.assertEqual(200, status)
        for key in ("identity", "manner", "bounds", "history"):
            self.assertIn(key, payload)
        self.assertNotIn("baseline", payload)
        self.assertEqual({"identity", "manner", "bounds"},
                         set(payload["raw"]))
        self.assertEqual(3, len(payload["identity"]["self_statements"]))
        self.assertEqual(["stance"], list(payload["manner"]))   # 待人就一句姿态，矩阵归脑
        self.assertEqual(2, len(payload["bounds"]["items"]))
        self.assertTrue(payload["digest"])
        self.assertIsInstance(payload["issues"], list)

    def test_get_soul_check_lists_issues(self):
        status, payload = self._call("GET", "/soul/check")
        self.assertEqual(200, status)
        self.assertIsInstance(payload["issues"], list)
        for issue in payload["issues"]:
            # blocking 也是对外形状的一部分（非阻塞的 error 只报不拦），别只认三键
            self.assertEqual(["blocking", "code", "level", "message"], sorted(issue))

    def test_get_soul_history_is_the_ledger(self):
        status, payload = self._call("GET", "/soul/history")
        self.assertEqual(200, status)
        self.assertEqual([], payload["history"])

    def test_read_endpoints_write_nothing(self):
        before = self._snapshot_bytes()
        for path in ("/soul", "/soul/check", "/soul/history"):
            status, _ = self._call("GET", path)
            self.assertEqual(200, status)
        self.assertEqual(before, self._snapshot_bytes())

    def test_raw_view_paths_edit_but_normalized_paths_do_not(self):
        """两个视图差别的契约：POST 的 path 只认 raw 形状。"""
        raw = self._call("GET", "/soul")[1]["raw"]
        if not raw["bounds"]["items"]:
            write_json(self.root / "bounds.json", {"items": [
                {"id": "b01", "rule": "临时塞的一条", "action": "block", "active": True,
                 "match": {"type": "literal", "pattern": "禁"}}]})
            raw = self._call("GET", "/soul")[1]["raw"]
        first = raw["bounds"]["items"][0]
        self.assertEqual(self.store.snapshot().bounds.items[0].match_type, first["match"]["type"])
        flipped = "regex" if first["match"]["type"] == "literal" else "literal"
        body = {"actor": "bowen", "reason": "面板按 raw 拼 path", "block": "bounds",
                "path": "items[0].match.type", "value": flipped}
        status, payload = self._call("POST", "/soul/edit", body)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, len(self.store.history()))
        self.assertEqual(flipped, self.store.raw("bounds")["items"][0]["match"]["type"])

        status, payload = self._call("POST", "/soul/edit", dict(body, path="items[0].match_type"))
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("path 无法定位", payload["message"])

    def test_post_edit_writes_and_logs_one_line(self):
        status, payload = self._call("POST", "/soul/edit", {
            "actor": "bowen", "reason": "改基调", "block": "manner",
            "path": "stance", "value": "改完的基调",
        })
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertIn("history_line", payload)
        self.assertEqual("改完的基调", json.loads(
            (self.root / "manner.json").read_text(encoding="utf-8"))["stance"])
        self.assertEqual(1, len(self.store.history()))

    def test_edit_refuses_the_probe_flag(self):
        """后门堵在这：probe 只有 POST /soul/acceptance 一个入口，edit 带了就 400 且不写盘。"""
        before = self._snapshot_bytes()
        status, payload = self._call("POST", "/soul/edit", {
            "actor": "壳线-验收", "reason": "验证写路径", "block": "manner",
            "path": "stance", "value": "探一针", "probe": True,
        })
        self.assertEqual(400, status)
        self.assertEqual("PROBE_VIA_EDIT_FORBIDDEN", payload["issues"][0]["code"])
        self.assertEqual(before, self._snapshot_bytes())
        self.assertEqual([], self.store.history())

    def test_probe_key_is_refused_whatever_its_value(self):
        """不看值：true / "true" / false 一律 400——探针没有「顺手从 edit 走一趟」这条路。"""
        for value in ("true", False, 1):
            status, payload = self._call("POST", "/soul/edit", {
                "actor": "bowen", "reason": "改基调", "block": "manner",
                "path": "stance", "value": "改完的基调", "probe": value,
            })
            self.assertEqual(400, status, value)
            self.assertEqual("PROBE_VIA_EDIT_FORBIDDEN", payload["issues"][0]["code"])

    def test_post_migrate_swaps_the_shape_and_logs_migrate(self):
        """旧形状是坏卡：migrate 只要 digest 对就换得动，账行 kind=migrate。"""
        write_json(self.root / "manner.json", {"tone": "旧形状", "matrix": {}})
        expect = hashlib.sha256(read_bytes(self.root / "manner.json")).hexdigest()
        status, payload = self._call("POST", "/soul/migrate", {
            "actor": "bowen", "reason": "manner 收成 stance", "block": "manner",
            "value": {"stance": "换完的姿态"}, "expect_old_digest": expect,
        })
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual("migrate", payload["history_line"]["kind"])
        self.assertEqual({"stance": "换完的姿态"}, read_json(self.root / "manner.json"))

    def test_post_migrate_with_a_stale_digest_writes_nothing(self):
        before = self._snapshot_bytes()
        status, payload = self._call("POST", "/soul/migrate", {
            "actor": "bowen", "reason": "换形状", "block": "manner",
            "value": {"stance": "不该落盘"}, "expect_old_digest": "sha256:" + "0" * 64,
        })
        self.assertEqual(400, status)
        self.assertEqual("MIGRATE_DIGEST_MISMATCH", payload["issues"][0]["code"])
        self.assertEqual(before, self._snapshot_bytes())

    def test_post_migrate_requires_every_field(self):
        status, payload = self._call("POST", "/soul/migrate", {"actor": "bowen"})
        self.assertEqual(400, status)
        self.assertIn("缺字段", payload["message"])

    def test_post_edit_deleting_a_bound_is_rejected_and_nothing_written(self):
        before = self._snapshot_bytes()
        status, payload = self._call("POST", "/soul/edit", {
            "actor": "bowen", "reason": "撤线", "block": "bounds",
            "path": "items[1]", "value": None,
        })
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["message"])
        self.assertEqual(before, self._snapshot_bytes())
        self.assertEqual([], self.store.history())

    def test_heart_and_brain_are_readonly_json(self):
        # 口径变更（2026-11-05 心线落地）：/heart 不再返回占位三键，而是七条真读数；
        # /brain 那半保持原口径（没给脑就是「未接入」）。心读自己的数据根，这里给空临时根：
        # 没卡就如实拒载（200 + ok=False + issues），不是 500、也不是假装成功。
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        status, payload = api.handle("GET", "/heart", None, self.store,
                                     query={"root": temp.name})
        self.assertEqual(200, status, "/heart")
        self.assertEqual("heart", payload["agent"])
        self.assertTrue(payload["readonly"])
        self.assertEqual("拒载", payload["status"])
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["issues"], "坏卡必须逐条报出来，不许白屏")

        status, payload = self._call("GET", "/brain")
        self.assertEqual(200, status, "/brain")
        self.assertEqual({"status": "未接入", "readonly": True, "agent": "brain"}, payload)

    def test_write_methods_on_heart_and_brain_are_405(self):
        for path in ("/heart", "/brain"):
            for method in ("POST", "PUT", "DELETE"):
                status, payload = self._call(method, path, {})
                self.assertEqual(405, status, "%s %s" % (method, path))
                self.assertFalse(payload["ok"])
                self.assertIn("只读", payload["message"])
        self.assertEqual(404, self._call("GET", "/heart/edit")[0])
        self.assertEqual(404, self._call("GET", "/brain/x")[0])

    def test_unknown_route_wrong_method_and_missing_field(self):
        self.assertEqual(404, self._call("GET", "/soul/unknown")[0])
        self.assertEqual(405, self._call("POST", "/soul")[0])
        self.assertEqual(405, self._call("GET", "/soul/edit")[0])
        status, payload = self._call("POST", "/soul/edit", {"actor": "bowen"})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])


class MainImportTest(unittest.TestCase):
    def test_import_main_without_astrbot(self):
        try:
            import astrbot  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest("本机装了 astrbot，降级分支测不到")
        import main

        self.assertTrue(issubclass(main.QQAgent, object))
        # 默认数据根不再是包内 organs/soul/data（那是出厂默认参数），而是实盘 <仓库>/home/organs/soul
        from api import roots
        self.assertEqual(roots.organ_root("soul"), main._organ_dir(roots, "soul", None))
        self.assertIn("home", roots.organ_root("soul").parts)


if __name__ == "__main__":
    unittest.main()
