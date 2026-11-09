"""情绪的契约与两个投递口的验收：契约可改、冷却按类、权重按档、静音与 cold 两件都落。

判据：下文的数都是契约默认值派生的期望，不是验收线——改 emotion_contract.json，
期望跟着改。所有用例都在 helpers.make_root 的临时目录里跑，绝不写真 data；
另外每个类都把 store.default_root 钉到临时根上——**就算哪一行漏了 root，
也不可能写到实盘去**（这条是拿一次真实事故换来的）。

三条钉死的立场（写在这里，免得用例读起来像随口定的）：
1. 核只递 kind，**碰不到 mood 的数值**：权重由契约给，推多少由心按冷却与权重拍；
2. 权重是「档」：一档 = model.MOOD_UNIT 分，正向小幅、负向明显；
3. 静音是行为，cold 是情绪——进了静音，两件都要落。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

import organs.heart as heart_pkg
from organs.heart import events, model, mood, proactive, report, store
from organs.heart.tests import helpers

BASE_HOUR = 14


def _local(hour: int = BASE_HOUR, minute: int = 0) -> datetime:
    """本机时区的一个固定时刻：2025-01-15 <hour>:<minute>。"""
    tz = datetime.now().astimezone().tzinfo
    return datetime(2025, 1, 15, hour, minute, 0, tzinfo=tz)


def _at(minutes: float = 0.0, base: Optional[datetime] = None) -> str:
    return model.iso((base or _local()) + timedelta(minutes=minutes))


def _read_contract_file(root: Path) -> Dict[str, Any]:
    return json.loads((root / report.CONTRACT_FILE).read_text(encoding="utf-8"))


def _write_contract_file(root: Path, table: Dict[str, Any]) -> None:
    (root / report.CONTRACT_FILE).write_text(
        json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")


def _log_rows(root: Path) -> Any:
    return store.read_jsonl(root / report.LOG_FILE)


class RootCase(unittest.TestCase):
    """共同的地基：临时根 + 出厂契约 + 把 default_root 钉死在临时根上。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)
        shutil.copy(helpers.heart_data() / report.CONTRACT_FILE,
                    self.root / report.CONTRACT_FILE)
        patcher = mock.patch.object(store, "default_root", lambda: Path(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t0 = _at()

    def _event(self, kind: str, who: str = "主人", at: Optional[str] = None,
               note: str = "", now: Optional[str] = None, force: bool = False,
               reason: Optional[str] = None) -> Dict[str, Any]:
        """投一件事件：root 一律是临时根（漏了也不许落到实盘，见类文档）。

        每个参数都明写出来、不用 **kwargs 转发：root= 得在守卫眼里看得见
        （tests/test_root_discipline.py 会扫这一行）。
        """
        return report.report_event(
            kind, who=who, at=at, note=note, root=self.root, now=now or self.t0,
            force=force, reason=reason)

    def _react(self, kind: str, who: str = "主人",
               now: Optional[str] = None) -> Dict[str, Any]:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            return report.report_reaction(
                kind, who=who, root=self.root, now=now or self.t0)


class ContractTest(RootCase):
    """契约读入：出厂那份就是默认值，坏文件永不抛。"""

    def test_factory_file_equals_the_builtin_defaults(self) -> None:
        """出厂那份与代码里的 DEFAULT_CONTRACT 逐字对齐：两处不许漂。"""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = report.load_contract(self.root)
        self.assertEqual([], [str(w.message) for w in caught], "出厂那份读进来还 WARN")
        self.assertEqual(report._factory(), table)

    def test_shape_is_complete(self) -> None:
        """五类事件、两条反馈一个不缺，每类都带 name / meaning / weight / ledger。"""
        table = report.load_contract(self.root)
        self.assertEqual(set(model.EVENTS), set(table["events"]))
        self.assertEqual((report.REACTION_WARM, report.REACTION_COLD),
                         tuple(table["reactions"]))
        for kind, entry in table["events"].items():
            self.assertEqual({"name", "meaning", "weight", "ledger"}, set(entry), kind)
            self.assertTrue(entry["ledger"], kind)
        # 「被骂 > 被冷落」由数自己说清，不靠形容词。
        self.assertGreater(abs(table["events"][model.EVENT_INSULT]["weight"]),
                           abs(table["events"][model.EVENT_COLD]["weight"]))
        self.assertGreater(table["events"][model.EVENT_PRAISE]["weight"], 0.0)
        self.assertLess(table["events"][model.EVENT_REJECTED]["weight"], 0.0)

    def test_missing_file_falls_back_to_builtin_defaults_with_one_warn(self) -> None:
        """读不到 → 内置默认 + WARN 一行，永不抛。"""
        (self.root / report.CONTRACT_FILE).unlink()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = report.load_contract(self.root)
        self.assertEqual(1, len(caught), "读不到应当只 WARN 一行")
        self.assertIn("读不到", str(caught[0].message))
        self.assertEqual(report._factory(), table)

    def test_broken_json_falls_back_to_builtin_defaults(self) -> None:
        helpers.write_raw(self.root / report.CONTRACT_FILE, "{不是 JSON")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = report.load_contract(self.root)
        self.assertEqual(report._factory(), table)
        self.assertEqual(1, len(caught))

    def test_missing_keys_and_out_of_range_are_filled_and_clamped(self) -> None:
        """缺键补默认、越界夹回，两处都进 WARN，仍然不抛。"""
        _write_contract_file(self.root, {
            "version": 1,
            "cooldown_min": -5,
            "events": {"praise": {"weight": 99, "ledger": []}},
            "reactions": {"warm": {}},
        })
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = report.load_contract(self.root)
        self.assertEqual(1, len(caught), "多处问题也只 WARN 一行")
        self.assertEqual(float(report._COOLDOWN_RANGE[0]), table["cooldown_min"])
        self.assertEqual(float(report._WEIGHT_RANGE[1]),
                         table["events"][model.EVENT_PRAISE]["weight"])
        self.assertEqual(list(report.LEDGER_FIELDS),
                         table["events"][model.EVENT_PRAISE]["ledger"],
                         "空账格式退回默认（空表等于不落账）")
        self.assertEqual(report.DEFAULT_CONTRACT["events"][model.EVENT_CARE]["weight"],
                         table["events"][model.EVENT_CARE]["weight"], "缺的类补默认")
        self.assertEqual(report.DEFAULT_CONTRACT["reactions"][report.REACTION_WARM]["name"],
                         table["reactions"][report.REACTION_WARM]["name"])

    def test_top_level_not_an_object_falls_back(self) -> None:
        helpers.write_raw(self.root / report.CONTRACT_FILE, "[1, 2, 3]")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = report.load_contract(self.root)
        self.assertEqual(report._factory(), table)
        self.assertEqual(1, len(caught))

    def test_ports_are_exported_from_the_package(self) -> None:
        """核只需要 heart.report_event / heart.report_reaction 这两个名字。"""
        self.assertIs(report.report_event, heart_pkg.report_event)
        self.assertIs(report.report_reaction, heart_pkg.report_reaction)
        self.assertEqual(("report_event", "report_reaction"), heart_pkg.__all__)


class ReportEventTest(RootCase):
    """投递口一：五类事件。一次投递 = 事件账一行 + 本模块账一行。"""

    def _mood(self, now: Optional[str] = None) -> Dict[str, Any]:
        return mood.read(self.root, now=now or self.t0)

    def test_praise_lifts_mood_and_lands_one_row_in_each_ledger(self) -> None:
        """验收 1：praise 推上去；事件账多**一**行（不是两行）；本模块账一行。"""
        before = self._mood()["now"]
        got = self._event("praise", who="主人", note="她说谢谢")
        after = self._mood()["now"]

        self.assertTrue(got["applied"], got)
        self.assertEqual("praise", got["kind"])
        self.assertEqual("主人", got["who"])
        self.assertEqual(1.0, got["weight"], "契约里 praise 是 +1 档")
        self.assertIsNone(got["reason"])
        self.assertAlmostEqual(60.0, got["mood_before"], places=1)
        self.assertAlmostEqual(64.0, got["mood_after"], places=1, msg="60 + 10×1×0.4")
        self.assertGreater(after, before, "投了一次夸，mood 没上行：%s → %s" % (before, after))

        rows = events.recent(root=self.root, now=self.t0, window_min=60.0)
        self.assertEqual(1, len(rows), "一次投递在事件账里只该有一行：%r" % (rows,))
        self.assertEqual("praise", rows[-1]["kind"])
        self.assertEqual("主人", rows[-1]["who"])

        log = _log_rows(self.root)
        self.assertEqual(1, len(log))
        self.assertEqual(list(report.LEDGER_FIELDS), list(log[-1]))
        self.assertEqual(True, log[-1]["applied"])
        self.assertIsNone(log[-1]["reason"])
        self.assertAlmostEqual(60.0, log[-1]["mood_before"], places=1)
        self.assertAlmostEqual(64.0, log[-1]["mood_after"], places=1)
        self.assertEqual("她说谢谢", log[-1]["note"])

    def test_ten_praises_in_one_minute_only_push_once(self) -> None:
        """验收 2：同一分钟连投十次 → 只推一次，其余九次 applied:false/cooldown。"""
        results = [self._event("praise", who="主人") for _ in range(10)]
        self.assertEqual([True] + [False] * 9, [row["applied"] for row in results])
        self.assertEqual([None] + ["cooldown"] * 9, [row["reason"] for row in results])
        for row in results[1:]:
            self.assertIsNone(row["mood_before"], "冷却行没推，就没有前后")
            self.assertIsNone(row["mood_after"])

        log = _log_rows(self.root)
        self.assertEqual(10, len(log), "冷却中的投递也要记账，一行都不能少")
        self.assertEqual(1, len([row for row in log if row["applied"] is True]))
        self.assertEqual(9, len([row for row in log if row["reason"] == "cooldown"]))
        # mood 只有一个台阶：60 → 64.0，十次投递也只动这一下。
        self.assertAlmostEqual(64.0, self._mood()["now"], places=1)

    def test_cooldown_is_counted_per_kind(self) -> None:
        """冷却按 kind 各算：夸完立刻被冷落，cold 不受 praise 的冷却影响。"""
        first = self._event("praise", who="甲")
        second = self._event("cold", who="乙")
        self.assertTrue(first["applied"])
        self.assertTrue(second["applied"], second)
        self.assertAlmostEqual(64.0, second["mood_before"], places=1)

    def test_bad_kind_is_refused_with_all_five_names(self) -> None:
        """验收 3：外星事件直接拒，消息列出五类并原样带出收到的值，一个文件都不写。"""
        with self.assertRaises(ValueError) as caught:
            self._event("外星事件", who="主人")
        message = str(caught.exception)
        for kind in model.EVENTS:
            self.assertIn(kind, message, "拒收消息里少了 %s" % kind)
        self.assertIn("外星事件", message, "得原样带出收到的值")
        self.assertFalse((self.root / report.LOG_FILE).exists(), "被拒的投递不许落账")
        self.assertFalse((self.root / mood.MOOD_FILE).exists(), "被拒的投递不许动 mood")
        self.assertFalse((self.root / events.EVENTS_FILE).exists())

    def test_contract_weight_changes_how_much_it_pushes(self) -> None:
        """验收 6 前半：把 praise 的 weight 改成 2.0 → 同一句夸推更多。"""
        default = self._event("praise", who="甲")

        table = _read_contract_file(self.root)
        table["events"]["praise"]["weight"] = 2.0
        _write_contract_file(self.root, table)
        # 同一分钟的第二投会踩冷却，这里只比权重：force 绕开冷却，其余一模一样。
        doubled = self._event("praise", who="乙", force=True)

        self.assertEqual(2.0, doubled["weight"])
        self.assertAlmostEqual(default["mood_after"], doubled["mood_before"], places=1,
                               msg="同一句话、同一个起点，只差契约权重")
        self.assertGreater(doubled["mood_after"] - doubled["mood_before"],
                           default["mood_after"] - default["mood_before"],
                           "改了契约权重却没推更多")
        log = _log_rows(self.root)
        self.assertEqual([1.0, 2.0], [row["weight"] for row in log])

    def test_zero_cooldown_lets_every_delivery_through(self) -> None:
        """验收 6 后半：cooldown_min 改成 0 → 同类连投全部 applied。

        同一个人也不再有额外的小时限制，热度和渐近线仍控制增量。
        """
        table = _read_contract_file(self.root)
        table["cooldown_min"] = 0
        _write_contract_file(self.root, table)

        results = [self._event("praise", who="主人") for _ in range(10)]
        self.assertEqual([True] * 10, [row["applied"] for row in results])
        self.assertEqual([None] * 10, [row["reason"] for row in results])
        self.assertEqual(10, len(_log_rows(self.root)))

    def test_disabled_cooldown_keeps_repeat_gain_diminishing(self) -> None:
        """用户关闭冷却后仍按热度递减，不偷偷增加另一层小时限制。"""
        table = _read_contract_file(self.root)
        table["cooldown_min"] = 0
        _write_contract_file(self.root, table)

        first = self._event("praise", who="主人")
        second = self._event("praise", who="主人")
        self.assertTrue(first["applied"])
        self.assertTrue(second["applied"])
        self.assertGreater(second['mood_after'], second['mood_before'])
        self.assertLess(second['mood_after'] - second['mood_before'],
                        first['mood_after'] - first['mood_before'])
        log = _log_rows(self.root)
        self.assertEqual(2, len(log))
        self.assertTrue(log[-1]['applied'])

    def test_force_bypasses_the_cooldown_and_writes_its_reason(self) -> None:
        """force=True 绕冷却，理由写进账（静音那条 cold 就走这条）。"""
        self._event("praise", who="甲")
        normal = self._event("praise", who="乙")
        forced = self._event("praise", who="丙", force=True, reason=report.REASON_MUTE)
        self.assertFalse(normal["applied"])
        self.assertEqual("cooldown", normal["reason"])
        self.assertTrue(forced["applied"], forced)
        self.assertEqual(report.REASON_MUTE, forced["reason"])
        self.assertEqual(report.REASON_MUTE, _log_rows(self.root)[-1]["reason"])

    def test_ledger_fields_and_order_come_from_the_contract(self) -> None:
        """账格式写在契约里：把 praise 的 ledger 改成三个字段，落账就只写这三个。"""
        table = _read_contract_file(self.root)
        table["events"]["praise"]["ledger"] = ["at", "kind", "applied"]
        _write_contract_file(self.root, table)

        self._event("praise", who="主人")
        row = _log_rows(self.root)[-1]
        self.assertEqual(["at", "kind", "applied"], list(row))
        self.assertEqual({"at": self.t0, "kind": "praise", "applied": True}, row)

    def test_at_and_now_are_the_same_clock(self) -> None:
        """只给 at 或只给 now，记的都是那个时刻。"""
        only_at = self._event("praise", who="甲", now=None, at=self.t0)
        self.assertEqual(self.t0, only_at["at"])
        self.assertEqual(self.t0, _log_rows(self.root)[-1]["at"])

        later = _at(minutes=11)
        only_now = self._event("praise", who="乙", now=later, at=None)
        self.assertEqual(later, only_now["at"])

    def test_care_is_half_a_tier(self) -> None:
        """care 只有半档（+0.5）：被关心比被夸轻。"""
        got = self._event("care", who="主人")
        self.assertEqual(0.5, got["weight"])
        self.assertAlmostEqual(62.0, got["mood_after"], places=1, msg="60 + 10×0.5×0.4")

    def test_rejected_is_two_tiers_down(self) -> None:
        """rejected（原来「冲突 / 被拒」那一类）照旧 −2 档：与 insult 同重，比 cold 重一倍。"""
        got = self._event("rejected", who="主人")
        self.assertEqual(-2.0, got["weight"])
        self.assertAlmostEqual(40.0, got["mood_after"], places=1)


class ReportReactionTest(RootCase):
    """投递口二：她开口之后有没有人理。静音是行为，cold 是情绪，两件都落。"""

    def _mood_now(self, now: Optional[str] = None) -> float:
        return mood.read(self.root, now=now or self.t0)["now"]

    def test_bad_reaction_kind_is_refused(self) -> None:
        """kind 不在 {warm, cold} → ValueError，消息列清两个并带出收到的值。"""
        with self.assertRaises(ValueError) as caught:
            self._react("hot")
        message = str(caught.exception)
        self.assertIn("warm", message)
        self.assertIn("cold", message)
        self.assertIn("hot", message)

    def test_three_colds_mute_and_push_exactly_one_cold_event(self) -> None:
        """验收 4：cold 连三次 → 第三次 streak=3 / muted=true，且 mood 下行。"""
        start = self._mood_now()
        first = self._react("cold")
        self.assertAlmostEqual(start, self._mood_now(), places=1,
                               msg="第一次没人理不该动 mood")
        self.assertEqual(1, first["streak"])
        self.assertFalse(first["muted"])
        self.assertIsNone(first["mood_event"], "还没到静音，不该推 cold")

        second = self._react("cold")
        self.assertEqual(2, second["streak"])
        self.assertIsNone(second["mood_event"])
        self.assertAlmostEqual(start, self._mood_now(), places=1,
                               msg="第二次没人理也不该动 mood")

        third = self._react("cold")
        self.assertEqual(3, third["streak"])
        self.assertTrue(third["muted"], "第三次没人理该静音了")
        self.assertIsNotNone(third["muted_until"])
        event = third["mood_event"]
        self.assertIsNotNone(event, "静音了却没落情绪：只有行为没有情绪")
        self.assertTrue(event["applied"])
        self.assertEqual(report.MUTE_EVENT, event["kind"])
        self.assertEqual(report.REASON_MUTE, event["reason"])
        self.assertAlmostEqual(start - 10.0, event["mood_after"], places=1,
                               msg="被冷落一档：mood 从 %s 掉到 %s" % (start, start - 10.0))
        self.assertLess(self._mood_now(), start, "被冷落到底了，mood 没下行")

        log = _log_rows(self.root)
        self.assertEqual(1, len(log), "前两次不推情绪，账上只该有静音那一条")
        self.assertEqual(report.REASON_MUTE, log[-1]["reason"])
        self.assertEqual(True, log[-1]["applied"])
        self.assertEqual("cold", log[-1]["kind"])

        # 静音自己也落盘了（proactive 那本账）：两件都要有。
        view = proactive.overview(root=self.root, now=self.t0)
        self.assertTrue(view["muted"])
        self.assertEqual(3, view["streak"])
        self.assertEqual(1, len(events.recent(root=self.root, now=self.t0,
                                              kinds=(model.EVENT_COLD,))))

    def test_warm_resets_the_streak_and_pushes_nothing(self) -> None:
        self._react("cold")
        self._react("cold")
        warm = self._react("warm")
        self.assertEqual(0, warm["streak"], "有人接她的话，streak 该清零")
        self.assertFalse(warm["muted"])
        self.assertIsNone(warm["mood_event"])
        self.assertEqual([], _log_rows(self.root), "warm 不推任何情绪")

    def test_already_muted_does_not_push_a_second_cold(self) -> None:
        """已经在静音里继续被无视：不重复推 cold（只认由 False 变 True 那一次）。"""
        for _ in range(3):
            self._react("cold")
        fourth = self._react("cold")
        self.assertEqual(4, fourth["streak"])
        self.assertTrue(fourth["muted"])
        self.assertIsNone(fourth["mood_event"])
        self.assertEqual(1, len(_log_rows(self.root)))

    def test_reaction_works_without_a_pending_opening(self) -> None:
        """没有未结算的开口也能结算：先补一条（proactive.sent 幂等），streak 照走。"""
        self.assertFalse((self.root / proactive.PROACTIVE_FILE).exists())
        got = self._react("cold")
        self.assertEqual(1, got["streak"])
        self.assertTrue((self.root / proactive.PROACTIVE_FILE).exists())
        self.assertEqual(1, len(proactive.overview(root=self.root, now=self.t0)["history"]))


class ApplyEventTiersTest(unittest.TestCase):
    """mood 只开的那一个口子：tiers 换名义档，其余一步没动。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = helpers.make_root(self.tmp.name)
        patcher = mock.patch.object(store, "default_root", lambda: Path(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t0 = _at()

    def test_default_tier_is_unchanged(self) -> None:
        got = mood.apply_event("praise", root=self.root, now=self.t0, who="甲")
        self.assertAlmostEqual(64.0, got["after"], places=1)

    def test_tiers_overrides_only_the_nominal_tier(self) -> None:
        """两个各自独立的根上投同一句夸：只差 tiers，两个系数一模一样、增量正好翻倍。"""
        other = self.root / "doubled"
        other.mkdir()
        default = mood.apply_event("praise", root=self.root, now=self.t0, who="甲")
        doubled = mood.apply_event("praise", root=other, now=self.t0, who="甲",
                                   tiers=2.0)
        self.assertAlmostEqual(1.0, default["heat"], places=4)
        self.assertAlmostEqual(default["heat"], doubled["heat"], places=4,
                               msg="tiers 只换名义档，热度折扣没动")
        self.assertAlmostEqual(default["headroom"], doubled["headroom"], places=4,
                               msg="tiers 只换名义档，渐近线没动")
        self.assertAlmostEqual(64.0, default["after"], places=1)
        self.assertAlmostEqual(68.0, doubled["after"], places=1, msg="60 + 10×2×0.4")
        self.assertAlmostEqual(2.0 * (default["after"] - default["before"]),
                               doubled["after"] - doubled["before"], places=1)

    def test_negative_tier_still_does_not_scale(self) -> None:
        """负向不缩放：给了 tiers 也照样不被 heat / headroom 打折。"""
        got = mood.apply_event("rejected", root=self.root, now=self.t0, who="甲",
                               tiers=-2.0)
        self.assertEqual(1.0, got["heat"])
        self.assertEqual(1.0, got["headroom"])
        self.assertAlmostEqual(40.0, got["after"], places=1)


class FactoryFileIsReadableTest(unittest.TestCase):
    """出厂那份真的被放行、真的能读：data/.gitignore 里没有它，人就看不见默认值。"""

    def test_contract_is_whitelisted_in_the_data_gitignore(self) -> None:
        text = (store.DATA_DIR / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("!%s" % report.CONTRACT_FILE, text)

    def test_factory_file_carries_the_note(self) -> None:
        """契约里要有一句人话：默认值不是验收线，改完即生效。"""
        table = json.loads((store.DATA_DIR / report.CONTRACT_FILE)
                           .read_text(encoding="utf-8"))
        note = table["note"]
        self.assertIn("不是验收线", note)
        self.assertIn("改这份即生效", note)
        self.assertEqual(note, report.NOTE, "出厂文件里的说明与代码里的不许漂")
        self.assertEqual(report.NOTE, report.load_contract(store.DATA_DIR)["note"])

    def test_factory_file_is_valid_json(self) -> None:
        table = json.loads((store.DATA_DIR / report.CONTRACT_FILE)
                           .read_text(encoding="utf-8"))
        self.assertEqual(set(model.EVENTS), set(table["events"]))
        for kind, entry in table["events"].items():
            self.assertIsInstance(entry["ledger"], list, kind)
            self.assertEqual(list(report.LEDGER_FIELDS), entry["ledger"], kind)
