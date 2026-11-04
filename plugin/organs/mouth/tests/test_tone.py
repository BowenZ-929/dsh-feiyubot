"""语气档只管一件事：这一句用什么口径说——整数档 + 一句可读口径行。

本文件守住四条：
1. 同一句话、两份人名录读数 → 称呼不同、档位不同、口径行不同，而且差在哪个词要指得出来；
2. 魂改了下一回合就变 —— 拷真 organs/soul/data 到临时目录、载入真 SoulStore，改 manner.stance
   再取一次，口径行跟着变（用真的 SoulStore，不伪造一个 store 类顶替这一步）；
3. 心还没做 —— 未接入必须说成「未接入」，不许当 mood=0 静默混过去；给了读数档位就要动；
4. 缺卡、违禁自称一律按中性出声，不抛错（嘴是出口的最后一段）。
"""
from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from organs.mouth import address, tone
from organs.mouth.warehouse import load
from organs.soul import store as soul_store

ROOT = Path(__file__).resolve().parents[3]
SOUL_DATA = ROOT / "organs" / "soul" / "data"
TABLE = load("address_table")

NEW_STANCE = "不装正经了，熟人面前会自己先笑一场"


def _cards() -> dict:
    """魂的三块原始卡：形状与 store.raw(block) 一致，读真魂，不手抄。"""
    return dict((name, json.loads((SOUL_DATA / ("%s.json" % name)).read_text(encoding="utf-8")))
                for name in ("identity", "manner", "bounds"))


def _person(reading: float, display: str = "老王", interactions=None) -> dict:
    """一份人名录读数：形状照 BrainStore._person_view 来（familiarity 是脑现算的读数）。"""
    return {"id": "u-1", "display": display, "aliases": [],
            "first_seen": "2024-04-01T09:00:00+00:00",
            "last_interaction": "2024-05-06T21:00:00+00:00",
            "cold": False, "shadow": False, "interactions": interactions or {},
            "prefs": [], "notes": [], "familiarity": reading}


def _label(tier: int) -> str:
    """这一档的名字：从数据里取，测试里不手抄「放松」「客气」这些字。"""
    return TABLE["tone"]["tiers"][str(tier)]["label"]


