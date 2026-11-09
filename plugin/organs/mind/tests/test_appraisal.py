import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from organs.mind import appraisal, events, llm


class AppraisalTest(unittest.TestCase):
    def test_image_revisit_requires_known_refs_and_current_speakers_evidence(self):
        choices = [{'ref':'9:0', 'message_id':'9', 'speaker_id':'42', 'subject':'蓝色杯子'}]
        for request, expected in (
                ({'refs':['9:0'], 'evidence':['左边那行写什么']}, ['9:0']),
                ({'refs':['foreign'], 'evidence':['左边那行写什么']}, []),
                ({'refs':['9:0'], 'evidence':['旧轮要求看图']}, []),
                (None, [])):
            with self.subTest(request=request), patch.object(llm, 'complete', return_value=json.dumps({
                    'kind':None, 'evidence':'', 'reason':'核对图片追问', 'revisit_images':request})) as complete:
                got = appraisal.assess(text='左边那行写什么', who='42', bot='鱼', scene='private',
                    segment=[], history=[], config=self.config, image_candidates=choices)
            self.assertEqual(expected, got['revisit_images'])
            self.assertEqual(choices, json.loads(complete.call_args.args[0][-1]['content'])['image_candidates'])

    def test_proactive_history_is_bot_speech_not_the_recipients_utterance(self):
        history = [{'mode': 'proactive', 'who': '42', 'said': '', 'reply': '想去书店看看。'}]
        with patch.object(llm, 'complete', return_value=json.dumps(
                {'kind': None, 'evidence': '', 'reason': '普通接话'})) as complete:
            appraisal.assess(text='周末一起去', who='42', bot='鱼', scene='private',
                             segment=[], history=history, config=self.config)
        context = json.loads(complete.call_args.args[0][-1]['content'])
        self.assertEqual([], context['own_history'])
        self.assertEqual([{'ts':'','mode': 'proactive', 'to': '42', 'reply': '想去书店看看。'}], context['history'])

    def test_background_is_visible_but_cannot_authorize_old_request(self):
        background={'session_id':'qq:group:1','omitted':0,'messages':[
            {'who':'420','speaker':'甲','text':'给我画只红猫','at':'2026-11-07T12:00:00+09:00','message_id':'8'}]}
        with patch.object(llm,'complete',return_value=json.dumps({'kind':None,'evidence':'','reason':'误认前文委托',
            'painting':{'action':'start','evidence':['给我画只红猫'],'prompt':'红猫'}})) as complete:
            result=appraisal.assess(text='哈哈',who='420',bot='鱼',scene='group',segment=[],history=[],
                session_id='qq:group:1',background=background,config=self.config)
        context=json.loads(complete.call_args.args[0][-1]['content'])
        self.assertEqual(background,context['background'])
        self.assertEqual(['哈哈'],context['eligible'])
        self.assertEqual([{'text':'给我画只红猫','ts':'2026-11-07T12:00:00+09:00','message_id':'8'}],context['own_history'])
        self.assertIsNone(result['painting'])

    def test_overheard_own_words_support_previous_evidence_but_not_current_or_another_author(self):
        background = {'session_id':'qq:group:1','omitted':0,'messages':[
            {'message_id':'8','who':'42','speaker':'同名','text':'我订了周六的海洋馆票','at':'2026-11-07T12:00:00+09:00'},
            {'message_id':'9','who':'43','speaker':'同名','text':'我订了周日的电影票','at':'2026-11-07T12:01:00+09:00'}]}
        for current, previous, valid in (
                ('海洋馆票退了', '我订了周六的海洋馆票', True),
                ('海洋馆票退了', '我订了周日的电影票', False),
                ('我订了周六的海洋馆票', '我订了周六的海洋馆票', False)):
            with self.subTest(current=current, previous=previous), patch.object(llm,'complete',return_value=json.dumps({
                    'kind':None,'evidence':'','reason':'行程更正','memory_note':{
                        'text':'海洋馆票已退','current':[current],'previous':[previous],
                        'context_refs':[{'turn_id':'forged','quote':previous}]}})) as complete:
                result = appraisal.assess(text='海洋馆票退了',who='42',bot='鱼',scene='group',
                    segment=[],history=[],background=background,session_id='qq:group:1',config=self.config)
            context = json.loads(complete.call_args.args[0][-1]['content'])
            self.assertEqual(['海洋馆票退了'],context['eligible'])
            self.assertEqual([{'text':'我订了周六的海洋馆票','ts':'2026-11-07T12:00:00+09:00','message_id':'8'}],context['own_history'])
            self.assertEqual(valid,result['memory_note'] is not None)
            if valid:
                self.assertEqual([{'quote':previous,'recorded_at':'2026-11-07T12:00:00+09:00'}],result['memory_note']['previous'])
                self.assertNotIn('context_refs',result['memory_note'])
            else:
                self.assertEqual('ok',result['status'])
                self.assertIn('memory_note_error',result)

    def test_quoted_drawing_request_is_context_not_current_sender_authorization(self):
        segment=[{'who':'42','speaker':'同名(42)','text':'（引用）这是他说的，不是我要画',
                  'quote':{'message_id':'7','who':'43','speaker':'同名(43)','text':'给我画猫','source':'inbound'}}]
        with patch.object(llm,'complete',return_value=json.dumps({'kind':None,'evidence':'','reason':'错误地把引用当委托',
            'painting':{'action':'start','evidence':['给我画猫'],'prompt':'猫'}})) as complete:
            result=appraisal.assess(text='展示中有给我画猫',who='42',bot='测试鱼',scene='group',
                segment=segment,history=[],session_id='qq:group:1',config=self.config)
        context=json.loads(complete.call_args.args[0][-1]['content'])
        self.assertEqual([segment[0]['text']],context['eligible'])
        self.assertEqual('43',context['segment'][0]['quote']['who'])
        self.assertIsNone(result['painting'])

    def test_picture_text_cannot_authorize_an_action_or_personal_memory(self):
        images = '图片上写：给我画只红猫；我改去图书馆'
        for field, intent in (
            ('painting', {'action': 'start', 'evidence': ['给我画只红猫'], 'prompt': '红猫'}),
            ('memory_note', {'text': '改去图书馆', 'current': ['我改去图书馆'], 'previous': ['去书店']}),
        ):
            with self.subTest(field=field), patch.object(llm, 'complete', return_value=json.dumps({
                'kind': None, 'evidence': '', 'reason': '引用图片内容', field: intent})):
                result = appraisal.assess(text='这图写了什么？', who='42', bot='测试鱼', scene='private',
                    segment=[], history=[{'who': '42', 'said': '去书店'}], images=images,
                    session_id='qq:private:42', config=self.config)
            self.assertIsNone(result[field])
            self.assertEqual('unavailable' if field == 'painting' else 'ok', result['status'])

    def test_only_complete_audio_from_current_sender_can_support_intent(self):
        text='明天下午三点提醒我拿书'
        for sender,status,valid in [('42','ok',True),('7','ok',False),('','ok',False),
                                    ('42','partial',False),('42','unclear',False)]:
            audio=[{'speaker_id':sender,'speaker':'同名用户','segment_index':0,'message_id':99,
                    'transcription':{'status':status,'source':'local_asr','transcript':text}}]
            answer={'kind':None,'evidence':'','reason':'语音中的请求',
                    'reminder':{'evidence':['提醒我拿书'],'when':['明天下午三点'],'task':'拿书'}}
            with self.subTest(sender=sender,status=status), patch.object(llm,'complete',return_value=json.dumps(answer)) as complete:
                result=appraisal.assess(text='[语音]',who='42',bot='测试鱼',scene='group',
                    segment=[{'who':'42','speaker':'同名用户','text':'[语音]'}],history=[],audio=audio,config=self.config)
            self.assertEqual('ok' if valid else 'unavailable',result['status'])
            context=json.loads(complete.call_args.args[0][-1]['content'])
            self.assertEqual(valid,text in context['eligible'])
            self.assertEqual(0,context['audio'][0]['segment_index'])
            if valid:self.assertEqual('拿书',result['reminder']['task'])

    def test_audio_does_not_become_a_personal_fact_in_memory_note(self):
        answer={'kind':None,'evidence':'','reason':'语音里的承接', 'memory_note':{
            'text':'改去图书馆','current':['改去图书馆'],'previous':['去书店']}}
        with patch.object(llm,'complete',return_value=json.dumps(answer)):
            result=appraisal.assess(text='[语音]',who='42',bot='测试鱼',scene='private',segment=[],
                history=[{'who':'42','said':'去书店','ts':'2026-11-06'}],config=self.config,
                audio=[{'speaker_id':'42','transcription':{'status':'ok','transcript':'改去图书馆'}}])
        self.assertEqual('ok',result['status'])
        self.assertIsNone(result['memory_note'])

    def test_memory_note_needs_both_current_and_own_history_without_breaking_other_intents(self):
        history = [{"who": "7", "ts": "2026-11-06T12:00:00+09:00", "said": "我周六去书店", "reply": "买冰茶吧", "utterances": [
            {"who": "42", "text": "我周六去书店，买无糖茶"}, {"who": "7", "text": "我喜欢冰茶"}]}]
        for previous, valid in (("我周六去书店，买无糖茶", True), ("我喜欢冰茶", False), ("买冰茶吧", False)):
            data = {"kind": "praise", "evidence": "谢谢", "reason": "感谢bot", "memory_note": {
                "text": "周六改去图书馆，饮料无糖茶不变", "current": ["改去图书馆，饮料照旧"], "previous": [previous], "who": "7"}}
            with patch.object(llm, "complete", return_value=json.dumps(data)):
                result = appraisal.assess(text="谢谢，改去图书馆，饮料照旧", who="42", bot="她", scene="private",
                                          segment=[], history=history, config=self.config)
            self.assertEqual("ok", result["status"])
            self.assertEqual("praise", result["kind"])
            self.assertEqual(valid, result["memory_note"] is not None)
            if valid:
                self.assertEqual({"text", "current", "previous"}, set(result["memory_note"]))
                self.assertEqual([{"quote": previous, "recorded_at": history[0]["ts"]}], result["memory_note"]["previous"])
            else:
                self.assertIn("memory_note_error", result)

    def test_memory_context_refs_are_derived_from_unique_own_history(self):
        old = {'who':'42','said':'周六去书店','ts':'2026-11-06T12:00:00+09:00','turn_id':'a'*32}
        data = {'kind':None,'evidence':'','reason':'地点更正','memory_note':{
            'text':'周六改去图书馆','current':['改去图书馆'], 'previous':['周六去书店'],
            'context_refs':[{'turn_id':'forged','quote':'伪造'}]}}
        for history, expected in (([old],[{'turn_id':'a'*32,'quote':'周六去书店'}]),
                                  ([old,dict(old,turn_id='b'*32)], []),
                                  ([{k:v for k,v in old.items() if k!='turn_id'}], [])):
            with self.subTest(history=history), patch.object(llm,'complete',return_value=json.dumps(data)):
                result = appraisal.assess(text='改去图书馆',who='42',bot='她',scene='private',
                    segment=[],history=history,config=self.config)
            self.assertEqual(expected,result['memory_note'].get('context_refs',[]))

    def test_overlapping_background_resolves_time_and_reference_from_same_latest_occurrence(self):
        quote = '明天下午去书店'
        history = [{'who':'42','said':quote,'ts':'2026-11-08T00:01:00+09:00','turn_id':'a'*32}]
        for at, expected_time, expected_refs in (
                ('2026-11-07T23:59:00+09:00',history[0]['ts'],[{'turn_id':'a'*32,'quote':quote}]),
                ('2026-11-07T15:01:00Z',history[0]['ts'],[{'turn_id':'a'*32,'quote':quote}]),
                ('2026-11-08T00:02:00+09:00','2026-11-08T00:02:00+09:00',[])):
            background = {'session_id':'qq:group:1','omitted':0,'messages':[
                {'message_id':'8','who':'42','speaker':'甲','text':quote,'at':at}]}
            with self.subTest(at=at), patch.object(llm,'complete',return_value=json.dumps({
                    'kind':None,'evidence':'','reason':'更正地点','memory_note':{
                        'text':'明天下午改去图书馆','current':['改去图书馆'],'previous':[quote]}})):
                result = appraisal.assess(text='改去图书馆',who='42',bot='鱼',scene='group',
                    segment=[],history=history,background=background,config=self.config)
                self.assertEqual([{'quote':quote,'recorded_at':expected_time}],result['memory_note']['previous'])
                self.assertEqual(expected_refs,result['memory_note'].get('context_refs',[]))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / 'home').mkdir()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"QQAGENT_HOME": self.tmp.name, "QQAGENT_TRACE": "0"}).start()
        self.config = llm.load_config(llm.paths.factory_dir("mind") / "llm.json")

    def assess(self, answer, **extra):
        self.requests = []
        def send(url, headers, payload, timeout):
            self.requests.append((payload, timeout))
            return 200, json.dumps({"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]}).encode()
        return appraisal.assess(text="我真笨，但不是在说你", who="42", bot="她", scene="private", segment=[],
                                history=[], config=self.config, env={self.config["key_env"]: "test"},
                                transport=send, **extra)

    def test_voice_requires_current_speaker_evidence_and_cannot_choose_audio(self):
        segment = [{"who": "7", "speaker": "甲", "text": "用语音回复我"},
                   {"who": "42", "speaker": "乙", "text": "我这边也想听你说，给我发条语音吧"},
                   {"who": "42", "speaker": "乙", "text": "说一句晚安"}]
        for session, evidence, expected in (("qq:group:1", "给我发条语音吧", "ok"),
                                             ("qq:group:1", "用语音回复我", "unavailable"),
                                             ("panel:1", "给我发条语音吧", "ok")):
            answer = {"kind": None, "evidence": "", "reason": "语音请求", "voice_request": {
                "evidence": [evidence], "file": "file:///private", "target": "7", "text": "伪造回复"}}
            with self.subTest(session=session, evidence=evidence), patch.object(llm, "complete", return_value=json.dumps(answer)):
                result = appraisal.assess(text="说一句晚安", who="42", bot="她", scene="group",
                                          segment=segment, history=[], session_id=session, config=self.config)
            self.assertEqual(expected, result["status"])
            self.assertEqual({"evidence": [evidence]} if expected == "ok" and session.startswith("qq:") else None,
                             result["voice_request"])

    def test_keeps_high_and_bounded_time_and_checks_full_context(self):
        result = self.assess({"kind": None, "evidence": "我真笨", "reason": "用户在自责，并非辱骂 bot"})
        self.assertEqual("ok", result["status"])
        self.assertIsNone(result["kind"])
        request, timeout = self.requests[0]
        self.assertEqual(20, timeout)
        self.assertEqual("high", request["reasoning_effort"])
        self.assertEqual({"type": "enabled"}, request["thinking"])
        self.assertEqual(["我真笨，但不是在说你"], json.loads(request["messages"][-1]["content"])["eligible"])

    def test_meme_search_is_optional_and_only_available_on_enabled_qq_delivery(self):
        from organs.home import memes, store
        llm.paths.organ_dir('home').mkdir(parents=True, exist_ok=True)
        store.atomic_write_text(memes.library_root()/'index.json', json.dumps(
            {'items':[], 'changes':[], 'chat_enabled':True}))
        for session, token, query, expected in (
                ('qq:group:7','a'*32,'白饭 干饭','白饭 干饭'),
                ('panel:1','a'*32,'白饭',None),('qq:group:7','','白饭',None),
                ('qq:group:7','a'*32,{'path':'/private'},None),
                ('qq:group:7','a'*32,'x'*201,None)):
            with self.subTest(session=session,query=query):
                result=self.assess({'kind':None,'evidence':'','reason':'普通聊天','meme_query':query},
                                   session_id=session,delivery_id=token)
                self.assertEqual('ok',result['status'])
                self.assertEqual(expected,result.get('meme_query'))

    def test_meme_search_distinguishes_no_request_missing_invalid_and_failure(self):
        from organs.home import memes
        base = {'kind':None,'evidence':'','reason':'普通聊天'}
        cases = [({},'missing'), ({'meme_query':None},'not_requested'),
                 ({'meme_query':''},'invalid'), ({'meme_query':{}},'invalid'),
                 ({'meme_query':'x'*201},'invalid'), ({'meme_query':'吃饭 开心'},'requested')]
        with patch.object(memes,'chat_enabled',return_value=True):
            for extra, expected in cases:
                with self.subTest(expected=expected,extra=extra):
                    result=self.assess(dict(base,**extra),session_id='qq:private:42',delivery_id='a'*32)
                    self.assertEqual(expected,result['meme_search_status'])
                    self.assertEqual('ok',result['status'])
            failed=self.assess({},session_id='qq:private:42',delivery_id='a'*32)
            self.assertEqual('unavailable',failed['meme_search_status'])
            self.assertEqual('unavailable',failed['status'])
        with patch.object(memes,'chat_enabled',return_value=False):
            disabled=self.assess(dict(base,meme_query='吃饭'),session_id='qq:private:42',delivery_id='a'*32)
            self.assertEqual('disabled',disabled['meme_search_status'])
            self.assertIsNone(disabled['meme_query'])

    def test_invalid_or_fabricated_evidence_does_not_fall_back_to_keywords(self):
        for response in ({"kind": "insult", "evidence": "你是废物", "reason": "编出来的"},
                         {"kind": "insult", "evidence": "", "reason": "缺证据"},
                         {"kind": "angry", "evidence": "我真笨", "reason": "类型不符"}, [],
                         {"kind": None, "evidence": "", "reason": ""}):
            with self.subTest(response=response):
                result = self.assess(response)
                self.assertEqual("unavailable", result["status"])
                self.assertIsNone(result["kind"])

    def test_other_speakers_and_history_cannot_become_current_actor_evidence(self):
        captured = []
        def complete(messages, **kw):
            captured.append(json.loads(messages[-1]["content"]))
            return json.dumps({"kind": "insult", "evidence": "你是废物", "reason": "甲骂了 bot"})
        with patch.object(llm, "complete", side_effect=complete):
            result = appraisal.assess(text="甲：你是废物\n乙：我不是这个意思", who="乙", bot="她", scene="group",
                segment=[{"speaker": "甲", "text": "你是废物"}, {"speaker": "乙", "text": "我不是这个意思"}],
                history=[{"who": "甲", "said": "你是废物", "reply": "别这样"}], config=self.config)
        self.assertEqual(["我不是这个意思"], captured[0]["eligible"])
        self.assertEqual(2, len(captured[0]["segment"]))
        self.assertEqual("unavailable", result["status"])
        self.assertIsNone(result["kind"])

    def test_learning_requires_current_request_and_owned_reference(self):
        response = {"kind": None, "evidence": "", "reason": "明确学习请求", "learning": {
            "action": "start", "evidence": ["学习一下"], "reference": "BV1tRxt68EZa", "target": "forged"}}
        for owner, expected in (("42", "ok"), ("43", "unavailable")):
            with patch.object(llm, "complete", return_value=json.dumps(response)):
                result = appraisal.assess(text="学习一下", who="42", bot="她", scene="private", segment=[],
                    history=[{"who": owner, "said": "https://www.bilibili.com/video/BV1tRxt68EZa", "reply": "嗯"}],
                    config=self.config, session_id="qq:private:42")
            self.assertEqual(expected, result["status"])
            if expected == "ok":
                self.assertNotIn("target", result["learning"])
            else:
                self.assertIsNone(result["learning"])

    def test_same_display_name_does_not_authorize_another_speakers_words(self):
        segment = [{"who": "123456789", "speaker": "同名", "text": "你是废物"},
                   {"who": "987654321", "speaker": "同名", "text": "我不是这个意思"}]
        captured = []
        def complete(messages, **kw):
            captured.append(json.loads(messages[-1]["content"]))
            return json.dumps({"kind": "insult", "evidence": "你是废物", "reason": "有人骂了 bot"})
        with patch.object(llm, "complete", side_effect=complete):
            result = appraisal.assess(text="整段", who="987654321", bot="她", scene="group",
                                      segment=segment, history=[], config=self.config)
        self.assertEqual(["我不是这个意思"], captured[0]["eligible"])
        self.assertEqual("unavailable", result["status"])
        self.assertIsNone(result["kind"])

    def test_history_time_belongs_to_its_original_speaker_not_round_addressee(self):
        history = [{"who": "987654321", "said": "两人的片段", "reply": "几点？", "utterances": [
            {"who": "123456789", "speaker": "甲", "text": "明天提醒我取快递"},
            {"who": "987654321", "speaker": "乙", "text": "你们先聊"}]}]
        response = {"kind": None, "evidence": "", "reason": "补答时间", "reminder": {
            "evidence": ["下午三点"], "when": ["明天", "下午三点"], "task": "取快递"}}
        for who, expected in (("123456789", "ok"), ("987654321", "unavailable")):
            with patch.object(llm, "complete", return_value=json.dumps(response)):
                result = appraisal.assess(text="下午三点", who=who, bot="她", scene="group", segment=[],
                                          history=history, config=self.config)
            self.assertEqual(expected, result["status"])

    def test_reminder_cannot_invent_time_or_take_another_speakers_request(self):
        response = {"kind": None, "evidence": "", "reason": "测试边界", "reminder": {
            "evidence": ["我真笨"], "when": ["明天中午"], "task": "喝水"}}
        self.assertEqual("unavailable", self.assess(response)["status"])
        response["reminder"]["when"] = []
        response["reminder"]["who"] = "999"
        result = self.assess(response)
        self.assertEqual("ok", result["status"])
        self.assertNotIn("who", result["reminder"])
        response["reminder"]["evidence"] = ["别人让我提醒"]
        self.assertEqual("unavailable", self.assess(response)["status"])

    def test_reminder_followup_preserves_own_previous_date_not_anothers(self):
        response = {"kind": None, "evidence": "", "reason": "补答时间", "reminder": {
            "evidence": ["下午三点"], "when": ["明天", "下午三点"], "task": "取快递"}}
        with patch.object(llm, "complete", return_value=json.dumps(response)):
            for owner in ("42", "43"):
                result = appraisal.assess(text="下午三点", who="42", bot="她", scene="private",
                    segment=[{"speaker": "测试者", "text": "下午三点"}], history=[{"who": owner, "said": "明天提醒我取快递", "reply": "几点？"}],
                    config=self.config)
                self.assertEqual("ok" if owner == "42" else "unavailable", result["status"])

    def test_image_intent_requires_current_quote_and_only_exports_name(self):
        response = {"kind": None, "evidence": "", "reason": "解析边界", "image_request": {
            "name": "自画像", "evidence": ["我真笨"], "path": "/secret.png", "target": 999}}
        result = self.assess(response)
        self.assertEqual({"name": "自画像", "evidence": ["我真笨"]}, result["image_request"])
        response["image_request"]["evidence"] = ["别人的索图"]
        self.assertEqual("unavailable", self.assess(response)["status"])

    def test_timeout_does_not_retry_or_apply_word_hit(self):
        with patch.object(llm, "complete", side_effect=llm.LLMError("timeout", "test timeout")) as complete:
            result = self.assess({})
        self.assertEqual(1, complete.call_count)
        self.assertEqual("unavailable", result["status"])
        self.assertIsNone(result["kind"])
        self.assertIn("timeout", result["note"])

    def test_scene_policy_remains_separate_from_semantic_result(self):
        interpretation = {"kind": "care", "who": "42", "note": "关心 bot", "status": "ok", "source": "semantic"}
        policy = events.load_config()
        policy['scenes']['care'] = ['private']
        with patch.object(events, "_deliver") as deliver:
            row = events.record("你累不累", who="42", scene="group", root=self.tmp.name,
                                interpretation=interpretation, data=policy)
        deliver.assert_not_called()
        self.assertEqual("", row["kind"])
        self.assertEqual("care", row["interpretation"]["kind"])
        self.assertIn("场景未启用", row["reason"])

    def test_drawing_intent_requires_current_evidence_and_cannot_supply_destination(self):
        response = {"kind": None, "evidence": "", "reason": "明确请求画画", "painting": {
            "action": "start", "evidence": ["我真笨"], "prompt": "一只蓝猫", "aspect": "4:3",
            "target": 999, "model": "unapproved", "self_portrait": False}}
        result = self.assess(response, session_id="qq:private:42")
        self.assertEqual("start", result["painting"]["action"])
        self.assertNotIn("target", result["painting"])
        self.assertNotIn("model", result["painting"])
        self.assertIsNone(self.assess(response)["painting"], "非 QQ 会话不能获得执行意图")
        response["painting"]["evidence"] = ["别人的话"]
        result = self.assess(response, session_id="qq:private:42")
        self.assertEqual("unavailable", result["status"])
        self.assertIsNone(result["painting"])

    def test_unsupported_action_and_invalid_clarification_are_not_executable(self):
        for action in ({"action": "shell", "evidence": ["我真笨"]},
                       {"action": "start", "evidence": []},
                       {"action": "clarify", "evidence": ["我真笨"], "question": []}):
            result = self.assess({"kind": None, "evidence": "", "reason": "判断", "painting": action},
                                 session_id="qq:private:42")
            self.assertEqual("unavailable", result["status"])
            self.assertIsNone(result["painting"])
