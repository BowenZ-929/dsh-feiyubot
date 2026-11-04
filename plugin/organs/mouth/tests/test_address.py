"""称呼只管三样：对谁、一个称呼、关系档；外加一句频次说明与两句实话。

数值一个都不写在这份文件里：四档档界、称呼词、频次说明、彩蛋改口、违禁自称、回退口径
全从 organs/mouth/data/address_table.json 读（走 warehouse），要改档界就改数据——
所以下面凡是断言档位的样例，都先拿数据里的档界当尺子，再验一遍「改了档界档位跟着变」。

人名录读数不许手编：第 4 个用例用真的脑·人名录（临时目录 + interact()）造出一个
有若干天互动的人，走「familiarity → 档位 → 称呼」整条链。只有对照用的读数
（test 1/3/5 的 _person）才是直接给的 float，那种地方要验的是档界与回退，不是脑。

改名这一块有三条判据（旧口径「rename_request → 退到中性」已作废，改成当场换）：
改名当场用上；改名落在违禁自称里仍退到回退词（不许靠改名绕过红线）；
会话覆盖 > 改名 > 人名录档位，而会话覆盖只活这个进程（换实例就空）。
投递给脑那一节用真的脑店 ingest() 投一次，验的是「草稿的形状就是 ingest 的入参」。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from organs.brain import store as brain_store
from organs.mouth import address
from organs.mouth.warehouse import load

ROOT = Path(__file__).resolve().parents[3]
SOUL_DATA = ROOT / "organs" / "soul" / "data"
DATA = ROOT / "organs" / "mouth" / "data" / "address_table.json"
TABLE = load("address_table")


def _person(reading: float, display: str = "老王", interactions=None, **extra) -> dict:
    """一份人名录读数：形状照 BrainStore._person_view 来（familiarity 是脑现算的读数）。"""
    person = {"id": "u-1", "display": display, "aliases": [],
              "first_seen": "2024-04-01T09:00:00+00:00",
              "last_interaction": "2024-05-06T21:00:00+00:00",
              "cold": False, "shadow": False, "interactions": interactions or {},
              "prefs": [], "notes": [], "familiarity": reading}
    person.update(extra)
    return person


def _soul_self_names() -> list:
    """魂 identity 的自称授权名单（读真魂，不手抄）：display + synonyms。"""
    names = json.loads((SOUL_DATA / "identity.json").read_text(encoding="utf-8"))["self_names"]
    return [names["display"]] + list(names["synonyms"])


def _book() -> dict:
    """把称呼表整份拷成一份可改的 dict（改完写进临时目录，再走 warehouse.load 读回来）。"""
    return json.loads(DATA.read_text(encoding="utf-8"))


def _write(book: dict, root: Path) -> dict:
    """把改过的表落进临时目录，再用真仓库读回来：档界只认数据文件，不认内存里那份。"""
    (root / "address_table.json").write_text(
        json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load("address_table", root=root)


class AddressTest(unittest.TestCase):
    def test_tier_of_follows_table_boundaries(self):
        """档界来自数据：每一档的下界取得到自己，低一点就掉回上一档。"""
        tiers = sorted(TABLE["tiers"], key=lambda row: row["min_familiarity"])
        for index, entry in enumerate(tiers):
            self.assertEqual(entry["tier"], address.tier_of(entry["min_familiarity"], TABLE),
                             "读数正好压在档界上，却没进这一档：%r" % (entry,))
            if index:
                lower = tiers[index - 1]["tier"]
                self.assertEqual(lower, address.tier_of(entry["min_familiarity"] - 0.001, TABLE),
                                 "档界下面那一点点没掉回上一档：%r" % (entry,))

    def test_relation_matches_tier_from_data(self):
        """档名与整数关系档一一对应，两个都取自数据。"""
        for entry in TABLE["tiers"]:
            got = address.resolve(_person(entry["min_familiarity"]), TABLE)
            self.assertEqual(entry["tier"], got.tier)
            self.assertEqual(entry["relation"], got.relation)
            self.assertEqual(entry["use_display"], got.address == "老王",
                             "这一档该不该叫人名，与数据里的 use_display 对不上：%r" % (entry,))

    def test_boundary_lives_in_data(self):
        """改数据里的一个档界，同一个人档位随之变（代码里没有第二个真源）。"""
        reading = 10.0
        self.assertEqual("熟人", address.tier_of(reading, TABLE))
        with TemporaryDirectory() as tmp:
            book = _book()
            for entry in book["tiers"]:
                if entry["tier"] == "熟人":
                    entry["min_familiarity"] = 50.0
            changed = _write(book, Path(tmp))
            self.assertEqual("认识", address.tier_of(reading, changed),
                             "档界改成 50 之后，读数 10 还在熟人档：档界写死在代码里了")
            self.assertEqual(2, address.resolve(_person(reading), changed).relation)
        self.assertEqual("熟人", address.tier_of(reading, load("address_table")),
                         "临时目录里的改动漏进了真数据文件")

    def test_resolve_nobody_falls_back_not_remembered(self):
        """人名录取不到人 → 生人档 + 回退称呼；这是当场回退，不是沿用上一次的档位。"""
        friend = address.resolve(_person(20.0, interactions={"2024-05-06": 40}), TABLE)
        self.assertEqual("挚友", friend.tier)
        self.assertNotEqual(TABLE["fallback"]["address"], friend.address)
        nobody = address.resolve(None, TABLE)
        self.assertEqual("生人", nobody.tier)
        self.assertEqual(1, nobody.relation)
        self.assertEqual(TABLE["fallback"]["address"], nobody.address, "没退回数据里的回退口径")
        self.assertEqual("", nobody.who)
        self.assertEqual("", nobody.display)
        self.assertEqual(nobody, address.resolve(None, TABLE), "两次取不到人，结果不一致：有东西被记住了")

    def test_brain_familiarity_chain(self):
        """真脑店 + 真 interact()：familiarity → 档位 → 称呼，一条链跑下来。"""
        days = ["2024-05-01", "2024-05-02", "2024-05-03",
                "2024-05-04", "2024-05-05", "2024-05-06"]
        with TemporaryDirectory() as tmp:
            store = brain_store.load(Path(tmp))
            for day in days:
                store.interact("u-1", display="老王", ts="%sT10:00:00+00:00" % day)
                store.interact("u-1", display="老王", ts="%sT20:00:00+00:00" % day)
            view = store.lookup("老王", now_ts="2024-05-07T12:00:00+00:00")
        self.assertIsNotNone(view, "人名录里没造出这个人")
        reading = view["familiarity"]
        self.assertGreater(reading, 0.0, "六天互动算出来的熟稔读数还是 0")
        tier_name = address.tier_of(reading, TABLE)
        entry = [row for row in TABLE["tiers"] if row["tier"] == tier_name][0]
        self.assertEqual("熟人", tier_name, "六天各两次互动的读数 %r 没落进熟人档" % reading)
        got = address.resolve(view, TABLE)
        self.assertEqual(entry["tier"], got.tier)
        self.assertEqual(entry["relation"], got.relation)
        self.assertEqual("老王", got.address, "这一档本来该叫人名")
        self.assertEqual(TABLE["frequency"]["value"], got.frequency)

    def test_banned_self_falls_back_without_raising(self):
        """自称落在魂的授权名单之外 → 退到中性叫法，记住被挡下的词，不抛错。"""
        with TemporaryDirectory() as tmp:
            book = _book()
            for entry in book["tiers"]:
                if entry["tier"] == "挚友":
                    entry["use_display"] = False
                    entry["addresses"] = ["人家"]
            changed = _write(book, Path(tmp))
            allowed = _soul_self_names()
            self.assertNotIn("人家", allowed, "样例词居然在授权名单里，这个用例就不作数了")
            got = address.resolve(_person(30.0), changed, allowed_self=allowed)
            self.assertEqual(TABLE["fallback"]["address"], got.address, "违禁自称没退回中性叫法")
            self.assertEqual("人家", got.banned_self, "被挡下的那个词没记下来")
            self.assertEqual("挚友", got.tier, "退称呼不许连档位一起退")
            # 授权名单里有这个字，就照用：红线只管「非授权名字当自称」
            permitted = address.resolve(_person(30.0), changed, allowed_self=allowed + ["人家"])
            self.assertEqual("人家", permitted.address)
            self.assertEqual("", permitted.banned_self)

    def test_rename_request_switches_on_the_spot(self):
        """本人要求换个叫法：当场就换——旧口径「退到中性」已作废（退到中性等于没听人话）。

        换的只是一个叫法：档位、关系档、显示名都不动，也不写回名录与魂。
        """
        got = address.resolve(_person(20.0, rename_request=" 小王 "), TABLE)
        self.assertEqual("小王", got.address, "本人要的叫法没当场用上")
        self.assertTrue(got.renamed, "换了却没说是换来的")
        self.assertEqual("", got.banned_self)
        self.assertEqual(TABLE["source"]["rename"], got.source)
        self.assertEqual(TABLE["self_guard"]["rename_applied"], got.note)
        self.assertEqual("挚友", got.tier, "换称呼不许连档位一起换")
        self.assertEqual(4, got.relation)
        self.assertEqual("老王", got.display)

    def test_rename_request_blank_or_not_a_string_is_no_request(self):
        """只认非空字符串：空白、非字符串都等于没要求，档位挑的叫法照旧。"""
        plain = address.resolve(_person(20.0), TABLE)
        for empty in ("", "   ", None, 123, ["小王"]):
            got = address.resolve(_person(20.0, rename_request=empty), TABLE)
            self.assertEqual(plain.address, got.address, "rename_request=%r 被当成了改名" % (empty,))
            self.assertFalse(got.renamed)
            self.assertEqual(plain.source, got.source)

    def test_rename_still_blocked_by_self_guard(self):
        """改名不许绕过自称红线：改名落到违禁自称词表里、又不在魂的授权名单里 → 照旧退到回退词。"""
        banned_word = TABLE["self_guard"]["words"][0]
        got = address.resolve(_person(20.0, rename_request=banned_word), TABLE, allowed_self=())
        self.assertEqual(TABLE["fallback"]["address"], got.address, "改名绕过了自称红线")
        self.assertEqual(banned_word, got.banned_self, "被哪一条拦下的没记下来")
        self.assertFalse(got.renamed, "被红线拦下的改名不许报成「换了」")
        self.assertEqual(TABLE["self_guard"]["rename_note"], got.note)
        self.assertEqual(TABLE["source"]["fallback"], got.source)
        # 授权名单里有这个字，就照用：红线只管「非授权名字当自称」
        ok = address.resolve(_person(20.0, rename_request=banned_word), TABLE,
                             allowed_self=(banned_word,))
        self.assertEqual(banned_word, ok.address)
        self.assertTrue(ok.renamed)

    def test_session_book_overrides_directory_until_forgotten(self):
        """会话覆盖 > 人名录档位：记了就用覆盖值，forget / clear 之后回到名录档位。"""
        person = _person(20.0)
        plain = address.resolve(person, TABLE)
        book = address.SessionAddressBook()
        book.remember("u-1", "阿宝")
        got = address.resolve(person, TABLE, session=book)
        self.assertEqual("阿宝", got.address)
        self.assertEqual(TABLE["source"]["session"], got.source)
        self.assertFalse(got.renamed, "会话覆盖不是这一轮的改名")
        self.assertEqual(plain.tier, got.tier, "覆盖换的只是叫法，不许连档位一起换")
        self.assertTrue(book.forget("u-1"))
        self.assertFalse(book.forget("u-1"), "本来就忘干净了还回 True")
        back = address.resolve(person, TABLE, session=book)
        self.assertEqual(plain.address, back.address)
        self.assertEqual(TABLE["source"]["directory"], back.source)
        book.remember("u-1", "阿宝")
        book.clear()
        self.assertEqual(plain.address, address.resolve(person, TABLE, session=book).address)

    def test_session_override_beats_rename_request(self):
        """优先级写死三条：会话里有覆盖 > 这一轮的 rename_request > 人名录档位。"""
        person = _person(20.0, rename_request="小王")
        book = address.SessionAddressBook()
        book.remember("u-1", "阿宝")
        got = address.resolve(person, TABLE, session=book)
        self.assertEqual("阿宝", got.address)
        self.assertEqual(TABLE["source"]["session"], got.source)
        self.assertFalse(got.renamed)
        self.assertEqual("小王", address.resolve(person, TABLE).address, "没覆盖时该走改名")

    def test_session_override_is_not_a_backdoor_for_self_names(self):
        """会话覆盖同样过 self_guard：别拿「记下来的」当绕过自称红线的后门。"""
        banned_word = TABLE["self_guard"]["words"][0]
        book = address.SessionAddressBook()
        book.remember("u-1", banned_word)
        got = address.resolve(_person(20.0), TABLE, allowed_self=(), session=book)
        self.assertEqual(TABLE["fallback"]["address"], got.address)
        self.assertEqual(banned_word, got.banned_self)

    def test_session_book_lives_only_in_this_process(self):
        """进程重启即丢：换一个实例（＝重启）读不到上一本记的东西，账上也没留盘。"""
        first = address.SessionAddressBook()
        first.remember("u-1", "阿宝")
        self.assertEqual({"u-1": "阿宝"}, first.snapshot())
        second = address.SessionAddressBook()
        self.assertEqual("", second.get("u-1"), "新实例读到了旧覆盖：这不是纯内存的会话覆盖")
        self.assertEqual({}, second.snapshot())
        self.assertEqual(TABLE["source"]["directory"],
                         address.resolve(_person(20.0), TABLE, session=second).source)

    def test_session_book_drops_blank_entries(self):
        """空 who / 空叫法都不记：空叫法等于是「退到中性」，那是 resolve 的事，不是这本账的事。"""
        book = address.SessionAddressBook()
        book.remember("", "阿宝")
        book.remember("u-1", "   ")
        self.assertEqual({}, book.snapshot())
        self.assertEqual("", book.get("u-1"))

    def test_resolve_never_writes_the_session_book(self):
        """resolve 是纯的：它读 session，不往 session 里写（落 book 是调用方的事）。"""
        book = address.SessionAddressBook()
        address.resolve(_person(20.0, rename_request="小王"), TABLE, session=book)
        address.resolve(_person(20.0), TABLE, session=book)
        self.assertEqual({}, book.snapshot(), "resolve 自己往 session 里写了")

    def test_rename_memory_draft_feeds_brain_ingest(self):
        """投递给脑：草稿的形状就是 ingest 的入参，措辞取自数据，投进去查得到、source_type=亲历。"""
        draft = address.rename_memory("u-1", "小王", ts="2024-05-07T12:00:00+00:00")
        self.assertEqual({"who", "text", "source_type", "ts"}, set(draft), "草稿多了 ingest 不认的键")
        self.assertEqual(TABLE["self_guard"]["rename_memory_text"].format(address="小王"), draft["text"],
                         "这句措辞不是从数据里来的")
        self.assertEqual("亲历", draft["source_type"])
        with TemporaryDirectory() as tmp:
            store = brain_store.load(Path(tmp))
            result = store.ingest(**draft)              # 脑不为这件事加任何字段
            entry = result["entry"]
            self.assertEqual("亲历", entry["source_type"])
            self.assertTrue(entry["evidence_key"], "亲历条目没带证据出处")
            kept = [row for row in store.today_entries(now_ts="2024-05-07T13:00:00+00:00")
                    if row["id"] == entry["id"]]
        self.assertEqual(1, len(kept), "投进去的记忆查不回来")
        self.assertEqual("他要求过叫他小王", kept[0]["text"])
        self.assertEqual("u-1", kept[0]["who"])

    def test_easter_egg_fires_only_above_data_threshold(self):
        """彩蛋改口：进了那一档、且累计互动够数才改口，没到数还是叫人名。"""
        egg = TABLE["easter_egg"]
        below = address.resolve(_person(20.0, interactions={"2024-05-06": 1}), TABLE)
        self.assertEqual("老王", below.address)
        above = address.resolve(_person(20.0, interactions={"2024-05-06": egg["min_interactions"]}),
                                TABLE)
        self.assertEqual(egg["address"], above.address)
        self.assertEqual(egg["note"], above.note)

    def test_missing_tier_section_is_a_data_error(self):
        """表坏了要人修：缺 tiers 就抛 PolicyMiss，不许静默给一个档位。"""
        from organs.mouth.model import PolicyMiss
        with self.assertRaises(PolicyMiss):
            address.tier_of(1.0, {"nothing": {}})


if __name__ == "__main__":
    unittest.main()
