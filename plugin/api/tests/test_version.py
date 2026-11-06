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

from api import api, devserver, version

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
        for key in ("started_at", "pid", "git_head", "git_head_now", "dirty",
                    "dirty_other", "source_digest", "source_digest_now", "stale"):
            self.assertIn(key, payload)
        self.assertEqual(payload["pid"], version.PID)
        self.assertEqual(payload["started_at"], version.STARTED_AT)
        self.assertRegex(payload["source_digest"], r"^[0-9a-f]{12}$")
        self.assertIsInstance(payload["stale"], bool)

    def test_write_methods_are_rejected(self):
        for method in ("POST", "PUT", "DELETE"):
            status, payload = api.handle(method, "/healthz", {}, store=None)
            self.assertEqual(status, 405, "%s /healthz 必须 405" % method)
            # 钉行为，不钉那句话怎么说的：405 + ok:false + 这条路由在表里就是只读的 GET
            self.assertIs(payload["ok"], False)
            self.assertEqual("GET", api.ROUTES["/healthz"])

    def test_dirty_only_counts_code_and_docs(self):
        """dirty 只反映代码与文档树：参考资料目录里改 rtf 不该把脏灯点亮。"""
        sample = (
            " M 参考文档和仓库/data.rtf\0"
            "?? 参考文档和仓库/guide.rtf\0"
        )
        dirty, other = version._dirty_report(sample)
        self.assertIs(dirty, False, "只改参考资料就报 dirty")
        self.assertEqual(2, other)

    def test_dirty_lights_up_for_code_and_docs(self):
        for path in ("plugin/api/version.py", "docs/plan.md", "her.sh",
                     "tools/x.py", "shell/index.ts"):
            dirty, other = version._dirty_report(" M %s\0" % path)
            self.assertIs(dirty, True, "%s 改了却没报 dirty" % path)
            self.assertEqual(0, other)

    def test_rename_entries_are_counted_once(self):
        sample = "R  plugin/api/new.py\0plugin/api/old.py\0"
        dirty, other = version._dirty_report(sample)
        self.assertIs(dirty, True)
        self.assertEqual(0, other)

    def test_stale_is_about_the_commit_not_the_source_tree(self):
        """判据是「进程跑的是不是旧代码」，不是「源码一个字节都没动」。

        工作树有未提交改动（本仓常态）时，只要提交没变，stale 必须是 False——
        否则每改一行都报假警，最后没人看这条。
        """
        original = version.GIT_HEAD
        try:
            version.GIT_HEAD = version._git_head()
            report = version.report()
            self.assertIs(report["stale"], False, "提交没变却报陈旧：%r" % (report["advice"],))
        finally:
            version.GIT_HEAD = original
        self.assertEqual(version.source_digest(), version.source_digest(),
                         "指纹仍要稳定（只作诊断）")

    def test_dirty_alone_does_not_make_it_stale(self):
        """有未提交改动 = dirty，不是 stale：它是「重启更保险」，不是「一定是旧代码」。"""
        original = version.GIT_HEAD
        try:
            version.GIT_HEAD = version._git_head()
            report = version.report()
        finally:
            version.GIT_HEAD = original
        # 钉行为，不钉文案：dirty 是布尔、stale 仍是 False（提交没变），
        # 顺带把 dirty_other 的数也检查一遍（它就是「另有 N 个非代码文件」那个计数）。
        self.assertIsInstance(report["dirty"], bool)
        self.assertIsInstance(report["dirty_other"], int)
        self.assertGreaterEqual(report["dirty_other"], 0)
        self.assertIs(report["stale"], False, report["advice"])
        self.assertEqual(report["git_head"], report["git_head_now"],
                         "提交没变却报了 stale")
        self.assertTrue(str(report["advice"]).strip(), "advice 是空的：人看不到任何提示")

    def test_stale_flips_when_the_commit_changes(self):
        """HEAD 与启动时不同，才是真的「在跑旧代码」。"""
        original = version.GIT_HEAD
        try:
            version.GIT_HEAD = "deadbee"
            report = version.report()
            self.assertIs(report["stale"], True)
            # 钉行为：stale 为真 ⇔ 启动时的 head 与此刻不同；advice 非空即可，措辞不归测试管
            self.assertNotEqual(report["git_head"], report["git_head_now"])
            self.assertTrue(str(report["advice"]).strip(), "advice 是空的：人看不到任何提示")
        finally:
            version.GIT_HEAD = original

    def test_digest_covers_organ_sources(self):
        """指纹要盖住 organs/*.py，否则「改了判据没重启」不会报警。"""
        covered = [p.relative_to(version.PLUGIN_DIR).as_posix() for p in version._source_files()]
        self.assertTrue(any(p.startswith("organs/") for p in covered), covered[:5])
        self.assertTrue(any(p.startswith("api/") for p in covered), covered[:5])
        self.assertFalse(any("__pycache__" in p or "/tests/" in p for p in covered))


class FooterShowsVersion(unittest.TestCase):
    """页脚那一行不是装饰：它得真去读 /healthz，并且分得出新旧两态。"""

    def test_pages_have_a_build_footer_that_calls_healthz(self):
        # 脑那页已删除（2026-11-06 脑页退场，内容搬去 about/memory），但它那条页脚
        # 判据没有跟着消失：能从 /about 或 /memory 里读同一行就算数——见下一个用例。
        # 这里只留还在的那张家页，删掉的页不再出现在名单里（不是放宽，是对象没了）。
        for page in ("home.html",):
            text = (PAGES / page).read_text(encoding="utf-8")
            self.assertRegex(text, BUILD_RE, "%s 没有页脚 build 节点" % page)
            self.assertIn('call("GET", "/healthz")', text, "%s 的页脚没有去读 /healthz" % page)
            self.assertIn("stale", text, "%s 的页脚分不出进程新旧" % page)
            # 钉分支，不钉那句中文：stale 为真时走红字那一支（读字段 + 挂 bad 样式）
            self.assertIn("if (d.stale)", text, "%s 的页脚没有按 stale 字段分支" % page)
            self.assertIn('"build bad"', text, "%s 的页脚没有陈旧那一支的红字样式" % page)
            self.assertIn("dirty", text, "%s 的页脚没有把 dirty 单独读出来" % page)

    def test_brain_page_is_really_gone(self):
        """名字从上面的名单里划掉，得有个看得见的凭据：文件真的没了。

        2026-11-06：脑那页整页删除（内容搬去 /about 与 /memory，/brain 回到 JSON 端点）。
        留着这条，是为了下次谁想「先把名字删了再说」时，这里会先红。
        """
        self.assertNotIn("brain", {path.stem for path in PAGES.glob("*.html")},
                         "脑那页应已删除（按目录现状断言，不写死文件名）")
        self.assertNotIn("/brain", devserver.PAGE_ROUTES, "/brain 不该还挂在静态页名单上")


if __name__ == "__main__":
    unittest.main()
