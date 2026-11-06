"""「给我看你的自画像」那条线的验收：认出来就把家里的图发出去、认不出就反问、闲聊一个字都不动。

判据（每条都对应交付里的一句话）：
1. **给我看你的自画像** → 真手（send_image）+ 临时 home：手账里有一行 done 的 send_image，
   回包 look.ok 为真、chunk 带 image（file://）、名字是「自画像」；
2. **给我看张三的照片** → look.ok 为假、why 表示没找着、回话就是数据里「没找着」那一句
   （手账里那一行是 IMAGE_NOT_FOUND）；
3. **给我看**（有意图没指名）→ **手一次都不调**（假手计数 0）、回话是反问；
4. **今天好热** → 不调手、回包里没有 look 那一格；
5. **成功那轮的 reply 不含「我画了／我拍的／我照的」**（清单在数据里，改数据即改判据）；
6. **词表 / 文案改数据** → 行为跟着变（代码里没有这些词）；
7. **数据坏（缺键）** → ValueError 并点名缺的是哪一格。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.brain import BrainStore
from organs.hand import store as hand_store
from organs.hand.tests import helpers
from organs.mind import conversation, look
from organs.soul import load as load_soul

PLUGIN = Path(__file__).resolve().parents[3]
SOUL_FIXTURES = PLUGIN / "organs" / "soul" / "fixtures"
CONFIG = {"provider": "fake", "model": "fake-model", "base_url": "http://example.invalid/v1",
          "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 1000, "retries": 0,
          "temperature": 0.0, "max_tokens": 200}
ENV = {"QQAGENT_LLM_KEY": "test-key"}
WHO = "3425635473"
#: 固定的「现在」：用例不看本机时钟（与手那边的 helpers.NOW 同一个时刻）。
NOW = helpers.NOW
PORTRAIT = "自画像"        # 家里的格子名（organs/home/residents.py 的 ARTWORK_KIND）
MISSING = "张三的照片"      # 家里没有这一张：认得出、找不着


def _transport(reply: str):
    """假 transport：一个字节都不发真网络。"""
    def send(url, headers, payload, timeout):
        body = {"choices": [{"message": {"content": reply}}]}
        return 200, json.dumps(body, ensure_ascii=False).encode("utf-8")
    return send


def _rendered_ok(cfg, name: str) -> str:
    """数据里 say.ok 那一句（{name} 填成这个名字）：断言「口径来自数据」时用它当准绳。"""
    return str(cfg["say"]["ok"]).replace("{name}", name)


class LookPlanTest(unittest.TestCase):
    """只认这一类话：抽名字、没指名、闲聊、改数据、数据坏。**一个字节都不网络。**"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _data(self):
        return look.load_config()

    def test_it_takes_the_name_out_of_the_sentence(self):
        cases = [("给我看你的自画像", PORTRAIT),
                 ("给我看看你的自画像。", PORTRAIT),
                 ("看看你的自画像", PORTRAIT),
                 ("把自画像给我看", PORTRAIT),
                 ("让我看看自画像", PORTRAIT),
                 ("给我看" + MISSING, MISSING)]
        for said, name in cases:
            with self.subTest(said=said):
                found = look.plan(said)
                self.assertIsNotNone(found, "这一句该认成要看图：%r" % (said,))
                self.assertEqual(name, found["name"], "抽出来的名字不对：%r" % (said,))
        self.assertEqual("", look.plan("给我看")["name"], "没指名就是空名字（不是随便给一张）")

    def test_chit_chat_is_not_a_look(self):
        for said in ("今天好热", "今天天气怎么样", "自画像真好看"):
            with self.subTest(said=said):
                self.assertIsNone(look.plan(said), "不沾意图词的句子不许认成看图")
                self.assertIsNone(look.handle(said, who=WHO), "闲聊不许动任何东西")

    def test_a_bare_request_asks_and_never_touches_the_hand(self):
        data = self._data()
        with mock.patch.object(look.hand_actions, "send_image") as hand:
            got = look.handle("给我看", who=WHO)
        self.assertEqual(0, hand.call_count, "没指名就不许调手（一次额度都不烧）")
        self.assertIsNotNone(got, "没指名也是一种回应（反问）")
        self.assertFalse(got["ok"])
        self.assertEqual(look.WHY_NO_NAME, got["why"])
        self.assertEqual(data["say"]["ask"], got["say"], "反问那句来自数据")
        self.assertEqual("", look.packet(got)["name"])

    def test_the_words_and_copy_come_from_the_data(self):
        data = json.loads(json.dumps(self._data(), ensure_ascii=False))
        data["intent"]["words"] = ["让我瞧一眼"]
        data["say"]["ok"] = "这份数据说了算：{name}"
        live = self.tmp / "mind"
        live.mkdir()
        (live / look.DATA_NAME).write_text(json.dumps(data, ensure_ascii=False),
                                           encoding="utf-8")
        config = look.load_config(root=live)

        self.assertIsNone(look.plan("给我看你的自画像", config=config),
                          "词表改了：老说法不再算看图")
        found = look.plan("让我瞧一眼自画像", config=config)
        self.assertEqual(PORTRAIT, found["name"], "词表改了，抽名字的框也跟着这句话走")
        read = {"ok": True, "name": PORTRAIT, "say": "", "image": "file:///x", "intent": "让我瞧一眼"}
        # 干净的那句照用（允许她补一句）；命红线才换成数据里那句 ok——那条口径也得跟着数据走
        self.assertEqual("她随口说的一句。", look.say(read, "她随口说的一句。", config=config))
        self.assertEqual("这份数据说了算：" + PORTRAIT, look.say(read, "我画了自画像",
                                                                config=config),
                         "成功那句口径没跟着数据走")

    def test_broken_data_names_the_key_it_is_missing(self):
        live = self.tmp / "mind"
        live.mkdir()
        data = json.loads(json.dumps(self._data(), ensure_ascii=False))
        data["say"].pop("ok")
        (live / look.DATA_NAME).write_text(json.dumps(data, ensure_ascii=False),
                                           encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            look.load_config(root=live)
        self.assertIn("say.ok", str(caught.exception), "报错要点名缺的是哪一格")

    def test_a_broken_word_list_is_a_named_error_too(self):
        live = self.tmp / "mind"
        live.mkdir()
        data = json.loads(json.dumps(self._data(), ensure_ascii=False))
        data["intent"]["words"] = []
        (live / look.DATA_NAME).write_text(json.dumps(data, ensure_ascii=False),
                                           encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            look.load_config(root=live)
        self.assertIn("intent.words", str(caught.exception))

    def test_the_red_line_list_and_the_ok_copy_carry_the_name(self):
        data = self._data()
        for word in ("我画了", "我拍的", "我照的"):
            self.assertIn(word, data["say"]["forbid"], "禁止清单里少了这一条：%s" % word)
        read = {"ok": True, "name": PORTRAIT, "say": "", "image": "file:///x", "intent": "给我看"}
        said = look.say(read, "我画了你的自画像")
        self.assertNotIn("我画了", said, "红线：图是家里的，不是她现做的")
        self.assertEqual(_rendered_ok(data, PORTRAIT), said, "命红线就换成数据里那一句口径")
        self.assertTrue(read["fixed"], "换成数据里那一句就是固定话：装配不许锯它")


class LookHandTest(unittest.TestCase):
    """真手 + 临时 home：手账里有那一行、图确实是家里那张、找不到就说找不到。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = helpers.make_root(self._tmp.name)      # 手的实盘根
        self.home = helpers.make_home(self.root)
        # 默认把通道指到一个**关着的端口**：没起假服务的用例一律走「连不上」，不碰真端口。
        self._env = mock.patch.dict(os.environ, {
            "HAND_CHANNEL_URL": "http://127.0.0.1:%d" % helpers.free_port(),
            "HAND_CHANNEL_TIMEOUT": "3"}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _use(self, service) -> None:
        patcher = mock.patch.dict(os.environ, {"HAND_CHANNEL_URL": service.url})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _look(self, text: str, **kwargs):
        return look.handle(text, who=WHO, scene="private", now=NOW, root=self.root, **kwargs)

    def test_the_portrait_goes_out_through_the_real_hand(self):
        image = helpers.register_portrait(self.root, "江迟-人设卡.jpg")
        with helpers.channel_service(sent=1, channel_id="say-1") as srv:
            self._use(srv)
            got = self._look("给我看你的" + PORTRAIT)
            records = list(srv.records)

        self.assertTrue(got["ok"], got)
        self.assertEqual(PORTRAIT, got["name"])
        self.assertEqual("", got["why"])
        self.assertEqual("file://" + str(image), got["image"], "图指向家里的那一张，不复制本体")
        self.assertEqual(look.PACKET_KEYS, tuple(look.packet(got)), "回包那一格的键是冻结的")
        # 通道真收到的那一泡：image.file 就是家里那张（手给的 file://）
        chunk = records[0]["body"]["chunks"][0]["image"]
        self.assertTrue(chunk["file"].endswith(str(image)), chunk["file"])
        # 手账：reserved + done 两行，动作是 send_image
        rows = helpers.ledger_lines(self.root)
        self.assertEqual(["reserved", "done"], [row["state"] for row in rows], rows)
        self.assertEqual("send_image", rows[1]["action"])
        self.assertEqual("OK", rows[1]["code"])
        self.assertEqual(1, rows[1]["params"]["sent"], "只有通道说 sent>0 才算成")
        # 手账是索引：长串会被截断（ledger.SUMMARY_CHARS + 省略号），这里只核对「指着哪一间格」
        self.assertIn(PORTRAIT, str(rows[1]["params"]["path"]), rows[1]["params"]["path"])
        self.assertTrue(rows[1]["params"]["sha256"], "账上要记指纹：不发同一张也认得出是哪张")

    def test_a_picture_that_is_not_at_home_is_missing(self):
        data = look.load_config()
        with helpers.channel_service() as srv:
            self._use(srv)
            got = self._look("给我看" + MISSING)
            records = list(srv.records)

        self.assertFalse(got["ok"], "家里没有就不许说发过去了")
        self.assertEqual(look.WHY_NOT_FOUND, got["why"])
        self.assertIsNone(got["image"])
        self.assertEqual(data["say"]["missing"], got["say"], "「没找着」那句来自数据")
        self.assertEqual([], records, "图都没找着，通道一个请求都不该收到")
        rows = helpers.ledger_lines(self.root)
        self.assertEqual("failed", rows[-1]["state"])
        self.assertEqual("IMAGE_NOT_FOUND", rows[-1]["code"])

    def test_a_channel_that_is_not_there_says_it_did_not_go_out(self):
        helpers.register_portrait(self.root, "江迟-人设卡.jpg")
        data = look.load_config()
        got = self._look("给我看你的" + PORTRAIT)      # setUp 把通道指向关着的端口
        self.assertFalse(got["ok"], "连不上就不许说发过了")
        self.assertEqual("CHANNEL_UNREACHABLE", got["why"], "why 原样带手那边的码")
        self.assertEqual(data["say"]["fail"], got["say"], "「没发出去」那句来自数据")


class LookInAssemblyTest(unittest.TestCase):
    """装配那一头：图挂到最后一泡上、回包多 look 一格、红线对着 reply 的文本判。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patches = mock.patch.dict(os.environ, {
            "QQAGENT_HOME": str(self.tmp / "home"),          # 装配说明书的实盘根：空着，走出厂
            "HEART_ROOT": str(self.tmp / "heart"),           # 心的状态文件：临时根
            "MOUTH_TRACE_DIR": str(self.tmp / "trace"),      # 弃言账本：临时根
            "HAND_CHANNEL_URL": "http://127.0.0.1:%d" % helpers.free_port(),
            "HAND_CHANNEL_TIMEOUT": "3",
        })
        patches.start()
        self.addCleanup(patches.stop)
        self.store = self._soul()
        self.brain = BrainStore(self.tmp / "brain")
        self.hand_root = helpers.make_root(self._tmp.name)   # 家的根是它的兄弟目录
        helpers.make_home(self.hand_root)
        self.portrait = helpers.register_portrait(self.hand_root, "江迟-人设卡.jpg")

    def _soul(self):
        target = self.tmp / "soul"
        shutil.copytree(str(SOUL_FIXTURES / "valid"), str(target))
        return load_soul(target)

    def _use(self, service) -> None:
        patcher = mock.patch.dict(os.environ, {"HAND_CHANNEL_URL": service.url})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _answer(self, text: str, reply: str = "在的。"):
        return conversation.answer(text, WHO, store=self.store, brain=self.brain,
                                   now=NOW, transport=_transport(reply), config=CONFIG,
                                   env=ENV, remind_root=self.hand_root, scene="private")

    def _sent_images(self, chunks):
        return [one["image"] for one in chunks if isinstance(one.get("image"), dict)]

    def test_the_picture_rides_in_the_last_chunk_and_the_packet_says_ok(self):
        with helpers.channel_service(sent=1, channel_id="say-9") as srv:
            self._use(srv)
            got = self._answer("给我看你的" + PORTRAIT)
            records = list(srv.records)

        self.assertTrue(got["ok"])
        self.assertEqual(look.PACKET_KEYS, tuple(got["look"]), "回包那一格的键是冻结的")
        cell = got["look"]
        self.assertTrue(cell["ok"], cell)
        self.assertEqual(PORTRAIT, cell["name"])
        self.assertEqual(WHO, cell["target"], "图发给说话的那个人")
        self.assertEqual("file://" + str(self.portrait), cell["image"])
        self.assertTrue(got["chunks"], "这一轮得有话出口")
        images = self._sent_images(got["chunks"])
        self.assertEqual(1, len(images), "图只挂一泡：%r" % (got["chunks"],))
        self.assertEqual(cell["image"], images[0]["file"])
        self.assertIn("image", got["chunks"][-1], "图挂在最后一泡上")
        self.assertTrue(records[0]["body"]["chunks"][0]["image"]["file"].endswith(
            str(self.portrait)), "通道收到的那一泡里就是家里那张")
        rows = helpers.ledger_lines(self.hand_root)
        self.assertEqual("send_image", rows[-1]["action"])
        self.assertEqual("done", rows[-1]["state"])

    def test_she_never_claims_she_drew_it(self):
        data = look.load_config()
        with helpers.channel_service() as srv:
            self._use(srv)
            got = self._answer("给我看你的" + PORTRAIT, reply="我画了你的自画像，好看吧？")

        self.assertTrue(got["look"]["ok"], got["look"])
        for word in data["say"]["forbid"]:
            self.assertNotIn(word, got["reply"],
                             "红线：她不许把家里的图说成自己现做的（%s）" % word)
        self.assertEqual(_rendered_ok(data, PORTRAIT), got["reply"],
                         "命红线就换成数据里那一句口径")

    def test_a_picture_that_is_not_at_home_says_so(self):
        data = look.load_config()
        with helpers.channel_service() as srv:
            self._use(srv)
            got = self._answer("给我看" + MISSING, reply="喏，这就是。")

        self.assertFalse(got["look"]["ok"], got["look"])
        self.assertEqual(look.WHY_NOT_FOUND, got["look"]["why"])
        self.assertIsNone(got["look"]["image"])
        self.assertEqual(data["say"]["missing"], got["reply"], "她说的是数据里「没找着」那一句")
        self.assertEqual([], self._sent_images(got["chunks"]), "找不到就不许挂一张图上去")

    def test_a_bare_request_asks_and_the_hand_is_not_called(self):
        data = look.load_config()
        calls = []

        def spy(**kwargs):
            calls.append(kwargs)
            raise AssertionError("没指名就不许调手：%r" % (kwargs,))

        with mock.patch.object(look.hand_actions, "send_image", side_effect=spy):
            got = self._answer("给我看")

        self.assertEqual([], calls, "假手一次都没被调")
        self.assertEqual(look.WHY_NO_NAME, got["look"]["why"])
        self.assertFalse(got["look"]["ok"])
        self.assertEqual(data["say"]["ask"], got["reply"], "反问那句整句说出来")
        self.assertEqual([], helpers.ledger_lines(self.hand_root), "没调手就没有手账")

    def test_chit_chat_has_no_look_cell(self):
        got = self._answer("今天好热")
        self.assertNotIn("look", got, "闲聊那一轮回包里不该有 look 这一格")
        self.assertEqual("在的。", got["reply"], "闲聊的回答一个字都不该变")
        self.assertEqual([], helpers.ledger_lines(self.hand_root), "闲聊不碰手")


if __name__ == "__main__":
    unittest.main()
