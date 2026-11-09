"""proactive 的验收：她主动开口之后有没有人理——记结果、静音、两条恢复路。

三条口径各有一组用例：
- **幂等**：一次开口只记一条 pending，重复 sent() 不长第二行；
- **读时现算**：静音到期靠 now 往前推导，不写盘、不挂定时器（两次读之间文件 sha 一致）；
- **结算必须响**：没有 pending 还来结账 → ok=False + code="no-pending"，不静默改数。

时间一律用带显式偏移的固定串（+08:00），跑在哪个时区都同一个结论——落盘的戳走
model.iso（本地时区、秒精度），所以断言比的是**同一时刻**（same 把它们归一成同一种写法），
不是同一段字符串。
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
import warnings
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from organs.heart import model, proactive, store
from organs.heart.tests import helpers

# 固定时刻：14:00 第一次开口；三次都没人理 → 14:25 静音，到次日 02:25（默认 12 小时）。
DAY = "2024-05-06T14:00:00+08:00"
MUTED_AT = "2024-05-07T02:25:00+08:00"


def after(hours: float, base: str = DAY) -> str:
    """base 往后 hours 小时的时刻串。"""
    return model.iso(model.parse_ts(base) + timedelta(hours=hours))


def minutes_after(minutes: float, base: str = DAY) -> str:
    return model.iso(model.parse_ts(base) + timedelta(minutes=minutes))


def same(moment: str) -> str:
    """把期望时刻归一成 model.iso 的写法，好直接比串（比的是时刻，不是写法）。"""
    return model.iso(model.parse_ts(moment))


def file_of(root: Path) -> Path:
    return root / proactive.PROACTIVE_FILE


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def history_of(root: Path) -> list:
    return helpers.read_json_file(file_of(root))["history"]


@contextmanager
def silenced():
    """静音那一刻的 WARN 是设计行为：不专门验收它的用例把它忽略掉，免得刷屏。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        yield


def mute_it(root: Path) -> None:
    """造 3 次主动开口全无回应：开口在 14:00 / 14:10 / 14:20，回执各晚 5 分钟。"""
    with silenced():
        for step in range(3):
            proactive.sent(root=root, now=minutes_after(step * 10))
            proactive.outcome("ignored", root=root, now=minutes_after(step * 10 + 5))


