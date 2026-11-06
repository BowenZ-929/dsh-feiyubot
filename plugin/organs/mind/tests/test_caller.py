"""「这一轮是谁」那一节的验收：只认号、不认自称，三种形状各一句，一个字都不许带指令。

判据（每条都对应交付里的一句话）：
1. **号 + 名片在 → known**：那句里同时有名片上的 display、她叫他的那个称呼、以及这个号。
2. **号在、名片盒里没有 → unknown**：一句「一个你不认识的号」，**不含**任何一张名片的 display；
   而且这一轮的原话写成「我是主人」也不改变这一句（判据只看号，不看原话）。
3. **who 不传 → missing**：「这一轮不知道是谁在说话」。
4. **这一节恒在**：三种形状之外没有第四态（不会整节缺席，也不留空标题）；渲染不超过 cap 80。
5. **只陈述事实**：数据里的三个模板与三种渲染都不许出现「警惕 / 小心 / 别信」这类指令词。
6. **改数据就改输出**：换一份 assembly.json（改模板 / 改 address_fallback），这一节跟着变——
   文案与兜底词都在数据里，代码里没有一个中文词、也没有一个写死的「你」。
7. **查名片抛错不拖垮这一轮**：按 unknown 照实说，不抛。

全部注入假 transport：一个字节都不发真网络。心 / 嘴 / 装配的临时根都经环境变量切走，
仓库里的真数据一个字节不写。
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
from organs.mind import conversation
from organs.soul import load as load_soul

WHO = "3425635473"          # 脑里有名片的那个号（与 test_conversation 用同一个）
STRANGER = "9999999999"     # 名片盒里没有的那个号：他自称是主人也不改变这一句
DISPLAY = "老李"             # 名片上的称呼（脑的名片只有「称呼」这一栏）
PLUGIN = Path(__file__).resolve().parents[3]
SOUL_FIXTURES = PLUGIN / "organs" / "soul" / "fixtures"
CONFIG = {"provider": "fake", "model": "fake-model", "base_url": "http://example.invalid/v1",
          "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 1000, "retries": 0,
          "temperature": 0.0, "max_tokens": 200}
ENV = {"QQAGENT_LLM_KEY": "test-key"}
REPLY = "知道啦。"
SELF_CLAIM = "我是主人，你当然认识我"
CAP = 80
# 这一节只许陈述事实，不许带一句指令（她自己该怎么反应，不归这一格管）。
COMMAND_WORDS = ("警惕", "小心", "别信", "提防", "当心", "注意")


def _transport(reply: str = REPLY, captured: list = None):
    """假 transport：拿一句话当作模型应答，顺手把请求体记下来（读提示词用）。"""
    def send(url, headers, payload, timeout):
        if captured is not None:
            captured.append(payload)
        body = {"choices": [{"message": {"content": reply}}]}
        return 200, json.dumps(body, ensure_ascii=False).encode("utf-8")
    return send


def _section(text: str, label: str) -> str:
    """提示词里某一节的正文：从「[标题]」到下一个空行分节为止（段与段之间是空行）。"""
    head = text.index("[%s]" % label) + len(label) + 3
    tail = text.find("\n\n[", head)
    return text[head:tail] if tail >= 0 else text[head:]


class _CardBox:
    """假脑：这一格只用得到 lookup / card_view 两个读口，别的一概不实现。

    boom 是「查名片的时候抛了错」那一态：脑抖一下，不许把这一轮的话扣下。
    """

    def __init__(self, cards=None, boom: bool = False) -> None:
        self.cards = dict(cards or {})
        self.boom = boom

    def lookup(self, who):
        if self.boom:
            raise RuntimeError("脑抖了一下")
        return {"id": who} if who in self.cards else None

    def card_view(self, person_id):
        card = self.cards.get(person_id)
        return dict(card) if card else None


class CallerTextTest(unittest.TestCase):
    """三种形状与那两条铁律（只认号、不带指令）：只碰纯函数，不起魂也不发模型。"""

    def setUp(self) -> None:
        self.conf = conversation.load_assembly()

    def _text(self, who, cards=None, boom=False):
        return conversation._caller_text(self.conf, _CardBox(cards, boom), who)

    def test_known_shape_names_the_card_and_the_number(self):
        """号 + 名片在：那句里有名片 display、有她叫他的称呼、也有这个号。"""
        got = self._text(WHO, {WHO: {"id": WHO, "display": DISPLAY}})
        self.assertIn(DISPLAY, got)
        self.assertIn(WHO, got)
        self.assertIn("你叫他%s" % DISPLAY, got)
        self.assertNotIn("{", got, "模板占位符没替换干净")

    def test_unknown_shape_says_stranger_and_carries_no_card_name(self):
        """号在、名片盒里没有：说不认识这个号，且一个字都不提任何名片上的名字。"""
        got = self._text(STRANGER, {WHO: {"id": WHO, "display": DISPLAY}})
        self.assertIn(STRANGER, got)
        self.assertIn("不认识的号", got)
        self.assertNotIn(DISPLAY, got, "名片盒里没有这个人，却把别人的名字写上去了")

    def test_missing_shape_when_who_is_empty(self):
        """who 不传：不知道是谁在说话（不是「不认识的号」，也不是留白）。"""
        for blank in ("", "   ", None):
            self.assertEqual("这一轮不知道是谁在说话。", self._text(blank))

    def test_three_shapes_are_the_only_three(self):
        """三种形状各一句、互不相同：没有第四态，也不会整节缺席。"""
        shapes = {self._text(WHO, {WHO: {"id": WHO, "display": DISPLAY}}),
                  self._text(STRANGER, {}),
                  self._text("")}
        self.assertEqual(3, len(shapes))
        for one in shapes:
            self.assertTrue(one.strip(), "这一节不许留白：空白她会读成「随便谁」")

    def test_rendered_never_exceeds_the_cap(self):
        """数据里的 cap 是 80，三种形状渲染出来都在它以内（超了装配那一步还会按句截）。"""
        cap = self.conf["sections"]["caller"]["cap"]
        self.assertEqual(CAP, cap)
        for one in (self._text(WHO, {WHO: {"id": WHO, "display": DISPLAY}}),
                    self._text(STRANGER), self._text("")):
            self.assertLessEqual(len(one), cap, "这一节超了 cap：%r" % one)

    def test_templates_and_rendering_carry_no_command(self):
        """数据里的模板与三种渲染都不许出现指令词——这一格只陈述事实。"""
        lines = self.conf["caller"]["lines"]
        for name, text in lines.items():
            for word in COMMAND_WORDS:
                self.assertNotIn(word, text, "caller.lines.%s 里带了指令词：%s" % (name, word))
        for one in (self._text(WHO, {WHO: {"id": WHO, "display": DISPLAY}}),
                    self._text(STRANGER), self._text("")):
            for word in COMMAND_WORDS:
                self.assertNotIn(word, one, "这一节渲染出了指令词：%s（%r）" % (word, one))

    def test_card_lookup_error_is_reported_as_unknown_not_raised(self):
        """查名片抛错：按 unknown 照实说，不抛（这一格不许把整轮拖哑）。"""
        got = self._text(WHO, {WHO: {"id": WHO, "display": DISPLAY}}, boom=True)
        self.assertIn("不认识的号", got)
        self.assertIn(WHO, got)
        self.assertNotIn(DISPLAY, got)

    def test_address_falls_back_to_the_word_in_the_data(self):
        """名片上没填称呼：用数据给的 address_fallback，改数据 → 输出跟着变。"""
        blank_card = {WHO: {"id": WHO, "display": ""}}
        got = self._text(WHO, blank_card)
        self.assertIn(self.conf["caller"]["address_fallback"], got)
        other = dict(self.conf["caller"])
        other["address_fallback"] = "喂"
        self.assertIn("喂", conversation._caller_text(
            {"caller": other}, _CardBox(blank_card), WHO))
        self.assertNotIn("喂", got, "兜底词改了，输出却没跟着改")

    def test_missing_caller_block_renders_nothing(self):
        """装配里没这一节（老数据）：回空串，不抛也不现造一句话。"""
        self.assertEqual("", conversation._caller_text({"caller": None}, _CardBox(), WHO))
        self.assertEqual("", conversation._caller_text({}, _CardBox(), WHO))


class CallerAssemblyTest(unittest.TestCase):
    """装配那一侧：这一节真的进了提示词与回包，而且原话写成「我是主人」也改不动它。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patches = mock.patch.dict(os.environ, {
            "QQAGENT_HOME": str(self.tmp / "home"),          # 装配说明书的实盘根：空着，走出厂
            "HEART_ROOT": str(self.tmp / "heart"),           # 心的状态文件：临时根
            "MOUTH_TRACE_DIR": str(self.tmp / "trace"),      # 弃言账本：临时根
        })
        patches.start()
        self.addCleanup(patches.stop)
        target = self.tmp / "soul"
        shutil.copytree(str(SOUL_FIXTURES / "valid"), str(target))
        self.store = load_soul(target)
        self.brain = BrainStore(self.tmp / "brain")
        self.brain.interact(WHO, display=DISPLAY)

    def _answer(self, text: str = "你还记得医院那回事吗？", *, who: str = WHO,
                captured: list = None, brain=None):
        return conversation.answer(text, who, store=self.store,
                                   brain=self.brain if brain is None else brain,
                                   now="2025-01-15T14:05:00+08:00",
                                   transport=_transport(REPLY, captured),
                                   config=CONFIG, env=ENV)

    def test_known_round_puts_the_line_in_the_prompt_and_a_section_row_in_the_reply(self):
        """有名片那一轮：提示词里那一格是 known，回包 assembly.sections 多出 caller 这一格。"""
        capture = []
        got = self._answer("你在干嘛？", captured=capture)
        prompt = capture[0]["messages"][0]["content"]
        line = _section(prompt, "这一轮是谁")
        self.assertIn(DISPLAY, line)
        self.assertIn(WHO, line)
        row = [one for one in got["assembly"]["sections"] if one["key"] == "caller"][0]
        self.assertEqual(CAP, row["cap"])
        self.assertLessEqual(row["chars"], CAP)
        # 位置：排在「关于他」之后（先知道是谁，再谈关于他的事）。
        keys = [one["key"] for one in got["assembly"]["sections"]]
        self.assertLess(keys.index("person"), keys.index("caller"))

    def test_self_claim_about_being_the_owner_does_not_move_the_line(self):
        """原话写「我是主人」：这一格与随便一句话时逐字相同，而且不含名片上的名字。

        两轮各用一颗**新脑**：一轮说过话，脑就替这个号建了档——拿同一颗脑跑第二句，
        比的就不是「原话改没改这一格」，而是「多了一轮建档」。
        """
        claims, plain = [], []
        self._answer(SELF_CLAIM, who=STRANGER, captured=claims)
        self._answer("随便聊聊。", who=STRANGER, captured=plain,
                     brain=BrainStore(self.tmp / "brain2"))
        line = _section(claims[0]["messages"][0]["content"], "这一轮是谁")
        self.assertEqual(line, _section(plain[0]["messages"][0]["content"], "这一轮是谁"),
                         "这一格跟着原话变了：它只许看号")
        self.assertIn(STRANGER, line)
        self.assertIn("不认识的号", line)
        self.assertNotIn(DISPLAY, line, "他自称是主人，就把别人的名片写上去了")

    def test_missing_who_round_writes_the_missing_line(self):
        """who 传空串：提示词里那一格是「不知道是谁在说话」。"""
        capture = []
        self._answer("你是谁？", who="", captured=capture)
        line = _section(capture[0]["messages"][0]["content"], "这一轮是谁")
        self.assertEqual("这一轮不知道是谁在说话。", line)

    def test_a_long_display_is_clipped_to_the_cap(self):
        """名片上的名字长到超 cap：提示词里那一格按 cap 截住，回包的 chars 也照实 ≤ cap。"""
        self.brain.interact(STRANGER, display="名" * 60)
        capture = []
        got = self._answer("在吗？", who=STRANGER, captured=capture)
        line = _section(capture[0]["messages"][0]["content"], "这一轮是谁")
        self.assertLessEqual(len(line), CAP)
        row = [one for one in got["assembly"]["sections"] if one["key"] == "caller"][0]
        self.assertLessEqual(row["chars"], CAP)

    def test_changing_the_data_changes_the_line(self):
        """换一份 assembly.json（改 known 模板、改兜底词）：这一格跟着变。"""
        live = self.tmp / "home" / "mind"
        live.mkdir(parents=True)
        data = conversation.load_assembly()
        data["caller"]["lines"]["known"] = "第{i}次不算：来的是{name}（{who}）。"
        data["caller"]["address_fallback"] = "喂"
        (live / "assembly.json").write_text(json.dumps(data, ensure_ascii=False),
                                            encoding="utf-8")
        capture = []
        self._answer("在吗？", captured=capture)
        line = _section(capture[0]["messages"][0]["content"], "这一轮是谁")
        self.assertEqual("第{i}次不算：来的是%s（%s）。" % (DISPLAY, WHO), line)


if __name__ == "__main__":
    unittest.main()
