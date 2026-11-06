"""home_diary / home_dream：素材只取今天的真东西、交核写整篇、落抽屉、脑里只留一行索引。

判据（第四轮任务书）：
- 素材只取今天的真东西（脑 today 里今天的、心的今晚心情档、日子自己的当天账），缺了就少给，不许编；
- 日记 80–150 字（区间在 data/writing.json，测试不抄这两个数）；
- 一天一张、幂等：已存在就返回既有那张，不写第二张；
- 脑里只有一行索引，source_type 是 日记 / 梦，evidence_key 为空（梦不作证词），正文不进脑；
- 写之前先把抽屉索引备一份到日子线根下：备份没成 = 这一步失败；
- 核失败一律 status=failed 且 detail 带 LLMError.kind，绝不记成成功、绝不写半篇。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple

from organs.brain import BrainStore
from organs.day import model, night
from organs.day.tests.helpers import (article, at, brain, drawer_cards, fake_mind, log_lines,
                                      make_home, seed_today, temp_root)
from organs.heart import mood as mood_module
from organs.mind import LLMError

NOW = "2026-11-06T05:00:00"
YESTERDAY = "2026-11-05T10:00:00"


def recording_mind(text: str = "") -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """假核 + 把每一次请求体记下来（要断言提示词里那几句硬话时用）。"""
    mind = fake_mind(text)
    seen: List[Dict[str, Any]] = []
    inner = mind["transport"]

    def transport(url, headers, payload, timeout):
        seen.append(payload)
        return inner(url, headers, payload, timeout)

    mind["transport"] = transport
    return mind, seen


class WritingTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root(self)
        self.heart = self.root / "heart"
        self.heart.mkdir()
        self.brain = brain(self.root)
        self.home = make_home(self)
        self.writing = night.load_writing()

    def _run(self, **kwargs):
        params = {"root": self.root, "brain": self.brain, "heart_root": self.heart,
                  "home_root": self.home, "mind": fake_mind()}
        params.update(kwargs)
        return night.run(at(NOW), **params)

    def _cards(self, kind: str = "") -> List[Dict[str, Any]]:
        rows = drawer_cards(self.root, self.home)
        return [card for card in rows if not kind or card["kind"] == kind]

    def _brain_today(self) -> List[Dict[str, Any]]:
        return self.brain.today_view(at(NOW))["entries"]


class DiaryLandingTest(WritingTestBase):
    def test_diary_lands_in_the_drawer_with_the_core_text(self):
        seed_today(self.brain, at(NOW))
        text = article()
        payload = self._run(mind=fake_mind(text))
        self.assertEqual("ok", payload["steps"]["home_diary"]["status"],
                         payload["steps"]["home_diary"]["detail"])
        cards = self._cards("日记")
        self.assertEqual(1, len(cards))
        card = cards[0]
        self.assertEqual(text, card["params"]["text"], "抽屉里是核回的整篇原文")
        self.assertEqual(model.day_key(at(NOW)), card["params"]["date"], "落的是今天")
        span = self.writing["word_range"]
        self.assertTrue(span["min"] <= len(card["params"]["text"]) <= span["max"])
        self.assertTrue(card["name"].endswith(model.day_key(at(NOW))), "名字里带当天日期")

    def test_card_body_is_the_written_piece_not_the_brain_index_line(self):
        """卡片正文栏必须是核写的那一篇：真源在抽屉，人打开面板要读得到日记本身。

        回归的是真缺陷：曾经把给脑用的那一行索引写进 note，正文只躲在 params 里，
        于是账上记 89 字、卡上躺着一句 60 字的索引。
        """
        seed_today(self.brain, at(NOW))
        text = article()
        payload = self._run(mind=fake_mind(text))
        card = self._cards("日记")[0]
        self.assertEqual(text, card["note"], "note 就是核写的那一篇")
        self.assertEqual(len(card["note"]), card["chars"], "chars 必须等于正文长度")
        self.assertEqual(text, card["params"]["text"], "params.text 与正文逐字相同")
        self.assertNotIn("正文只在抽屉一份", card["note"], "索引那一句不许冒充正文")
        index_line = payload["steps"]["home_diary"].get("index", "")
        self.assertIn("正文只在抽屉一份", index_line, "索引那一句确实进了脑")
        self.assertNotEqual(index_line, card["note"], "索引只进脑、不进正文栏")

    def test_diary_records_which_memories_it_was_written_from(self):
        entry = seed_today(self.brain, at(NOW))["entry"]
        self._run()
        card = self._cards("日记")[0]
        self.assertEqual([entry["id"]], card["params"]["from_memories"])
        self.assertEqual(mood_module.read(self.heart, now=at(NOW))["tier"],
                         card["params"]["mood"])

    def test_dummy_never_appears_when_nothing_happened_today(self):
        """今天没有素材：如实报 skipped，不写、不编。"""
        payload = self._run()
        step = payload["steps"]["home_diary"]
        self.assertEqual(night.STATUS_SKIPPED, step["status"])
        self.assertIn("没素材" if False else "真素材", step["detail"])
        self.assertEqual([], self._cards("日记"))
        self.assertEqual([], [row for row in self._brain_today()
                              if row.get("source_type") == "日记"])

    def test_only_today_entries_become_material(self):
        seed_today(self.brain, at(YESTERDAY), text="昨天搬过一次书架，今天没动它。")
        seed_today(self.brain, at(NOW))
        mind, seen = recording_mind()
        self._run(mind=mind)
        block = seen[0]["messages"][1]["content"]
        self.assertIn("今天下午把书架整个挪了一次", block)
        self.assertNotIn("昨天搬过一次书架", block, "非今天的条目不许当素材")
        card = self._cards("日记")[0]
        self.assertEqual(1, len(card["params"]["from_memories"]))

    def test_prompt_forbids_writing_anything_but_today(self):
        seed_today(self.brain, at(NOW))
        mind, seen = recording_mind()
        self._run(mind=mind)
        system = seen[0]["messages"][0]["content"]
        self.assertIn("只能写今天真的发生过的事", system, "任务书要求的那句硬话必须在提示词里")
        span = self.writing["entries"]["home_diary"]["prompt_range"]
        self.assertIn("%d–%d" % (span["min"], span["max"]), system,
                      "提示词里的字数来自数据文件，不是代码里的字面量")


class DreamLandingTest(WritingTestBase):
    def test_dream_lands_in_the_drawer_and_is_not_testimony(self):
        seed_today(self.brain, at(NOW))
        payload = self._run()
        self.assertEqual("ok", payload["steps"]["home_dream"]["status"],
                         payload["steps"]["home_dream"]["detail"])
        card = self._cards("梦")[0]
        self.assertEqual("梦", card["params"]["source_type"], "家给梦补上来源类型")
        self.assertFalse(card["testimony"], "梦只是谈资，不作证词")
        self.assertTrue(card["display"].startswith("（梦）"))
        self.assertTrue(card["params"]["dream_of"], "梦要记它梦的是哪一夜")

    def test_dream_prompt_forbids_inventing_people_and_events(self):
        seed_today(self.brain, at(NOW))
        mind, seen = recording_mind()
        self._run(mind=mind)
        system = seen[1]["messages"][0]["content"]
        self.assertIn("不许凭空造人物与事件", system)
        self.assertIn("重组", system)
        self.assertIn("荒诞", system)


class BrainIndexTest(WritingTestBase):
    def test_brain_keeps_one_index_line_per_kind_and_no_body(self):
        seed_today(self.brain, at(NOW))
        text = article()
        self._run(mind=fake_mind(text))
        rows = [row for row in self._brain_today()
                if row.get("source_type") in ("日记", "梦")]
        self.assertEqual(["日记", "梦"], [row["source_type"] for row in rows])
        for row in rows:
            self.assertEqual("", row.get("evidence_key"), "日记与梦都不作证词")
            self.assertNotIn(text, row["text"], "正文只在抽屉一份，脑里只有一句索引")
            self.assertIn("抽屉", row["text"], "索引要指得回家那张卡")
        self.assertIn("她自己·日记", [row["who"] for row in rows])

    def test_brain_rejects_an_unknown_source_type_instead_of_guessing(self):
        out = self.brain.ingest(who="人", text="来源不明的一句话", source_type="外星",
                                ts=at(NOW))
        self.assertFalse(out["ok"])
        self.assertIn("source_type", out["message"])
        self.assertEqual([], [row for row in self._brain_today()
                              if row.get("text") == "来源不明的一句话"],
                         "不认识的来源直接拒，绝不静默当亲历")


class IdempotentAndFailureTest(WritingTestBase):
    def test_second_run_same_day_returns_the_existing_cards(self):
        seed_today(self.brain, at(NOW))
        first = self._run()
        second = self._run()
        self.assertEqual(["日记", "梦"], sorted(card["kind"] for card in self._cards()))
        for step in ("home_diary", "home_dream"):
            self.assertFalse(first["steps"][step].get("rerun"), "首跑就是真写，不是幂等返回")
            self.assertTrue(second["steps"][step]["rerun"], "同一天重跑只回既有那张")
            self.assertEqual(first["steps"][step]["card_id"],
                             second["steps"][step]["card_id"])
        self.assertEqual(2, len([row for row in self._brain_today()
                                 if row.get("source_type") in ("日记", "梦")]),
                         "脑里也不许多出一行索引")

    def test_core_failure_is_a_failed_step_with_the_kind_and_no_card(self):
        seed_today(self.brain, at(NOW))
        mind = fake_mind()
        mind["config"] = dict(mind["config"], base_url="http://127.0.0.1:9/v1", retries=0)

        def broken(url, headers, payload, timeout):
            raise LLMError("timeout", "模型没在时限内回话：注入的超时")

        mind["transport"] = broken
        payload = self._run(mind=mind)
        step = payload["steps"]["home_diary"]
        self.assertEqual(night.STATUS_FAILED, step["status"])
        self.assertTrue(step["detail"].startswith("timeout: "), step["detail"])
        self.assertEqual("timeout", step["error_kind"])
        self.assertIn("模型没在时限内回话", step["detail"])
        self.assertEqual([], self._cards(), "失败时抽屉里一张都不许多")
        self.assertEqual([], [row for row in self._brain_today()
                              if row.get("source_type") in ("日记", "梦")])
        line = log_lines(self.root)[-1]
        self.assertEqual(night.STATUS_FAILED, line["steps"]["home_diary"]["status"])
        self.assertIn("home_diary", payload["failed"])
        self.assertIn("home_dream", payload["failed"])

    def test_wrong_length_is_refused_instead_of_truncated(self):
        seed_today(self.brain, at(NOW))
        span = self.writing["word_range"]
        payload = self._run(mind=fake_mind("短" * (span["min"] - 1)))
        step = payload["steps"]["home_diary"]
        self.assertEqual(night.STATUS_FAILED, step["status"])
        self.assertIn("字", step["detail"])
        self.assertEqual([], self._cards(), "不许截半篇、也不许照样落红")

    def test_backup_failure_means_the_step_fails(self):
        seed_today(self.brain, at(NOW))
        config = json.loads(json.dumps(night.load_writing()))
        (self.root / "blocked").write_text("占了这条路径", encoding="utf-8")
        config["rollback"]["dir"] = "blocked"      # 那是一个文件：建目录必然失败
        path = self.root / "bad-writing.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        payload = self._run(writing=str(path))
        step = payload["steps"]["home_diary"]
        self.assertEqual(night.STATUS_FAILED, step["status"])
        self.assertIn("回滚点", step["detail"])
        self.assertEqual([], self._cards(), "没备份不照写")


class RollbackPointTest(WritingTestBase):
    def test_drawer_indexes_are_copied_to_the_day_root_before_writing(self):
        seed_today(self.brain, at(NOW))
        from organs.home import cards
        cards.append_cards(self.home, role="agent", by="她", cards=[{
            "room": "抽屉", "kind": "日记", "name": "日记-2026-11-05",
            "chars": len(article()), "at": at(YESTERDAY),
            "params": {"date": "2026-11-05", "text": article(), "mood": "平静",
                       "from_memories": []}}])
        payload = self._run()
        self.assertEqual("ok", payload["steps"]["home_diary"]["status"],
                         payload["steps"]["home_diary"]["detail"])
        day = model.day_key(at(NOW))
        target = self.root / self.writing["rollback"]["dir"] / \
            self.writing["rollback"]["shelf_template"].format(
                day=day, step="home_diary", shelf="日记")
        self.assertTrue(target.is_file(), "抽屉索引的备份要落在日子线自己的根下")
        lines = [row for row in target.read_text(encoding="utf-8").splitlines() if row.strip()]
        self.assertEqual(1, len(lines), "备份的是**写之前**那一份（1 行），不是写完那份")
        self.assertIn("2026-11-05", lines[0])
        self.assertEqual(str(target), payload["steps"]["home_diary"]["backup"]["copied"][0]["to"])


if __name__ == "__main__":
    unittest.main()
