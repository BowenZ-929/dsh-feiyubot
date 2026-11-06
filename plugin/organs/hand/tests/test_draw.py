"""draw：去外服要一张图 → 落自己的盘（经 store）→ 登记进家·仓库 → 发出去。

判据（手线第二批）：
- 选图顺序固定：先取 `marked == false` 的第一张，没有才取 `marked == true` 的第一张；
  **只有带标记的不算失败**，账上如实记 `marked: true`；一张都没有 → `DRAW_FAILED`；
- 没配 `HAND_DRAW_URL`／连不上／超时／回的东西认不出 → `DRAW_UNAVAILABLE`，人话「这个我画不出来」；
- 落盘只经 `store.land_draw`：文件名是内容指纹的前 16 位，扩展名从响应／URL／Content-Type 猜；
- 登记进家·仓库**只记指针**（不复制本体），卡上的来源与 marked 也进手账；
- 发出去与 send_image 同一条判据；发送失败时图已落盘、仓库已有卡 → `failed` 且 `landed=true`，
  人话要说清「画出来了、但没发出去」；
- **场景／发给谁不过闸就连画都不画**：`scene` 只认 group／private、`target` 归一成正整数，
  不成立 → `BAD_ARGUMENT`，生图服务与通道**一次请求都收不到**（回炉新增）；
- 假通道照真通道的规矩挑（scene／target 类型／chunks／image.file），不合规回 400。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.hand import actions, draw as draw_client, ledger, store

from . import helpers

WHO = "1000xxxx"
#: 群号：通道要的是**数字**（字符串形式在 actions 里归一，出站时必须是 int）。
GROUP = 700000001


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)
        self.pictures = Path(self._tmp.name) / "pictures"
        self.pictures.mkdir()
        self._env = mock.patch.dict(os.environ, {
            "HAND_DRAW_URL": "", "HAND_DRAW_KEY": "", "HAND_DRAW_TIMEOUT": "3",
            "HAND_CHANNEL_URL": "http://127.0.0.1:%d" % helpers.free_port(),
            "HAND_CHANNEL_TIMEOUT": "3"}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def draw_service(self, images, ok: bool = True):
        return helpers.FakeService(helpers.json_reply({"ok": ok, "images": images}))

    def use(self, **services) -> None:
        """services: HAND_DRAW_URL=<service> / HAND_CHANNEL_URL=<service>"""
        env = {name: service.url for name, service in services.items()}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def channel_ok(self, sent: int = 1, reason_code: str = "sent"):
        """挑剔的假通道：不合规就 400（宽容的假通道会漏掉集成缺陷）。"""
        return helpers.channel_service(sent=sent, reason_code=reason_code, channel_id="msg-9")


class MarkedTest(BaseCase):
    def test_only_marked_still_succeeds_and_is_recorded_as_marked(self):
        picture = helpers.make_image(self.pictures, "画好的.png", b"only-marked-bytes")
        with self.draw_service([{"path": str(picture), "marked": True}]) as painter, \
                self.channel_ok() as srv:
            self.use(HAND_DRAW_URL=painter, HAND_CHANNEL_URL=srv)
            out = actions.draw(who=WHO, description="一只在窗台上打盹的猫", scene="group",
                               target=GROUP, now=helpers.NOW, root=self.root)

        self.assertEqual("done", out["state"])
        self.assertEqual("OK", out["code"])
        self.assertIs(True, out["detail"]["marked"])
        self.assertIn("marked=true", out["say"], "只有带标记的，账上要如实说")

        # 落点：<手实盘>/draw/<sha256 前 16 位>.png
        landed = Path(out["detail"]["path"])
        self.assertEqual(store.draw_dir(self.root), landed.parent)
        self.assertEqual(helpers.sha256_of(b"only-marked-bytes")[:16], landed.stem)
        self.assertEqual(".png", landed.suffix)
        self.assertEqual(b"only-marked-bytes", landed.read_bytes())

        # 登记进仓库：素材卡记的是**指针**，note 说清来源与 marked
        cards = helpers.warehouse(self.root)
        self.assertEqual(1, len(cards))
        self.assertEqual("素材", cards[0]["kind"])
        self.assertEqual(str(landed), cards[0]["path"])
        self.assertIn("来源=draw", cards[0]["note"])
        self.assertIn("marked=true", cards[0]["note"])
        self.assertEqual(str(landed), out["detail"]["path"])

        # 发出去的是刚落盘的那张
        self.assertEqual(1, len(srv.records))
        chunk = srv.records[0]["body"]["chunks"][0]["image"]
        self.assertTrue(chunk["file"].startswith("file://"))
        self.assertTrue(chunk["file"].endswith(str(landed)))

        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "done"], [row["state"] for row in rows])
        self.assertIs(True, rows[-1]["params"]["marked"])
        self.assertIs(True, rows[-1]["params"]["landed"])
        self.assertEqual(out["detail"]["sha256"], rows[-1]["params"]["sha256"])

    def test_unmarked_wins_even_when_the_marked_one_comes_first(self):
        marked = helpers.make_image(self.pictures, "带标记.png", b"marked-bytes")
        plain = helpers.make_image(self.pictures, "没标记.png", b"unmarked-bytes")
        with self.draw_service([{"path": str(marked), "marked": True},
                                {"path": str(plain), "marked": False}]) as painter, \
                self.channel_ok() as srv:
            self.use(HAND_DRAW_URL=painter, HAND_CHANNEL_URL=srv)
            out = actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                               now=helpers.NOW, root=self.root)

        self.assertEqual("done", out["state"])
        self.assertIs(False, out["detail"]["marked"])
        self.assertEqual(helpers.sha256_of(b"unmarked-bytes"), out["detail"]["sha256"])
        self.assertEqual(helpers.sha256_of(b"unmarked-bytes")[:16],
                         Path(out["detail"]["path"]).stem)
        self.assertIn("marked=false", helpers.warehouse(self.root)[0]["note"])

    def test_two_unmarked_takes_the_first(self):
        first = helpers.make_image(self.pictures, "一.png", b"first-bytes")
        second = helpers.make_image(self.pictures, "二.png", b"second-bytes")
        with self.draw_service([{"path": str(first), "marked": False},
                                {"path": str(second), "marked": False}]) as painter, \
                self.channel_ok() as srv:
            self.use(HAND_DRAW_URL=painter, HAND_CHANNEL_URL=srv)
            out = actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                               now=helpers.NOW, root=self.root)
        self.assertEqual(helpers.sha256_of(b"first-bytes"), out["detail"]["sha256"])


class UnavailableTest(BaseCase):
    def test_without_a_service_it_says_it_cannot_draw(self):
        with mock.patch.dict(os.environ, {"HAND_DRAW_URL": ""}):
            out = actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                               now=helpers.NOW, root=self.root)
        self.assertEqual("failed", out["state"])
        self.assertEqual("DRAW_UNAVAILABLE", out["code"])
        self.assertIn("这个我画不出来", out["say"])
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "failed"], [row["state"] for row in rows])
        self.assertEqual("HAND_DRAW_URL_UNSET", rows[-1]["origin"])
        self.assertFalse(store.draw_dir(self.root).is_dir(), "没画出来就不该建 draw 目录")

    def test_closed_port_is_unavailable(self):
        with mock.patch.dict(os.environ,
                             {"HAND_DRAW_URL": "http://127.0.0.1:%d" % helpers.free_port()}):
            out = actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                               now=helpers.NOW, root=self.root)
        self.assertEqual("DRAW_UNAVAILABLE", out["code"])
        self.assertIn("这个我画不出来", out["say"])
        self.assertEqual("UNREACHABLE", helpers.ledger_lines(self.root)[-1]["origin"])

    def test_unreadable_answer_is_unavailable(self):
        def reply(record):
            return 200, {"content-type": "text/plain"}, b"<html>nope</html>"

        with helpers.FakeService(reply) as painter:
            self.use(HAND_DRAW_URL=painter)
            out = actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                               now=helpers.NOW, root=self.root)
        self.assertEqual("DRAW_UNAVAILABLE", out["code"])
        self.assertEqual("BAD_RESPONSE", helpers.ledger_lines(self.root)[-1]["origin"])

    def test_no_images_at_all_is_draw_failed(self):
        with self.draw_service([]) as painter:
            self.use(HAND_DRAW_URL=painter)
            out = actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                               now=helpers.NOW, root=self.root)
        self.assertEqual("failed", out["state"])
        self.assertEqual("DRAW_FAILED", out["code"])
        self.assertIn("没落盘", out["say"])
        self.assertFalse(store.draw_dir(self.root).is_dir(), "没图就不该建 draw 目录")


class DownloadTest(BaseCase):
    def test_url_is_downloaded_and_the_extension_follows_the_content_type(self):
        def reply(record):
            if record["method"] == "GET":
                return 200, {"content-type": "image/jpeg"}, b"\xff\xd8fake-jpeg-bytes"
            payload = {"ok": True, "images": [{"url": record["base"] + "/img", "marked": False}]}
            return (200, {"content-type": "application/json"},
                    json.dumps(payload).encode("utf-8"))

        with helpers.FakeService(reply) as painter, self.channel_ok() as srv:
            self.use(HAND_DRAW_URL=painter, HAND_CHANNEL_URL=srv)
            out = actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                               now=helpers.NOW, root=self.root)

        self.assertEqual("done", out["state"])
        self.assertEqual(".jpg", Path(out["detail"]["path"]).suffix)
        self.assertEqual("url", out["detail"]["source"])
        self.assertEqual(2, len(painter.records), "一次 POST 要图、一次 GET 下载")
        self.assertEqual(helpers.sha256_of(b"\xff\xd8fake-jpeg-bytes")[:16],
                         Path(out["detail"]["path"]).stem)

    def test_authorization_header_follows_the_configured_key(self):
        picture = helpers.make_image(self.pictures, "画.png", b"key-bytes")
        with self.draw_service([{"path": str(picture), "marked": False}]) as painter, \
                self.channel_ok() as srv:
            self.use(HAND_DRAW_URL=painter, HAND_CHANNEL_URL=srv)
            with mock.patch.dict(os.environ, {"HAND_DRAW_KEY": ""}):
                actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                             now=helpers.NOW, root=self.root)
            self.assertNotIn("authorization",
                             {key.lower() for key in painter.records[0]["headers"]})
            with mock.patch.dict(os.environ, {"HAND_DRAW_KEY": "test-key"}):
                actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                             now=helpers.NOW, root=self.root)
            sent = {key.lower(): value for key, value in painter.records[1]["headers"].items()}
            self.assertEqual("Bearer test-key", sent.get("authorization"))
        self.assertEqual("猫", painter.records[0]["body"]["prompt"])
        self.assertIs(True, painter.records[0]["body"]["no_watermark"])


class ChannelFieldsTest(BaseCase):
    """场景／发给谁不成立：**连画都不画**，两个外部服务一次请求都收不到（回炉新增）。"""

    def test_bad_target_never_reaches_the_painter_or_the_channel(self):
        with self.draw_service([{"path": str(helpers.make_image(self.pictures, "画.png")),
                                 "marked": False}]) as painter, self.channel_ok() as srv:
            self.use(HAND_DRAW_URL=painter, HAND_CHANNEL_URL=srv)
            out = actions.draw(who=WHO, description="猫", scene="group", target="abc",
                               now=helpers.NOW, root=self.root)

        self.assertEqual("failed", out["state"])
        self.assertEqual("BAD_ARGUMENT", out["code"])
        self.assertIn("群号或 QQ 号", out["say"])
        self.assertEqual([], painter.records, "发给谁都不对，画了也发不出去")
        self.assertEqual([], srv.records)
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "failed"], [row["state"] for row in rows])
        self.assertEqual("BAD_TARGET", rows[-1]["origin"])
        self.assertFalse(store.draw_dir(self.root).is_dir(), "没画成事就不该落盘")

    def test_bad_scene_never_reaches_the_painter(self):
        with self.draw_service([{"path": str(helpers.make_image(self.pictures, "画.png")),
                                 "marked": False}]) as painter, self.channel_ok() as srv:
            self.use(HAND_DRAW_URL=painter, HAND_CHANNEL_URL=srv)
            out = actions.draw(who=WHO, description="猫", scene="客厅", target=GROUP,
                               now=helpers.NOW, root=self.root)
        self.assertEqual("BAD_ARGUMENT", out["code"])
        self.assertIn("private", out["say"])
        self.assertEqual([], painter.records)
        self.assertEqual("BAD_SCENE", helpers.ledger_lines(self.root)[-1]["origin"])

    def test_digit_string_target_goes_out_as_a_number(self):
        picture = helpers.make_image(self.pictures, "画.png", b"digit-target-bytes")
        with self.draw_service([{"path": str(picture), "marked": False}]) as painter, \
                self.channel_ok() as srv:
            self.use(HAND_DRAW_URL=painter, HAND_CHANNEL_URL=srv)
            out = actions.draw(who=WHO, description="猫", scene="group", target=str(GROUP),
                               now=helpers.NOW, root=self.root)
        self.assertEqual("done", out["state"], out)
        sent = srv.records[0]["body"]["target"]
        self.assertIsInstance(sent, int)
        self.assertNotIsInstance(sent, bool)
        self.assertEqual(GROUP, sent)


class SendBackTest(BaseCase):
    def test_send_failure_keeps_the_picture_and_says_so(self):
        picture = helpers.make_image(self.pictures, "画.png", b"kept-bytes")
        with self.draw_service([{"path": str(picture), "marked": False}]) as painter, \
                self.channel_ok(sent=0, reason_code="RATE_LIMITED") as srv:
            self.use(HAND_DRAW_URL=painter, HAND_CHANNEL_URL=srv)
            out = actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                               now=helpers.NOW, root=self.root)

        self.assertEqual("failed", out["state"])
        self.assertEqual("CHANNEL_BLOCKED", out["code"])
        self.assertIn("画出来了、但没发出去", out["say"])
        self.assertIn("RATE_LIMITED", out["say"])
        self.assertIs(True, out["detail"]["landed"])
        self.assertTrue(Path(out["detail"]["path"]).is_file(), "图已经落了盘，不许删了装作没画")
        self.assertEqual(1, len(helpers.warehouse(self.root)), "仓库里的卡也还在")
        self.assertEqual("RATE_LIMITED", helpers.ledger_lines(self.root)[-1]["origin"])
        self.assertIs(True, helpers.ledger_lines(self.root)[-1]["params"]["landed"])

    def test_unwritable_result_row_turns_success_into_failure(self):
        picture = helpers.make_image(self.pictures, "画.png", b"ledger-bytes")
        real = ledger.append
        calls = {"count": 0}

        def flaky(entry, *, root=None):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise OSError("盘满了（测试注入）")
            return real(entry, root=root)

        with self.draw_service([{"path": str(picture), "marked": False}]) as painter, \
                self.channel_ok() as srv:
            self.use(HAND_DRAW_URL=painter, HAND_CHANNEL_URL=srv)
            with mock.patch.object(ledger, "append", flaky):
                out = actions.draw(who=WHO, description="猫", scene="group", target=GROUP,
                                   now=helpers.NOW, root=self.root)

        self.assertEqual("failed", out["state"])
        self.assertEqual("LEDGER_WRITE_FAILED", out["code"])
        self.assertEqual("result", out["detail"]["stage"])
        # 图确实落了盘（动手了），但账没记上，所以对外绝不说是成功
        self.assertTrue(Path(out["detail"]["path"]).is_file())


class StoreLandingTest(BaseCase):
    def test_landing_rejects_empty_bytes(self):
        with self.assertRaises(ValueError):
            store.land_draw(b"", root=self.root)
        self.assertFalse(store.draw_dir(self.root).is_dir())

    def test_same_bytes_land_on_the_same_name(self):
        first = store.land_draw(b"same-bytes", root=self.root)
        second = store.land_draw(b"same-bytes", root=self.root)
        self.assertEqual(first, second)
        self.assertEqual(helpers.sha256_of(b"same-bytes")[:16], first.stem)
        self.assertEqual(0o600, first.stat().st_mode & 0o777)

    def test_ext_falls_back_to_png_when_nothing_says_otherwise(self):
        landed = store.land_draw(b"unknown-format", root=self.root, ext="")
        self.assertEqual(".png", landed.suffix)


class RiskTest(unittest.TestCase):
    def test_draw_is_reversible_and_needs_no_grant(self):
        from organs.hand import permit
        self.assertEqual("可撤销", permit.risk_of("draw"))
        self.assertFalse(permit.needs_grant("draw"))
        self.assertEqual("DRAW_FAILED", draw_client.DRAW_FAILED)


if __name__ == "__main__":
    unittest.main()
