"""独处循环（日子线）：三档阈值、一次只做一件、账是真的、夜里不做事。

造真数据，不等真实时钟：心情与精力钉在 mood.json / energy.json 里（心的公开读照着读），
"多久没人说话"由脑 today 里的真条目现算，旧日记与草稿都走家的公开口子落盘。
阈值全从 data/limits.json 读；改那份配置就改这里的行为（本文件不抄那几个数当验收线，
只抄"三个阈值必须短于白天清醒时长"这一条断言）。
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from organs.brain import BrainStore
from organs.day import model, plan, proactive, proactive_ask, solitude
from organs.heart import energy as heart_energy

from .helpers import article, at, temp_root

NOW = "2026-11-05T14:00:00"        # 下午两点：白天，清醒
NIGHT = "2026-11-05T23:30:00"      # 夜里：engage 第一道硬门
HEARD = "2026-11-05T13:15:00"      # 45 分钟前他说了一句


def home_root(root: Path) -> Path:
    """临时家：十一间建好 + access.json，就在 <root>/home 那一棵树下（rhythm 自己会派生）。"""
    from organs.home import rooms
    home = Path(root) / "home"
    for name in rooms.ROOM_NAMES:
        (home / name).mkdir(parents=True, exist_ok=True)
    (home / "access.json").write_text(
        json.dumps({"version": 1, "writable": ["玄关", "工作台", "仓库", "素材盒"]},
                   ensure_ascii=False),
        encoding="utf-8")
    return home


def set_readings(heart: Path, now: str, mood: float = 60.0, energy: float = 80.0) -> None:
    """把心的两个读数钉住：真源是心的 mood.json / energy.json（本模块只读档位）。"""
    stamp = at(now)
    (Path(heart) / "mood.json").write_text(
        json.dumps({"version": 1, "now": mood, "baseline": 60.0, "updated_at": stamp,
                    "day": model.day_key(stamp), "conflict_used": 0, "window": {}},
                   ensure_ascii=False), encoding="utf-8")
    (Path(heart) / "energy.json").write_text(
        json.dumps({"version": 1, "value": energy, "updated_at": stamp,
                    "day": model.day_key(stamp, 0), "deducted": 0.0,
                    "last_burst_at": None, "tier": None}, ensure_ascii=False),
        encoding="utf-8")


class SolitudeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root(self)
        self.heart = Path(self.root) / "heart"
        self.heart.mkdir(parents=True, exist_ok=True)
        self.brain = BrainStore(Path(self.root) / "brain")
        self.home = home_root(self.root)
        set_readings(self.heart, NOW)          # 平常 + 充沛 → both_mid

    # —— 造数据 ——

    def heard(self, text: str = "今天下午把书架整个挪了一次，挪完手上全是灰。",
              now: str = HEARD, who: str = "人", display: str = "") -> None:
        out = self.brain.ingest(who=who, text=text, ts=at(now), display=display or None)
        self.assertTrue(out["ok"], out)

    def her_turn(self, text: str = "嗯，我记下了。", now: str = "2026-11-05T13:20:00") -> None:
        """最后一条变成她说的：unfinished 那条由头就不成立了（它看的是"最后一手是谁"）。"""
        out = self.brain.ingest(who="她说", text=text, ts=at(now), side="她说")
        self.assertTrue(out["ok"], out)

    def diary(self, date: str, head: str = "那天") -> str:
        """往抽屉/日记里放一篇（她自己的日记：一天一张、80–150 字）。"""
        from organs.home import cards
        out = cards.append_cards(root=self.home, role="agent", by="她", cards=[
            {"kind": "日记", "room": "抽屉", "name": "日记-%s" % date,
             "params": {"date": date, "text": article(chars=100, head=head), "mood": "平常"}}])
        return out["cards"][0]["name"]

    # —— 走一次独处 ——

    def consider(self, now: str = NOW, **kw):
        return solitude.consider(at(now), root=self.root, brain=self.brain,
                                 heart_root=self.heart, home_root=self.home, **kw)

    def rows(self):
        return solitude.log_lines(self.root)

    def last(self):
        return self.rows()[-1]

    def repo_cards(self, room: str = "") -> list:
        """草稿落哪间房由 data/solitude.json 说了算——测试不许把房名抄一遍。"""
        from organs.home import cards
        room = room or solitude.load_config()["draft"]["room"]
        return list(cards.list_room(root=self.home, room=room).get("items") or [])


class ThresholdTest(SolitudeCase):
    """三档阈值全在数据文件里，而且三个都必须短于白天清醒时长。"""

    def test_three_thresholds_come_from_the_data_file(self):
        table = solitude.thresholds(root=self.root)
        self.assertEqual({"double_low": 180, "both_mid": 40, "otherwise": 90}, table)

    def test_every_threshold_is_shorter_than_the_awake_day(self):
        awake = solitude.awake_minutes()
        self.assertEqual(900, awake, "23 → 8 睡 9 小时，白天清醒 15 小时 = 900 分钟")
        self.assertEqual(awake, solitude.awake_minutes(heart_energy.DEFAULT_SLEEP_WINDOW))
        for key, minutes in solitude.thresholds(root=self.root).items():
            self.assertLess(minutes, awake,
                            "%s 那一档 %d 分钟不短于白天清醒的 %d 分钟：那就等于永不触发"
                            % (key, minutes, awake))

    def test_the_file_is_the_source_of_truth(self):
        (Path(self.root) / "limits.json").write_text(json.dumps({
            "version": 1, "day_cutover_hour": 4,
            "solitude": {"thresholds_minutes": {"double_low": 120, "both_mid": 15,
                                                "otherwise": 60}}},
            ensure_ascii=False), encoding="utf-8")
        self.assertEqual({"double_low": 120, "both_mid": 15, "otherwise": 60},
                         solitude.thresholds(root=self.root))
        picked = solitude.threshold("平常", "一般", root=self.root)
        self.assertEqual(("both_mid", 15), (picked["key"], picked["minutes"]))

    def test_tier_map_covers_the_five_mood_and_four_energy_words(self):
        for tier in ("冰点", "偏冷"):
            self.assertEqual("low", solitude.tier_level("mood", tier))
        self.assertEqual("mid", solitude.tier_level("mood", "平常"))
        for tier in ("偏热", "滚烫"):
            self.assertEqual("high", solitude.tier_level("mood", tier))
        for tier in ("累", "偏累"):
            self.assertEqual("low", solitude.tier_level("energy", tier))
        self.assertEqual("mid", solitude.tier_level("energy", "一般"))
        self.assertEqual("high", solitude.tier_level("energy", "充沛"))
        self.assertEqual("low", solitude.tier_level("mood", "不认识的档"),
                         "不认识的档位按最长那档算：宁可晚点动，不许凭猜就动")

    def test_the_three_tiers_pick_the_three_thresholds_in_order(self):
        low_low = solitude.threshold("冰点", "累")
        self.assertEqual(("double_low", 180, "idle_time"),
                         (low_low["key"], low_low["minutes"], low_low["reason_code"]))
        both = solitude.threshold("平常", "一般")
        self.assertEqual(("both_mid", 40, "idle_mood"),
                         (both["key"], both["minutes"], both["reason_code"]))
        both_high = solitude.threshold("滚烫", "充沛")
        self.assertEqual(("both_mid", 40), (both_high["key"], both_high["minutes"]))
        mixed = solitude.threshold("偏冷", "一般")
        self.assertEqual(("otherwise", 90, "idle_mood"),
                         (mixed["key"], mixed["minutes"], mixed["reason_code"]))


class IdleTest(SolitudeCase):
    """「多久没人说话」：最后一条别人的 ts；今天没有就用换日点。"""

    def test_the_clock_starts_at_the_last_thing_he_said(self):
        self.heard()
        self.assertEqual(at(HEARD), solitude.idle_since(at(NOW), brain=self.brain))
        self.assertEqual(45.0, solitude.idle_minutes(at(NOW), brain=self.brain))

    def test_what_she_said_herself_does_not_stop_the_clock(self):
        self.heard()
        out = self.brain.ingest(who="她说", text="我也挪了一次。", ts=at("2026-11-05T13:50:00"),
                                side="她说")
        self.assertTrue(out["ok"], out)
        self.assertEqual(at(HEARD), solitude.idle_since(at(NOW), brain=self.brain),
                         "她自己的话不算「有人跟她说话」")

    def test_no_entry_today_starts_at_the_cutover(self):
        self.assertEqual(at("2026-11-05T04:00:00"),
                         solitude.idle_since(at(NOW), brain=self.brain))
        self.assertEqual(600.0, solitude.idle_minutes(at(NOW), brain=self.brain))


class ConsiderTest(SolitudeCase):
    """45 分钟 + 心情中高 → 触发一次；双低 45 分钟不触发、3 小时触发。"""

    def test_forty_minutes_threshold_fires_at_forty_five(self):
        self.heard()                      # 45 分钟前他说过话 → 阈值 40 那档
        out = self.consider()
        self.assertTrue(out["fired"], out)
        self.assertEqual(40, out["readings"]["threshold_minutes"])
        self.assertEqual("idle_mood", out["readings"]["reason_code"])
        row = self.last()
        for key in ("at", "action", "reason_code", "threshold_minutes", "idle_minutes",
                    "mood_tier", "energy_tier", "why"):
            self.assertIn(key, row, "账行缺 %s" % key)
        self.assertEqual(40, row["threshold_minutes"])
        self.assertEqual("idle_mood", row["reason_code"])
        self.assertEqual(("平常", "充沛"), (row["mood_tier"], row["energy_tier"]))
        self.assertEqual(45.0, row["idle_minutes"])

    def test_double_low_keeps_quiet_at_forty_five_minutes(self):
        set_readings(self.heart, NOW, mood=20.0, energy=10.0)      # 冰点 + 累 → 180 那档
        self.heard()
        out = self.consider()
        self.assertFalse(out["fired"])
        self.assertEqual("not-idle", out["skipped"])
        self.assertEqual(180, out["readings"]["threshold_minutes"])
        self.assertEqual([], self.rows(), "没到阈值是常态，不落账")

    def test_double_low_fires_at_three_hours(self):
        set_readings(self.heart, "2026-11-05T12:00:00", mood=20.0, energy=10.0)
        self.heard(now="2026-11-05T11:00:00")      # 12:00 → 3 小时后才到 180
        early = self.consider("2026-11-05T12:00:00")
        self.assertFalse(early["fired"])
        self.assertEqual(180, early["readings"]["threshold_minutes"])
        out = self.consider("2026-11-05T14:00:00")
        self.assertTrue(out["fired"], out)
        row = self.last()
        self.assertEqual(180, row["threshold_minutes"])
        self.assertEqual("idle_time", row["reason_code"])
        self.assertEqual(180.0, row["idle_minutes"])
        self.assertEqual("low", solitude.tier_level("mood", row["mood_tier"]),
                         "两小时过去心情往基线漂了一点，但仍是 low 那一档")
        self.assertEqual("累", row["energy_tier"])

    def test_one_trigger_does_one_thing(self):
        self.diary("2026-10-01")
        self.heard()
        out = self.consider()
        self.assertEqual("a", out["action"])
        self.assertEqual(1, len(self.rows()), "一次触发只落一行账")
        self.assertEqual([], self.repo_cards(), "读了日记就不再顺手记一句")


class DiaryTest(SolitudeCase):
    """a 读一篇旧日记：真的读出来才算，7 天内读过的不再读。"""

    def test_three_triggers_read_three_different_diaries(self):
        names = [self.diary("2026-10-0%d" % day) for day in (1, 2, 3)]
        self.heard()
        read = []
        for _ in range(3):
            out = self.consider()
            self.assertEqual("a", out["action"], out)
            read.append(self.last()["read"]["name"])
        self.assertEqual(names, read, "从最旧的一篇起，一篇只读一次")
        self.assertEqual(3, len(set(read)), "三篇互不相同")
        self.assertEqual([r["read"]["date"] for r in self.rows()],
                         ["2026-10-01", "2026-10-02", "2026-10-03"])

    def test_a_fourth_trigger_does_not_read_the_same_one_again(self):
        for day in (1, 2, 3):
            self.diary("2026-10-0%d" % day)
        self.heard()
        for _ in range(3):
            self.consider()
        out = self.consider()           # 三篇都读过了：换成 b（记一句今天的事）
        self.assertEqual("b", out["action"], out)
        names = [row["read"]["name"] for row in self.rows() if row.get("read")]
        self.assertEqual(3, len(set(names)))

    def test_a_diary_that_cannot_be_read_is_not_recorded_as_read(self):
        first = self.diary("2026-10-01")
        second = self.diary("2026-10-02")
        third = self.diary("2026-10-03")
        self.heard()

        def fake_read(root=None, *, room, name, shelf=None):
            if name == first:
                raise RuntimeError("这张卡的文件不在了")
            if name == second:
                return {"card": {"name": second}, "body": "", "external": True}
            return {"card": {"name": name}, "body": "那天写的正文在这里。", "external": False}

        with mock.patch("organs.home.cards.read_item", side_effect=fake_read):
            out = self.consider()
        self.assertEqual("a", out["action"])
        self.assertEqual(third, self.last()["read"]["name"],
                         "读不到正文的两张都不算读过，读到的那张才是这一行的 read")
        seen = solitude.recent_reads(self.root, at(NOW))
        self.assertEqual({third}, seen["names"])

    def test_the_same_diary_is_readable_again_after_the_window(self):
        name = self.diary("2026-10-01")
        self.heard()
        self.assertEqual(name, self.consider()["did"]["read"]["name"])
        later = "2026-11-13T14:00:00"            # 8 天后
        set_readings(self.heart, later)
        out = self.consider(later)
        self.assertEqual("a", out["action"], out)
        self.assertEqual(name, self.last()["read"]["name"])


class NoteTodayTest(SolitudeCase):
    """b 记一句今天的事：草稿落仓库（kind=素材），这句话只能由今天的真条目拼出来。"""

    def test_a_real_material_becomes_a_draft_card(self):
        self.heard()
        out = self.consider(request={"action": "b"})
        self.assertEqual("b", out["action"], out)
        cards = self.repo_cards()
        self.assertEqual(1, len(cards))
        card = cards[0]
        self.assertEqual("素材", card["kind"])
        self.assertIs(True, card["params"]["draft"])
        self.assertEqual("2026-11-05", card["params"]["day"])
        self.assertIn("书架", card["params"]["note"], "那句话只能来自今天他说的那一手")
        self.assertIn("人说", card["params"]["note"])
        self.assertEqual("b", self.last()["action"])

    def test_no_material_today_means_no_draft(self):
        out = self.consider(request={"action": "b"}, now="2026-11-05T14:00:00")
        self.assertNotEqual("b", out["action"], out)
        self.assertEqual([], self.repo_cards(), "今天没有真素材就不许编一句")

    def test_a_platform_id_never_gets_into_her_note(self):
        """只给 who、不给 display：正文里不许出现平台号与长数字串，只能叫「人」。"""
        self.heard(who="qq:3425635473")
        out = self.consider(request={"action": "b"})
        self.assertEqual("b", out["action"], out)
        note = self.repo_cards()[0]["params"]["note"]
        self.assertNotIn("qq:", note)
        self.assertNotIn("3425635473", note)
        self.assertIsNone(re.search(r"[0-9]{7,}", note), "正文里不许出现长数字串：%s" % note)
        self.assertIn("人说", note, "取不到称呼就叫「人」：%s" % note)
        because = proactive.own_doing_lines(self.root)[0]["because"]
        self.assertNotIn("qq:", because)
        self.assertIsNone(re.search(r"[0-9]{7,}", because),
                          "由头里同样不许念号：%s" % because)

    def test_a_display_name_is_used_when_there_is_one(self):
        """给了 display 就用它，不回退成「人」；号照样不出现。"""
        self.heard(who="qq:1111111111", display="小明")
        self.consider(request={"action": "b"})
        note = self.repo_cards()[0]["params"]["note"]
        self.assertTrue(note.startswith("今天小明说："), note)
        self.assertNotIn("qq:", note)
        self.assertNotIn("1111111111", note)
        because = proactive.own_doing_lines(self.root)[0]["because"]
        self.assertIn("小明", because)

    def test_a_note_never_invents_anything(self):
        self.heard(text="键盘坏了，明天拿去修。")
        self.consider(request={"action": "b"})
        note = self.repo_cards()[0]["params"]["note"]
        self.assertEqual("今天人说：「键盘坏了，明天拿去修。」", note)


class OwnCardTest(SolitudeCase):
    """c 给自己开一张卡：工作台、kind=物件、owner=她自己、不设 due。"""

    def test_she_opens_a_card_for_herself_without_a_due(self):
        out = self.consider(request={"action": "c"})
        self.assertEqual("c", out["action"], out)
        cards = self.repo_cards("工作台")
        self.assertEqual(1, len(cards))
        card = cards[0]
        self.assertEqual("物件", card["kind"])
        self.assertEqual("她自己", card["params"]["owner"])
        self.assertNotIn("due", card["params"], "她自己的事不许被当成别人的截止时间")
        self.assertIn("她自己想做的事 2026-11-05", card["name"])


class OneAtATimeTest(SolitudeCase):
    """一次只做一件：同一次里塞两件事，一律拒并说明为什么。"""

    def test_two_drafts_in_one_trigger_are_refused(self):
        self.heard()
        request = {"drafts": [{"name": "草稿一", "note": "第一张"},
                              {"name": "草稿二", "note": "第二张"}]}
        with self.assertRaises(solitude.SolitudeError) as caught:
            self.consider(request=request)
        message = str(caught.exception)
        self.assertIn("一次只做一件", message)
        self.assertIn("2 张草稿", message)
        self.assertIn("草稿一", message)
        self.assertEqual([], self.rows(), "被拒的那一次一个字都不落")
        self.assertEqual([], self.repo_cards())

    def test_two_actions_in_one_trigger_are_refused(self):
        with self.assertRaises(solitude.SolitudeError) as caught:
            self.consider(request={"actions": ["b", "c"]})
        self.assertIn("一次只做一件", str(caught.exception))

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(solitude.SolitudeError):
            self.consider(request={"action": "d"})

    def test_one_action_is_accepted(self):
        self.assertEqual("b", solitude.parse_request({"action": "b"}))
        self.assertEqual("b", solitude.parse_request({"actions": ["b"]}))
        self.assertEqual("b", solitude.parse_request({"drafts": [{"name": "一张"}]}))
        self.assertEqual("", solitude.parse_request({}))


class NothingToDoTest(SolitudeCase):
    """到了阈值却什么都做不成：落一行 skipped，不许假装做了。"""

    def test_nothing_doable_lands_as_skipped(self):
        self.heard()
        with mock.patch.object(solitude, "_write_card",
                               return_value=(500, {"ok": False, "message": "家写不进去"})):
            out = self.consider()
        self.assertFalse(out["fired"])
        self.assertEqual("nothing-to-do", out["skipped"])
        row = self.last()
        self.assertEqual("skipped", row["action"])
        self.assertEqual("nothing-to-do", row["why"])
        self.assertEqual([{"action": "a", "ok": False}, {"action": "b", "ok": False},
                          {"action": "c", "ok": False}], out["tried"])


class NightTest(SolitudeCase):
    """夜里：不做事、不调核，idle 账落一行 held（why=night），同一夜只落一行。"""

    def test_night_holds_and_touches_nothing(self):
        self.diary("2026-10-01")
        set_readings(self.heart, NIGHT)
        with mock.patch.object(solitude, "_write_card",
                               side_effect=AssertionError("夜里不写卡")) as wrote, \
                mock.patch.object(proactive_ask, "ask",
                                  side_effect=AssertionError("夜里不调核")) as asked:
            out = self.consider(NIGHT)
        self.assertEqual(0, wrote.call_count)
        self.assertEqual(0, asked.call_count, "独处自己不调核，夜里更不调")
        self.assertFalse(out["fired"])
        self.assertEqual("night", out["skipped"])
        row = self.last()
        self.assertEqual("held", row["action"])
        self.assertEqual("night", row["why"])
        self.assertTrue(row["mood_tier"])

    def test_one_held_row_per_night(self):
        set_readings(self.heart, NIGHT)
        self.consider(NIGHT)
        self.consider("2026-11-05T23:59:00")
        self.assertEqual(1, len(self.rows()), "一整夜只落一行 held，不是一分钟一行")


class OwnDoingTest(SolitudeCase):
    """做完了想跟你说一声：由头池一条 own_doing，走既有 proactive 链。"""

    def test_reading_a_diary_leaves_one_own_doing_in_the_pool(self):
        self.diary("2026-10-01")
        self.heard()
        out = self.consider()
        self.assertEqual("a", out["action"])
        pool = proactive.own_doing_lines(self.root)
        self.assertEqual(1, len(pool), "一次独处最多一条")
        self.assertEqual("a", pool[0]["action"])
        self.assertIn("日记-2026-10-01", pool[0]["because"])
        self.assertEqual(at(NOW), pool[0]["at"])

    def test_only_one_pool_line_while_the_first_is_still_open(self):
        self.diary("2026-10-01")
        self.diary("2026-10-02")
        self.heard()
        self.consider()
        self.consider()
        self.assertEqual(1, len(proactive.own_doing_lines(self.root)),
                         "池里挂着一条没被用掉的就不再写第二条")

    def test_opening_a_card_for_herself_says_nothing(self):
        out = self.consider(request={"action": "c"})
        self.assertEqual("c", out["action"])
        self.assertEqual([], proactive.own_doing_lines(self.root),
                         "order 里没点名 c：她自己给自己开卡不必跟人说")

    def test_the_pool_line_becomes_the_fifth_reason(self):
        self.diary("2026-10-01")
        self.heard()
        self.consider()
        fired = proactive.sources(at(NOW), root=self.root, heart_root=self.heart,
                                  brain=self.brain, home_root=self.home)
        codes = [item["reason_code"] for item in fired]
        self.assertIn(proactive.REASON_OWN_DOING, codes)
        self.assertIn(proactive.REASON_OWN_DOING, proactive.REASONS)
        self.assertEqual(proactive.REASON_OWN_DOING, proactive.REASON_ORDER[-1],
                         "她自己的事排在最后：别人的由头先说话")

    def test_a_used_own_doing_does_not_come_back(self):
        self.diary("2026-10-01")
        self.heard()
        self.her_turn()        # 最后一手是她说的：unfinished 不成立，只剩 own_doing 这条由头
        self.consider()
        said = proactive.consider(at(NOW), root=self.root, heart_root=self.heart,
                                  brain=self.brain, home_root=self.home)
        self.assertTrue(said["fired"], said)
        self.assertEqual(proactive.REASON_OWN_DOING, said["reason_code"])
        self.assertTrue(said["row"]["own_doing_id"])
        self.assertIsNone(proactive.open_own_doing(self.root, at(NOW)),
                          "用掉的那条不再挂回池里")


class TickTest(SolitudeCase):
    """接线段：tick 里那一小段跑得上，坏了也不许让节律停摆。"""

    def tick(self, now: str = NOW, **kw):
        from organs.day import rhythm
        return rhythm.tick(at(now), root=self.root, brain=self.brain,
                           heart_root=self.heart, **kw)

    def test_tick_runs_solitude_after_proactive(self):
        self.heard()
        payload = self.tick()
        self.assertTrue(payload["ok"], payload)
        self.assertIn("solitude", payload)
        self.assertTrue(payload["solitude"]["fired"], payload["solitude"])
        self.assertEqual(1, len(self.rows()))

    def test_a_broken_solitude_is_reported_not_swallowed(self):
        with mock.patch.object(solitude, "consider", side_effect=RuntimeError("独处炸了")):
            payload = self.tick()
        self.assertTrue(payload["ok"], "tick 照常返回")
        self.assertFalse(payload["solitude"]["ok"])
        self.assertIn("solitude", payload["solitude"]["error"])
        from organs.day import rhythm
        self.assertIn("solitude", rhythm.load_state(self.root)["last_error"])

    def test_tick_does_not_write_a_second_held_row_at_night(self):
        set_readings(self.heart, NIGHT)
        self.tick(NIGHT)
        self.tick("2026-11-05T23:59:00")
        self.assertEqual(["held"], [row["action"] for row in self.rows()])


class OverviewTest(SolitudeCase):
    """GET /day/solitude 的读数：阈值、档位、闲置分钟、账尾；纯读不落账。"""

    def test_overview_reads_without_writing(self):
        self.heard()
        payload = solitude.overview(at(NOW), root=self.root, heart_root=self.heart,
                                    brain=self.brain, home_root=self.home)
        self.assertTrue(payload["ok"])
        self.assertEqual(40, payload["threshold_minutes"])
        self.assertEqual("idle_mood", payload["reason_code"])
        self.assertEqual(("平常", "mid"), (payload["mood_tier"], payload["mood_level"]))
        self.assertEqual(45.0, payload["idle_minutes"])
        self.assertEqual(900, payload["awake_minutes"])
        self.assertFalse(payload["night"])
        self.assertEqual([], payload["log"], "GET 不该写账")
        self.assertEqual([], plan.load(self.root)["pending"])


if __name__ == "__main__":
    unittest.main()
