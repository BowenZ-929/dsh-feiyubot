"""评测脚本自己的验收：漂移识别、全同识别、四条判定各触发一次、假模型冒烟。

判据（每条都对应交付里的一句话）：
1. **A 轨的取值域由数据定死**：称呼/自称/自称AI/口癖/句长档都从 data/questions.json 的词表与档界来，
   代码里没有第二份词表。
2. **漂移识别**：10 次里任一 A 特征出现过不同取值 = 该题漂移，且报出是哪几个特征。
3. **全同识别**：10 次逐字相同 = 该题全同（措辞），三特征全同才算「疑似复制」那一格。
4. **四条判定各触发一次**（顺序写死）：1 无效（含「完全相同 → 无效轮」）／2 换人／3 死板／4 通过，
   并且 A 漂移优先于死板（顺序不是摆设）。
5. **假模型冒烟**：--dry（哈希确定性）／--template（固定模板）／--vary（轮次计数）都能跑完，
   表里有行、判定字段齐全、耗时短；template 那格必须判「无效」，vary 那格不许是无效或死板。
6. **不联网、不需要 key**：全部假 transport 注入（配置与 key 都是假的，见 bench.FAKE_*）。
7. **C 轨（口癖命中率）只读数、不进判定**：命中率全用 organs.mind.usage.match 算（测试里不重写
   匹配规则）；表里有 C 列、汇总里有「不进判定」那一句；C 拉满也不改四档判定；假模型一律 0.00。
8. **前缀字数现读 assembly.json**：同一份逻辑喂 5/9 就出 5/9，缺件才退回文档默认值并记下缺了哪项。
"""
from __future__ import annotations

import contextlib
import io
import json
import time
import unittest

from organs.mind.bench import consistency as bench

DATA = bench.load_data()
RULES = DATA["rules"]
QUESTIONS = DATA["questions"]


def sample(reply: str, tier: str = "平常") -> dict:
    """一条合成读数：A 轨从回复原文提，B 轨三特征照返回体的形状拼。"""
    return {"a": bench.a_features(reply, RULES),
            "b": bench.b_features({"reply": reply, "heart": {"mood": {"tier": tier}}})}


def series(replies, tiers=None) -> list:
    """一题的十次读数：回复与情绪档位逐条给。"""
    tiers = tiers or ["平常"] * len(replies)
    return [sample(reply, tier) for reply, tier in zip(replies, tiers)]


def cell(per_question) -> dict:
    """一格汇总：每题的 question_stats → cell_stats（判定在这里发生）。"""
    return bench.cell_stats({question: bench.question_stats(samples)
                             for question, samples in per_question.items()})


class AFeatureTest(unittest.TestCase):
    """A 轨：取值域全在数据里，提取是确定性的。"""

    def test_words_and_buckets_come_from_the_data(self):
        text = "主人呀，小的记住了。作为一个AI我不好说。"
        got = bench.a_features(text, RULES)
        self.assertIn("主人", got["称呼口径"])
        self.assertIn("小的", got["自称"])
        self.assertEqual("是", got["自称AI"])
        self.assertEqual("是", got["口癖"])
        self.assertIn(got["句长量级"], RULES["length"]["labels"])
        for word in got["称呼口径"].split("、") + got["自称"].split("、"):
            if word != "无":
                self.assertTrue(word in RULES["address"]["words"] or word in RULES["self"]["words"],
                                "冒出一个不在数据词表里的词：%s" % word)

    def test_bucket_edges_are_read_from_the_data(self):
        edges = RULES["length"]["edges"]
        labels = RULES["length"]["labels"]
        self.assertEqual(labels[0], bench._bucket(edges[0], RULES["length"]))
        self.assertEqual(labels[1], bench._bucket(edges[0] + 1, RULES["length"]))
        self.assertEqual(labels[-1], bench._bucket(edges[-1] + 1000, RULES["length"]))

    def test_clean_answer_has_no_hits_and_no_ai(self):
        got = bench.a_features("今天的云很好看。", RULES)
        self.assertEqual({"称呼口径": "无", "自称": "无", "自称AI": "否",
                          "口癖": "否", "句长量级": RULES["length"]["labels"][0]}, got)


