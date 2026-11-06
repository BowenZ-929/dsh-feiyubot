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
                "/brain/recall", "/brain/selfnarration", "/brain/proposals",
                "/brain/tiers", "/brain/cold", "/brain/judge")


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
                if path == "/brain/judge" and method == "POST":
                    continue   # /brain/judge 是唯一写端点，POST 是它的正方法
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

    def test_round4_endpoints_answer(self):
        status, payload = self._call("GET", "/brain/tiers")
        self.assertEqual(200, status)
        for tier in ("today", "hot", "longterm", "cold"):
            self.assertIn(tier, payload)
        status, payload = self._call("GET", "/brain/cold")
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])

    def test_cold_and_tiers_distinguish_empty_from_read_error(self):
        self.assertEqual(200, self._call("GET", "/brain/cold")[0])
        with open(self.brain.root / "longterm.jsonl", "a", encoding="utf-8") as f:
            f.write("{bad json\n")
        self.assertEqual(500, self._call("GET", "/brain/cold")[0])
        self.assertEqual(500, self._call("GET", "/brain/tiers")[0])

    def test_card_patch_whitelist_and_familiarity_rejected(self):
        self.brain.ingest("3425635473", "我妈住院了")
        status, payload = self._call("PATCH", "/brain/people/3425635473/card",
                                     {"actor": "panel", "fields": {"称呼": "老板", "雷点": ["别提工作"]}})
        self.assertEqual(200, status, payload)
        self.assertEqual(2, len(payload["history_lines"]))
        self.assertEqual("老板", payload["card"]["display"])
        self.assertEqual(400, self._call("PATCH", "/brain/people/3425635473/card",
                                         {"actor": "panel", "fields": {"熟稔": 99}})[0])
        self.assertEqual(400, self._call("PATCH", "/brain/people/3425635473/card",
                                         {"actor": "panel", "fields": {"爱好": ["猫"]}})[0])
        self.assertEqual(404, self._call("GET", "/brain/people/999999999/card")[0])
        self.assertEqual(405, self._call("POST", "/brain/people/3425635473/card", {})[0])

    def test_today_and_people_annotate_retention_and_memories_ts(self):
        self.brain.ingest("3425635473", "我妈住院了")
        status, payload = self._call("GET", "/brain/today")
        self.assertEqual(200, status)
        self.assertIn("retention", payload["entries"][0])
        self.assertIn("clarity", payload["entries"][0])
        status, payload = self._call("GET", "/brain/people")
        person = payload["people"][0]
        self.assertTrue(person["memories"])
        self.assertTrue(all("ts" in m for m in person["memories"]))

    def test_panel_routes_mount_all_brain_endpoints(self):
        routes = panel.routes(self.store, self.brain)
        brain_routes = {(method, path) for method, path, _ in routes
                        if path.startswith("/brain")}
        expected = {(api.ROUTES[path], path) for path in BRAIN_ROUTES}
        self.assertEqual(expected, brain_routes)


if __name__ == "__main__":
    unittest.main()
