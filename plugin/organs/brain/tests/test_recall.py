"""回想：第一档原文 + 字面检索；空就返回空不硬凑；向量只留接口。"""
from __future__ import annotations

import unittest

from organs.brain.tests.helpers import fresh_store, ingest


class RecallTest(unittest.TestCase):
    def context_rows(self):
        rows = [{'id':'original', 'who':'100000001', 'text':'周六去书店，选红叶楼西门旧街书店',
                 'ts':'2026-11-07T10:00:00+00:00', 'turn_id':'turn-original'}]
        rows += [{'id':f'distractor-{i}', 'who':'100000001', 'text':f'红叶楼西门旧街书店资料{i}',
                  'ts':f'2026-11-07T10:0{i}:00+00:00'} for i in range(1,7)]
        rows.append({'id':'correction', 'who':'100000001', 'text':'改去那里吧',
            'note':'周六改去河岸图书馆', 'ts':'2026-11-07T10:08:00+00:00', 'turn_id':'turn-correction',
            'context_refs':[{'turn_id':'turn-original','quote':'周六去书店'}]})
        return rows

    def test_context_links_recall_corrections_under_existing_slot_budget(self):
        from organs.brain.recall import recall
        rows = self.context_rows()
        rows.append({'id':'second-correction','who':'100000001','text':'还是取消吧',
            'note':'周六不出门了', 'ts':'2026-11-07T10:09:00+00:00',
            'context_refs':[{'turn_id':'turn-correction','quote':'改去那里吧'}]})
        result = recall('红叶楼西门旧街书店', rows, [], who='100000001')
        self.assertEqual(['second-correction','correction'], [r['id'] for r in result['items'][:2]])
        self.assertEqual(['correction'], result['items'][0]['context_from'])
        self.assertEqual(['original'], result['items'][1]['context_from'])
        self.assertIn('context_link', result['items'][0]['matched_by'])
        self.assertFalse(result['items'][0]['note_testimony'])
        self.assertEqual(6, result['slots_used'])
        self.assertEqual('周六去书店，选红叶楼西门旧街书店', rows[0]['text'])

    def test_context_links_continue_through_merged_turn_understanding(self):
        from organs.brain.recall import recall
        rows = self.context_rows()
        rows[-1]['text'] = '五点到'
        rows.append({'id':'correction-first-line','who':'100000001',
            'text':'改去河岸图书馆吧','ts':rows[-1]['ts'],'turn_id':'turn-correction'})
        rows.append({'id':'latest','who':'100000001','text':'还是取消吧',
            'note':'周六不出门了','ts':'2026-11-07T10:09:00+00:00',
            'context_refs':[{'turn_id':'turn-correction','quote':'改去河岸图书馆吧'}]})
        result = recall('红叶楼西门旧街书店', rows, [], who='100000001')
        self.assertEqual('latest', result['items'][0]['id'])
        self.assertEqual(['correction'], result['items'][0]['context_from'])

    def test_context_links_do_not_guess_ambiguous_or_foreign_sources(self):
        from organs.brain.recall import recall
        for case in ('other_person','unknown_person','ambiguous','wrong_quote','non_lived','earlier','no_note'):
            with self.subTest(case=case):
                rows = self.context_rows()
                if case == 'other_person': rows[-1]['who'] = '100000002'
                if case == 'unknown_person': rows[0]['who'] = rows[-1]['who'] = '人'
                if case == 'ambiguous': rows.insert(1, dict(rows[0], id='duplicate'))
                if case == 'wrong_quote': rows[-1]['context_refs'][0]['quote'] = '并没说过'
                if case == 'non_lived': rows[-1]['source_type'] = '日记'
                if case == 'earlier': rows[-1]['ts'] = '2026-11-07T09:00:00+00:00'
                if case == 'no_note': rows[-1]['note'] = ''
                result = recall('红叶楼西门旧街书店', rows, [], who='100000001')
                self.assertNotIn('correction', [r['id'] for r in result['items']])
                self.assertFalse(any(r.get('context_from') for r in result['items'] + result['skipped']))

    def test_context_links_keep_private_boundary_through_hot_demotion(self):
        import json
        from organs.brain.store import TIERS_FILE
        from organs.mind import conversation
        store,tmp=fresh_store(); self.addCleanup(tmp.cleanup)
        (store.root / TIERS_FILE).write_text(json.dumps(dict(store._tiers(),today_max=1,today_demote_count=1)))
        source=store.ingest('100000001','周六去书店，选红叶楼西门旧街书店',
            ts='2026-11-07T10:00:00+00:00',turn_id='source')['entry']
        new=store.ingest('100000001','改去那里吧',note='改去私密测试图书馆',audience='私密',
            ts='2026-11-07T10:01:00+00:00',context_refs=[{'turn_id':'source','quote':'周六去书店'}])['entry']
        store.ingest('100000002','天气不错',ts='2026-11-07T10:02:00+00:00')
        self.assertTrue(any(r['id']==new['id'] and r.get('context_refs') for r in store.longterm_active_entries()))
        result=store.recall('红叶楼西门旧街书店',who='100000001',scene='group',now_ts='2026-11-07T10:03:00+00:00')
        correction=next(r for r in result['items'] if r['id']==new['id'])
        self.assertEqual([source['id']],correction['context_from'])
        self.assertFalse(correction['retellable'])
        recalled,prompt,_=conversation._recall(store,'红叶楼西门旧街书店','100000001','group',cap=900)
        self.assertFalse(next(r for r in recalled if r['id']==new['id'])['retellable'])
        self.assertIn('私密：不当众复述',prompt)

    def test_editing_understanding_drops_its_old_model_context_link(self):
        import json
        for archived in (False,True):
            with self.subTest(archived=archived):
                store,tmp=fresh_store();self.addCleanup(tmp.cleanup)
                new=store.ingest('100000001','改去那里吧',note='旧理解',ts='2026-11-07T10:00:00+00:00',
                    context_refs=[{'turn_id':'source','quote':'周六去书店'}])['entry']
                if archived: store.today_entries('2026-11-08T10:00:00+00:00')
                self.assertTrue(store.edit_note(new['id'],'operator','这句话不是在改行程')['ok'])
                rows=store.longterm_active_entries() if archived else store.today_entries('2026-11-07T10:01:00+00:00')
                record=next(r for r in rows if r['id']==new['id'])
                self.assertFalse(record.get('context_refs'))
                self.assertEqual('改去那里吧',record['quote'])
                history=[json.loads(line) for line in (store.root/'note_history.jsonl').read_text().splitlines()]
                self.assertEqual(new['context_refs'],history[-1]['old_context_refs'])

    def test_import_rejects_malformed_context_refs_at_input_boundary(self):
        from organs.brain.store import BrainReadError
        store,tmp=fresh_store();self.addCleanup(tmp.cleanup)
        for refs in ('text',[{'turn_id':42,'quote':'旧话'}],[{'turn_id':'x','quote':''}]):
            with self.subTest(refs=refs),self.assertRaises(BrainReadError):
                store.import_rows([{'id':'bad','text':'原话','context_refs':refs}])
        self.assertEqual([],store.longterm_active_entries())

    def test_newer_correction_wins_equal_scores_across_timezones_and_rollover(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        for i in range(6):
            store.ingest('100000001', f'周六计划去图书馆，安排{i}',
                         ts=f'2026-11-07T19:0{i}:00+09:00')
        correction = store.ingest('100000001', '周六计划改去公园',
                                 ts='2026-11-07T10:10:00+00:00')['entry']
        for at in ('2026-11-07T10:11:00+00:00', '2026-11-08T10:11:00+00:00'):
            result = store.recall('周六计划', who='100000001', now_ts=at)
            with self.subTest(at=at):
                self.assertEqual(6, result['slots_used'])
                self.assertEqual(correction['id'], result['items'][0]['id'])
                self.assertEqual(correction['ts'], result['items'][0]['ts'])

    def test_imported_utc_z_time_remains_recallable(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        stamp = '2026-11-07T10:01:00Z'
        store.import_rows([{'id': 'legacy-z', 'who': '100000001',
                            'text': '周六计划去公园', 'ts': stamp}])
        result = store.recall('周六计划', now_ts='2026-11-07T10:10:00+00:00')
        self.assertEqual(['legacy-z'], [r['id'] for r in result['items']])
        self.assertEqual(stamp, result['items'][0]['ts'])

    def test_duplicate_words_keep_actual_latest_evidence_across_timezones(self):
        from organs.brain.recall import recall
        rows = [{'id': 'older', 'who': '100000001', 'text': '改去那里吧', 'ts': '2026-11-07T19:00:00+09:00'},
                {'id': 'newer', 'who': '100000001', 'text': '改去那里吧', 'ts': '2026-11-07T10:05:00+00:00'}]
        for query in ('改去那里', '记得我之前说过什么吗'):
            with self.subTest(query=query):
                self.assertEqual(['newer'], [r['id'] for r in recall(query, rows, [])['items']])

    def test_equal_instants_are_stable_and_missing_time_is_not_invented(self):
        from organs.brain.recall import recall
        rows = [{'id': 'first', 'text': '计划甲', 'ts': '2026-11-07T10:00:00+00:00'},
                {'id': 'same', 'text': '计划乙', 'ts': '2026-11-07T19:00:00+09:00'},
                {'id': 'utc_z', 'text': '计划丁', 'ts': '2026-11-07T10:00:00Z'},
                {'id': 'missing', 'text': '计划丙'}]
        result = recall('计划', rows, [])['items']
        self.assertEqual(['first', 'same', 'utc_z', 'missing'], [r['id'] for r in result])
        self.assertEqual('', result[-1]['ts'])

    def test_spare_slots_keep_later_correction_today_and_after_rollover(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        texts = ['周六去河岸图书馆，下午两点出发', '周六去河岸图书馆，带上笔记本',
                 '周六去河岸图书馆，在门口碰面', '计划改了，周六改去松林公园，不去图书馆了']
        for i, text in enumerate(texts):
            store.ingest('100000001', text, ts=f'2026-11-07T10:0{i}:00+00:00')
        for at in ('2026-11-07T10:05:00+00:00', '2026-11-08T10:05:00+00:00'):
            with self.subTest(at=at):
                result = store.recall('周六去河岸图书馆', who='100000001', now_ts=at)
                self.assertEqual(set(texts), {r['display_text'] for r in result['items']})
                self.assertLessEqual(result['slots_used'], result['budget']['slots'])
                correction = next(r for r in result['items'] if '计划改了' in r['display_text'])
                self.assertTrue(correction['borrowed_slot'])
                self.assertEqual('2026-11-07T10:03:00+00:00', correction['ts'])

    def test_same_words_with_changed_referent_survive_recall_and_rollover(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        for stamp, note in (
                ('10:00', '承接理解（可错，非原话）：周六改去河岸图书馆'),
                ('11:00', '承接理解（可错，非原话）：周六再次改去松林公园，原计划取消')):
            store.ingest('3425635473', '改去那里吧',
                         ts='2026-11-07T' + stamp + ':00+00:00', note=note)
        for at in ('2026-11-07T11:01:00+00:00', '2026-11-08T11:01:00+00:00'):
            with self.subTest(at=at):
                result = store.recall('周六去河岸图书馆还是哪里', who='3425635473', now_ts=at)
                self.assertEqual(2, len(result['items']))
                self.assertEqual({'改去那里吧'}, {r['display_text'] for r in result['items']})
                self.assertEqual({'2026-11-07T10:00:00+00:00', '2026-11-07T11:00:00+00:00'},
                                 {r['ts'] for r in result['items']})
                self.assertTrue(any('松林公园' in r['note_display'] for r in result['items']))
                self.assertTrue(all(not r['note_testimony'] for r in result['items']))

    def test_same_words_and_understanding_still_deduplicate(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        for stamp in ('10:00', '11:00'):
            store.ingest('3425635473', '改去那里吧',
                         ts='2026-11-07T' + stamp + ':00+00:00', note='周六去河岸图书馆')
        result = store.recall('周六去哪', who='3425635473', now_ts='2026-11-07T11:01:00+00:00')
        self.assertEqual(1, len(result['items']))
        self.assertEqual('2026-11-07T11:00:00+00:00', result['items'][0]['ts'])

    def test_contextual_understanding_can_retrieve_original_but_is_not_testimony(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        row = store.ingest("3425635473", "改去那里吧", ts="2026-11-07T10:00:00+00:00",
                           note="承接理解（可错，非原话）：周六去河岸图书馆")
        result = store.recall("周六去哪", who="3425635473", now_ts="2026-11-07T10:01:00+00:00")
        item = result["items"][0]
        self.assertEqual(row["entry"]["id"], item["id"])
        self.assertEqual("改去那里吧", item["display_text"])
        self.assertIn("周六去河岸图书馆", item["note_display"])
        self.assertFalse(item["note_testimony"])
        store.retire(item["id"], ts="2026-11-07T10:02:00+00:00")
        self.assertFalse(store.recall("河岸图书馆", now_ts="2026-11-07T10:03:00+00:00")["has_material"])

    def test_today_question_can_recall_established_preference(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "我平时喝的饮料是无糖茶", "2026-11-05T10:00:00+00:00")
        ingest(store, "3425635473", "今晚饮料临时换热可可，明天就照旧", "2026-11-06T10:00:00+00:00")
        result = store.recall("今天是11月7日，我平时的饮料应该选什么？", who="3425635473",
                              now_ts="2026-11-07T10:00:00+00:00")
        self.assertEqual({"我平时喝的饮料是无糖茶", "今晚饮料临时换热可可，明天就照旧"},
                         {row["display_text"] for row in result["items"]})

    def test_past_question_can_recall_todays_correction_about_past(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "上周喝过的饮料是无糖茶", "2026-11-05T10:00:00+00:00")
        ingest(store, "3425635473", "刚才说错了，上周喝过的饮料其实是热可可", "2026-11-07T10:00:00+00:00")
        result = store.recall("上周喝过的饮料是什么？", who="3425635473", now_ts="2026-11-07T10:01:00+00:00")
        self.assertEqual({"today", "longterm"}, {row["source"] for row in result["items"]})
        self.assertTrue(any("其实是热可可" in row["display_text"] for row in result["items"]))

    def test_last_time_can_refer_to_an_earlier_conversation_today(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "我妈住院那事，别到处说", "2026-11-03T10:00:00+00:00")
        for word in ("上次", "之前", "以前"):
            result = store.recall("你%s跟我说你妈住院那事" % word,
                                  now_ts="2026-11-03T10:15:00+00:00")
            self.assertTrue(result["has_material"], word)
            self.assertEqual("我妈住院那事，别到处说", result["items"][0]["text"])

    def test_recall_today_literal_hit_with_source(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "你今天有点不理人", "2026-11-03T10:00:00+00:00")
        result = store.recall("今天不理人", now_ts="2026-11-03T10:01:00+00:00")
        self.assertTrue(result["has_material"])
        self.assertEqual(1, len(result["items"]))
        self.assertEqual("today", result["items"][0]["source"])
        self.assertEqual("3425635473", result["items"][0]["who"])

    def test_recall_past_finds_original_text_in_longterm(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "3425635473", "我妈住院了", "2026-11-03T10:00:00+00:00")
        result = store.recall("以前说过什么我妈", now_ts="2026-11-04T05:00:00+00:00")
        self.assertTrue(result["has_material"])
        self.assertEqual("longterm", result["items"][0]["source"])
        self.assertEqual("我妈住院了", result["items"][0]["text"])

    def test_recall_empty_returns_empty_not_fabricated(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = store.recall("一个从来没出现过的话题", now_ts="2026-11-03T10:00:00+00:00")
        self.assertFalse(result["has_material"])
        self.assertEqual([], result["items"])
        self.assertIn("不硬凑", result["empty_reason"])

    def test_recall_empty_query_is_empty_material(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = store.recall("  ")
        self.assertFalse(result["has_material"])
        self.assertEqual([], result["items"])

    def test_vector_rerank_is_rerank_only_never_gate(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        candidates = [{"id": "a", "text": "候选一"}, {"id": "b", "text": "候选二"}]
        result = store.vector_rerank("随便什么", candidates)
        self.assertFalse(result["connected"])
        self.assertEqual(candidates, result["candidates"], "未接入时原样返回，不删任何候选")
        self.assertIn("永不 gate", result["note"])

    def test_forget_marks_status_and_drops_from_recall(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "3425635473", "我妈住院了", "2026-11-03T10:00:00+00:00")
        entry_id = result["entry"]["id"]
        self.assertEqual("active", result["entry"]["status"])
        store.forget(entry_id, ts="2026-11-03T10:05:00+00:00")
        self.assertFalse(store.recall("我妈", now_ts="2026-11-03T10:06:00+00:00")["has_material"])
        raw = store.today_view("2026-11-03T10:06:00+00:00")
        self.assertEqual(0, len(raw["entries"]), "检索面不再返回已遗忘条目")
        # 物理没删：今日文件里那行还在，只是 status=stale
        import json
        on_disk = json.loads((store.root / "today.json").read_text(encoding="utf-8"))
        self.assertEqual("stale", on_disk["entries"][0]["status"])


if __name__ == "__main__":
    unittest.main()
