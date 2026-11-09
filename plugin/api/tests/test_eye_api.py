import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from api import api


class EyeApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.dict("os.environ", {"QQAGENT_HOME": str(self.root), "QQAGENT_VISION_KEY": "test-hidden-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_toggle_is_persistent_audited_and_secret_free(self):
        status, data = api.handle("GET", "/eye/status", None, None)
        self.assertEqual(status, 200)
        self.assertFalse(data["config"]["enabled"])
        self.assertTrue(data["credential_ready"])
        self.assertNotIn("test-hidden-key", json.dumps(data))
        self.assertFalse((self.root / "eye").exists())
        status, data = api.handle("POST", "/eye/config", {"enabled": True}, None)
        self.assertEqual(status, 200)
        self.assertTrue(api.handle("GET", "/eye/status", None, None)[1]["config"]["enabled"])
        status, page = api.handle("GET", "/observe/records?source=eye", None, None)
        self.assertEqual(status, 200)
        self.assertEqual(page["rows"][0]["record"]["before"], False)
        self.assertEqual(page["rows"][0]["record"]["after"], True)

    def test_only_the_boolean_switch_is_editable(self):
        for body in ({"enabled": "yes"}, {"enabled": True, "api_key": "secret"}, {}, None):
            self.assertEqual(api.handle("POST", "/eye/config", body, None)[0], 400)
        self.assertEqual(api.handle("GET", "/eye/config", None, None)[0], 405)
        self.assertEqual(api.handle("POST", "/eye/status", {}, None)[0], 405)
