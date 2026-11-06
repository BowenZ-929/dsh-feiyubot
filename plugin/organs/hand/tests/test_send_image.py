"""send_image：图从家·仓库取**指针**、交给通道发；只有通道说 sent>0 才算 done。

判据（手线第二批，第五轮回炉后）：
- 图有两个来源——绝对路径 / 家·仓库里的名字；两个都给或都不给 → `BAD_ARGUMENT`（不自作主张）；
- **发给谁 / 场景在本地就收口**：`target` 归一成**正整数**（真通道要求 JSON 里是数字，
  字符串会被它 400——这是用真通道抓到的集成缺陷），`scene` 只认 group／private；
  不成立就 `BAD_ARGUMENT`，**一次请求都不发**，绝不发出去之后才靠通道报错；
- 从仓库取时**不复制本体**：仓库里的图还在原地，账上记的是它的路径与指纹；
- 通道回 `ok=true 且 sent>0` 才 done；`sent=0` → `CHANNEL_BLOCKED`（origin 是**通道自己的**
  reason_code）；端口关着 → `CHANNEL_UNREACHABLE`；回的东西不对劲 → `SEND_FAILED`；
- 假通道**照真通道的规矩挑**（scene／target 类型／chunks／image.file），不合规回 400；
- 结果行写不进 → 改判 failed（照旧口径，新动作也一样）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.hand import actions, channel, ledger

from . import helpers

WHO = "1000xxxx"
#: 群号：通道要的是**数字**。字符串形式也接受，但出去时必须已经是 int。
GROUP = 700000001


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)
        self.assets = Path(self._tmp.name) / "assets"
        self.assets.mkdir()
        # 默认把通道指到一个**关着的端口**：没起假服务的用例一律走「连不上」，不碰真端口。
        self._env = mock.patch.dict(os.environ, {
            "HAND_CHANNEL_URL": "http://127.0.0.1:%d" % helpers.free_port(),
            "HAND_CHANNEL_TIMEOUT": "3"}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def channel(self, **ack):
        """挑剔的假通道：不合规就 400（宽容的假通道会漏掉缺陷）。"""
        return helpers.channel_service(**ack)

    def use(self, service) -> None:
        patcher = mock.patch.dict(os.environ, {"HAND_CHANNEL_URL": service.url})
        patcher.start()
        self.addCleanup(patcher.stop)


class FromWarehouseTest(BaseCase):
    def test_name_pulls_the_pointer_out_of_the_warehouse_and_posts_it(self):
        image = helpers.make_image(self.assets, "参考图.png")
        helpers.register_asset(self.root, image)
        with self.channel(sent=1, channel_id="msg-1") as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="group", target=GROUP, name="参考图.png",
                                     now=helpers.NOW, root=self.root)

        self.assertEqual("done", out["state"])
        self.assertEqual("OK", out["code"])
        self.assertEqual("send_image", out["action"])
        self.assertIn("sent=1", out["say"], "done 的人话必须带通道给的 sent")
        self.assertIn("msg-1", out["say"])

        # POST 出去的原文：/say、体里的 image.file 必须是 file:// 开头的绝对路径
        self.assertEqual(1, len(srv.records))
        record = srv.records[0]
        self.assertEqual("POST", record["method"])
        self.assertEqual("/say", record["path"])
        self.assertEqual("group", record["body"]["scene"])
        chunk = record["body"]["chunks"][0]["image"]
        self.assertTrue(chunk["file"].startswith("file://"), chunk["file"])
        self.assertTrue(chunk["file"].endswith(str(image)), chunk["file"])
        self.assertEqual("参考图.png", chunk["summary"])

        # 手账两行，结果行把这次说了什么记清楚
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "done"], [row["state"] for row in rows])
        self.assertEqual(rows[0]["ledger_id"], rows[1]["ledger_id"])
        done = rows[1]
        self.assertEqual("OK", done["origin"])
        self.assertEqual(helpers.sha256_of(helpers.IMAGE_BYTES), done["params"]["sha256"])
        self.assertEqual(1, done["params"]["sent"])
        self.assertEqual("msg-1", done["params"]["channel_id"])
        self.assertEqual("group", done["params"]["scene"])
        self.assertEqual(GROUP, done["params"]["target"])
        self.assertEqual(str(image), done["params"]["path"])

        # 只取指针：图还在原地，一个副本都没多出来
        self.assertTrue(image.is_file())
        self.assertEqual(["参考图.png"], sorted(part.name for part in self.assets.iterdir()))

    def test_target_goes_out_as_a_json_number_not_a_string(self):
        """真通道抓到的缺陷：字符串 target 会被它 400。出站必须是 JSON 数字。"""
        image = helpers.make_image(self.assets, "参考图.png")
        with self.channel() as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="group", target=str(GROUP),
                                     path=str(image), now=helpers.NOW, root=self.root)

        self.assertEqual("done", out["state"], out)
        sent = srv.records[0]["body"]["target"]
        self.assertIsInstance(sent, int)
        self.assertNotIsInstance(sent, bool)
        self.assertEqual(GROUP, sent)
        self.assertEqual(GROUP, out["detail"]["target"])

    def test_absolute_path_is_the_other_source(self):
        image = helpers.make_image(self.assets, "本地图.png")
        with self.channel(sent=2, channel_id="msg-2") as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="group", target=GROUP, path=str(image),
                                     now=helpers.NOW, root=self.root)
        self.assertEqual("done", out["state"])
        self.assertEqual(2, out["detail"]["sent"])
        self.assertEqual(str(image), out["detail"]["path"])
        self.assertEqual(helpers.sha256_of(helpers.IMAGE_BYTES), out["detail"]["sha256"])

    def test_unwritable_result_row_turns_success_into_failure(self):
        image = helpers.make_image(self.assets, "参考图.png")
        real = ledger.append
        calls = {"count": 0}

        def flaky(entry, *, root=None):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise OSError("盘满了（测试注入）")
            return real(entry, root=root)

        with self.channel(sent=1, channel_id="msg-3") as srv:
            self.use(srv)
            with mock.patch.object(ledger, "append", flaky):
                out = actions.send_image(who=WHO, scene="group", target=GROUP, path=str(image),
                                         now=helpers.NOW, root=self.root)

        self.assertEqual("failed", out["state"])
        self.assertEqual("LEDGER_WRITE_FAILED", out["code"])
        self.assertEqual("result", out["detail"]["stage"])
        self.assertEqual("done", out["detail"]["wanted"]["state"])


class ChannelFieldsTest(BaseCase):
    """场景与发给谁：本地就收口，**一次请求都不发**（回炉新增）。"""

    def setUp(self):
        super().setUp()
        self.image = helpers.make_image(self.assets, "参考图.png")

    def test_target_that_is_not_a_number_never_reaches_the_channel(self):
        with self.channel() as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="group", target="abc", path=str(self.image),
                                     now=helpers.NOW, root=self.root)

        self.assertEqual("failed", out["state"])
        self.assertEqual("BAD_ARGUMENT", out["code"])
        self.assertIn("群号或 QQ 号", out["say"])
        self.assertEqual([], srv.records, "参数不成立就不该发出去，更不该等通道 400")
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "failed"], [row["state"] for row in rows])
        self.assertEqual("BAD_TARGET", rows[-1]["origin"])
        self.assertEqual("channel_fields", rows[-1]["params"].get("stage", "channel_fields"))

    def test_zero_and_negative_and_float_targets_are_bad_arguments(self):
        for bad in ("0", 0, "-7", -7, "7.5", 7.5, "", None, True):
            with self.subTest(target=bad):
                out = actions.send_image(who=WHO, scene="group", target=bad,
                                         path=str(self.image), now=helpers.NOW, root=self.root)
                self.assertEqual("BAD_ARGUMENT", out["code"], bad)
                self.assertEqual("BAD_TARGET", helpers.ledger_lines(self.root)[-1]["origin"])

    def test_scene_must_be_group_or_private(self):
        with self.channel() as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="客厅", target=GROUP, path=str(self.image),
                                     now=helpers.NOW, root=self.root)
        self.assertEqual("BAD_ARGUMENT", out["code"])
        self.assertIn("group", out["say"])
        self.assertIn("private", out["say"])
        self.assertEqual([], srv.records)
        self.assertEqual("BAD_SCENE", helpers.ledger_lines(self.root)[-1]["origin"])

    def test_private_is_fine_too(self):
        with self.channel() as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="private", target=GROUP,
                                     path=str(self.image), now=helpers.NOW, root=self.root)
        self.assertEqual("done", out["state"])
        self.assertEqual("private", srv.records[0]["body"]["scene"])

    def test_channel_itself_refuses_to_send_a_string_target(self):
        """通道模块单独被调用时也不许把违规 body 发出去（内部闸，不发请求）。"""
        with self.channel() as srv:
            outcome = channel.say_image(scene="group", target=str(GROUP), path=self.image,
                                        name="参考图.png", url=srv.url)
        self.assertEqual("SEND_FAILED", outcome["code"])
        self.assertIs(True, outcome["local"])
        self.assertIn("内部闸", outcome["error"])
        self.assertEqual([], srv.records)


class ChannelAnswersTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.image = helpers.make_image(self.assets, "参考图.png")

    def test_sent_zero_is_blocked_and_keeps_the_channel_reason_code(self):
        with self.channel(sent=0, reason_code="TARGET_MUTED", channel_id="msg-4") as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="group", target=GROUP, path=str(self.image),
                                     now=helpers.NOW, root=self.root)

        self.assertEqual("failed", out["state"])
        self.assertEqual("CHANNEL_BLOCKED", out["code"])
        self.assertIn("sent=0", out["say"])
        self.assertIn("TARGET_MUTED", out["say"], "理由码是通道给的，要原样带出来")
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "failed"], [row["state"] for row in rows])
        self.assertEqual("TARGET_MUTED", rows[-1]["origin"], "origin 必须是通道自己的理由码")
        self.assertEqual(0, rows[-1]["params"]["sent"])

    def test_closed_port_is_unreachable_not_sent(self):
        out = actions.send_image(who=WHO, scene="group", target=GROUP, path=str(self.image),
                                 now=helpers.NOW, root=self.root)
        self.assertEqual("failed", out["state"])
        self.assertEqual("CHANNEL_UNREACHABLE", out["code"])
        self.assertIn("没连上", out["say"])
        self.assertNotIn("发出去了", out["say"])
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "failed"], [row["state"] for row in rows])
        self.assertEqual("CHANNEL_UNREACHABLE", rows[-1]["code"])
        self.assertNotEqual("OK", rows[-1]["origin"])

    def test_http_error_is_send_failed(self):
        with helpers.FakeService(helpers.channel_gate(
                helpers.json_reply({"ok": False, "sent": 0, "reason_code": "BOOM"},
                                   status=502))) as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="group", target=GROUP, path=str(self.image),
                                     now=helpers.NOW, root=self.root)
        self.assertEqual("failed", out["state"])
        self.assertEqual("SEND_FAILED", out["code"])
        self.assertIn("不对劲", out["say"])

    def test_garbage_body_is_send_failed(self):
        def reply(record):
            return 200, {"content-type": "text/plain"}, b"not json at all"

        with helpers.FakeService(helpers.channel_gate(reply)) as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="group", target=GROUP, path=str(self.image),
                                     now=helpers.NOW, root=self.root)
        self.assertEqual("SEND_FAILED", out["code"])
        self.assertEqual("failed", out["state"])


class NotFoundTest(BaseCase):
    def test_unknown_name_is_image_not_found_in_plain_words(self):
        helpers.make_image(self.assets, "别的图.png")     # 仓库里根本没有它
        with self.channel() as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="group", target=GROUP,
                                     name="不存在的图.png", now=helpers.NOW, root=self.root)

        self.assertEqual("failed", out["state"])
        self.assertEqual("IMAGE_NOT_FOUND", out["code"])
        self.assertIn("图没找着", out["say"])
        self.assertEqual([], srv.records, "图都没找着，一个请求都不该发")
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "failed"], [row["state"] for row in rows])
        self.assertEqual("NAME_NOT_IN_WAREHOUSE", rows[-1]["origin"])

    def test_warehouse_card_whose_file_is_gone_is_not_found(self):
        image = helpers.make_image(self.assets, "会消失.png")
        helpers.register_asset(self.root, image)
        image.unlink()
        out = actions.send_image(who=WHO, scene="group", target=GROUP, name="会消失.png",
                                 now=helpers.NOW, root=self.root)
        self.assertEqual("IMAGE_NOT_FOUND", out["code"])
        self.assertEqual("FILE_MISSING", helpers.ledger_lines(self.root)[-1]["origin"])

    def test_missing_path_is_not_found(self):
        out = actions.send_image(who=WHO, scene="group", target=GROUP,
                                 path=str(self.assets / "没有这个文件.png"),
                                 now=helpers.NOW, root=self.root)
        self.assertEqual("IMAGE_NOT_FOUND", out["code"])
        self.assertIn("图没找着", out["say"])


class ArgumentTest(BaseCase):
    def test_both_sources_is_bad_argument(self):
        image = helpers.make_image(self.assets, "参考图.png")
        out = actions.send_image(who=WHO, scene="group", target=GROUP, path=str(image),
                                 name="参考图.png", now=helpers.NOW, root=self.root)
        self.assertEqual("failed", out["state"])
        self.assertEqual("BAD_ARGUMENT", out["code"])
        self.assertEqual("BOTH_PATH_AND_NAME", helpers.ledger_lines(self.root)[-1]["origin"])

    def test_neither_source_is_bad_argument(self):
        out = actions.send_image(who=WHO, scene="group", target=GROUP, now=helpers.NOW,
                                 root=self.root)
        self.assertEqual("BAD_ARGUMENT", out["code"])
        self.assertEqual("NEITHER_PATH_NOR_NAME", helpers.ledger_lines(self.root)[-1]["origin"])

    def test_relative_path_is_refused_rather_than_guessed(self):
        out = actions.send_image(who=WHO, scene="group", target=GROUP, path="参考图.png",
                                 now=helpers.NOW, root=self.root)
        self.assertEqual("BAD_ARGUMENT", out["code"])
        self.assertIn("绝对路径", out["say"])
        self.assertEqual("PATH_NOT_ABSOLUTE", helpers.ledger_lines(self.root)[-1]["origin"])


class GateTest(BaseCase):
    def test_quota_exhausted_refuses_before_any_network(self):
        helpers.write_limits(self.root, 0)
        image = helpers.make_image(self.assets, "参考图.png")
        with self.channel() as srv:
            self.use(srv)
            out = actions.send_image(who=WHO, scene="group", target=GROUP, path=str(image),
                                     now=helpers.NOW, root=self.root)
        self.assertEqual("refused", out["state"])
        self.assertEqual("QUOTA_EXCEEDED", out["code"])
        self.assertEqual([], srv.records)

    def test_risk_is_reversible_and_needs_no_grant(self):
        from organs.hand import permit
        self.assertEqual("可撤销", permit.risk_of("send_image"))
        self.assertFalse(permit.needs_grant("send_image"))
        self.assertEqual("CHANNEL_BLOCKED", channel.CHANNEL_BLOCKED)
        self.assertEqual(("group", "private"), channel.SCENES)


if __name__ == "__main__":
    unittest.main()
