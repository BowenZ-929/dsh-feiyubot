"""评测脚本自己的验收：漂移识别、全同识别、四条判定各触发一次、假模型冒烟。

判据（每条都对应交付里的一句话）：
1. **A 轨按闭集判**：自称/称呼的取值只有「集内」「越界:<词>」两种形状，闭集分别是魂
   identity.self_names 与称呼表 + 数据里那个中性第二人称；集合内换词不漂、跳出集合才漂；
   完全没有自称/称呼记「集内」。自称AI/口癖/句长档照旧（档界与词表都在 data/questions.json，
   代码里没有第二份词表）；句长只在跨档时算漂移。
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
9. **脑的两列**：隔离脑（结构基线，照旧）／实盘脑快照（跑之前整份拷到临时目录，此后每轮从快照
   再拷一份，实盘只读不写）；快照列条数 == 实盘条数；实盘脑为空就如实报「快照列不跑」并照出隔离脑
   那一列；--brain both 出两张带标题的表。实盘未被写用整棵目录的内容哈希验。
10. **基线**：--write-baseline 写得出去、load_baseline 读得回来（缺件／坏 JSON 抛人话），
    compare_baseline 逐格比出「哪一格、哪个数、从 x 到 y」，同值不报、时刻/模型名/note 不参与比。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from organs.brain import BrainStore
from organs.mind.bench import consistency as bench

DATA = bench.load_data()
RULES = DATA["rules"]
QUESTIONS = DATA["questions"]

#: 测试用的闭集：合成一份，不跟着实盘卡变（实盘闭集由 ClosedSetTest 单独验）。
#: 取值域刻意窄（自称只认魂名单里的三个词、称呼只认「你」），这样下面的用例不依赖
#: 魂的自称名单和称呼表现在收了哪些词——那两份会变，用例不该跟着变。
CLOSED = {
    "self": {"words": ["我", "本鱼", "迟迟"],
             "scan": ["人家", "咱家", "咱", "小的", "本人", "我", "本鱼", "迟迟"],
             "source": "合成（自称闭集）", "fallback": False},
    "address": {"words": ["你"],
                "scan": ["您", "主人", "宝宝", "宝贝", "亲爱的", "你"],
                "source": "合成（称呼闭集）", "fallback": False},
}


def sample(reply: str, tier: str = "平常") -> dict:
    """一条合成读数：A 轨从回复原文提（闭集用合成那份），B 轨三特征照返回体的形状拼。"""
    return {"a": bench.a_features(reply, RULES, CLOSED),
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
    """A 轨：取值域全在数据与闭集里，提取是确定性的。"""

    def test_out_of_set_words_are_named(self):
        text = "主人呀，小的记住了。作为一个AI我不好说。"
        got = bench.a_features(text, RULES, CLOSED)
        self.assertEqual("越界:主人", got["称呼口径"])
        self.assertEqual("越界:小的", got["自称"])
        self.assertEqual("是", got["自称AI"])
        self.assertEqual("是", got["口癖"])
        self.assertIn(got["句长量级"], RULES["length"]["labels"])

    def test_in_set_words_are_not_drift_and_out_of_set_ones_are(self):
        """闭集的两种形状：集合内换词还是「集内」，集合外才有「越界:<词>」。"""
        self.assertEqual("集内", bench.a_features("我记住了。", RULES, CLOSED)["自称"])
        self.assertEqual("集内", bench.a_features("本鱼记住了。", RULES, CLOSED)["自称"])
        self.assertEqual("集内", bench.a_features("迟迟记住了。", RULES, CLOSED)["自称"])
        self.assertEqual("越界:人家", bench.a_features("人家记住了。", RULES, CLOSED)["自称"])
        self.assertEqual("集内", bench.a_features("你早呀。", RULES, CLOSED)["称呼口径"])
        self.assertEqual("越界:主人", bench.a_features("主人早呀。", RULES, CLOSED)["称呼口径"])

    def test_only_the_first_out_of_set_word_is_reported(self):
        got = bench.a_features("主人呀，人家记住了。", RULES, CLOSED)
        self.assertEqual("越界:主人", got["称呼口径"])
        self.assertEqual("越界:人家", got["自称"])

    def test_bucket_edges_are_read_from_the_data(self):
        edges = RULES["length"]["edges"]
        labels = RULES["length"]["labels"]
        self.assertEqual(labels[0], bench._bucket(edges[0], RULES["length"]))
        self.assertEqual(labels[1], bench._bucket(edges[0] + 1, RULES["length"]))
        self.assertEqual(labels[-1], bench._bucket(edges[-1] + 1000, RULES["length"]))

    def test_clean_answer_is_in_set_not_out_of_set(self):
        """一个自称/称呼都没有 = 「集内」，不是越界，也不算漂移。"""
        got = bench.a_features("今天的云很好看。", RULES, CLOSED)
        self.assertEqual({"称呼口径": "集内", "自称": "集内", "自称AI": "否",
                          "口癖": "否", "句长量级": RULES["length"]["labels"][0]}, got)


class ClosedSetTest(unittest.TestCase):
    """闭集从哪儿读、读不到怎么退、以及三种漂移判法（集内换词／越界／跨档）。"""

    def test_self_closed_set_is_the_soul_card_not_a_word_list_in_code(self):
        got = bench.read_self_names()
        self.assertFalse(got["fallback"], "仓库里的魂卡应当读得到：%s" % got.get("reason"))
        self.assertIn("identity.self_names", got["source"])
        self.assertTrue(got["words"], "魂的自称名单读成了空的")

    def test_address_closed_set_is_the_mouth_table_plus_the_neutral_second_person(self):
        got = bench.read_address_names(RULES)
        self.assertFalse(got["fallback"], "称呼表应当读得到：%s" % got.get("reason"))
        self.assertIn("address_table", got["source"])
        for word in RULES["address"]["closed_extra"]:
            self.assertIn(word, got["words"], "数据里那个中性第二人称没并进闭集")

    def test_unreadable_closed_sets_fall_back_and_say_so(self):
        with tempfile.TemporaryDirectory() as empty:
            closed = bench.read_closed_sets(RULES, soul_root=empty, mouth_root=empty)
        self.assertTrue(closed["self"]["fallback"])
        self.assertTrue(closed["address"]["fallback"])
        self.assertEqual(RULES["self"]["fallback"], closed["self"]["words"])
        self.assertEqual(RULES["address"]["fallback"], closed["address"]["words"])
        self.assertIn("退回", bench.closed_fallback_note(closed))
        self.assertIn("自称", bench.closed_fallback_note(closed))
        # 退回了 fallback 就判不出越界：扫到的词必然都在这份表里（这是「退回了」的代价，如实报）。
        self.assertEqual("集内", bench.a_features("人家记住了。", RULES, closed)["自称"])

    def test_live_closed_sets_say_they_did_not_fall_back(self):
        note = bench.closed_fallback_note(bench.read_closed_sets(RULES))
        self.assertIn("没有退回 fallback", note)
        self.assertIn("扫词表", note)

    def test_swapping_words_inside_the_set_is_not_drift(self):
        replies = ["我记住了。第 %d 遍。" % run if run % 2 else "本鱼记住了。第 %d 遍。" % run
                   for run in range(10)]
        row = bench.question_stats(series(replies))
        self.assertFalse(row["drift"], "「我」换「本鱼」在闭集内，不该算漂移（%r）" % row["drift_features"])
        self.assertFalse(row["same_text"], "措辞每轮都不同——那是 B 轨的事")

    def test_leaving_the_set_is_drift(self):
        replies = ["我记住了。第 %d 遍。" % run if run % 2 else "人家记住了。第 %d 遍。" % run
                   for run in range(10)]
        row = bench.question_stats(series(replies))
        self.assertTrue(row["drift"])
        self.assertEqual(["自称"], row["drift_features"])
        self.assertEqual({"集内", "越界:人家"}, set(row["a_values"]["自称"]))

    def test_address_leaving_the_set_is_drift_too(self):
        replies = ["你早呀。第 %d 遍。" % run if run % 2 else "主人早呀。第 %d 遍。" % run
                   for run in range(10)]
        row = bench.question_stats(series(replies))
        self.assertEqual(["称呼口径"], row["drift_features"])

    def test_length_drifts_only_when_the_bucket_changes(self):
        """同一档里字数怎么变都不算漂移；跨过档界才算——档界在数据里。"""
        edges = RULES["length"]["edges"]
        inside = ["嗯" * (edges[0] - run) + "。" for run in range(1, 9)]   # 全在「短」这一档
        row = bench.question_stats(series(inside))
        self.assertNotIn("句长量级", row["drift_features"], "同一档内的字数变化不该算漂移")
        across = ["嗯" * (edges[0] - 1) + "。", "嗯" * (edges[0] + 2) + "。"] * 5
        row = bench.question_stats(series(across))
        self.assertIn("句长量级", row["drift_features"], "跨档了就该算漂移")

    def test_report_header_states_the_three_things(self):
        closed = bench.read_closed_sets(RULES)
        header = "\n".join(["- A 轨怎么判（不该变）：%s" % bench.a_rule_note(RULES),
                             "- A 轨的闭集从哪读：%s" % bench.closed_source_note(closed),
                             "- A 轨有没有退回 fallback：%s" % bench.closed_fallback_note(closed)])
        self.assertIn("集内", header)
        self.assertIn("越界", header)
        self.assertIn("跨档", header)
        self.assertIn("identity.self_names", header)
        self.assertIn("address_table", header)
        self.assertIn("没有退回 fallback", header)


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
                "guard_note": "现读", "anchors_note": "合成",
                "a_note": "合成口径：集内／越界／跨档", "closed_note": "合成闭集",
                "fallback_note": "合成：没有退回 fallback",
                "closed": bench.read_closed_sets(RULES)}
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

    def test_header_carries_the_three_a_track_lines(self):
        header = bench.render_meta(self._report())
        self.assertIn("A 轨怎么判（不该变）：合成口径", header)
        self.assertIn("A 轨的闭集从哪读", header)
        self.assertIn("A 轨有没有退回 fallback", header)

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

    def test_a_values_in_json_say_in_set_or_out_of_set(self):
        """--json 里 A 轨每条都看得到「集内／越界:xx」，闭集来源也跟着进 meta。"""
        report = bench.run_bench(runs=2, transport=bench.template_transport(),
                                 questions=QUESTIONS[:1], data=DATA,
                                 config_data=bench.FAKE_CONFIG, env_data=bench.FAKE_ENV)
        json.dumps(report, ensure_ascii=False)
        meta = report["meta"]
        self.assertIn("identity.self_names", meta["closed_note"])
        self.assertIn("address_table", meta["closed_note"])
        for name in ("self", "address"):
            self.assertTrue(meta["closed"][name]["words"])
            self.assertFalse(meta["closed"][name]["fallback"])
        for row in report["cells"]:
            for entry in row["per_question"].values():
                for reading in entry["samples"]:
                    for key in ("自称", "称呼口径"):
                        self.assertTrue(reading["a"][key] == "集内"
                                        or reading["a"][key].startswith("越界:"),
                                        "--json 里 A 轨取值看不清是集内还是越界：%r"
                                        % reading["a"][key])

    def test_anchors_are_read_once_for_the_whole_run(self):
        report = bench.run_bench(runs=1, transport=bench.template_transport(),
                                 questions=QUESTIONS[:1], data=DATA,
                                 config_data=bench.FAKE_CONFIG, env_data=bench.FAKE_ENV)
        self.assertEqual(bench.read_anchors()["total"], report["meta"]["anchors"]["total"])


#: 合成基线：形状与 --write-baseline 写出来的那份一致（只留可比的数）。
MIN_BASELINE = {
    "measured_at": "2026-11-05T20:00:00+00:00", "model": "合成", "runs": 3, "questions": 1,
    "brains": {
        "isolated": {"same": {"empty": {"a_drift_rate": 0.0, "b_same_rate": 1.0,
                                        "c_rate": 0.0, "verdict": "无效"}}, "shuffled": {}},
        "snapshot": {"rows": 2, "same": {}, "shuffled": {}},
    },
    "note": bench.BASELINE_NOTE,
}


def write_baseline(path, blob=None) -> dict:
    """把一份基线写到盘上（默认就用上面那份最小形状）。"""
    data = blob if blob is not None else MIN_BASELINE
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return dict(data)


class BrainSnapshotTest(unittest.TestCase):
    """脑的两列：实盘脑快照只读、条数对得上、跑完实盘一个字节不变；隔离脑行为照旧。"""

    def _home(self, tmp: str) -> Path:
        """造一个临时实盘根：<tmp>/home/organs（paths.organ_dir 认 QQAGENT_HOME）。"""
        home = Path(tmp) / "home" / "organs"
        (home / "brain").mkdir(parents=True)
        return home

    def _seed(self, brain_root: Path) -> int:
        """两条记忆：一条正常原话，一条 quote 空 + note 有值（迁移记忆就长这样）。"""
        store = BrainStore(brain_root)
        store.import_rows([{
            "id": "m-live-1", "ts": "2026-11-04T09:00:00+00:00", "day": "2026-11-04",
            "who": "主人", "text": "我这两天睡不好。", "quote": "我这两天睡不好。",
            "note": "她说睡不好", "side": "他说", "source_type": "亲历",
        }])
        store._append_longterm([{
            "id": "m-migrated-1", "ts": "2026-11-03T09:00:00+00:00", "day": "2026-11-03",
            "who": "主人", "text": "", "quote": "", "note": "迁移过来的：她说医院那事",
            "status": "active", "side": "他说", "source_type": "亲历", "count": 1,
        }])
        return bench.count_memory_rows(brain_root)

    def _digest_tree(self, root: Path) -> dict:
        """整棵目录的指纹：文件名 → 内容哈希（跑完对一次就知道实盘有没有被动过）。"""
        return {str(item.relative_to(root)): hashlib.sha1(item.read_bytes()).hexdigest()
                for item in sorted(root.rglob("*")) if item.is_file()}

    def _run(self, home: Path, *, brain: str, snapshot_of=None, configs=("both",),
             anchors=None, closed=None) -> dict:
        """在临时实盘根下跑一遍：台词库/闭集要在切根之前读，所以这里显式传进去。"""
        previous = os.environ.get("QQAGENT_HOME")
        os.environ["QQAGENT_HOME"] = str(home)
        try:
            return bench.run_bench(runs=1, questions=QUESTIONS[:1], data=DATA,
                                   configs=configs, modes=("same",),
                                   transport=bench.template_transport(),
                                   config_data=bench.FAKE_CONFIG, env_data=bench.FAKE_ENV,
                                   anchors=anchors if anchors is not None else bench.read_anchors(),
                                   closed=(closed if closed is not None
                                           else bench.read_closed_sets(RULES)),
                                   brain=brain,
                                   brain_root=(snapshot_of if snapshot_of is not None
                                               else home / "brain"))
        finally:
            if previous is None:
                os.environ.pop("QQAGENT_HOME", None)
            else:
                os.environ["QQAGENT_HOME"] = previous

    def test_snapshot_carries_the_real_brain_and_never_writes_it(self):
        anchors, closed = bench.read_anchors(), bench.read_closed_sets(RULES)
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            real = home / "brain"
            rows = self._seed(real)
            before = self._digest_tree(real)
            seen = []
            original = bench.ask

            def spy(*args, **kwargs):
                seen.append(len(kwargs["brain"].longterm_active_entries()))
                return original(*args, **kwargs)

            with mock.patch.object(bench, "ask", spy):
                report = self._run(home, brain="snapshot", anchors=anchors, closed=closed)
            self.assertEqual([rows], seen, "每一轮的脑应当是从快照再拷的一份（条数对得上）")
            self.assertEqual(rows, report["meta"]["snapshot"]["rows"])
            self.assertTrue(report["meta"]["snapshot"]["available"])
            self.assertEqual(before, self._digest_tree(real), "跑完实盘脑被动过：快照必须只读")
            self.assertIn("实盘脑只读", report["meta"]["brain_note"])

    def test_isolated_column_carries_no_memories(self):
        anchors, closed = bench.read_anchors(), bench.read_closed_sets(RULES)
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            self._seed(home / "brain")
            seen = []
            original = bench.ask

            def spy(*args, **kwargs):
                seen.append(len(kwargs["brain"].longterm_active_entries()))
                return original(*args, **kwargs)

            with mock.patch.object(bench, "ask", spy):
                report = self._run(home, brain="isolated", anchors=anchors, closed=closed)
            self.assertEqual({0}, set(seen), "隔离脑不该带她的实盘记忆")
            self.assertFalse(report["meta"]["snapshot"]["available"])
            self.assertEqual(["isolated"], report["meta"]["brains"])

    def test_both_brains_render_two_tables_with_titles(self):
        anchors, closed = bench.read_anchors(), bench.read_closed_sets(RULES)
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            rows = self._seed(home / "brain")
            report = self._run(home, brain="both", anchors=anchors, closed=closed)
            self.assertEqual(["isolated", "snapshot"], report["meta"]["brains"])
            self.assertEqual({"isolated", "snapshot"}, {cell["brain"] for cell in report["cells"]})
            tables = bench.render_tables(report)
            self.assertIn("## 隔离脑（结构基线）", tables)
            self.assertIn("## 实盘脑快照（%d 条记忆，拷贝于" % rows, tables)
            self.assertEqual(2, len([line for line in tables.splitlines()
                                     if line.startswith("## ")]))
            self.assertIn("两列并列", report["meta"]["brain_note"])

    def test_empty_real_brain_says_so_and_keeps_the_isolated_column(self):
        anchors, closed = bench.read_anchors(), bench.read_closed_sets(RULES)
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)                       # 目录在，但一条记忆都没有
            report = self._run(home, brain="both", anchors=anchors, closed=closed)
            self.assertFalse(report["meta"]["snapshot"]["available"])
            self.assertIn("目录是空的", report["meta"]["snapshot"]["reason"])
            self.assertEqual(["isolated"], report["meta"]["brains"])
            self.assertIn("实盘脑为空，快照列不跑", report["meta"]["brain_note"])
            self.assertIn("实盘脑为空", bench.render_meta(report))
            self.assertEqual(1, len(report["cells"]), "只跑了一格（both 配置 × 同序 1 轮）" +                              "；快照列不跑时这一格必须还在")

    def test_missing_real_brain_falls_back_to_the_isolated_column(self):
        anchors, closed = bench.read_anchors(), bench.read_closed_sets(RULES)
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            report = self._run(home, brain="snapshot", snapshot_of=Path(tmp) / "没有这个目录",
                               anchors=anchors, closed=closed)
            self.assertIn("目录不在", report["meta"]["snapshot"]["reason"])
            self.assertEqual(["isolated"], report["meta"]["brains"])
            self.assertTrue(report["cells"], "快照列不跑时也要出隔离脑那一列")


class BaselineTest(unittest.TestCase):
    """基线：写得出去、读得回来、逐格比得出「哪一格、哪个数、从 x 到 y」。"""

    def test_load_baseline_reads_a_minimal_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "baseline.json"
            write_baseline(target)
            self.assertEqual(MIN_BASELINE["brains"], bench.load_baseline(target)["brains"])

    def test_missing_or_broken_baseline_is_an_error_not_an_empty_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                bench.load_baseline(Path(tmp) / "没有这个文件.json")
            broken = Path(tmp) / "broken.json"
            broken.write_text("{不是 JSON", encoding="utf-8")
            with self.assertRaises(ValueError):
                bench.load_baseline(broken)
            array = Path(tmp) / "array.json"
            array.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(ValueError):
                bench.load_baseline(array)

    def test_identical_baseline_reports_no_difference(self):
        self.assertEqual([], bench.compare_baseline(MIN_BASELINE, MIN_BASELINE))

    def test_changed_number_is_reported_with_cell_and_both_values(self):
        current = json.loads(json.dumps(MIN_BASELINE))
        current["brains"]["isolated"]["same"]["empty"]["a_drift_rate"] = 0.5
        diffs = bench.compare_baseline(MIN_BASELINE, current)
        self.assertEqual(1, len(diffs))
        got = diffs[0]
        self.assertEqual("brains.isolated.same.empty.a_drift_rate", got["path"])
        self.assertEqual((0.0, 0.5), (got["before"], got["after"]))
        self.assertIn("从 0.0 到 0.5", got["text"])

    def test_changed_verdict_and_missing_cell_are_reported(self):
        current = json.loads(json.dumps(MIN_BASELINE))
        current["brains"]["isolated"]["same"]["empty"]["verdict"] = "换人"
        del current["brains"]["snapshot"]["rows"]
        diffs = bench.compare_baseline(MIN_BASELINE, current)
        paths = [item["path"] for item in diffs]
        self.assertIn("brains.isolated.same.empty.verdict", paths)
        self.assertIn("brains.snapshot.rows", paths)
        self.assertTrue(any("这次没有这一格" in item["text"] for item in diffs))

    def test_non_metric_fields_do_not_count_as_differences(self):
        current = json.loads(json.dumps(MIN_BASELINE))
        current["measured_at"] = "2026-12-01T00:00:00+00:00"
        current["model"] = "换了个模型"
        current["note"] = "另一句注记"
        self.assertEqual([], bench.compare_baseline(MIN_BASELINE, current))

    def test_write_baseline_flag_writes_a_readable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "baseline.json"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = bench.main(["--template", "--runs", "1", "--limit", "1",
                                   "--write-baseline", str(target)])
            self.assertEqual(0, code)
            self.assertIn("基线已写入", out.getvalue())
            blob = bench.load_baseline(target)
            for key in ("measured_at", "model", "runs", "questions", "brains", "note"):
                self.assertIn(key, blob)
            self.assertEqual(bench.BASELINE_NOTE, blob["note"])
            self.assertEqual((1, 1), (blob["runs"], blob["questions"]))
            cell = blob["brains"]["isolated"]["same"]["empty"]
            self.assertEqual(set(bench.BASELINE_FIELDS), set(cell))
            self.assertEqual([], bench.compare_baseline(blob, blob))

    def test_blob_keeps_the_snapshot_rows_when_both_brains_ran(self):
        anchors, closed = bench.read_anchors(), bench.read_closed_sets(RULES)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home" / "organs"
            (home / "brain").mkdir(parents=True)
            store = BrainStore(home / "brain")
            store._append_longterm([{"id": "m-1", "ts": "2026-11-03T09:00:00+00:00",
                                     "day": "2026-11-03", "who": "主人", "text": "",
                                     "quote": "", "note": "迁移过来的：她说医院那事",
                                     "status": "active", "side": "他说", "source_type": "亲历"}])
            previous = os.environ.get("QQAGENT_HOME")
            os.environ["QQAGENT_HOME"] = str(home)
            try:
                report = bench.run_bench(runs=1, questions=QUESTIONS[:1], data=DATA,
                                         configs=("both",), modes=("same",),
                                         transport=bench.template_transport(),
                                         config_data=bench.FAKE_CONFIG, env_data=bench.FAKE_ENV,
                                         anchors=anchors, closed=closed, brain="both")
            finally:
                if previous is None:
                    os.environ.pop("QQAGENT_HOME", None)
                else:
                    os.environ["QQAGENT_HOME"] = previous
            blob = bench.baseline_blob(report)
            self.assertEqual(1, blob["brains"]["snapshot"]["rows"])
            self.assertEqual({"same", "rows"}, set(blob["brains"]["snapshot"]),
                             "这次只跑了同序——基线只记跑过的跑法，不补空表")
            self.assertEqual({"same"}, set(blob["brains"]["isolated"]))


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
