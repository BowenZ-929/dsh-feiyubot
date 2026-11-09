import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from organs.home import memes
from organs.mind import delivery, llm, meme_reply, recent
from organs.brain.store import BrainStore


class MemeReplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.dict('os.environ',{'HOME_ROOT':str(self.root),'QQAGENT_HOME':str(self.root),'QQAGENT_TRACE':'0'}).start()
        Image.new('RGB',(16,16),'blue').save(self.root/'rice.png')
        (self.root/'metadata.json').write_text(json.dumps({'images':{'one':{
            'relative_path':'rice.png','caption':'捧碗吃饭','visible_text':'还有饭吗','tags':['米饭']}}}))
        memes.import_legacy(self.root/'metadata.json'); memes.set_chat_enabled(True)
        self.ident=memes.catalog()['items'][0]['id']
        self.context={'query':'米饭','text':'一起吃饭吗','segment':[],'history':[],'recent_memes':[]}
        self.payload={'reply':'给我也留点。','chunks':[{'text':'给我也留点。','gap_before':0,'send_at':0}],'send':True}
        self.args={'who':'42','scene':'private','target':'42','delivery_id':'a'*32}
        self.record={'ts':'2026-11-07T10:00:00+09:00','msg_id':'one','who':'42','speaker':'鱼',
                     'scene':'private','said':'一起吃饭吗','reply':'','session_id':'qq:private:42'}
        recent.append(self.record); delivery.stage('a'*32,self.record)
        self.send=patch('organs.hand.channel._post_json',side_effect=AssertionError('测试不能发送QQ')).start()

    def test_model_abstention_invalid_id_and_timeout_preserve_text_and_no_image_reservation(self):
        for result in ({'id':None,'reason':'当前不需要表情'},
                       {'id':'file:///private','reason':'不属于候选'}, 'not json',
                       llm.LLMError('timeout','测试超时')):
            with self.subTest(result=result):
                response=patch.object(llm,'complete',side_effect=result) if isinstance(result,Exception) else patch.object(
                    llm,'complete',return_value=json.dumps(result) if isinstance(result,dict) else result)
                with response, patch.object(meme_reply.actions,'send_image',side_effect=AssertionError('不应准备图片')):
                    meme_reply.apply(self.payload,self.context,**self.args)
                self.assertEqual([{'text':'给我也留点。','gap_before':0,'send_at':0}],self.payload['chunks'])
                self.assertEqual('给我也留点。',self.payload['reply'])
                self.assertIn(self.payload['meme']['status'],('not_selected','unavailable'))

    def test_gated_or_other_media_and_function_replies_do_not_call_selection(self):
        for extra in ({'send':False},{'chunks':[]},{'painting':{}},{'learning':{}},{'remind':{}},{'look':{}},
                      {'chunks':[{'text':'画好啦','image':{'file':'file:///drawing.png'}}]},
                      {'chunks':[{'audio':{'file':'file:///voice.wav'}}]}):
            with self.subTest(extra=extra), patch.object(llm,'complete',side_effect=AssertionError('不应增加选图调用')):
                meme_reply.apply(dict(self.payload,**extra),self.context,**self.args)

    def test_disabled_or_missing_candidates_never_fall_back_to_another_image(self):
        row=memes.catalog()['items'][0]
        memes.edit(dict(row,enabled=False))
        with patch.object(llm,'complete',side_effect=AssertionError('无候选不调模型')):
            meme_reply.apply(self.payload,self.context,**self.args)
        self.assertEqual('no_candidates',self.payload['meme']['status'])
        memes.set_chat_enabled(False)
        fresh=dict(self.payload); fresh.pop('meme')
        with patch.object(llm,'complete',side_effect=AssertionError('关闭不调模型')):
            meme_reply.apply(fresh,self.context,**self.args)
        self.assertNotIn('meme',fresh)

    def test_recent_image_exclusion_survives_text_history_budget_truncation(self):
        context=dict(self.context,history=[],recent_memes=[{'id':self.ident}])
        with patch.object(llm,'complete',side_effect=AssertionError('文字窗口裁剪不能让已发同图重新入选')):
            meme_reply.apply(self.payload,context,**self.args)
        self.assertEqual('no_candidates',self.payload['meme']['status'])

    def test_missing_image_ack_does_not_record_usage_and_other_sessions_do_not_inherit_it(self):
        with patch.object(llm,'complete',return_value=json.dumps({'id':self.ident,'reason':'捧碗一起吃饭'})):
            meme_reply.apply(self.payload,self.context,**self.args)
        self.assertEqual('prepared',self.payload['meme']['status'])
        self.assertEqual(2,len(self.payload['chunks']))
        self.assertNotIn('image',self.payload['chunks'][0])
        self.assertEqual('',self.payload['chunks'][1]['text'])
        self.assertIn('image',self.payload['chunks'][1])
        brain=BrainStore(self.root/'brain-test')
        self.assertTrue(delivery.confirm('a'*32,self.payload['reply'],brain,images=[]))
        rows=recent.load(session_id='qq:private:42')['turns']
        self.assertNotIn('meme',rows[-1])
        self.assertNotIn('她已发的表情',recent.render(rows))
        self.assertEqual([],recent.load(session_id='qq:private:99')['turns'])