class DriftAndSameTest(unittest.TestCase):
    """两条轨道各自的识别：漂移（A）与全同（B）。"""

    def test_drift_is_any_a_feature_taking_two_values(self):
        replies = ["宝宝早。"] * 9 + ["主人早。"]     # 同样四个字，只换称呼
        row = bench.question_stats(series(replies))
        self.assertTrue(row["drift"])
        self.assertEqual(["称呼口径"], row["drift_features"])
        self.assertFalse(row["same_text"], "称呼换了，措辞自然也换了——但那是 B 轨的事，不影响漂移判定")

    def test_no_drift_when_a_features_are_stable(self):
        replies = ["嗯，今天挺好的。第 %d 遍。" % run for run in range(10)]
        row = bench.question_stats(series(replies))
        self.assertFalse(row["drift"])
        self.assertFalse(row["same_text"], "措辞每轮都不同，不该判全同")
        self.assertFalse(row["same_all"])

    def test_identical_ten_times_is_all_same(self):
        row = bench.question_stats(series(["我在的，你说吧。"] * 10))
        self.assertTrue(row["same_text"])
        self.assertTrue(row["same_all"])
        self.assertFalse(row["drift"])

    def test_empty_series_is_an_error_not_a_zero(self):
        with self.assertRaises(ValueError):
            bench.question_stats([])


class JudgeTest(unittest.TestCase):
    """四条判定，顺序写死；每一条都能触发。"""

    def test_rule1_completely_identical_is_invalid(self):
        got = cell({"q1": series(["同一句。"] * 10), "q2": series(["同一句。"] * 10)})
        self.assertEqual(1, got["rule"])
        self.assertEqual("无效", got["verdict"])
        self.assertEqual(1.0, got["copy_rate"])
        self.assertIn("疑似复制", got["why"])

    def test_rule1_fires_at_the_threshold(self):
        per_question = {"q%d" % index: series(["同一句。"] * 10) for index in range(9)}
        per_question["q9"] = series(["第 %d 遍。" % run for run in range(10)])
        got = cell(per_question)
        self.assertEqual(0.9, round(got["copy_rate"], 2))
        self.assertEqual((1, "无效"), (got["rule"], got["verdict"]))

    def test_rule2_a_drift_is_someone_else(self):
        replies = ["你早呀。"] * 5 + ["主人早呀。"] * 5
        got = cell({"q1": series(replies), "q2": series(["第 %d 遍。" % run for run in range(10)])})
        self.assertEqual((2, "换人"), (got["rule"], got["verdict"]))
        self.assertEqual(["称呼口径"], got["drift_features"])
        self.assertIn("不该变的变了", got["why"])

    def test_rule2_wins_over_rule3(self):
        """顺序不是摆设：A 漂移 + 措辞全同，判的是「换人」，不是「死板」。"""
        got = bench.judge({"copy_rate": 0.0, "a_drift_rate": 0.5,
                           "b_same_rate": 1.0, "drift_features": ["自称"]})
        self.assertEqual((2, "换人"), (got["rule"], got["verdict"]))

    def test_rule3_same_words_but_heart_moves_is_rigid(self):
        """措辞十次一字不改、只有情绪档位在动 = 死板（且没到「三特征全同」那一格）。"""
        tiers = ["平常", "偏热", "平常", "偏冷"] * 2 + ["平常", "偏热"]
        replies = ["我在的，你说吧。"] * 10
        got = cell({"q1": series(replies, tiers)})
        self.assertEqual(1.0, got["b_same_rate"])
        self.assertLess(got["copy_rate"], bench.COPY_INVALID)
        self.assertEqual((3, "死板"), (got["rule"], got["verdict"]))
        self.assertIn("模板化", got["why"])

    def test_rule4_pass(self):
        per_question = {"q%d" % index: series(["嗯，第 %d 遍，你继续说。" % run
                                               for run in range(10)]) for index in range(3)}
        got = cell(per_question)
        self.assertEqual((4, "通过"), (got["rule"], got["verdict"]))
        self.assertEqual(0.0, got["a_drift_rate"])
        self.assertEqual(0.0, got["b_same_rate"])

    def test_every_verdict_is_reachable(self):
        seen = {
            bench.judge({"copy_rate": 1.0, "a_drift_rate": 0.0, "b_same_rate": 1.0})["verdict"],
            bench.judge({"copy_rate": 0.0, "a_drift_rate": 0.2, "b_same_rate": 0.0})["verdict"],
            bench.judge({"copy_rate": 0.0, "a_drift_rate": 0.0, "b_same_rate": 1.0})["verdict"],
            bench.judge({"copy_rate": 0.0, "a_drift_rate": 0.0, "b_same_rate": 0.2})["verdict"],
        }
        self.assertEqual({"无效", "换人", "死板", "通过"}, seen)


