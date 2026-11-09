"""home_diary / home_dream：素材只取今天的真东西、交核写整篇、落抽屉、脑里只留一行索引。

判据（第四轮任务书）：
- 素材只取今天的真东西（脑 today 里今天的、心的今晚心情档、日子自己的当天账），缺了就少给，不许编；
- 日记非空且不超过配置上限（data/writing.json），允许按素材短写；
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


class DiaryReviewTest(WritingTestBase):
    def setUp(self):
        super().setUp()
        cfg = json.loads(night.writing_file().read_text())
        cfg['entries']['日记']['review_prompt'] = '对照资料复核日记；返回text和changes。'
        self.review_config = self.root / 'review-writing.json'
        self.review_config.write_text(json.dumps(cfg, ensure_ascii=False))
        seed_today(self.brain, at(NOW), text='今晚想安静待一会儿。')

    def run_review(self, review):
        draft = '他说想安静待一会儿。我关掉电视陪他坐在沙发上。'
        seen = []
        mind = fake_mind()
        def transport(url, headers, payload, timeout):
            seen.append(payload['messages'])
            content = draft if len(seen) == 1 else review
            return 200, json.dumps({'choices': [{'message': {'content': content}}]}).encode()
        mind['transport'] = transport
        result = self._run(mind=mind, writing=self.review_config, only=['home_diary'])
        return result, seen, mind

    def test_reviewed_body_and_original_are_kept_and_rerun_does_not_call_model(self):
        final = '他说今晚想安静待一会儿。'
        changes = [{'quote': '我关掉电视陪他坐在沙发上。', 'reason': '只有聊天，没有这些动作记录'}]
        result, seen, mind = self.run_review(json.dumps({'text': final, 'changes': changes}))
        self.assertEqual('ok', result['steps']['home_diary']['status'])
        card = self._cards('日记')[0]
        self.assertEqual(final, card['note'])
        self.assertEqual(final, card['params']['text'])
        self.assertEqual(changes, card['params']['review']['changes'])
        self.assertIn('我关掉电视', card['params']['review']['draft'])
        self.assertEqual(2, len(seen))
        self.assertIn('今晚想安静待一会儿', seen[1][-1]['content'])
        self._run(mind=mind, writing=self.review_config, only=['home_diary'])
        self.assertEqual(2, len(seen))

    def test_invalid_review_leaves_no_diary_or_brain_index(self):
        for review in ('not json', json.dumps({'text': '改过了', 'changes': []}),
                       json.dumps({'text': '改过了', 'changes': [{'quote': '不存在的引文', 'reason': '假依据'}]})):
            with self.subTest(review=review):
                result, _, _ = self.run_review(review)
                self.assertEqual(night.STATUS_FAILED, result['steps']['home_diary']['status'])
                self.assertEqual([], self._cards('日记'))
                self.assertFalse(any(r.get('source_type') == '日记' for r in self._brain_today()))

    def test_writing_trace_links_card_review_failure_retry_and_idempotence(self):
        from unittest.mock import patch
        from api import observe
        with patch.dict('os.environ', {'QQAGENT_TRACE':'1','QQAGENT_HOME_ROOT':str(self.root)}):
            failed, _, _ = self.run_review('invalid review')
            old = failed['steps']['home_diary'].get('model_turn_id')
            self.assertIsNotNone(old)
            status, view = observe.dispatch({'source':'model','turn_id':old})
            self.assertEqual(200, status)
            self.assertEqual(['日记写作','日记事实复核'], [r['record']['purpose'] for r in view['rows']])
            self.assertEqual([1,2], [r['record']['call_index'] for r in view['rows']])
            final = '他说想安静待一会儿。我关掉电视陪他坐在沙发上。'
            done, seen, mind = self.run_review(json.dumps({'text':final,'changes':[]}))
            turn = done['steps']['home_diary']['model_turn_id']
            self.assertNotEqual(old, turn)
            self.assertEqual(turn, self._cards('日记')[0]['params']['model_turn_id'])
            rerun = self._run(mind=mind, writing=self.review_config, only=['home_diary'], carry=done['steps'])
            self.assertEqual(turn, rerun['steps']['home_diary']['model_turn_id'])
            self.assertEqual(2, len(seen))
            self.assertEqual(old, log_lines(self.root)[0]['steps']['home_diary']['model_turn_id'])
            dream = self._run(only=['home_dream'], carry=done['steps'])
            dream_id = dream['steps']['home_dream']['model_turn_id']
            self.assertNotIn(dream_id, (old, turn))
            self.assertEqual(turn, dream['steps']['home_diary']['model_turn_id'])
            self.assertEqual(dream_id, self._cards('梦')[0]['params']['model_turn_id'])
            _, traces = observe.dispatch({'source':'model','turn_id':dream_id})
            self.assertEqual(['梦境写作'], [r['record']['purpose'] for r in traces['rows']])
            self.assertNotIn('model_turn_id', dream['steps']['heart_settle'])


class DiaryLandingTest(WritingTestBase):
    def test_material_completes_linked_turns_without_reviving_filtered_entries(self):
        from organs.brain.model import SIDE_OTHER, SIDE_SELF
        ids = []
        for index, text in enumerate(('更早的一问', '更早的一答', '应该补回的提问', '对应的回答', '新的提问', '新的回答')):
            result = self.brain.ingest(who='123456789', text=text, ts=at(NOW),
                                      side=SIDE_OTHER if index % 2 == 0 else SIDE_SELF,
                                      turn_id='turn-' + str(index // 2))
            ids.append(result['entry']['id'])
        retired = self.brain.ingest(who='123456789', text='已退役的材料', ts=at(NOW), turn_id='turn-2')['entry']
        self.brain.retire(retired['id'], ts=at(NOW))
        mind, seen = recording_mind()
        self._run(mind=mind)
        user = seen[0]['messages'][1]['content']
        self.assertIn('应该补回的提问', user)
        self.assertNotIn('更早的一问', user)
        self.assertNotIn('更早的一答', user)
        self.assertNotIn('已退役的材料', user)
        self.assertEqual(ids[2:], self._cards('日记')[0]['params']['from_memories'])

    def test_legacy_same_time_messages_are_not_guessed_into_a_turn(self):
        for index in range(4):
            self.brain.ingest(who='123456789', text='旧记录-' + str(index), ts=at(NOW))
        mind, seen = recording_mind()
        self._run(mind=mind)
        user = seen[0]['messages'][1]['content']
        self.assertNotIn('旧记录-0', user)
        for index in (1, 2, 3):
            self.assertIn('旧记录-' + str(index), user)

    def test_diary_and_dream_explain_the_actual_speaker_labels(self):
        from organs.brain.model import SIDE_OTHER, SIDE_SELF
        self.assertTrue(self.brain.ingest(who='123456789', text='今天只想安静待会儿。',
                                         ts=at(NOW), side=SIDE_OTHER)['ok'])
        self.assertTrue(self.brain.ingest(who='她', text='好，我陪你。',
                                         ts=at(NOW), side=SIDE_SELF)['ok'])
        mind, seen = recording_mind()
        self._run(mind=mind)
        self.assertEqual(3, len(seen))
        for request in (seen[0], seen[2]):
            system, user = [message['content'] for message in request['messages']]
            for side in (SIDE_OTHER, SIDE_SELF):
                self.assertIn('side=' + side, system)
                self.assertIn('"side": ' + json.dumps(side, ensure_ascii=False), user)
            self.assertIn('好，我陪你。', user)

    def test_short_diary_is_saved_without_padding(self):
        seed_today(self.brain,at(NOW),text='今天就想安静待一会儿，不用给建议。')
        text='听他说今天只想安静待一会儿，不用给建议。'
        payload=self._run(mind=fake_mind(text))
        self.assertEqual('ok',payload['steps']['home_diary']['status'])
        self.assertEqual(text,self._cards('日记')[0]['params']['text'])
        self.assertEqual(len(text),self._cards('日记')[0]['chars'])

    def test_phone_only_day_is_material_with_evidence_ids(self):
        from organs.day import phone_browse
        for ident,stamp,status,origin in (
            ('observed','2026-11-06T04:30:00','partial','automatic'),
            ('acceptance','2026-11-06T04:40:00','done','acceptance'),
            ('yesterday','2026-11-06T03:30:00','done','automatic'),
            ('future','2026-11-06T06:00:00','done','automatic'),
            ('failed','2026-11-06T04:50:00','failed','automatic')):
            phone_browse.save({'id':ident,'created_at':at(stamp),'status':status,'origin':origin,
                'title':ident,'observations':[{'status':'ok','observation':{'subject':'猫坐在纸箱旁'}}],
                'reflection':{'status':'ok','at':at(stamp),'effect':'positive',
                              'impression':'这个动作有点可爱','evidence':'猫坐在纸箱旁'},
                'audio':{'status':'unclear','transcript':'不可靠的对白','raw_transcript':'不能引入'}},self.root)
        mind,seen=recording_mind()
        result=self._run(mind=mind)
        self.assertEqual('ok',result['steps']['home_diary']['status'])
        user=seen[0]['messages'][1]['content']
        self.assertIn('猫坐在纸箱旁',user)
        self.assertIn('这个动作有点可爱',user)
        for excluded in ('acceptance','yesterday','future','failed','不可靠的对白','不能引入'):
            self.assertNotIn(excluded,user)
        for kind in ('日记','梦'):
            self.assertEqual(['observed'],self._cards(kind)[0]['params']['from_phone'])
        calls=len(seen)
        self._run(mind=mind)
        self.assertEqual(calls,len(seen),'已写过的日记不应重复生成')

    def test_simulated_schedule_alone_does_not_become_real_diary(self):
        saved=self.brain.ingest(who='她自己',text='去海边捡贝壳',side='self',source_type='日程模拟',ts=at(NOW))
        self.assertTrue(saved['ok'],saved)
        (self.root/'schedule_log.jsonl').write_text(json.dumps(
            {'day':model.day_key(at(NOW)),'slot':'清晨','item':'去海边捡贝壳'},ensure_ascii=False)+'\n')
        mind,seen=recording_mind()
        result=self._run(mind=mind)
        self.assertEqual('skipped',result['steps']['home_diary']['status'])
        self.assertEqual([],seen)
        self.assertEqual([],self._cards())

    def test_chat_material_retains_speaker_time_and_side(self):
        row=self.brain.ingest(who='123456789',text='我搬了书架，手上都是灰',ts=at(NOW))['entry']
        mind,seen=recording_mind()
        self._run(mind=mind)
        user=seen[0]['messages'][1]['content']
        for key in ('who','side','ts','source_type'):
            self.assertIn(json.dumps(key)+': '+json.dumps(row[key],ensure_ascii=False),user)
        self.assertIn('对方的发言',seen[0]['messages'][0]['content'])

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
        self.assertNotIn('model_turn_id', step)
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
        self.assertIn("不超过 %d 字" % span["max"], system,
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
        system = seen[-1]["messages"][0]["content"]
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
        self.assertEqual(32, len(step['model_turn_id']))
        self.assertNotEqual(step['model_turn_id'], payload['steps']['home_dream']['model_turn_id'])
        self.assertNotIn('model_turn_id', payload['steps']['heart_settle'])
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
        payload = self._run(mind=fake_mind("长" * (span["max"] + 1)))
        step = payload["steps"]["home_diary"]
        self.assertEqual(night.STATUS_FAILED, step["status"])
        self.assertIn("字", step["detail"])
        self.assertEqual([], self._cards(), "不许截半篇、也不许照样落红")

    def test_blank_writing_is_not_saved(self):
        seed_today(self.brain, at(NOW))
        payload = self._run(mind=fake_mind(" \n\t"))
        self.assertEqual(night.STATUS_FAILED, payload["steps"]["home_diary"]["status"])
        self.assertEqual([], self._cards())

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


class AutonomousWritingMaterialsTest(WritingTestBase):
    def test_actual_autonomous_writing_enters_night_without_chat_or_phone(self):
        from unittest.mock import patch
        from organs.day import autonomy, growth, solitude
        growth.configure(True, root=self.root)
        with patch('organs.day.phone_browse._feeling', return_value={'asleep':False,'energy':{'value':80}}), \
             patch('organs.hand.phone.config', return_value={}):
            done = autonomy.tick(at('2026-11-06T04:30:00'), root=self.root, home_root=self.home,
                heart_root=self.heart, brain=self.brain, complete=lambda _: json.dumps({
                    'action':'write','reason':'想写一个故事','args':{'text':'故事里的我登上月球。'}}))
        self.assertEqual(done['status'], 'done')
        for ident, stamp, status, event, extra in (
            ('old', '2026-11-06T03:59:00', 'done', 'autonomy_result', {}),
            ('future', '2026-11-06T06:00:00', 'done', 'autonomy_result', {}),
            ('queued', '2026-11-06T04:35:00', 'queued', 'autonomy_result', {}),
            ('test', '2026-11-06T04:35:00', 'done', 'autonomy_result', {'origin':'acceptance'}),
            ('paint-failed', '2026-11-06T04:40:00', 'failed', 'autonomy_finished', {})):
            solitude.append_log(self.root, {'at':at(stamp),'event':event,'model_turn_id':ident,
                'status':status,'action':'paint','reason':ident,'receipt':{'card_id':ident},**extra})
        mind, seen = recording_mind('今天写了一个登月故事，画画没成功。')
        result = self._run(mind=mind,only=['home_diary','home_dream'])
        self.assertEqual(result['steps']['home_diary']['status'],'ok')
        self.assertEqual(result['steps']['home_dream']['status'],'ok')
        for request in (seen[0],seen[-1]):
            system,user = [m['content'] for m in request['messages']]
            self.assertIn('创作内容不代表实际经历',system)
            self.assertIn('故事里的我登上月球。',user)
            self.assertIn('paint-failed',user)
            for excluded in ('"old"','"future"','"queued"','"test"'):
                self.assertNotIn(excluded,user)
        self.assertIn('故事里的我登上月球。',seen[1]['messages'][1]['content'])
        for kind in ('日记','梦'):
            self.assertEqual(self._cards(kind)[0]['params']['from_activities'],
                [done['model_turn_id'],'paint-failed'])
        count=len(seen)
        self._run(mind=mind,only=['home_diary','home_dream'])
        self.assertEqual(len(seen),count)

    def test_legacy_mode_does_not_take_new_activity_material(self):
        from organs.day import solitude
        solitude.append_log(self.root, {'at':at(NOW),'event':'autonomy_result','model_turn_id':'unused',
            'status':'done','action':'write','receipt':{'text':'旧的活动'}})
        mind,seen=recording_mind()
        result=self._run(mind=mind,only=['home_diary','home_dream'])
        self.assertEqual(result['steps']['home_diary']['status'],night.STATUS_SKIPPED)
        self.assertEqual(result['steps']['home_dream']['status'],night.STATUS_SKIPPED)
        self.assertEqual(seen,[])