class ToneTest(unittest.TestCase):
    def test_same_text_two_readings(self):
        """同一句话：熟人是「老王」、生人是「你」，档位与口径行都不同，差在哪个词指得出来。"""
        cards = _cards()
        near = tone.render(cards, address.resolve(_person(20.0), TABLE), table=TABLE)
        far = tone.render(cards, address.resolve(None, TABLE), table=TABLE)
        self.assertEqual("老王", near.address)
        self.assertEqual("你", far.address, "没人名录取数就该退回中性叫法")
        self.assertNotEqual(near.tier, far.tier, "熟人与生人落到了同一档")
        self.assertNotEqual(near.line, far.line, "两份读数吐出同一句口径行：档位没进句子")
        self.assertIn("「老王」", near.line)
        self.assertIn("「你」", far.line)
        self.assertIn(_label(near.tier), near.line)
        self.assertNotIn(_label(near.tier), far.line)
        # 逐字比对：两行只差「称呼」和「档名」这两处，别的地方一个字都不许飘
        self.assertEqual(near.line.replace("「老王」", "「X」").replace(_label(near.tier), "Y"),
                         far.line.replace("「你」", "「X」").replace(_label(far.tier), "Y"),
                         "两行差在别的地方了，不只是称呼与档名")

    def test_soul_change_takes_effect_next_round(self):
        """改 manner.stance → 下一回合的口径行就变，并且含新 stance 原文（真 SoulStore）。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("identity.json", "manner.json", "bounds.json"):
                shutil.copyfile(SOUL_DATA / name, root / name)
            store = soul_store.load(root)
            addressee = address.resolve(_person(20.0), TABLE)
            before = tone.from_store(store, addressee)
            old_stance = json.loads((root / "manner.json").read_text(encoding="utf-8"))["stance"]
            self.assertEqual(old_stance, before.stance, "魂的原句没照抄进 Tone.stance")
            self.assertIn(old_stance, before.line)
            path = root / "manner.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["stance"] = NEW_STANCE
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            after = tone.from_store(store, addressee)
            self.assertNotEqual(before.line, after.line, "魂改了，口径行没变：有东西被缓存了")
            self.assertEqual(NEW_STANCE, after.stance)
            self.assertIn(NEW_STANCE, after.line, "新的 stance 原文没进口径行")
            self.assertNotIn(old_stance, after.line, "旧 stance 还留在行里")

    def test_heart_not_connected_is_named_not_zero(self):
        """心未接入 → 明说未接入、软度取本档中性值；给一条 energy=15 的读数作对照。"""
        cards = _cards()
        addressee = address.resolve(_person(20.0), TABLE)
        idle = tone.render(cards, addressee, table=TABLE)
        self.assertIn("未接入", idle.heart_source, "心没接上，返回里却没说「未接入」")
        self.assertNotEqual("heart:0", idle.heart_source)
        self.assertEqual(TABLE["tone"]["tiers"][str(idle.tier)]["softness"], idle.softness,
                         "未接入时软度不是本档的中性值")
        self.assertFalse(idle.ban_joke)
        wired = tone.render(cards, addressee,
                            heart={"mood": None, "energy": 15.0, "offset": None, "engage": None},
                            table=TABLE)
        self.assertNotIn("未接入", wired.heart_source, "拿到读数了还说未接入")
        self.assertIn("energy=15", wired.heart_source)
        self.assertNotEqual(idle.softness, wired.softness, "给了低精力读数，软度没动：接口没通")
        self.assertTrue(wired.ban_joke, "精力低还能开玩笑：心的折算没生效")
        self.assertNotEqual(idle.tier, wired.tier, "给了低精力读数，档位没动")
        blank = tone.render(cards, addressee,
                            heart={"mood": None, "energy": None, "offset": None, "engage": None},
                            table=TABLE)
        self.assertIn("无读数", blank.heart_source, "接上了但没读数，也没照实说")
        self.assertEqual(idle.softness, blank.softness, "没读数却动了软度：那就是把 None 当 0 了")

    def test_stance_is_copied_verbatim(self):
        """魂的原句只照抄：Tone.stance 与 manner.stance 逐字相等。"""
        cards = _cards()
        got = tone.render(cards, address.resolve(None, TABLE), table=TABLE)
        self.assertEqual(cards["manner"]["stance"], got.stance)
        self.assertEqual(cards["identity"]["self_names"]["display"], got.self_name)
        self.assertIn(cards["manner"]["stance"], got.line)

    def test_broken_cards_fall_back_without_raising(self):
        """缺卡不抛错：魂没给姿态就中性出声，称呼仍是回退口径。"""
        got = tone.render({}, address.resolve(None, TABLE), table=TABLE)
        self.assertEqual("", got.stance)
        self.assertEqual(TABLE["fallback"]["address"], got.address)
        self.assertIn("魂没给姿态", got.line)
        self.assertIn("未接入", got.heart_source)

    def test_tone_guards_self_name_against_identity_allowlist(self):
        """违禁自称漏到语气这一层也退中性：不抛错，也不把「人家」当称呼用。"""
        got = tone.render(_cards(), {"relation": 4, "address": "人家", "banned_self": ""},
                          table=TABLE)
        self.assertEqual(TABLE["fallback"]["address"], got.address)
        self.assertNotIn("「人家」", got.line)

    def test_addressee_can_be_a_mapping(self):
        """addressee 是 dataclass 或同样字段的 dict 都认（第四批 api 两种都可能传）。"""
        got = tone.render(_cards(), {"relation": 3, "address": "老王"}, table=TABLE)
        self.assertEqual("老王", got.address)
        self.assertEqual(3, got.tier)
        self.assertEqual(_label(3), got.label)


if __name__ == "__main__":
    unittest.main()