class DrySmokeTest(unittest.TestCase):
    """假模型冒烟：不联网、不要 key、跑得完、判定字段齐全。"""

    def _run(self, transport, *, runs: int = 2, questions=None, config="empty", mode="same"):
        return bench.run_cell(mode, config, runs=runs,
                              questions=questions or QUESTIONS[:2], rules=RULES,
                              transport=transport, config_data=bench.FAKE_CONFIG,
                              env_data=bench.FAKE_ENV)

    def test_dry_reports_a_full_table(self):
        started = time.time()
        report = bench.run_bench(runs=2, transport=bench.hash_transport(),
                                 questions=QUESTIONS[:2], data=DATA,
                                 config_data=bench.FAKE_CONFIG, env_data=bench.FAKE_ENV)
        elapsed = time.time() - started
        self.assertLess(elapsed, 30.0, "假模型冒烟跑了 %.1fs，太久了" % elapsed)
        self.assertEqual(len(bench.CONFIGS) * len(bench.MODES), len(report["cells"]))
        for row in report["cells"]:
            stats = row["stats"]
            self.assertIn(stats["verdict"], ("无效", "换人", "死板", "通过"))
            self.assertIn(stats["rule"], (1, 2, 3, 4))
            self.assertTrue(stats["why"])
            self.assertGreaterEqual(stats["a_drift_rate"], 0.0)
            self.assertLessEqual(stats["b_same_rate"], 1.0)
        table = bench.render_table(report)
        self.assertEqual(len(bench.CONFIGS) + 2, len(table.strip().splitlines()),
                         "表头 + 分隔行 + 每个配置一行")
        self.assertTrue(any(row["stats"]["verdict"] == "无效" for row in report["cells"]),
                        "同序 + 确定性假模型，应当自己判出无效轮")
        summary = bench.render_summary(report)
        self.assertIn("只报同序会高估", summary)
        self.assertIn("空基线", summary)
        self.assertIn("净增益", summary)
        self.assertIn("最近 N 轮：每一轮独立的 recent_root", bench.render_meta(report))

    def test_template_answers_are_judged_invalid(self):
        row = self._run(bench.template_transport())
        self.assertEqual((1, "无效"), (row["stats"]["rule"], row["stats"]["verdict"]))
        self.assertEqual(1.0, row["stats"]["b_same_rate"])

    def test_vary_answers_pass(self):
        row = self._run(bench.vary_transport())
        self.assertEqual((4, "通过"), (row["stats"]["rule"], row["stats"]["verdict"]))
        self.assertEqual(0.0, row["stats"]["b_same_rate"])
        self.assertEqual(0.0, row["stats"]["a_drift_rate"])

    def test_no_key_prints_the_message_and_exits_nonzero(self):
        """真模型缺 key：把人话原样打出来、退出码非 0——不装成跑通了（这里不联网，直接替掉 preflight）。"""
        from unittest import mock
        from organs.mind import llm
        message = "还没配模型 key，去 /tmp/.env 写 QQAGENT_LLM_KEY=<你的 key>"
        stderr = io.StringIO()
        with mock.patch.object(bench, "preflight", side_effect=llm.LLMError("no-key", message)):
            with contextlib.redirect_stderr(stderr):
                code = bench.main(["--runs", "2", "--limit", "1"])
        self.assertEqual(2, code)
        self.assertEqual(message, stderr.getvalue().strip())

    def test_missing_assembly_param_falls_back_to_the_isolated_home(self):
        """万一 answer() 不认 assembly：退回临时实盘那份，仓库里的真数据一个字节都不读。"""
        from unittest import mock
        from organs.mind import conversation
        assembly = bench.assembly_for("empty")
        self.assertNotIn("soul", assembly["order"])
        with bench.isolated_environment():
            with mock.patch.object(bench, "answer_params", return_value=("text", "store", "brain")):
                self.assertEqual({}, bench._assembled_kwargs(assembly))
            written = conversation.load_assembly()
        self.assertEqual(assembly["order"], written["order"])
        self.assertNotIn("memory", written["order"])

    def test_repo_files_are_untouched_by_a_run(self):
        """装配说明与嘴的账本都落在临时目录：跑完仓库里的实盘一个字节都不动。"""
        from organs import paths
        live = paths.organ_dir("mind") / "assembly.json"
        before = live.exists()
        self._run(bench.vary_transport(), questions=QUESTIONS[:1])
        self.assertEqual(before, live.exists(), "评测把装配说明写进了实盘")


