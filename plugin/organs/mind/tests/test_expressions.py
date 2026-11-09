import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from organs.home import expressions
from organs.mind import expression_learning, appraisal, llm


SNAPSHOT = {"at": "2026-11-07T09:07:25+09:00", "nodes": [
    {"text": "评论", "description": "", "package": "tv.danmaku.bili"},
    {"text": "这福利真是蒸蒸日上啊", "description": "", "package": "tv.danmaku.bili", "in_scrollable": True, "in_comment_list": True},
    {"text": "别的应用秘密", "description": "", "package": "other.app"}]}
CARD = {"node": 1, "quote": "这福利真是蒸蒸日上啊", "phrase": "蒸蒸日上",
        "meaning": "用表面称赞反讽福利缩水", "when": "熟人一起吐槽游戏福利缩水",
        "avoid": "真诚祝贺、难过求安慰、不了解游戏背景时"}


class ExpressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def learn(self, cards=None):
        return expression_learning.learn(SNAPSHOT, context="游戏更新福利讨论", root=self.root,
                                         complete=lambda messages: json.dumps(cards if cards is not None else [CARD]))

    def recall_library(self):
        old = dict(CARD, id='old:0', phrase='终于能喘口气了', quote='终于能喘口气了',
                   meaning='做完耗时的事情后放松', when='忙完工作想歇一会儿', enabled=True)
        for i in range(13):
            card = old if i == 0 else dict(CARD, id=str(i)+':0', phrase='钓鱼趣话'+str(i),
                                          meaning='看别人钓鱼的玩笑', when='池塘边钓鱼聊天', enabled=True)
            expressions.save({'id':format(i,'032x'), 'created_at':f'2026-11-08T{i:02}:00:00+09:00',
                              'enabled':True,'cards':[card],'source':{'context':'隔离虚构测试语料'}},self.root)
        return old

    def test_recall_older_expression_without_expanding_candidate_budget_or_writing(self):
        old = self.recall_library()
        paths = list(expressions.library_root(self.root).glob('*.json'))
        before = {p:p.read_bytes() for p in paths}
        self.assertNotIn(old['id'], [c['id'] for c in expressions.candidates(self.root)])
        found = expressions.candidates(self.root, query='终于忙完工作了，来歇会儿')
        self.assertEqual(found[0]['id'],old['id']);self.assertEqual(len(found),12)
        self.assertEqual({p:p.read_bytes() for p in paths},before)
        self.assertEqual(expressions.candidates(self.root,query='完全无关的天气预报'),expressions.candidates(self.root))

    def test_query_does_not_revive_disabled_rejected_or_duplicate_cards(self):
        self.recall_library()
        for i, extra in ((20,{'enabled':False}),(21,{'source_rejected':True}),(22,{})):
            expressions.save({'id':format(i,'032x'), 'created_at':f'2026-11-08T{i}:00:00+09:00',
                'enabled':True, 'cards':[dict(CARD,id=str(i)+':0',phrase='钓鱼趣话12',when='忙完工作想歇一会儿')],
                'source':{'context':'隔离虚构测试语料'},**extra},self.root)
        found=expressions.candidates(self.root,query='忙完工作想歇一会儿')
        ids=[c['id'] for c in found]
        self.assertNotIn('20:0',ids);self.assertNotIn('21:0',ids)
        self.assertIn('22:0',ids);self.assertNotIn('12:0',ids)

    def test_appraisal_retrieves_for_current_speaker_whole_segment_and_audio(self):
        old=self.recall_library()
        config=llm.load_config(llm.paths.factory_dir('mind')/'llm.json')
        with patch.dict(os.environ,{'HOME_ROOT':str(self.root)}), patch.object(llm,'complete',return_value=json.dumps(
                {'kind':None,'evidence':'','reason':'普通聊天','expressions':[old['id']]})) as call:
            result=appraisal.assess(text='群里最后一句',who='42',bot='她',scene='group',config=config,
                segment=[{'who':'42','speaker':'甲','text':'终于忙完工作了'},
                         {'who':'99','speaker':'乙','text':'池塘边钓鱼聊天'}],history=[])
            offered=json.loads(call.call_args.args[0][1]['content'])['expressions']
            self.assertEqual(offered[0]['id'],old['id'])
            self.assertEqual(result['expressions'][0]['id'],old['id'])
            appraisal.assess(text='在吗',who='42',bot='她',scene='private',segment=[],history=[],config=config,
                audio=[{'speaker_id':'42','transcription':{'status':'ok','transcript':'终于忙完工作了'}}])
            self.assertEqual(json.loads(call.call_args.args[0][1]['content'])['expressions'][0]['id'],old['id'])

    def test_viewed_clip_is_source_material_and_changes_deduplication(self):
        sent=[]
        def complete(messages):
            sent.append(messages)
            return json.dumps([CARD])
        first=expression_learning.learn(SNAPSHOT, context='游戏更新', viewed_clip='画面：福利从十抽减为五抽；仅见片段', root=self.root, complete=complete)
        again=expression_learning.learn(SNAPSHOT, context='游戏更新', viewed_clip='画面：福利从十抽减为五抽；仅见片段', root=self.root, complete=complete)
        changed=expression_learning.learn(SNAPSHOT, context='游戏更新', viewed_clip='画面：福利从五抽升为十抽；仅见片段', root=self.root, complete=complete)
        self.assertEqual(len(sent),2)
        self.assertEqual(first['id'],again['id'])
        self.assertNotEqual(first['id'],changed['id'])
        self.assertIn('十抽减为五抽',json.loads(sent[0][1]['content'])['viewed_clip'])
        self.assertEqual(first['source']['viewed_clip'],json.loads(sent[0][1]['content'])['viewed_clip'])
        self.assertFalse(first['enabled'])
        with self.assertRaises(ValueError):
            expression_learning.learn(SNAPSHOT,context='视频',viewed_clip='字'*6001,root=self.root,complete=complete)

    def test_drafts_keep_source_and_are_not_chat_candidates(self):
        job = self.learn()
        self.assertFalse(job["enabled"])
        self.assertEqual(job["cards"][0]["quote"], CARD["quote"])
        self.assertEqual(job["source"]["at"], SNAPSHOT["at"])
        self.assertEqual(expressions.candidates(self.root), [])
        self.assertNotIn("别的应用秘密", json.dumps(job, ensure_ascii=False))
        expressions.enable(job["id"], True, self.root)
        self.assertEqual(expressions.candidates(self.root)[0]["phrase"], CARD["phrase"])
        expressions.enable(job["id"], False, self.root)
        self.assertEqual(expressions.candidates(self.root), [])

    def test_multi_screen_capture_ranges_are_preserved_but_time_does_not_change_content_identity(self):
        captures=[{'at':SNAPSHOT['at'],'node_start':0,'node_end':len(SNAPSHOT['nodes'])}]
        seen=[]
        row=expression_learning.learn(dict(SNAPSHOT,captures=captures),context='评论来源',root=self.root,
            complete=lambda messages:seen.append(messages) or json.dumps([CARD]))
        self.assertEqual(captures,row['source']['captures'])
        self.assertEqual(captures,json.loads(seen[0][1]['content'])['captures'])
        later=[dict(captures[0],at='2026-11-08T10:00:00+09:00')]
        again=expression_learning.learn(dict(SNAPSHOT,captures=later),context='评论来源',root=self.root,
            complete=lambda _:self.fail('相同内容不重复提炼'))
        self.assertEqual(row['id'],again['id'])

    def test_bad_quotes_and_invented_phrases_never_become_cards(self):
        for changes in ({"quote": "伪造评论"}, {"node": 0}, {"phrase": "纯属杜撰"}, {"node": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.learn([dict(CARD, **changes)])
        self.assertEqual(expressions.records(self.root), [])

    def test_same_observation_reuses_result_without_model_or_losing_enable_state(self):
        first = self.learn()
        expressions.enable(first["id"], True, self.root)
        with patch.object(llm, "complete") as complete:
            second = expression_learning.learn(SNAPSHOT, context="游戏更新福利讨论", root=self.root)
        complete.assert_not_called()
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["enabled"])

    def test_foreign_app_content_never_goes_to_model(self):
        sent = []
        expression_learning.learn(SNAPSHOT, context="评论", root=self.root,
                                  complete=lambda messages: sent.append(messages) or "[]")
        self.assertNotIn("别的应用秘密", json.dumps(sent, ensure_ascii=False))
        with self.assertRaises(ValueError):
            expression_learning.learn(dict(SNAPSHOT, nodes=[SNAPSHOT["nodes"][2]]), context="评论", root=self.root)

    def test_fixed_footer_and_input_hints_are_not_comment_samples(self):
        snapshot=dict(SNAPSHOT,nodes=SNAPSHOT['nodes']+[
            {'text':'万水千山总是情，评论两句行不行','package':'tv.danmaku.bili','in_scrollable':False},
            {'text':'请输入评论','package':'tv.danmaku.bili','in_scrollable':True,'class':'android.widget.EditText'}])
        sent=[]
        expression_learning.learn(snapshot,context='视频评论',root=self.root,complete=lambda messages:sent.append(messages) or '[]')
        self.assertNotIn('万水千山',json.dumps(sent,ensure_ascii=False))
        self.assertNotIn('请输入评论',json.dumps(sent,ensure_ascii=False))

    def test_recommendations_and_unverified_old_lists_never_reach_model(self):
        for flag in (False, None):
            node=dict(SNAPSHOT['nodes'][1],resource_id='tv.danmaku.bili:id/title')
            node.pop('in_comment_list')
            if flag is not None:
                node['in_comment_list']=flag
            with self.subTest(flag=flag), patch.object(llm,'complete') as complete:
                with self.assertRaisesRegex(ValueError,'评论列表'):
                    expression_learning.learn(dict(SNAPSHOT,nodes=[node]),context='视频推荐',root=self.root)
                complete.assert_not_called()
        self.assertEqual([],expressions.records(self.root))

    def test_only_confirmed_comment_nodes_are_sent_with_original_indices(self):
        snapshot=dict(SNAPSHOT,nodes=[dict(SNAPSHOT['nodes'][1],in_comment_list=False,text='推荐标题'),
                                     SNAPSHOT['nodes'][1]])
        sent=[]
        expression_learning.learn(snapshot,context='视频评论',root=self.root,
                                  complete=lambda messages:sent.append(messages) or json.dumps([CARD]))
        nodes=json.loads(sent[0][1]['content'])['nodes']
        self.assertEqual([1],[n['node'] for n in nodes])
        self.assertNotIn('推荐标题',json.dumps(sent,ensure_ascii=False))

    def test_semantic_selection_only_accepts_known_ids_and_does_not_break_emotion(self):
        first = self.learn()
        expressions.enable(first["id"], True, self.root)
        cards = expressions.candidates(self.root)
        config = llm.load_config(llm.paths.factory_dir("mind") / "llm.json")
        for picks, expected in (([cards[0]["id"]], 1), (["invented"], 0), ("bad", 0), ([], 0)):
            with patch.object(expressions, "candidates", return_value=cards), patch.object(llm, "complete", return_value=json.dumps(
                    {"kind": "praise", "evidence": "谢谢", "reason": "明确感谢", "expressions": picks})):
                result = appraisal.assess(text="谢谢", who="42", bot="她", scene="private", segment=[], history=[], config=config)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["kind"], "praise")
            self.assertEqual(len(result.get("expressions", [])), expected)

    def test_empty_library_keeps_existing_appraisal_prompt(self):
        with patch.object(expressions, "candidates", return_value=[]), patch.object(llm, "complete", return_value=json.dumps(
                {"kind": None, "evidence": "", "reason": "普通聊天"})) as call:
            appraisal.assess(text="你好", who="42", bot="她", scene="private", segment=[], history=[],
                             config=llm.load_config(llm.paths.factory_dir("mind") / "llm.json"))
        self.assertEqual(call.call_args.args[0][0]["content"], appraisal.PROMPT)
        self.assertNotIn("expressions", json.loads(call.call_args.args[0][1]["content"]))
