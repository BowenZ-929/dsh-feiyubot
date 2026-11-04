"""核自报身份：跑的是哪一版、什么时候起的、源码有没有在它脚下被改过。

为什么要这条：本项目三次踩到「源码里有、请求 404/500」——核是 Python，源码改了不重启就还在跑
旧代码（CONTRIBUTING §1.3）。规矩拦不住人，但进程自己报一句「我的源码指纹变了」能一眼看出。

被看住的三件事：
1. `GET /healthz` 回一份可读的身份：git HEAD、启动时间、进程号、启动时的源码指纹；
2. `stale` 只在源码真被改过时为真——现算指纹与启动指纹不一致；
3. 家页与脑页的页脚真的去读这一行，并且有「进程旧了」的红字分支。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from api import api, version

PAGES = Path(__file__).resolve().parents[2] / "pages"
BUILD_RE = re.compile(r'<footer[^>]*\bid="build"', re.S)


class HealthzTest(unittest.TestCase):
    def test_healthz_is_a_registered_get(self):
        self.assertEqual(api.ROUTES.get("/healthz"), "GET")

    def test_payload_self_identifies(self):
        status, payload = api.handle("GET", "/healthz", None, store=None)
        self.assertEqual(status, 200)
        self.assertIs(payload["ok"], True)
        self.assertIs(payload["readonly"], True)
        for key in ("started_at", "pid", "git_head", "source_digest", "source_digest_now", "stale"):
            self.assertIn(key, payload)
        self.assertEqual(payload["pid"], version.PID)
        self.assertEqual(payload["started_at"], version.STARTED_AT)
        self.assertRegex(payload["source_digest"], r"^[0-9a-f]{12}$")
        self.assertIsInstance(payload["stale"], bool)

    def test_write_methods_are_rejected(self):
        for method in ("POST", "PUT", "DELETE"):
            status, payload = api.handle(method, "/healthz", {}, store=None)
            self.assertEqual(status, 405, "%s /healthz 必须 405" % method)
            self.assertIn("只收 GET", payload["message"])

    def test_digest_is_stable_when_source_is_unchanged(self):
        self.assertEqual(version.source_digest(), version.source_digest())
        self.assertFalse(version.report()["stale"], "没改源码就不该报陈旧")

    def test_stale_flips_when_source_changes(self):
        """把「启动指纹」换掉，就等于源码在进程脚下变过——`stale` 必须为真。"""
        original = version.BOOT_DIGEST
        try:
            version.BOOT_DIGEST = "0" * 64
            report = version.report()
            self.assertIs(report["stale"], True)
            self.assertIn("重启核", report["advice"])
        finally:
            version.BOOT_DIGEST = original

    def test_digest_covers_organ_sources(self):
        """指纹要盖住 organs/*.py，否则「改了判据没重启」不会报警。"""
        covered = [p.relative_to(version.PLUGIN_DIR).as_posix() for p in version._source_files()]
        self.assertTrue(any(p.startswith("organs/") for p in covered), covered[:5])
        self.assertTrue(any(p.startswith("api/") for p in covered), covered[:5])
        self.assertFalse(any("__pycache__" in p or "/tests/" in p for p in covered))


class FooterShowsVersion(unittest.TestCase):
    """页脚那一行不是装饰：它得真去读 /healthz，并且分得出新旧两态。"""

    def test_pages_have_a_build_footer_that_calls_healthz(self):
        for page in ("home.html", "brain.html"):
            text = (PAGES / page).read_text(encoding="utf-8")
            self.assertRegex(text, BUILD_RE, "%s 没有页脚 build 节点" % page)
            self.assertIn('call("GET", "/healthz")', text, "%s 的页脚没有去读 /healthz" % page)
            self.assertIn("stale", text, "%s 的页脚分不出进程新旧" % page)
            self.assertIn("进程旧了", text, "%s 没有「进程旧了」的红字分支" % page)


if __name__ == "__main__":
    unittest.main()
