import json
from datetime import timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api import api


class ObserveApiTest(unittest.TestCase):
    def test_accounting_only_voice_attempt_is_not_a_synthesis_record(self):
        path=self.root/'mouth/voice.jsonl';path.parent.mkdir(parents=True)
        path.write_text('\n'.join(json.dumps(r) for r in [
            {'at':'2026-11-09T10:00:00+09:00','kind':'attempt','accounting_only':True},
            {'at':'2026-11-09T14:00:00+09:00','kind':'attempt'}]))
        before=path.read_bytes()
        code,data=self.get('?source=voice')
        self.assertEqual(200,code);self.assertEqual(1,data['total'])
        self.assertEqual('2026-11-09T14:00:00+09:00',data['rows'][0]['record']['at'])
        self.assertEqual(before,path.read_bytes())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trace = self.root / "mind/trace"
        self.trace.mkdir(parents=True)
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"QQAGENT_HOME_ROOT": str(self.root), "QQAGENT_HOME": str(self.root)}).start()
        patch("api.observe.paths.REPO_ROOT", self.root).start()

    def get(self, query=""):
        return api.handle("GET", "/observe/records" + query, None, None)

    def test_session_filter_runs_before_pagination_and_preserves_recorded_attribution(self):
        for source, relative, stamp in [('chat','mind/recent_turns.jsonl','ts'),
                                       ('tone','mind/tone_log.jsonl','at'),
                                       ('emotion','mind/event_log.jsonl','at'),
                                       ('model','mind/trace/2026-11-07.jsonl','at')]:
            with self.subTest(source=source):
                path=self.root/relative
                rows=[]
                for n in range(45):
                    session='qq:private:42' if n%2==0 else 'qq:group:42'
                    row={stamp:'2026-11-07T12:00:00+09:00','part':n,'turn_id':('a' if n==44 else 'b')*32,
                         'call_index':n+1,'attempt':1}
                    row.update({'interpretation':{'session_id':session}} if source=='emotion' else {'session_id':session})
                    rows.append(row)
                rows += [{stamp:'2026-11-07T13:00:00+09:00','who':'42','part':'legacy'}]
                path.write_text('\n'.join(json.dumps(r) for r in rows));before=path.read_bytes()
                query='?source='+source+'&date=2026-11-07&session_id=qq:private:42'
                code,data=self.get(query+'&offset=20&limit=2')
                self.assertEqual(code,200)
                self.assertEqual(data['session_id'],'qq:private:42')
                self.assertEqual(data['sessions'],['qq:group:42','qq:private:42'])
                self.assertEqual(data['total'],23)
                self.assertEqual([r['record']['part'] for r in data['rows']],[4,2])
                self.assertTrue(data['has_older'])
                self.assertEqual(self.get(query+'&offset=22')[1]['rows'][0]['record']['part'],0)
                self.assertEqual(self.get(query+'&turn_id='+'a'*32)[1]['total'],1)
                other=query.replace('qq:private:42','qq:group:42')
                self.assertEqual(self.get(other+'&turn_id='+'a'*32)[1]['total'],0)
                missing=self.get(query.replace('qq:private:42','qq:private:99'))[1]
                self.assertEqual(missing['total'],0)
                self.assertEqual(self.get('?source='+source+'&date=2026-11-07')[1]['total'],46)
                self.assertEqual(before,path.read_bytes())
        self.assertEqual(400,self.get('?source=eye&session_id=qq:private:42')[0])
        self.assertEqual(400,self.get('?source=chat&session_id='+('x'*201))[0])

    def test_date_filter_covers_ledgers_in_local_timezone_without_hiding_undated_all_view(self):
        locations={'chat':('mind/recent_turns.jsonl','ts'),'delivery':('channels/onebot/.state/ledger.jsonl','ts'),
                   'decision':('channels/onebot/.state/decisions.jsonl','ts'),
                   'eye':('eye/observations.jsonl','at'),'ear':('ear/observations.jsonl','at'),
                   'voice':('mouth/voice.jsonl','at'),'learning':('knowledge/usage.jsonl','at'),
                   'emotion':('mind/event_log.jsonl','at'),'tone':('mind/tone_log.jsonl','at')}
        with patch('api.observe.day_model.local_tz',return_value=timezone(timedelta(hours=9))), \
             patch('api.observe.knowledge.library_root',return_value=self.root/'knowledge'):
            for source,(relative,key) in locations.items():
                with self.subTest(source=source):
                    path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True)
                    rows=[{key:'2026-11-06T14:59:00Z','part':'previous'},
                          {key:'2026-11-06T15:00:00Z','part':'first','turn_id':'a'*32},
                          {key:'2026-11-07T23:59:00+09:00','part':'last'},
                          {key:'2026-11-08T00:00:00+09:00','part':'next','turn_id':'a'*32},
                          {'part':'legacy'},{key:[],'part':'bad-time'}]
                    path.write_text('\n'.join(json.dumps(row) for row in rows));before=path.read_bytes()
                    code,data=self.get('?source='+source+'&date=2026-11-07&limit=1&offset=1')
                    self.assertEqual(code,200);self.assertEqual(data['total'],2)
                    self.assertEqual(data['rows'][0]['record']['part'],'first')
                    self.assertEqual(data['undated_records'],2);self.assertEqual(data['invalid_lines'],0)
                    self.assertEqual(data['dates'],['2026-11-08','2026-11-07','2026-11-06'])
                    self.assertEqual(self.get('?source='+source)[1]['total'],6)
                    self.assertEqual(self.get('?source='+source+'&date=2026-11-05')[1]['rows'],[])
                    if source in ('chat','emotion','tone'):
                        page=self.get('?source='+source+'&date=2026-11-06&turn_id='+'a'*32)[1]
                        self.assertEqual(page['total'],2)
                    self.assertEqual(path.read_bytes(),before)

    def test_group_participation_counts_segments_not_bubbles_and_separates_sources(self):
        state = self.root / 'channels/onebot/.state'
        state.mkdir(parents=True)
        config = self.root / 'channels/onebot/data'
        config.mkdir()
        (config / 'gate.json').write_text('{"chitchatSampleMod":10}')
        rows = []
        def add(rule, reason='sent', target='group:7', source='inbound', at='2026-11-07T10:00:00+09:00'):
            rows.append({'ts':'2026-11-08T01:00:00+09:00', 'kind':'decision', 'machine':{
                'scene':'group' if target.startswith('group:') else 'private', 'target':target,
                'source':source, 'direct':{'rule':rule}, 'reason_code':reason,
                'decided':'sent' if reason in ('sent','replaced_too_long') else 'silent',
                'batch':{'id':'b1', 'msgs':6, 'at':at}, 'style':{'split':[1,2,3]}}})
        add('chitchat')
        for _ in range(4): add('chitchat','chitchat_skipped')
        add('chitchat','superseded')  # 没有一泡发出，既不计参与也不是发送失败。
        add('continuation','replaced_too_long')  # 正在处理的定向对话补充，不算主动插话。
        add('quote','outbound_failed')
        add('recalled')  # 旧规则不猜为定向或主动。
        add(None,'media_only')
        add(None,source='proactive')
        add('private',target='user:42')
        add('at',target='group:8',at='2026-11-06T15:01:00Z')  # 本地7日。
        add('at',target='group:8',at='2026-11-06T14:59:00Z')  # 本地6日。
        p = state / 'decisions.jsonl'
        p.write_text('\n'.join(json.dumps(r) for r in rows)+'\nnot json\n[]\n')
        before = p.read_bytes()
        with patch('organs.day.model.local_tz', return_value=timezone(timedelta(hours=9))):
            status, data = api.handle('GET','/observe/participation?date=2026-11-07',None,None)
        self.assertEqual(200,status)
        self.assertEqual(10,data['sample_every'])
        self.assertEqual(2,data['invalid_lines'])
        a,b = data['groups']
        self.assertEqual('group:7',a['target'])
        self.assertEqual((10,3,6,1,2,1,2,1,1,1),tuple(a[k] for k in (
            'segments','sent','spontaneous','spontaneous_sent','directed','directed_sent',
            'other','other_sent','unconfirmed','proactive_sent')))
        self.assertEqual((10.0,50.0,30.0),tuple(a[k] for k in ('spontaneous_pct','directed_pct','total_pct')))
        self.assertEqual((1,1), (b['segments'],b['sent']))
        self.assertEqual(11,data['total']['segments'])
        self.assertEqual(before,p.read_bytes())

    def test_participation_empty_invalid_and_unknown_configuration_are_explicit(self):
        route='/observe/participation'
        status,data=api.handle('GET',route+'?date=2026-11-07',None,None)
        self.assertEqual(200,status)
        self.assertFalse(data['exists'])
        self.assertEqual([],data['groups'])
        self.assertIsNone(data['total']['total_pct'])
        self.assertIsNone(data['sample_every'])
        self.assertTrue(data['config_error'])
        for invalid in ('../bad','2026-02-30'):
            self.assertEqual(400,api.handle('GET',route+'?date='+invalid,None,None)[0])
        self.assertEqual(405,api.handle('POST',route,{},None)[0])
        state = self.root / 'channels/onebot/.state'
        state.mkdir(parents=True)
        (state / 'decisions.jsonl').write_text('\n'.join(json.dumps(r) for r in (
            {'machine':[]}, {'machine':{'scene':'group','target':'group:7','source':'inbound',
              'batch':{'at':'bad'},'direct':{'rule':'at'},'decided':'sent','reason_code':'sent'}})))
        status,data=api.handle('GET',route+'?date=2026-11-07',None,None)
        self.assertEqual(200,status)
        self.assertTrue(data['exists'])
        self.assertEqual(2,data['invalid_lines'])

    def test_silence_reasons_use_final_structured_result_without_guessing_or_sharing(self):
        state=self.root/'channels/onebot/.state'
        state.mkdir(parents=True)
        cases=[('group:7','inbound','core_empty','rest-deep'),
               ('group:7','inbound','core_empty','rest-asleep'),
               ('group:7','inbound','core_empty','rest-holding'),
               ('group:7','inbound','core_empty',None),
               ('group:7','inbound','chitchat_skipped',None),
               ('group:8','inbound','core_error',None),
               ('group:8','inbound','outbound_failed',None),
               ('group:8','inbound','core_empty','rest-deep'),
               ('group:8','inbound','sent','rest-deep'),
               ('group:8','proactive','outbound_failed',None)]
        rows=[{'ts':'2026-11-07T12:00:00+09:00','human':'没回：她这轮不想说',
               'machine':{'scene':'group','target':target,'source':source,'reason_code':code,'reason':reason,
              'decided':'sent' if code=='sent' else 'silent','direct':{'rule':'chitchat'},
              'batch':{'id':'b'+str(i),'msgs':3,'at':'2026-11-07T12:00:00+09:00'}}}
              for i,(target,source,code,reason) in enumerate(cases)]
        path=state/'decisions.jsonl'
        path.write_text('\n'.join(json.dumps(r) for r in rows))
        before=path.read_bytes()
        status,data=api.handle('GET','/observe/participation?date=2026-11-07',None,None)
        self.assertEqual(200,status)
        self.assertEqual({'rest-deep':2,'rest-asleep':1,'rest-holding':1,'core_empty':1,
                          'chitchat_skipped':1,'core_error':1,'outbound_failed':1},data['total']['silent_reasons'])
        self.assertEqual(5,sum(data['groups'][0]['silent_reasons'].values()))
        self.assertEqual(3,sum(data['groups'][1]['silent_reasons'].values()))
        self.assertEqual(8,data['total']['segments']-data['total']['sent'])
        self.assertEqual(before,path.read_bytes())
        _,timeline=api.handle('GET','/observe/activity?source=decision&date=2026-11-07',None,None)
        sleeping=[r for r in timeline['rows'] if r.get('reason')=='rest-deep' and r.get('decision')=='silent']
        self.assertEqual(2,len(sleeping))
        self.assertTrue(all(r['title']=='彻底睡着，未回复' for r in sleeping))
        self.assertEqual(before,path.read_bytes())

    def test_full_reasoning_and_prompt_preserved_in_newest_first_page(self):
        p = self.trace / "2026-11-07.jsonl"
        records = [{"at": str(n), "messages": [{"role": "system", "content": "完整上下文" * 5000}],
                    "raw": json.dumps({"choices": [{"message": {"reasoning_content": "模型推理" * 5000}}]})}
                   for n in range(5)]
        p.write_text("\n".join(json.dumps(row) for row in records))
        original = p.read_bytes()
        status, result = self.get("?limit=2&offset=1")
        self.assertEqual(status, 200)
        self.assertEqual([row["record"] for row in result["rows"]], [records[3], records[2]])
        self.assertEqual([row["line"] for row in result["rows"]], [4, 3])
        self.assertTrue(result["has_older"])
        self.assertEqual(p.read_bytes(), original)

    def test_dates_missing_and_corrupt_records_are_reported(self):
        (self.trace / "2026-11-06.jsonl").write_text('{"reply":"older"}\n')
        (self.trace / "2026-11-07.jsonl").write_text('{"reply":"newer"}\nnot-json\n[]\n')
        status, result = self.get()
        self.assertEqual(result["dates"], ["2026-11-07", "2026-11-06"])
        self.assertEqual(result["invalid_lines"], 2)
        self.assertEqual(result["total"], 1)
        self.assertEqual(self.get("?date=2026-11-06")[1]["rows"][0]["record"]["reply"], "older")
        missing = self.get("?date=2026-11-05")[1]
        self.assertFalse(missing["exists"])
        self.assertEqual(missing["rows"], [])

    def test_chat_and_protocol_ledgers_remain_separate(self):
        state = self.root / "channels/onebot/.state"
        state.mkdir(parents=True)
        (self.root / "mind/recent_turns.jsonl").write_text('{"said":"问题","reply":"生成稿"}\n')
        (state / "ledger.jsonl").write_text('{"kind":"uncertain","text":"最终文本"}\n')
        (state / "decisions.jsonl").write_text('{"human":"没有发言"}\n')
        for source, key, value in (("chat", "reply", "生成稿"), ("delivery", "kind", "uncertain"), ("decision", "human", "没有发言")):
            status, result = self.get("?source=" + source)
            self.assertEqual(status, 200)
            self.assertEqual(result["rows"][0]["record"][key], value)

    def test_chat_delivery_uses_only_matching_receipts_and_keeps_raw_records(self):
        deliveries = self.root / 'mind/deliveries'
        deliveries.mkdir()
        records = []
        for index, receipt in enumerate((
            {'confirmed':True, 'text':'实际确认文字'},
            {'confirmed':True, 'text':'', 'stop_reason':'superseded'},
            {'confirmed':False},
            None,
            'broken json',
            {'confirmed':True, 'text':'另一轮文字', 'turn_id':'f'*32},
        ), 1):
            turn = format(index, '032x')
            records.append({'turn_id':turn, 'said':'虚构问题', 'reply':'原始底稿'})
            if receipt is not None:
                (deliveries / (turn+'.json')).write_text(receipt if isinstance(receipt,str) else
                    json.dumps({'turn_id':turn, **receipt}, ensure_ascii=False))
        records += [{'said':'没有编号的旧记录','reply':'旧底稿'},
                    {'turn_id':'../../not-a-receipt', 'said':'非法旧编号','reply':''}]
        path = self.root / 'mind/recent_turns.jsonl'
        path.write_text('\n'.join(json.dumps(row,ensure_ascii=False) for row in records))
        before = {p:p.read_bytes() for p in (self.root/'mind').rglob('*') if p.is_file()}
        status, data = self.get('?source=chat')
        self.assertEqual(200, status)
        rows = list(reversed(data['rows']))
        self.assertEqual(records, [row['record'] for row in rows])
        self.assertEqual(['confirmed','confirmed','pending','unrecorded','unavailable','unavailable','unrecorded','unrecorded'],
                         [row['delivery']['state'] for row in rows])
        self.assertEqual('实际确认文字', rows[0]['delivery']['text'])
        self.assertEqual('', rows[1]['delivery']['text'])
        self.assertEqual('superseded', rows[1]['delivery'].get('stop_reason'))
        self.assertNotIn('text', rows[2]['delivery'])
        self.assertNotIn('另一轮文字', json.dumps(data,ensure_ascii=False))
        self.assertEqual(before, {p:p.read_bytes() for p in (self.root/'mind').rglob('*') if p.is_file()})
        status, page = self.get('?source=chat&offset=7&limit=1')
        self.assertEqual([rows[0]], page['rows'])

    def test_learning_usage_is_read_without_rewriting_or_mixing_chat(self):
        library = self.root / "knowledge"
        library.mkdir()
        record = {"kind": "offered_to_model", "session_id": "private:1", "cards": [{"card_id": "verified"}]}
        path = library / "usage.jsonl"
        path.write_text(json.dumps(record) + "\n")
        original = path.read_bytes()
        with patch("api.observe.knowledge.library_root", return_value=library):
            status, result = self.get("?source=learning")
        self.assertEqual(status, 200)
        self.assertEqual(result["rows"][0]["record"], record)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.get("?source=chat")[1]["rows"], [])

    def test_invalid_inputs_and_write_methods_rejected(self):
        for query in ("?source=secret", "?date=../../.env", "?date=2026-02-30", "?limit=101", "?offset=-1", "?offset=no"):
            self.assertEqual(self.get(query)[0], 400, query)
        self.assertEqual(api.handle("POST", "/observe/records", {}, None)[0], 405)

    def test_emotion_and_tone_show_actual_distinct_read_only_ledgers(self):
        event = {"kind": "insult", "applied": False, "reason": "未投递", "note": "规则命中：笨"}
        tone = {"mood_tier": "平", "max_chars": 180, "truncated": False}
        for source, filename, record in (("emotion", "event_log.jsonl", event), ("tone", "tone_log.jsonl", tone)):
            path = self.root / "mind" / filename
            path.write_text(json.dumps(record) + "\n")
            original = path.read_bytes()
            status, result = self.get("?source=" + source)
            self.assertEqual(status, 200)
            self.assertEqual(result["rows"][0]["record"], record)
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.get("?source=chat")[1]["rows"], [])

    def test_turn_filter_on_chat_emotion_and_tone_keeps_only_exact_rows(self):
        turn = 'a' * 32
        for source, filename in [('chat', 'recent.jsonl'), ('emotion', 'event_log.jsonl'), ('tone', 'tone_log.jsonl')]:
            from organs.mind import recent
            path = recent.path_of() if source == 'chat' else self.root / 'mind' / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = [{'turn_id':turn, 'part':1}, {'part':'legacy'},
                    {'turn_id':'b'*32, 'part':'other'}, {'turn_id':turn, 'part':2}]
            path.write_text('\n'.join(json.dumps(r) for r in rows))
            before = path.read_bytes()
            code, data = self.get('?source='+source+'&turn_id='+turn+'&limit=1')
            self.assertEqual(code, 200, source)
            self.assertEqual(data['total'], 2)
            self.assertEqual(data['rows'][0]['record']['part'], 2)
            self.assertTrue(data['has_older'])
            self.assertEqual(self.get('?source='+source+'&turn_id='+turn+'&offset=1')[1]['rows'][0]['record']['part'], 1)
            self.assertEqual(self.get('?source='+source+'&turn_id='+'c'*32)[1]['total'], 0)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.get('?source=eye&turn_id='+turn)[0], 400)

    def test_offset_beyond_end_returns_empty_page(self):
        (self.trace / "2026-11-07.jsonl").write_text('{"reply":"hello"}\n')
        result = self.get("?offset=20")[1]
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["has_older"])

    def test_turn_filter_spans_dates_and_orders_calls_and_retries_not_finish_time(self):
        turn='a'*32
        rows=[{'turn_id':turn,'call_index':2,'attempt':1,'reply':'后调用先结束'},
              {'turn_id':'b'*32,'call_index':1,'attempt':1},
              {'reply':'旧记录不可猜归属'}, {'turn_id':turn,'call_index':'损坏','attempt':1}]
        (self.trace/'2026-11-06.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
        (self.trace/'2026-11-07.jsonl').write_text('\n'.join(json.dumps(r) for r in [
            {'turn_id':turn,'call_index':1,'attempt':1,'error':'timeout'},
            {'turn_id':turn,'call_index':1,'attempt':2,'reply':'较晚返回'}]))
        before={p:p.read_bytes() for p in self.trace.iterdir()}
        code,data=self.get('?turn_id='+turn+'&date=2026-11-06&limit=2')
        self.assertEqual(200,code)
        self.assertEqual(3,data['total'])
        self.assertEqual(1,data['invalid_lines'])
        self.assertEqual([(1,1),(1,2)],[(r['record'].get('call_index'),r['record'].get('attempt')) for r in data['rows']])
        self.assertTrue(data['has_older'])
        last=self.get('?turn_id='+turn+'&offset=2')[1]['rows'][0]
        self.assertEqual(2,last['record']['call_index'])
        self.assertEqual('2026-11-06',last['date'])
        self.assertEqual(before,{p:p.read_bytes() for p in self.trace.iterdir()})
        self.assertEqual(400,self.get('?turn_id=../../env')[0])


    def test_rice_is_read_only_and_rejects_invalid_date(self):
        status, data = api.handle("GET", "/observe/rice?date=2026-11-07", None, None)
        self.assertEqual(status, 200)
        self.assertEqual(data['daily']['calls'], 0)
        self.assertFalse(data['enabled'])
        self.assertTrue(data['balance']['stale'])
        self.assertEqual(api.handle("GET", "/observe/rice?date=bad", None, None)[0], 400)
        self.assertEqual(api.handle("POST", "/observe/rice", {}, None)[0], 405)

    def ledger(self, rows, trailing=""):
        path = self.root / "hand"
        path.mkdir(parents=True, exist_ok=True)
        target = path / "ledger.jsonl"
        target.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n" + trailing)
        return target

    def test_lookup_account_reads_four_states_from_hand_ledger_and_leaves_unknowns_blank(self):
        def entry(ledger_id, state, code, params, *, at="2026-11-07T12:00:00+09:00", elapsed=0, action="lookup"):
            return {"ledger_id": ledger_id, "at": at, "who": "42", "action": action,
                    "params": params, "state": state, "code": code, "elapsed_ms": elapsed,
                    "origin": code, "grant_id": None}
        rows = [
            entry("lg-reserved", "reserved", "OK", {"count": 5, "query_sha256": "aa", "query_chars": 4}),
            entry("lg-not", "refused", "MISSING_QUERY",
                  {"calls": 0, "state": "error", "results_count": 0, "http": None, "landed": False}, elapsed=2),
            entry("lg-failed", "failed", "LOOKUP_TIMEOUT",
                  {"calls": 1, "state": "error", "results_count": 0, "http": None, "landed": False}, elapsed=15000),
            entry("lg-unsaved", "done", "OK",
                  {"calls": 1, "state": "ok", "results_count": 3, "http": 200, "landed": False}, elapsed=820),
            entry("lg-landed", "done", "OK",
                  {"calls": 1, "state": "ok", "results_count": 2, "http": 200, "landed": True,
                   "card_id": "web-1", "gate": "privacy", "action": "search"}, elapsed=700),
            entry("lg-task", "done", "OK", {"calls": 9}, action="add_task"),
            entry("lg-undated", "done", "OK",
                  {"calls": 1, "state": "ok", "results_count": 1, "landed": False}, at=""),
        ]
        target = self.ledger(rows, trailing="not json\n")
        before = target.read_bytes()
        status, data = self.get("?source=lookup")
        self.assertEqual(200, status)
        self.assertEqual(("lookup", 6, 1, 1), (data["source"], data["total"], data["invalid_lines"],
                                                data["undated_records"]))
        # 账是只增的：新的一行在最后，观察页先给最近的。
        self.assertEqual(["lg-undated", "lg-landed", "lg-unsaved", "lg-failed", "lg-not", "lg-reserved"],
                         [item["ledger_id"] for item in data["rows"]])
        seen = {item["ledger_id"]: item["record"] for item in data["rows"]}
        self.assertEqual({"lg-not": "not_requested", "lg-failed": "requested_failed",
                          "lg-unsaved": "looked_up_not_saved", "lg-landed": "landed"},
                         {key: seen[key]["outcome"] for key in ("lg-not", "lg-failed", "lg-unsaved", "lg-landed")})
        self.assertEqual("已查到未落盘", seen["lg-unsaved"]["outcome_label"])
        self.assertEqual({"gate": None, "action": None, "reason": None, "status": "ok", "code": "OK",
                          "http": 200, "results_count": 3, "elapsed_ms": 820, "calls": 1, "landed": False,
                          "card_id": None, "turn_id": None, "outcome": "looked_up_not_saved",
                          "billed_calls": None, "billing_status": None, "currency": None, "cost": None,
                          "price_url": None, "query": None, "query_sha256": None,
                          "outcome_label": "已查到未落盘"}, seen["lg-unsaved"])
        self.assertEqual(("privacy", "search", "web-1", "landed"),
                         (seen["lg-landed"]["gate"], seen["lg-landed"]["action"],
                          seen["lg-landed"]["card_id"], seen["lg-landed"]["outcome"]))
        # 老账行缺字段＝未知：留空，不拿默认值凑成某一态。
        self.assertEqual((None, None, None), (seen["lg-reserved"]["outcome"],
                                             seen["lg-reserved"]["outcome_label"],
                                             seen["lg-reserved"]["calls"]))
        self.assertNotIn("lg-task", seen, "别的动作不进检索账")
        with patch("api.observe.day_model.local_tz", return_value=timezone(timedelta(hours=9))):
            dated = self.get("?source=lookup&date=2026-11-07")[1]
            self.assertEqual(5, dated["total"])
            self.assertEqual(["2026-11-07"], dated["dates"])
        page = self.get("?source=lookup&limit=2&offset=1")[1]
        self.assertEqual(["lg-landed", "lg-unsaved"], [item["ledger_id"] for item in page["rows"]])
        self.assertTrue(page["has_older"])
        self.assertEqual(before, target.read_bytes())
        for bad in ("?source=lookup&date=bad", "?source=lookup&limit=101", "?source=lookup&offset=-1",
                    "?source=lookup&turn_id=../../env", "?source=lookup&session_id=qq:private:1"):
            self.assertEqual(400, self.get(bad)[0], bad)
        self.assertEqual(405, api.handle("POST", "/observe/records", {}, None)[0])

    def test_lookup_account_keeps_privacy_block_to_reason_and_turn_only(self):
        secret, turn = "我的手机号是 13800000000", "a" * 32
        rows = [{"ledger_id": "lg-private", "at": "2026-11-07T12:00:00+09:00", "who": "42",
                 "action": "lookup", "state": "refused", "code": "PRIVACY_BLOCKED",
                 "elapsed_ms": 0, "origin": "privacy", "grant_id": None,
                 "params": {"gate": "privacy", "action": "deny",
                            "reason": "query 含号码，按门0 不查", "turn_id": turn,
                            "query": secret, "query_sha256": "deadbeefdeadbeef",
                            "calls": 0, "results_count": 0, "state": "error", "landed": False}},
                {"ledger_id": "lg-normal", "at": "2026-11-07T12:05:00+09:00", "who": "42",
                 "action": "lookup", "state": "done", "code": "OK", "elapsed_ms": 300,
                 "origin": "OK", "grant_id": None,
                 "params": {"gate": "privacy", "action": "search", "turn_id": "b" * 32,
                            "calls": 1, "state": "ok", "results_count": 1, "http": 200,
                            "landed": False}}]
        target = self.ledger(rows)
        before = target.read_bytes()
        status, data = self.get("?source=lookup")
        self.assertEqual(200, status)
        seen = {item["ledger_id"]: item["record"] for item in data["rows"]}
        self.assertEqual({"gate": "privacy", "action": "deny", "reason": "query 含号码，按门0 不查",
                          "turn_id": turn, "redacted": True}, seen["lg-private"])
        body = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("13800000000", body)
        self.assertNotIn("deadbeefdeadbeef", body)
        self.assertEqual(("privacy", "search", "b" * 32), (seen["lg-normal"]["gate"],
                                                           seen["lg-normal"]["action"],
                                                           seen["lg-normal"]["turn_id"]))
        self.assertEqual(["lg-private"], [item["ledger_id"] for item in
                                          self.get("?source=lookup&turn_id=" + turn)[1]["rows"]])
        self.assertEqual(0, self.get("?source=lookup&turn_id=" + "c" * 32)[1]["total"])
        self.assertEqual(before, target.read_bytes())
