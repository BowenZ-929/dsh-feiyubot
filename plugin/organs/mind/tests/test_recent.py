"""最近几轮那份账的验收：存原文、截断留痕、只读最近 N 轮、坏行不炸。

判据（每条都对应交付里的一句话）：
1. **存原文不存摘要**：「谁说的 + 原话」四栏（ts / who / said / reply）一字不动地存下来。
2. **截断留痕**：两侧各按 cap 锯，锯过的在 truncated 里记 true，渲染时带「（截断）」——
   绝不静默锯掉一半还当原话。
3. **只读最近几轮**：盘上留着全部行（append-only），读时只切最后 turns 条，
   omitted = 总行数 − 读到的条数。
4. **notice 模板来自数据**：那句话一个字都不在代码里，模板来自 assembly.json 的
   recent.notice；omitted 为 0 时是空串，不编「还有 0 轮」。
5. **坏行跳过不炸**：不是 JSON、不是对象的那一行当没有过。
6. **追加不重写**：第二轮之后第一行还在（逐字节相同）。
7. **亲历与补记分得开**：append 落 fidelity（默认 live）与 msg_id（可空串）；旧行缺这两栏
   读成 live / 空串（不把旧数据标成补的）；补记的那几轮插在最前面，渲染时两行都带
   「（补记）」，她那句写「（没有记到她那句）」——不替她编一句她没说过的话。

一次都不发真网络：这里只碰临时目录。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.mind import conversation, recent

QUESTION = "你还记得医院那回事吗？"
SENTENCE = "记得，是陪你妈去复查对吧。"
LONG = "长" * 50


class RecentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "mind"
        patcher = mock.patch.dict(os.environ, {"QQAGENT_HOME": str(self.tmp / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _row(self, **change):
        row = {"ts": "2025-01-15T14:05:00+08:00", "who": "主人",
               "said": QUESTION, "reply": SENTENCE}
        row.update(change)
        return row

    def _lines(self) -> list:
        path = recent.path_of(self.root)
        if not path.is_file():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def test_quoted_utterances_reload_without_replacing_round_metadata(self):
        quote={'message_id':'99','who':'430','speaker':'同名(430)','text':'原作者的计划','source':'inbound'}
        original=self._row(who='420',turn_id='a'*32,session_id='qq:group:1',msg_id='t-one',
            memory_recall={'selected':[]},utterances=[{'who':'420','speaker':'同名(420)','text':'我的回应','quote':quote}])
        recent.append(original,root=self.root)
        path=recent.path_of(self.root);before=path.read_bytes()
        row=recent.load(root=self.root,session_id='qq:group:1')['turns'][0]
        for key in ('ts','who','said','reply','turn_id','session_id','msg_id','memory_recall','utterances'):
            self.assertEqual(original[key],row[key],key)
        self.assertIn('引用同名(430)',recent.spoken_text(row))
        self.assertEqual(before,path.read_bytes())

    def test_session_filter_precedes_window_and_keeps_legacy_records_on_disk(self):
        for text, session in (("A旧", "qq:group:1"), ("旧记录", ""),
                              ("私聊", "qq:private:2"), ("A新", "qq:group:1"),
                              ("B最新", "qq:group:3")):
            recent.append(self._row(said=text, session_id=session), root=self.root)
        before = recent.path_of(self.root).read_bytes()
        got = recent.load(root=self.root, turns=1, session_id="qq:group:1")
        self.assertEqual(["A新"], [row["said"] for row in got["turns"]])
        self.assertEqual((2, 1), (got["total"], got["omitted"]))
        self.assertEqual([], recent.load(root=self.root, session_id="")["turns"])
        self.assertEqual(5, recent.load(root=self.root)["total"])
        self.assertEqual(before, recent.path_of(self.root).read_bytes())

    # —— 1) 存原文 ————————————————————————————————

    def test_render_keeps_recording_time_without_rewriting_relative_words(self):
        for extra in ({}, {'mode':'proactive'}, {'fidelity':recent.RECONCILED}, {'ts':''}):
            with self.subTest(extra=extra):
                row = self._row(said='明天上午十点去海洋馆',reply='明天见',**extra)
                original = json.dumps(row,ensure_ascii=False)
                text = recent.render([row])
                self.assertIn('[记录时间 '+(row['ts'] or '未记录')+']',text.splitlines()[0])
                self.assertIn('明天见',text)
                if extra.get('mode') != 'proactive':
                    self.assertIn('明天上午十点去海洋馆',text)
                self.assertEqual(original,json.dumps(row,ensure_ascii=False))

    def test_media_stays_separate_from_speech_and_survives_delivery_receipt(self):
        observation = "[图片观察：不是亲历] " + "画面细节" * 100
        row = recent.append(self._row(media_context=observation, session_id="qq:group:1", msg_id="m1"),
                            root=self.root, cap=10)
        recent.confirm_reply(row, "实际发送的答复", root=self.root)
        got = recent.load(root=self.root, session_id="qq:group:1")["turns"][0]
        self.assertEqual(observation, got[recent.MEDIA_CONTEXT])
        self.assertEqual("实际发送的答复", got["reply"])
        self.assertNotIn("画面细节", recent.render([got]))

    def test_one_round_lands_as_one_line_with_both_sides(self):
        written = recent.append(self._row(), root=self.root)
        self.assertEqual({"ts", "who", "said", "reply", "truncated", "fidelity", "msg_id"},
                         set(written))
        self.assertEqual(QUESTION, written["said"])
        self.assertEqual(SENTENCE, written["reply"])
        self.assertEqual({"said": False, "reply": False}, written["truncated"])
        self.assertEqual(1, len(self._lines()), "一轮该只落一行")
        self.assertEqual(written, json.loads(self._lines()[0]), "盘上那一行与返回值不一致")

        got = recent.load(root=self.root)
        self.assertEqual({"turns", "omitted", "total"}, set(got))
        self.assertEqual(1, got["total"])
        self.assertEqual(0, got["omitted"])
        self.assertEqual(written, got["turns"][0])

    def test_append_does_not_rewrite_earlier_lines(self):
        recent.append(self._row(said="第一句", reply="第一答"), root=self.root)
        first = self._lines()[0]
        recent.append(self._row(said="第二句", reply="第二答"), root=self.root)
        lines = self._lines()
        self.assertEqual(2, len(lines))
        self.assertEqual(first, lines[0], "追加把上一行重写了")
        self.assertEqual("第二句", json.loads(lines[1])["said"])

    # —— 2) 截断留痕 ————————————————————————————

    def test_both_sides_are_clipped_to_the_cap_and_marked(self):
        written = recent.append(self._row(said=LONG, reply=LONG), root=self.root, cap=10)
        self.assertEqual("长" * 10, written["said"])
        self.assertEqual("长" * 10, written["reply"])
        self.assertEqual({"said": True, "reply": True}, written["truncated"],
                         "截断了却没留痕")
        self.assertEqual(written, json.loads(self._lines()[0]))
        text = recent.render([written])
        self.assertEqual(2, text.count(recent.CUT_MARK), "截断过的一侧渲染时没带痕迹")

    def test_short_sides_are_not_marked(self):
        written = recent.append(self._row(), root=self.root, cap=len(SENTENCE) + 5)
        self.assertEqual(SENTENCE, written["reply"])
        self.assertEqual({"said": False, "reply": False}, written["truncated"])
        self.assertNotIn(recent.CUT_MARK, recent.render([written]))

    def test_default_storage_preserves_long_message_and_late_correction(self):
        text = "前面的情况。" * 80 + "最后改为周日，不是周六。"
        written = recent.append(self._row(said=text), root=self.root)
        self.assertEqual(text, written["said"])
        self.assertFalse(written["truncated"]["said"])
        self.assertEqual(text, recent.load(root=self.root)["turns"][-1]["said"])

    def test_legacy_clipped_row_can_be_claimed_after_full_text_storage_enabled(self):
        text = "已有长消息" * 50
        recent.append(self._row(said=text, msg_id=""), root=self.root, cap=20)
        row = recent.load(root=self.root)["turns"][0]
        claimed = recent.claim([{"ts": row["ts"], "text": text, "id": "legacy-full"}], root=self.root, cap=0)
        self.assertEqual(1, claimed)
        stored = recent.load(root=self.root)["turns"][0]
        self.assertEqual("legacy-full", stored["msg_id"])
        self.assertEqual(text[:20], stored["said"], "只补凭据，不假装旧日志恢复了全文")

    def test_delivery_receipt_preserves_full_sent_reply(self):
        row = recent.append(self._row(reply="尚未发送的草稿", msg_id="receipt-1"), root=self.root)
        sent = "已实际发出的内容。" * 60 + "最后确定周日。"
        recent.confirm_reply(row, sent, root=self.root)
        got = recent.load(root=self.root)["turns"][-1]
        self.assertEqual(sent, got["reply"])
        self.assertFalse(got["truncated"]["reply"])
        self.assertEqual(QUESTION, got["said"])

    def test_cap_defaults_to_the_data_file(self):
        """默认保存全文，显式正 cap 的旧调用仍由相邻测试覆盖。"""
        self.assertEqual(0, conversation.load_assembly()["recent"]["cap"])
        written = recent.append(self._row(said=LONG), root=self.root)
        self.assertEqual(LONG, written["said"])
        self.assertFalse(written["truncated"]["said"])

    # —— 3) 只读最近几轮 ————————————————————————

    def test_load_keeps_only_the_last_turns_and_counts_the_rest(self):
        for index in range(15):
            recent.append(self._row(said="第%d句" % index, reply="第%d答" % index),
                          root=self.root)
        got = recent.load(root=self.root, turns=12)
        self.assertEqual(15, got["total"])
        self.assertEqual(12, len(got["turns"]))
        self.assertEqual(3, got["omitted"])
        self.assertEqual("第3句", got["turns"][0]["said"], "切到的不是最后 12 条")
        self.assertEqual("第14句", got["turns"][-1]["said"])
        self.assertEqual(15, len(self._lines()), "读的时候把盘上的行删了")

    def test_load_with_everything_fits_reports_nothing_omitted(self):
        for index in range(5):
            recent.append(self._row(said="第%d句" % index), root=self.root)
        got = recent.load(root=self.root, turns=12)
        self.assertEqual(5, len(got["turns"]))
        self.assertEqual(0, got["omitted"])
        self.assertEqual(5, got["total"])

    def test_default_turns_and_cap_come_from_the_data_file(self):
        conf = conversation.load_assembly()["recent"]
        for index in range(conf["turns"] + 2):
            recent.append(self._row(said="第%d句" % index), root=self.root)
        got = recent.load(root=self.root)
        self.assertEqual(conf["turns"], len(got["turns"]), "默认轮数不是数据里的那一个")
        self.assertEqual(2, got["omitted"])

    def test_missing_file_is_empty_not_an_error(self):
        got = recent.load(root=self.root, turns=12)
        self.assertEqual({"turns": [], "omitted": 0, "total": 0}, got)

    # —— 4) notice 模板来自数据 ——————————————————

    def test_notice_comes_from_the_assembly_template(self):
        template = conversation.load_assembly()["recent"]["notice"]
        self.assertIn(recent.NOTE_SLOT, template)
        self.assertEqual("更早还有 3 轮没有加载", recent.notice(3, template))
        self.assertEqual(1, template.count(recent.NOTE_SLOT))

    def test_notice_is_empty_when_nothing_is_omitted(self):
        template = conversation.load_assembly()["recent"]["notice"]
        self.assertEqual("", recent.notice(0, template), "omitted 为 0 时不许编出「还有 0 轮」")
        self.assertEqual("", recent.notice(None, template))
        self.assertEqual("", recent.notice(-1, template))

    def test_notice_template_is_not_hardcoded_in_the_module(self):
        """换一份模板（内存里给），输出跟着变——证明那句话来自数据，不在代码里。"""
        self.assertEqual("少了 7 轮", recent.notice(7, "少了 {omitted} 轮"))
        self.assertEqual("", recent.notice(7, ""), "空模板就是一句话都不说")

    # —— 5) 坏行跳过 ————————————————————————————

    def test_bad_lines_are_skipped_without_raising(self):
        path = recent.path_of(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps(self._row(), ensure_ascii=False)
        path.write_text("\n".join([good, "{ 这不是 json", "[]", "", '{"said": "半条"}', good])
                        + "\n", encoding="utf-8")
        got = recent.load(root=self.root, turns=12)
        self.assertEqual(3, got["total"], "坏行（不是 JSON / 不是对象）被当成一条对话数了进去")
        self.assertEqual([QUESTION, "半条", QUESTION], [row["said"] for row in got["turns"]])
        self.assertEqual("", got["turns"][1]["reply"], "缺栏的记录也没炸，只是那一栏是空")

    # —— 渲染 ————————————————————————————————

    def test_render_is_who_plus_the_original_text(self):
        rows = [self._row(), self._row(who="主人", said="第二句", reply="")]
        text = recent.render(rows)
        self.assertIn("主人：%s" % QUESTION, text, "人的那一侧缺「谁说的 + 原文」")
        self.assertIn("%s：%s" % (recent.HER, SENTENCE), text, "她那一侧缺原文")
        self.assertIn(recent.SILENT, text, "没说出口那一轮渲染成了空白")
        self.assertEqual(4, len(text.splitlines()))

    def test_path_of_honours_the_root_argument(self):
        self.assertEqual(self.root / recent.FILE_NAME, recent.path_of(self.root))
        self.assertEqual(recent.FILE_NAME, recent.path_of(self.tmp / "x").name)

    # —— 7) 亲历 / 补记 ————————————————————————————

    def test_append_lands_fidelity_and_msg_id(self):
        written = recent.append(self._row(msg_id="t-abc123"), root=self.root)
        self.assertEqual(recent.LIVE, written["fidelity"], "当场落盘的那一轮该是 live")
        self.assertEqual("t-abc123", written["msg_id"], "与脑里那条对齐的 id 没落下来")
        self.assertEqual(written, json.loads(self._lines()[-1]))

    def test_append_defaults_to_live_and_an_empty_msg_id(self):
        written = recent.append(self._row(), root=self.root)
        self.assertEqual(recent.LIVE, written["fidelity"], "没给 fidelity 时不该默认成补记")
        self.assertEqual("", written["msg_id"], "没给 msg_id 时该是空串，不是编一个")

    def test_an_unknown_fidelity_is_not_taken_as_reconciled(self):
        written = recent.append(self._row(fidelity="archived"), root=self.root)
        self.assertEqual(recent.LIVE, written["fidelity"],
                         "认不出来的 fidelity 被当成了补记（等于凭空给她加一句声明）")

    def test_old_rows_without_the_two_columns_read_as_live(self):
        """旧文件里那一行没有 fidelity / msg_id：按 live / 空串读，不许标成补的。"""
        path = recent.path_of(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._row(), ensure_ascii=False) + "\n", encoding="utf-8")
        got = recent.load(root=self.root, turns=12)["turns"][0]
        self.assertEqual(recent.LIVE, got["fidelity"], "旧行被标成补记了")
        self.assertEqual("", got["msg_id"])
        text = recent.render([got])
        self.assertNotIn(recent.BACKFILL_MARK, text, "旧行渲染时带了「（补记）」")
        self.assertNotIn(recent.MISSING_REPLY, text, "旧行的她那句被换成了「没有记到」")

    def test_backfilled_row_lands_where_its_ts_belongs_and_is_marked(self):
        recent.append(self._row(ts="2025-01-15T14:09:00+08:00", said="亲历那句",
                                reply="亲历那答"), root=self.root)
        written = recent.insert_reconciled(
            [{"ts": "2025-01-15T14:01:00+08:00", "who": "主人", "said": "补回来的那句",
              "reply": "", "msg_id": "t-back1"}], root=self.root)
        self.assertEqual(1, len(written))
        self.assertEqual(recent.RECONCILED, written[0]["fidelity"])
        self.assertEqual("t-back1", written[0]["msg_id"])

        got = recent.load(root=self.root, turns=12)
        self.assertEqual(2, got["total"])
        self.assertEqual("补回来的那句", got["turns"][0]["said"],
                         "补记没插到 ts 该在的位置（它比亲历那一轮早）")
        self.assertEqual("", got["turns"][0]["reply"], "补记的轮被她编了一句她说的话")
        self.assertEqual(1, recent.reconciled(got["turns"]), "补记条数数错了")

        lines = recent.render(got["turns"]).splitlines()
        self.assertEqual(4, len(lines))
        self.assertEqual("- [记录时间 2025-01-15T14:01:00+08:00] %s主人：补回来的那句" % recent.BACKFILL_MARK, lines[0])
        self.assertEqual("- %s%s：%s" % (recent.BACKFILL_MARK, recent.HER, recent.MISSING_REPLY),
                         lines[1])
        self.assertNotIn(recent.BACKFILL_MARK, lines[2], "亲历那一轮被标成了补记")
        self.assertNotIn(recent.BACKFILL_MARK, lines[3], "亲历那一轮被标成了补记")
        self.assertNotIn(recent.SILENT, lines[1],
                         "补记的轮说成「没说出口」：那是编的，她可能说过，只是没记到")

    # —— 回填的位置：按 ts 并进整份账，不是一律插最前面 ————

    def _old_rounds(self, stamps) -> None:
        """按给定时刻先落几轮亲历的（ts 递增）。"""
        for index, stamp in enumerate(stamps):
            recent.append(self._row(ts=stamp, said="第%d句" % index,
                                    reply="第%d答" % index), root=self.root)

    def test_a_missed_latest_round_is_appended_not_jumped_to_the_front(self):
        """缺的是最新那一轮：补在末尾。塞到最前面就把「刚才」写成了「最早」。"""
        self._old_rounds(["2025-01-15T13:00:00+08:00", "2025-01-15T13:20:00+08:00",
                          "2025-01-15T13:40:00+08:00"])
        recent.insert_reconciled(
            [{"ts": "2025-01-15T14:00:00+08:00", "who": "主人", "said": "第3句",
              "reply": "", "msg_id": "t-3"}], root=self.root)
        got = recent.load(root=self.root, turns=12)["turns"]
        self.assertEqual(["13:00", "13:20", "13:40", "14:00"],
                         [row["ts"][11:16] for row in got], "缺的最新那一轮没补在末尾")
        self.assertEqual("第3句", got[-1]["said"])
        self.assertEqual(recent.RECONCILED, got[-1]["fidelity"])
        lines = recent.render(got).splitlines()
        self.assertEqual(8, len(lines))
        self.assertIn(recent.BACKFILL_MARK, lines[-2], "补记没出现在提示词最后（说明它被挪到最前了）")
        self.assertIn(recent.BACKFILL_MARK, lines[-1])
        self.assertNotIn(recent.BACKFILL_MARK, "\n".join(lines[:-2]), "亲历那几轮被标成了补记")

    def test_a_missed_middle_round_lands_in_the_middle(self):
        """缺的是中间那一轮：补在中间，前后两行的相对顺序不变。"""
        self._old_rounds(["2025-01-15T13:00:00+08:00", "2025-01-15T13:20:00+08:00",
                          "2025-01-15T13:40:00+08:00"])
        recent.insert_reconciled(
            [{"ts": "2025-01-15T13:30:00+08:00", "who": "主人", "said": "中间那句",
              "reply": "", "msg_id": "t-mid"}], root=self.root)
        got = recent.load(root=self.root, turns=12)["turns"]
        self.assertEqual(["13:00", "13:20", "13:30", "13:40"],
                         [row["ts"][11:16] for row in got])
        self.assertEqual(["第0句", "第1句", "中间那句", "第2句"],
                         [row["said"] for row in got], "补记没落在它 ts 该在的位置")
        lines = recent.render(got).splitlines()
        marked = [index for index, line in enumerate(lines) if recent.BACKFILL_MARK in line]
        self.assertEqual([4, 5], marked, "（补记）没只落在中间那一轮上")

    def test_two_backfills_one_at_each_end_keep_the_ledger_monotonic(self):
        """首尾各补一条：整份账按 ts 单调不减（同 ts 允许相邻），标记只在补的那两轮。"""
        self._old_rounds(["2025-01-15T13:00:00+08:00", "2025-01-15T13:20:00+08:00",
                          "2025-01-15T13:40:00+08:00"])
        written = recent.insert_reconciled(
            [{"ts": "2025-01-15T14:10:00+08:00", "who": "主人", "said": "最新那句",
              "reply": "", "msg_id": "t-last"},
             {"ts": "2025-01-15T12:50:00+08:00", "who": "主人", "said": "最早那句",
              "reply": "", "msg_id": "t-first"}], root=self.root)
        self.assertEqual(2, len(written))
        got = recent.load(root=self.root, turns=12)["turns"]
        self.assertEqual(5, len(got))
        stamps = [row["ts"] for row in got]
        self.assertEqual(sorted(stamps), stamps, "回填后这份账的 ts 不是单调不减")
        self.assertEqual(["最早那句", "第0句", "第1句", "第2句", "最新那句"],
                         [row["said"] for row in got])
        self.assertEqual(2, recent.reconciled(got))
        lines = recent.render(got).splitlines()
        marked = [index for index, line in enumerate(lines) if recent.BACKFILL_MARK in line]
        self.assertEqual([0, 1, 8, 9], marked, "（补记）跑到别的轮上去了")

    # —— 旧行认领（升级迁移）：补凭据，不补声明 ————

    def _legacy_line(self, **change) -> str:
        """升级前那一行的形状：没有 fidelity / msg_id 两栏。"""
        row = {"ts": "2025-01-15T14:05:00+08:00", "who": "主人",
               "said": QUESTION, "reply": SENTENCE,
               "truncated": {"said": False, "reply": False}}
        row.update(change)
        return json.dumps(row, ensure_ascii=False)

    def _write(self, lines) -> None:
        path = recent.path_of(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def test_stamps_covers_every_row_not_just_the_loaded_ones(self):
        self._write([self._legacy_line(ts="2025-01-15T14:0%d:00+08:00" % index)
                     for index in range(5)])
        self.assertEqual(["2025-01-15T14:0%d:00+08:00" % index for index in range(5)],
                         recent.stamps(root=self.root), "stamps 漏了没读进提示词的那几轮")

    def test_claim_writes_the_id_back_and_keeps_the_row_live(self):
        self._write([self._legacy_line()])
        claimed = recent.claim([{"ts": "2025-01-15T14:05:00+08:00", "said": QUESTION,
                                 "msg_id": "t-old"}], root=self.root)
        self.assertEqual(1, claimed)
        got = recent.load(root=self.root, turns=12)["turns"][0]
        self.assertEqual("t-old", got["msg_id"], "旧行没认领回 id")
        self.assertEqual(recent.LIVE, got["fidelity"], "认领把旧行改成了补记")
        self.assertNotIn(recent.BACKFILL_MARK, recent.render([got]))

    def test_claim_leaves_rows_that_match_nothing_alone(self):
        self._write([self._legacy_line()])
        self.assertEqual(0, recent.claim([{"ts": "2025-01-15T14:05:00+08:00",
                                          "said": "另外一句", "msg_id": "t-x"}],
                                        root=self.root))
        got = recent.load(root=self.root, turns=12)["turns"][0]
        self.assertEqual("", got["msg_id"], "对不上的行也被改写了")

    def test_claim_is_idempotent(self):
        self._write([self._legacy_line()])
        entries = [{"ts": "2025-01-15T14:05:00+08:00", "said": QUESTION, "msg_id": "t-old"}]
        self.assertEqual(1, recent.claim(entries, root=self.root))
        once = recent.path_of(self.root).read_text(encoding="utf-8")
        self.assertEqual(0, recent.claim(entries, root=self.root), "第二次又认领了一遍")
        self.assertEqual(once, recent.path_of(self.root).read_text(encoding="utf-8"),
                         "第二次认领改写了文件（不幂等）")

    def test_claim_never_touches_a_row_that_already_has_an_id(self):
        recent.append(self._row(msg_id="t-own"), root=self.root)
        once = recent.path_of(self.root).read_text(encoding="utf-8")
        self.assertEqual(0, recent.claim([{"ts": "2025-01-15T14:05:00+08:00", "said": QUESTION,
                                          "msg_id": "t-other"}], root=self.root))
        self.assertEqual(once, recent.path_of(self.root).read_text(encoding="utf-8"),
                         "已有 id 的行被改写了")

    def test_claim_matches_the_text_after_the_cap(self):
        """脑里存全文、账上存截断版：按 cap 截断后比，长句也认得出（不然又是一次误标）。"""
        recent.append({"ts": "2025-01-15T14:05:00+08:00", "who": "主人", "said": LONG,
                       "reply": ""}, root=self.root, cap=10)
        first = json.loads(recent.path_of(self.root).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual("长" * 10, first["said"])
        self.assertEqual(1, recent.claim([{"ts": "2025-01-15T14:05:00+08:00", "said": LONG,
                                           "msg_id": "t-long"}], root=self.root, cap=10))
        got = recent.load(root=self.root, turns=12)["turns"][0]
        self.assertEqual("t-long", got["msg_id"], "长到被截断的旧行没认出来")
        self.assertEqual(recent.LIVE, got["fidelity"])

    def test_old_rows_without_a_ts_follow_their_neighbour(self):
        """旧行没有 ts：跟在上一条有 ts 的行后面（不是一律排最前，也不是跑到末尾）。"""
        path = recent.path_of(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"ts": "2025-01-15T13:00:00+08:00", "who": "人", "said": "有 ts 那句", "reply": "答"},
            {"who": "人", "said": "旧行没有 ts", "reply": "答"},
            {"ts": "2025-01-15T13:20:00+08:00", "who": "人", "said": "更晚那句", "reply": "答"},
        ]
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")
        recent.insert_reconciled(
            [{"ts": "2025-01-15T13:10:00+08:00", "who": "人", "said": "补在中间那句",
              "reply": "", "msg_id": "t-mid"}], root=self.root)
        got = recent.load(root=self.root, turns=12)["turns"]
        self.assertEqual(["有 ts 那句", "旧行没有 ts", "补在中间那句", "更晚那句"],
                         [row["said"] for row in got],
                         "没有 ts 的旧行没跟着它上一条走（位置乱了）")


if __name__ == "__main__":
    unittest.main()