#: C 轨用的合成台词库（不是家·仓库那份）：前缀口径与 usage.match 的判据一致即可。
ANCHORS = [{"say": "这个啊……以后再说吧。", "scene": "不想聊"},
           {"say": "你忙你的，我在这儿。", "scene": "你要走"}]
PREFIX_CHARS = 3
OPENING_CHARS = 6
PLAIN = "今天的云很好看，第 %d 遍。"


def with_c(reply: str, *, anchors=ANCHORS, prefix_chars=PREFIX_CHARS,
           opening_chars=OPENING_CHARS) -> dict:
    """一条合成读数：A/B 照旧，C 走 bench.c_features（匹配规则只在 usage.match 里）。"""
    got = sample(reply)
    got["c"] = bench.c_features(reply, anchors, prefix_chars=prefix_chars,
                                opening_chars=opening_chars)
    return got


def c_cell(replies) -> dict:
    return cell({"q1": [with_c(reply) for reply in replies]})


class CTrackTest(unittest.TestCase):
    """C 轨：口癖命中率 = 该格所有回答里命中的比例，用 usage.match 算，不在这里重写匹配规则。"""

    def setUp(self):
        if bench.usage_module() is None:
            self.skipTest("organs.mind.usage 还没落地：C 轨按「未落地」报，不补模块也不造假命中")

    def test_all_answers_open_with_an_anchor(self):
        got = c_cell([ANCHORS[0]["say"] + "第 %d 遍。" % run for run in range(10)])
        self.assertEqual(1.0, got["c_rate"])
        self.assertEqual((10, 10), (got["c_hits"], got["c_answers"]))
        self.assertEqual("不想聊", bench.c_features(ANCHORS[0]["say"] + "好", ANCHORS,
                                                    prefix_chars=PREFIX_CHARS,
                                                    opening_chars=OPENING_CHARS)["scene"])

    def test_no_answer_opens_with_an_anchor(self):
        got = c_cell([PLAIN % run for run in range(10)])
        self.assertEqual(0.0, got["c_rate"])
        self.assertEqual(0, got["c_hits"])

    def test_half_of_the_answers_hit(self):
        replies = [ANCHORS[1]["say"] + "第 %d 遍。" % run if run % 2 == 0 else PLAIN % run
                   for run in range(10)]
        got = c_cell(replies)
        self.assertEqual(0.5, got["c_rate"])
        self.assertEqual(5, got["c_hits"])

    def test_rate_is_over_every_answer_not_per_question(self):
        """口癖命中率的分母是「这一格所有回答」：两题各 10 次、其中 5 次命中 → 0.25。"""
        rows = {
            "q1": [with_c(ANCHORS[0]["say"] + "第 %d 遍。" % run) if run < 5 else with_c(PLAIN % run)
                   for run in range(10)],
            "q2": [with_c(PLAIN % run) for run in range(10)],
        }
        got = bench.cell_stats({question: bench.question_stats(samples)
                                for question, samples in rows.items()})
        self.assertEqual(0.25, got["c_rate"])
        self.assertEqual((5, 20), (got["c_hits"], got["c_answers"]))

    def test_c_rate_does_not_touch_the_four_rules(self):
        """C 拉满也不改判定：四条还是那四条，C 只是读数。"""
        got = c_cell([ANCHORS[0]["say"] + "第 %d 遍。" % run for run in range(10)])
        self.assertEqual(1.0, got["c_rate"])
        self.assertEqual(4, got["rule"])
        self.assertEqual("通过", got["verdict"])