class _Root(unittest.TestCase):
    """每个用例一个临时根，一个都不碰实盘。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = helpers.make_root(self._tmp.name)


class SentTest(_Root):
    """sent：她**主动**开口了 → 记一条 pending；同一条未结算的不重复记。"""

    def test_sent_records_one_pending_and_last_sent_at(self) -> None:
        result = proactive.sent(root=self.root, now=DAY)
        self.assertTrue(result["ok"])
        self.assertEqual(proactive.OUTCOME_PENDING, result["outcome"])
        data = helpers.read_json_file(file_of(self.root))
        self.assertEqual(proactive.VERSION, data["version"])
        self.assertEqual([{"at": DAY, "outcome": "pending"}], data["history"])
        self.assertEqual(DAY, data["last_sent_at"])
        self.assertEqual(0, data["streak"])
        self.assertIsNone(data["muted_until"])

    def test_second_sent_is_idempotent(self) -> None:
        proactive.sent(root=self.root, now=DAY)
        again = proactive.sent(root=self.root, now=minutes_after(5))
        self.assertFalse(again["ok"])
        self.assertEqual("already-pending", again["code"])
        self.assertEqual(1, len(history_of(self.root)), "一次开口只许记一行")
        self.assertEqual(DAY, helpers.read_json_file(file_of(self.root))["last_sent_at"],
                         "重复调用不是新的一次开口，last_sent_at 不许被顶掉")

    def test_sent_after_settlement_records_a_new_row(self) -> None:
        proactive.sent(root=self.root, now=DAY)
        proactive.outcome("ignored", root=self.root, now=minutes_after(30))
        proactive.sent(root=self.root, now=minutes_after(60))
        self.assertEqual(2, len(history_of(self.root)))
        self.assertEqual(["ignored", "pending"],
                         [row["outcome"] for row in history_of(self.root)])


class OutcomeTest(_Root):
    """outcome：结算最新那条 pending；ignored 加一，replied 清零并解静音。"""

    def test_three_ignored_mute_for_twelve_hours(self) -> None:
        mute_it(self.root)
        self.assertEqual(3, proactive.is_muted(root=self.root, now=DAY)["streak"])
        live = proactive.is_muted(root=self.root, now=minutes_after(25))
        self.assertTrue(live["muted"])
        self.assertEqual(720.0, live["remaining_min"], "刚好 12 小时 = 720 分钟")
        self.assertEqual(same(MUTED_AT), live["muted_until"])
        data = helpers.read_json_file(file_of(self.root))
        self.assertEqual(3, data["streak"])
        self.assertEqual(same(MUTED_AT), data["muted_until"])

    def test_mute_warns_once_with_the_numbers(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for step in range(3):
                proactive.sent(root=self.root, now=minutes_after(step * 10))
                proactive.outcome("ignored", root=self.root,
                                  now=minutes_after(step * 10 + 5))
        muted_warnings = [item for item in caught if "主动开口没人回" in str(item.message)]
        self.assertEqual(1, len(muted_warnings), "静音那一刻只警告一次")
        text = str(muted_warnings[0].message)
        self.assertIn("连续 3 次", text)
        self.assertIn(same(MUTED_AT), text, "要写清安静到什么时候")

    def test_at_the_boundary_the_mute_is_already_lifted(self) -> None:
        mute_it(self.root)
        read = proactive.is_muted(root=self.root, now=MUTED_AT)
        self.assertFalse(read["muted"], "到点那一刻就算解除")
        self.assertEqual(0.0, read["remaining_min"])

    def test_replied_clears_streak_and_mute(self) -> None:
        mute_it(self.root)
        self.assertTrue(proactive.is_muted(root=self.root, now=minutes_after(25))["muted"])
        with silenced():
            proactive.sent(root=self.root, now=minutes_after(60))
            result = proactive.outcome("replied", root=self.root, now=minutes_after(65))
        self.assertEqual(0, result["streak"])
        self.assertIsNone(result["muted_until"])
        self.assertFalse(result["muted"])
        self.assertEqual({"muted": False, "remaining_min": 0.0, "streak": 0,
                          "muted_until": None},
                         proactive.is_muted(root=self.root, now=minutes_after(65)))

    def test_no_pending_is_reported_not_silent(self) -> None:
        result = proactive.outcome("ignored", root=self.root, now=DAY)
        self.assertFalse(result["ok"])
        self.assertEqual("no-pending", result["code"])
        self.assertFalse(file_of(self.root).exists(), "没账可结就别凭空造一份")

    def test_no_pending_does_not_touch_settled_rows(self) -> None:
        proactive.sent(root=self.root, now=DAY)
        proactive.outcome("replied", root=self.root, now=minutes_after(5))
        before = sha(file_of(self.root))
        result = proactive.outcome("ignored", root=self.root, now=minutes_after(10))
        self.assertEqual("no-pending", result["code"])
        self.assertEqual(before, sha(file_of(self.root)), "没 pending 时一个字节都不许动")

    def test_bad_result_raises(self) -> None:
        proactive.sent(root=self.root, now=DAY)
        with self.assertRaises(ValueError):
            proactive.outcome("pending", root=self.root, now=DAY)

    def test_ignored_below_threshold_does_not_mute(self) -> None:
        proactive.sent(root=self.root, now=DAY)
        result = proactive.outcome("ignored", root=self.root, now=minutes_after(5))
        self.assertEqual(1, result["streak"])
        self.assertFalse(result["muted"])
        self.assertIsNone(result["muted_until"])


class OtherSideSpokeTest(_Root):
    """the_other_side_spoke：对方主动找她 → 立刻清 streak 与 muted_until。"""

    def test_clears_and_returns_the_previous_values(self) -> None:
        mute_it(self.root)
        result = proactive.the_other_side_spoke(root=self.root, now=minutes_after(120))
        self.assertTrue(result["ok"])
        self.assertEqual(3, result["streak_before"], "要返回清之前的 streak")
        self.assertEqual(same(MUTED_AT), result["muted_until_before"])
        self.assertTrue(result["was_muted"])
        self.assertEqual({"muted": False, "remaining_min": 0.0, "streak": 0,
                          "muted_until": None},
                         proactive.is_muted(root=self.root, now=minutes_after(120)))
        data = helpers.read_json_file(file_of(self.root))
        self.assertEqual(0, data["streak"])
        self.assertIsNone(data["muted_until"])
        self.assertEqual(3, len(data["history"]), "恢复不清账本：history 一行都不删")

    def test_nothing_to_clear_is_still_ok(self) -> None:
        result = proactive.the_other_side_spoke(root=self.root, now=DAY)
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["streak_before"])
        self.assertFalse(result["was_muted"])
        self.assertFalse(file_of(self.root).exists(), "本来就没账：清了个寂寞，不必造文件")


class LaterReplyTest(_Root):
    def test_return_is_scoped_to_latest_ignored_recipient_and_keeps_history(self):
        import json
        context={'who':'10000011','session_id':'qq:group:70001','delivery_id':'test-delivery'}
        store.write_json(file_of(self.root),{'config':{'mute_after':1,'mute_hours':12}})
        proactive.sent(root=self.root,now=DAY,context=context)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore');proactive.outcome('ignored',root=self.root,now=minutes_after(5))
        before=file_of(self.root).read_bytes()
        for who,session,when in [('10000012',context['session_id'],minutes_after(10)),
                                  (context['who'],'qq:private:10000011',minutes_after(10)),
                                  (context['who'],context['session_id'],minutes_after(-1))]:
            result=proactive.receive_reply(who,session,root=self.root,now=when)
            self.assertFalse(result['ok'])
            self.assertEqual(before,file_of(self.root).read_bytes())
        result=proactive.receive_reply(context['who'],context['session_id'],root=self.root,now=minutes_after(10))
        self.assertTrue(result['ok'])
        self.assertTrue(result['was_muted'])
        after=proactive.overview(root=self.root,now=minutes_after(10))
        self.assertFalse(after['muted']);self.assertEqual(0,after['streak'])
        self.assertEqual(json.loads(before)['history'],after['history'])
        saved={p.name:p.read_bytes() for p in self.root.iterdir()}
        proactive.receive_reply(context['who'],context['session_id'],root=self.root,now=minutes_after(11))
        self.assertEqual(saved,{p.name:p.read_bytes() for p in self.root.iterdir()})

    def test_legacy_unattributed_or_older_other_recipient_does_not_restore(self):
        old={'at':DAY,'outcome':'ignored','context':{'who':'10000011','session_id':'qq:private:10000011','delivery_id':'old'}}
        for latest in ({'at':minutes_after(1),'outcome':'ignored'},dict(old,context={**old['context'],'who':'10000012'})):
            store.write_json(file_of(self.root),{'streak':3,'muted_until':minutes_after(60),'history':[old,latest]})
            before=file_of(self.root).read_bytes()
            result=proactive.receive_reply('10000011','qq:private:10000011',root=self.root,now=minutes_after(10))
            self.assertFalse(result['ok']);self.assertEqual(before,file_of(self.root).read_bytes())


class ExpiryTest(_Root):
    """时间到也恢复：is_muted 读时现算，不写盘、不挂定时器。"""

    def test_time_alone_lifts_the_mute_without_writing(self) -> None:
        mute_it(self.root)
        before = sha(file_of(self.root))
        ongoing = proactive.is_muted(root=self.root, now=minutes_after(30))
        self.assertTrue(ongoing["muted"])
        self.assertEqual(715.0, ongoing["remaining_min"])
        expired = proactive.is_muted(root=self.root, now=MUTED_AT)
        self.assertFalse(expired["muted"])
        self.assertEqual(0.0, expired["remaining_min"])
        self.assertIsNone(expired["muted_until"], "到期就不再生效，报 null 而不是旧戳")
        self.assertEqual(3, expired["streak"], "时间到只解除静音，不清 streak")
        self.assertEqual(before, sha(file_of(self.root)), "读时现算：文件 sha 前后一致")

    def test_overview_also_lifts_the_mute_read_only(self) -> None:
        mute_it(self.root)
        before = sha(file_of(self.root))
        seen = proactive.overview(root=self.root, now=after(13))
        self.assertFalse(seen["muted"])
        self.assertEqual(0.0, seen["remaining_min"])
        self.assertEqual(before, sha(file_of(self.root)))

    def test_speaking_again_after_expiry_mutes_again(self) -> None:
        mute_it(self.root)
        later = after(13)
        proactive.sent(root=self.root, now=later)
        with silenced():
            result = proactive.outcome("ignored", root=self.root,
                                       now=minutes_after(1, later))
        self.assertEqual(4, result["streak"])
        self.assertTrue(result["muted"], "到期后再说一次仍没人理，立刻再安静")


class ConfigTest(_Root):
    """config 落盘、人可改：改文件即生效，写回时不许被默认值顶掉。"""

    def test_mute_after_two_means_two_ignored(self) -> None:
        store.write_json(file_of(self.root),
                         {"version": 1, "config": {"mute_after": 2, "mute_hours": 12},
                          "streak": 0, "muted_until": None, "last_sent_at": None,
                          "history": []})
        proactive.sent(root=self.root, now=DAY)
        first = proactive.outcome("ignored", root=self.root, now=minutes_after(1))
        self.assertFalse(first["muted"], "第一次还不到 2")
        proactive.sent(root=self.root, now=minutes_after(10))
        with silenced():
            second = proactive.outcome("ignored", root=self.root, now=minutes_after(11))
        self.assertTrue(second["muted"], "第二次就到 2 了")
        self.assertEqual(2, second["mute_after"])
        self.assertEqual(same(minutes_after(11 + 12 * 60)), second["muted_until"])
        self.assertEqual({"mute_after": 2, "mute_hours": 12},
                         helpers.read_json_file(file_of(self.root))["config"],
                         "人改过的 config 要原样留在文件里")

    def test_mute_hours_is_also_human_editable(self) -> None:
        store.write_json(file_of(self.root),
                         {"version": 1, "config": {"mute_after": 1, "mute_hours": 1},
                          "streak": 0, "muted_until": None, "last_sent_at": None,
                          "history": []})
        proactive.sent(root=self.root, now=DAY)
        with silenced():
            result = proactive.outcome("ignored", root=self.root, now=DAY)
        self.assertEqual(same(after(1)), result["muted_until"])
        self.assertEqual(60.0, proactive.is_muted(root=self.root, now=DAY)["remaining_min"])

    def test_mute_after_zero_is_clamped_to_one(self) -> None:
        store.write_json(file_of(self.root),
                         {"version": 1, "config": {"mute_after": 0, "mute_hours": 12},
                          "streak": 0, "muted_until": None, "last_sent_at": None,
                          "history": []})
        proactive.sent(root=self.root, now=DAY)
        result = proactive.outcome("replied", root=self.root, now=minutes_after(1))
        self.assertFalse(result["muted"], "有人回她的时候绝不许当场静音")
        self.assertEqual(1, result["mute_after"])

    def test_defaults_are_three_and_twelve(self) -> None:
        self.assertEqual({"mute_after": 3, "mute_hours": 12}, proactive.CONFIG_DEFAULTS)
        self.assertEqual({"mute_after": 3, "mute_hours": 12},
                         proactive.overview(root=self.root, now=DAY)["config"])


class OverviewTest(_Root):
    """overview：只读汇总（config + streak + muted + 最近 5 条 history）。"""

    def test_empty_root_reads_as_factory_default(self) -> None:
        seen = proactive.overview(root=self.root, now=DAY)
        self.assertEqual(proactive.VERSION, seen["version"])
        self.assertEqual(0, seen["streak"])
        self.assertFalse(seen["muted"])
        self.assertEqual(0.0, seen["remaining_min"])
        self.assertIsNone(seen["muted_until"])
        self.assertIsNone(seen["last_sent_at"])
        self.assertFalse(seen["pending"])
        self.assertEqual([], seen["history"])
        self.assertFalse(file_of(self.root).exists(), "overview 是纯读：不许造文件")

    def test_history_keeps_only_the_last_five(self) -> None:
        rows = [{"at": minutes_after(index), "outcome": "replied" if index % 2 else "ignored"}
                for index in range(10)]
        store.write_json(file_of(self.root),
                         {"version": 1, "config": {"mute_after": 3, "mute_hours": 12},
                          "streak": 0, "muted_until": None, "last_sent_at": rows[-1]["at"],
                          "history": rows})
        seen = proactive.overview(root=self.root, now=minutes_after(30))
        self.assertEqual(5, len(seen["history"]))
        self.assertEqual(rows[-5:], seen["history"], "给的是最近 5 条，不是最早 5 条")
        self.assertEqual(rows[-1]["at"], seen["last_sent_at"])

    def test_pending_flag_explains_the_idempotent_call(self) -> None:
        proactive.sent(root=self.root, now=DAY)
        self.assertTrue(proactive.overview(root=self.root, now=DAY)["pending"])
        proactive.outcome("ignored", root=self.root, now=minutes_after(5))
        self.assertFalse(proactive.overview(root=self.root, now=DAY)["pending"])


class BrokenFileTest(_Root):
    """坏文件不炸调用方：读入口一律回默认值。"""

    def test_broken_json_reads_as_factory_default(self) -> None:
        helpers.write_raw(file_of(self.root), "{半截")
        seen = proactive.overview(root=self.root, now=DAY)
        self.assertEqual(0, seen["streak"])
        self.assertEqual([], seen["history"])
        self.assertFalse(proactive.is_muted(root=self.root, now=DAY)["muted"])

    def test_unknown_history_rows_are_skipped(self) -> None:
        helpers.write_raw(file_of(self.root),
                          '{"history": [{"at": "x", "outcome": "??"}, 7]}')
        self.assertEqual([], proactive.overview(root=self.root, now=DAY)["history"])


class ContractTest(unittest.TestCase):
    """文件头要说清边界：只说「有没有人理」，不说嘴的事，也不说日子的事。"""

    def test_docstring_draws_both_boundaries(self) -> None:
        text = proactive.__doc__ or ""
        self.assertIn("不管她说什么", text)
        self.assertIn("不管几点该开口", text)
        self.assertIn("对方主动找她", text)
        self.assertIn("时间到", text)

    def test_module_does_not_import_the_mouth_or_the_calendar(self) -> None:
        source = Path(proactive.__file__).read_text(encoding="utf-8")
        for banned in ("import mouth", "import day", "import engage", "import chat"):
            self.assertNotIn(banned, source, "本模块只记结果，不碰嘴与日子：%s" % banned)


if __name__ == "__main__":
    unittest.main()
