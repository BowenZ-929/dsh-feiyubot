"""嘴的切分按人走：家·偏好卡的两个旋钮怎么盖掉 policy 的 split。

这一层**只读卡、不写卡**：卡由家里的 add_preference 造在临时家根（HOME_ROOT）下，走的是真链路
（api/chat.py 的 _style_for / _policy_for → organs/home 的 preference_of），不 monkeypatch 家里的读法。
一条卡都没有时，行为与改前逐字一致——这条是底线，第一、六、七条守着它。

四件事各有用例：
- 没卡：policy 一个键都不动，回包形状也不许变（连 style 这一格都不许出现）；
- max_chars=N：每一泡都不超过 N 个字（并且 sentences_per_part 必须跟着压成 1，两句一泡就破限）；
- chunks_per_reply=M：泡数被合并到不超过 M，字一个都不丢；
- 两个都有：**字限赢**，chunks_per_reply 这次不生效，但必须在 style 里如实留痕（applied / ignored）。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List
from unittest import mock

from api import chat
from organs.home import add_preference
from organs.mouth import style_policy, warehouse
from organs.mouth.split import split as split_text

WHO = "10000000001"
OTHER = "10000000009"      # 一次卡都没给他设过：他是「别人」那一侧

# 112 字、八句（默认两句一泡就是四泡）：测字限时句子够多、泡数又不会撞上防炸上限。
EIGHT = ("今天风大，河边的柳树一直往水里压。豆浆摊刚开锅，白汽一团团地冒。我买了一杯，烫得两手来回倒换。"
         "摊主说话慢，笑起来眼睛弯成缝。她说这河以前能看见鱼。现在只剩塑料瓶。"
         "太阳爬上来了，桥面亮得像薄铁皮。风一停，腥味就变成豆浆的甜味。")

# 95 字、十二句（默认两句一泡 = 六泡）：测「合并到不超过 M 泡」要的就是能切出六泡的一段话。
SIX = ("清早的雾还没散。屋顶上落了一层灰。她端了一杯热水。窗外的猫在打盹。巷口有人在叫卖。"
       "电车慢慢开过去。桌角的闹钟响了。我抬头看了一眼。云从东边压过来。茶叶沉在杯底。"
       "楼下传来笑声。天色一下子亮了。")


class StyleKnobsCase(unittest.TestCase):
    """一次一间空家：卡全造在临时家根里，一条也不往实盘写。"""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="knobs-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {"HOME_ROOT": self.home})
        env.start()
        self.addCleanup(env.stop)

    def card(self, who: str, **knobs: Any) -> dict:
        """用家的真 API 造一张卡（追加一行到 名片盒/prefs.jsonl）。"""
        return add_preference(root=None, role="human", by="human", who=who, **knobs)

    def texts(self, chunks: List[Dict[str, Any]]) -> List[str]:
        return [str(one["text"]) for one in chunks]

    def boundary_chunks(self, text: str, who: str) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {}
        chat._say_boundary(payload, text, who)
        return payload["chunks"]

    def knobs(self, who: str) -> Dict[str, Any]:
        rules = chat._policy_for(who)["split"]
        return {key: rules[key] for key in ("max_chars", "max_parts", "sentences_per_part")}


class NoCardCase(StyleKnobsCase):
    """没卡 = 与改前逐字一致：policy 不动、回包不多一格。"""

    def test_one_policy_identical_without_card(self) -> None:
        """① 没卡：三个旋钮逐字等于原始 policy，整份 policy 也相等。"""
        raw = warehouse.load("policy")
        mine = chat._policy_for(OTHER)
        self.assertEqual(raw, mine, "没卡时 policy 一个键都不许动")
        self.assertEqual({}, chat._style_for(OTHER), "没卡就是 {}（不补默认值）")
        self.assertEqual({}, chat._style_for(""), "空串不是一个人，不去问家要卡")

    def test_seven_payload_shape_unchanged_without_card(self) -> None:
        """⑦ 没卡时回包形状不变：只有 reply / chunks，没有 style 这一格。"""
        payload: Dict[str, Any] = {}
        chat._say_boundary(payload, EIGHT, OTHER)
        self.assertNotIn("style", payload, "没有偏好时不许加这一格")
        self.assertEqual(["chunks", "reply"], sorted(payload))


class CharLimitCase(StyleKnobsCase):
    """max_chars：每一泡都不超过这个字数。"""

    def test_two_max_chars_caps_every_part(self) -> None:
        """② max_chars=20：112 字分出来的每一泡都 ≤20，且字一个不丢。"""
        self.card(WHO, max_chars=20)
        knobs = self.knobs(WHO)
        self.assertEqual(20, knobs["max_chars"])
        self.assertEqual(1, knobs["sentences_per_part"], "两句一泡会突破字限，必须压成 1")
        self.assertEqual(warehouse.load("policy")["split"]["max_parts"], knobs["max_parts"],
                         "只给字限时不动泡数上限")
        chunks = self.boundary_chunks(EIGHT, WHO)
        self.assertGreater(len(chunks), 1, "字限下不该是一整泡")
        for one in chunks:
            self.assertLessEqual(len(one["text"]), 20, "这一泡破了 20 字：%r" % (one["text"],))
        self.assertEqual(EIGHT, "".join(self.texts(chunks)), "切泡只改切点，一个字都不许丢")


class PartsCase(StyleKnobsCase):
    """chunks_per_reply：泡数合并到不超过 M。"""

    def test_three_chunks_per_reply_one_part(self) -> None:
        """③ chunks_per_reply=1：同一段话恰好一泡，拼起来等于原文。"""
        self.card(WHO, chunks_per_reply=1)
        self.assertEqual(1, self.knobs(WHO)["max_parts"])
        self.assertEqual(warehouse.load("policy")["split"]["max_chars"],
                         self.knobs(WHO)["max_chars"], "只给泡数时不动字限")
        chunks = self.boundary_chunks(SIX, WHO)
        self.assertEqual(1, len(chunks), "说了合并到 1 泡就得是 1 泡")
        self.assertEqual(SIX, chunks[0]["text"])

    def test_five_chunks_per_reply_merges_six_into_three(self) -> None:
        """⑤ chunks_per_reply=3：本来切得出六泡的一段话，合并到不超过三泡，字一个不丢。"""
        default = self.boundary_chunks(SIX, OTHER)
        self.assertGreater(len(default), 3, "这段话本来就切得出多于三泡，才测得出合并")
        self.card(WHO, chunks_per_reply=3)
        chunks = self.boundary_chunks(SIX, WHO)
        self.assertLessEqual(len(chunks), 3)
        self.assertEqual(SIX, "".join(self.texts(chunks)), "合并只拼字")


class BothCase(StyleKnobsCase):
    """两个旋钮都有：字限优先，另一个不生效但必须留痕。"""

    def test_four_char_limit_first_and_style_note(self) -> None:
        """④ max_chars=20 + chunks_per_reply=1：泡数 >1（字限赢）、每泡 ≤20、style 里记着 ignored。"""
        self.card(WHO, chunks_per_reply=1, max_chars=20)
        knobs = self.knobs(WHO)
        self.assertEqual(20, knobs["max_chars"])
        self.assertEqual(1, knobs["sentences_per_part"])
        self.assertEqual(warehouse.load("policy")["split"]["max_parts"], knobs["max_parts"],
                         "字限赢时 chunks_per_reply 不许改 max_parts")
        chunks = self.boundary_chunks(EIGHT, WHO)
        self.assertGreater(len(chunks), 1, "字限赢：泡数不许被压成 1")
        for one in chunks:
            self.assertLessEqual(len(one["text"]), 20, "这一泡破了 20 字：%r" % (one["text"],))

        payload: Dict[str, Any] = {}
        chat._say_boundary(payload, EIGHT, WHO)
        self.assertGreater(len(payload["chunks"]), 1, "边界回复仍按同一份人物偏好分泡")
        note = payload["style"]
        self.assertEqual(["max_chars"], note["applied"])
        self.assertEqual([{"key": "chunks_per_reply", "reason": "char_limit_first"}], note["ignored"])
        self.assertEqual({"chunks_per_reply": 1, "max_chars": 20},
                         {key: note[key] for key in ("chunks_per_reply", "max_chars")})


class OtherPersonCase(StyleKnobsCase):
    """同一条代码路径，别人仍按默认。"""

    def test_six_other_person_matches_default(self) -> None:
        """⑥ 没设偏好的那个人走同一段话：逐条与默认 policy 的切分一致。"""
        self.card(WHO, chunks_per_reply=1, max_chars=20)      # 别人的卡不等于他的
        plain = split_text(SIX, warehouse.load("policy"), warehouse.load("action_note"))
        mine = self.boundary_chunks(SIX, OTHER)
        self.assertEqual([part.text for part in plain.parts], self.texts(mine),
                         "没卡的人必须与默认 policy 逐条一致")
        self.assertEqual(self.texts(self.boundary_chunks(EIGHT, OTHER)),
                         [part.text for part in split_text(
                             EIGHT, warehouse.load("policy"), warehouse.load("action_note")).parts])


class OverlayCase(unittest.TestCase):
    """判据那一份本身：纯函数、不改入参、没有卡就一个键都不动（mind 与 api 共用这一处）。"""

    def test_eight_overlay_is_pure_and_char_limit_first(self) -> None:
        """⑧ overlay：深拷、不改入参；空卡逐字相等且无留痕；坏读数算没设；两个都有字限赢。"""
        raw = warehouse.load("policy")
        snapshot = warehouse.load("policy")
        same, note = style_policy.overlay(raw, {})
        self.assertEqual(snapshot, same, "空卡时 policy 逐字相等")
        self.assertIsNone(note)
        self.assertIsNot(same, raw, "返回的必须是新表（深拷），不是入参本身")

        for knob in ("max_chars", "chunks_per_reply"):
            for bad in (0, -3, True, "20", 2.5):
                bad_policy, bad_note = style_policy.overlay(raw, {knob: bad})
                self.assertEqual(snapshot, bad_policy, "坏读数不许改 policy：%r=%r" % (knob, bad))
                self.assertIsNone(bad_note, "坏读数不叫冲突，不留痕")

        both, note = style_policy.overlay(raw, {"max_chars": 20, "chunks_per_reply": 1})
        self.assertEqual(20, both["split"]["max_chars"])
        self.assertEqual(1, both["split"]["sentences_per_part"])
        self.assertEqual(raw["split"]["max_parts"], both["split"]["max_parts"], "字限赢：泡数上限不动")
        self.assertEqual({"chunks_per_reply": 1, "max_chars": 20, "applied": ["max_chars"],
                          "ignored": [{"key": "chunks_per_reply",
                                       "reason": "char_limit_first"}]}, note)
        self.assertEqual(snapshot, raw, "入参 policy 一个字都不许动")


if __name__ == "__main__":
    unittest.main()