class CSurfaceTest(unittest.TestCase):
    """表、汇总、JSON 里 C 都要露面，并且明说它不进判定。"""

    def _report(self, c_rate=0.5):
        cells = []
        for mode in bench.MODES:
            for config in bench.CONFIGS:
                cells.append({"config": config, "mode": mode, "runs": 3, "questions": ["q1"],
                              "dropped": 0,
                              "anchors": {"file": "合成", "total": 2, "available": True},
                              "guard": dict(bench.GUARD_DEFAULTS, from_assembly=True, missing=[]),
                              "stats": {"a_drift_rate": 0.0, "b_same_rate": 0.0, "copy_rate": 0.0,
                                        "drift_features": [], "c_rate": c_rate, "c_hits": 3,
                                        "c_answers": 6, "verdict": "通过", "rule": 4,
                                        "why": "合成数据"},
                              "per_question": {}})
        meta = {"model_label": "合成", "questions": ["q1"], "question_count": 1, "runs": 3,
                "configs": list(bench.CONFIGS), "modes": list(bench.MODES),
                "seed_same": 1, "seed_shuffled": 2, "now": bench.NOW, "who": bench.WHO,
                "copy_invalid": bench.COPY_INVALID, "dead_same": bench.DEAD_SAME,
                "answer_params": [], "recent_note": "", "assembly_note": "",
                "anchors": {"file": "合成", "total": 2, "file_exists": True},
                "usage_ready": True, "prefix_chars": 3, "opening_chars": 6,
                "guard_note": "现读", "anchors_note": "合成"}
        report = {"meta": meta, "cells": cells}
        report["summary"] = bench.summary_stats(report)
        return report

    def test_table_has_the_c_column_next_to_a_and_b(self):
        table = bench.render_table(self._report())
        self.assertIn("C 命中 0.50", table)
        self.assertIn("A 漂移 0.00 · B 全同 0.00 · C 命中 0.50", table)

    def test_summary_reports_c_per_cell_and_says_it_is_not_judged(self):
        summary = bench.render_summary(self._report())
        self.assertIn("C 口癖命中率", summary)
        self.assertIn("不进判定", summary)
        self.assertIn("1→2→3→4", summary)
        for config in bench.CONFIGS:
            self.assertIn("%s 0.50" % config, summary)

    def test_c_is_rendered_even_when_usage_is_not_there(self):
        table = bench.render_table(self._report(c_rate=None))
        self.assertIn("C 命中 —", table)

    def test_guard_numbers_are_read_from_the_assembly_not_from_code(self):
        got = bench.guard_settings({"anchors": {"guard": {"prefix_chars": 5, "opening_chars": 9}}})
        self.assertEqual((5, 9), (got["prefix_chars"], got["opening_chars"]))
        self.assertTrue(got["from_assembly"])

    def test_missing_guard_falls_back_and_says_which_keys_were_missing(self):
        got = bench.guard_settings({"anchors": {}})
        self.assertEqual({"prefix_chars": 3, "opening_chars": 6},
                         {key: got[key] for key in bench.GUARD_DEFAULTS})
        self.assertEqual(["prefix_chars", "opening_chars"], got["missing"])
        self.assertFalse(got["from_assembly"])


class CJsonTest(unittest.TestCase):
    """--json 的原始统计里：每格有 c_rate，每次回答有 hit/scene/opening。"""

    def test_every_cell_and_every_answer_carries_c(self):
        report = bench.run_bench(runs=2, transport=bench.hash_transport(),
                                 questions=QUESTIONS[:1], data=DATA,
                                 config_data=bench.FAKE_CONFIG, env_data=bench.FAKE_ENV)
        json.dumps(report, ensure_ascii=False)          # 能序列化才谈得上 --json
        ready = bench.usage_module() is not None
        for row in report["cells"]:
            stats = row["stats"]
            self.assertIn("c_rate", stats)
            self.assertEqual(stats["c_answers"], len(row["per_question"][QUESTIONS[0]]["samples"]))
            for entry in row["per_question"].values():
                for reading in entry["samples"]:
                    self.assertEqual({"hit", "scene", "opening"}, set(reading["c"]))
            if ready:
                self.assertEqual(0.0, stats["c_rate"], "假模型不用台词：C 必须是 0.00（不造假命中）")
            else:
                self.assertIsNone(stats["c_rate"], "没落地就报 None，不拿 0 冒充没命中")

    def test_anchors_are_read_once_for_the_whole_run(self):
        report = bench.run_bench(runs=1, transport=bench.template_transport(),
                                 questions=QUESTIONS[:1], data=DATA,
                                 config_data=bench.FAKE_CONFIG, env_data=bench.FAKE_ENV)
        self.assertEqual(bench.read_anchors()["total"], report["meta"]["anchors"]["total"])


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
