import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from api import api


class VoiceApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.dict("os.environ", {"QQAGENT_HOME": str(self.root), "BREEZE_API_KEY": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_account_required_and_status_is_read_only(self):
        status, data = api.handle("GET", "/voice/status", None, None)
        self.assertEqual(status, 200)
        self.assertFalse(data["config"]["enabled"])
        self.assertEqual(data["config"]["voice_id"], "voc_8yud7hz7c9q2")
        self.assertFalse(data["credential_ready"])
        self.assertFalse((self.root / "mouth").exists())
        self.assertEqual(api.handle("POST", "/voice/config", {"enabled": True}, None)[0], 409)

    def test_key_stays_private_and_setting_changes_are_recorded(self):
        with patch.dict("os.environ", {"BREEZE_API_KEY": "brz_secret"}):
            status, data = api.handle("POST", "/voice/config", {"enabled": True}, None)
        self.assertEqual(status, 200)
        self.assertTrue(data["config"]["enabled"])
        self.assertNotIn("brz_secret", json.dumps(data))
        page = api.handle("GET", "/observe/records?source=voice", None, None)[1]
        self.assertEqual(page["rows"][0]["record"]["after"], True)
        for body in ({"enabled": "yes"}, {"api_key": "secret"}, {}, None):
            self.assertEqual(api.handle("POST", "/voice/config", body, None)[0], 400)
        self.assertEqual(api.handle("GET", "/voice/config", None, None)[0], 405)
