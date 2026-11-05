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

    # —— 1) 存原文 ————————————————————————————————

    def test_one_round_lands_as_one_line_with_both_sides(self):
        written = recent.append(self._row(), root=self.root)
        self.assertEqual({"ts", "who", "said", "reply", "truncated"}, set(written))
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

    def test_cap_defaults_to_the_data_file(self):
        """cap 没给就按 assembly.json 的 recent.cap 锯——数只在数据里。"""
        cap = conversation.load_assembly()["recent"]["cap"]
        written = recent.append(self._row(said="长" * (cap + 5)), root=self.root)
        self.assertEqual(cap, len(written["said"]))
        self.assertTrue(written["truncated"]["said"])

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


if __name__ == "__main__":
    unittest.main()
