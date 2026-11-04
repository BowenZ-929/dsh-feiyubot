"""脑的 HTTP 层：六个只读端点、写方法一律 405、brain 未载入时如实报。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from api import api, panel
from api.tests.helpers import copy_fixture
from organs.brain import BrainStore
from organs.soul import load

BRAIN_ROUTES = ("/brain", "/brain/overview", "/brain/today", "/brain/people",
                "/brain/recall", "/brain/selfnarration", "/brain/proposals")


class BrainApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = load(copy_fixture("valid", self._tmp.name))
        self.brain = BrainStore(Path(self._tmp.name) / "brain")

    def _call(self, method: str, path: str,
              body: Optional[Dict[str, Any]] = None,
              brain: Any = "default") -> Tuple[int, Dict[str, Any]]:
        if brain == "default":
            brain = self.brain
        status, payload = api.handle(method, path, body, self.store, brain=brain)
        json.dumps(payload, ensure_ascii=False)
        return status, payload

    def test_brain_overview_reports_connected_readonly(self):
        status, payload = self._call("GET", "/brain")
        self.assertEqual(200, status)
        self.assertEqual("已接入", payload["status"])
        self.assertTrue(payload["readonly"])
        self.assertEqual("brain", payload["agent"])
        for key in ("today_entries", "people", "longterm_entries",
                    "pending_proposals", "selfnarration_version", "pending"):
            self.assertIn(key, payload)

    def test_brain_readonly_endpoints_answer(self):
        self.assertEqual(200, self._call("GET", "/brain/today")[0])
        self.assertEqual(200, self._call("GET", "/brain/people")[0])
        self.assertEqual(200, self._call("GET", "/brain/proposals")[0])
        status, payload = self._call("GET", "/brain/selfnarration")
        self.assertEqual(200, status)
        self.assertIn("versions", payload)

    def test_brain_recall_requires_query_and_returns_packed_shape(self):
        status, payload = self._call("GET", "/brain/recall?q=以前说过什么")
        self.assertEqual(200, status)
        self.assertIn("items", payload)
        self.assertIn("budget", payload)
        self.assertIn("skipped", payload)
        self.assertEqual(400, self._call("GET", "/brain/recall")[0])

    def test_brain_routes_reject_write_methods(self):
        for path in BRAIN_ROUTES:
            for method in ("POST", "PUT", "DELETE"):
                status, payload = self._call(method, path, {})
                self.assertEqual(405, status, "%s %s" % (method, path))
                self.assertIn("只读", payload["message"])

    def test_brain_missing_store_reports_unconnected(self):
        status, payload = self._call("GET", "/brain", brain=None)
        self.assertEqual(200, status)
        self.assertEqual({"status": "未接入", "readonly": True, "agent": "brain"}, payload)
        status, payload = self._call("GET", "/brain/today", brain=None)
        self.assertEqual(503, status)
        self.assertFalse(payload["ok"])

    def test_panel_routes_mount_all_brain_get_endpoints(self):
        routes = panel.routes(self.store, self.brain)
        brain_routes = {(method, path) for method, path, _ in routes
                        if path.startswith("/brain")}
        self.assertEqual({("GET", path) for path in BRAIN_ROUTES}, brain_routes)


if __name__ == "__main__":
    unittest.main()
