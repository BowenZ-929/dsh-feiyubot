import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from organs.eye import vision
from .test_vision import picture, response


class ImageFollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = [100.0]
        self.config = dict(vision.load_config(), enabled=True)
        for patcher in (patch.object(vision.paths, 'organ_dir', return_value=Path(self.tmp.name)),
                        patch.object(vision, 'read_key', return_value='test'),
                        patch.object(vision, 'record'),
                        patch.object(vision.time, 'monotonic', side_effect=lambda: self.clock[0]),
                        patch.dict(vision._CACHE, {}, clear=True),
                        patch.dict(vision._RECENT_IMAGES, {}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def remember(self, who='41', message=9, session='qq:group:1', sensitive=False):
        return vision.observe([{'kind':'image', 'url':'https://qpic.cn/test',
            'speaker_id':who, 'speaker':'同名', 'messageId':message}], text='看这张',
            session_id=session, delivery_id='old-'+str(message), config=self.config,
            fetch=lambda *a:picture(str('#%06x' % message)),
            transport=lambda *a:response(sensitive))

    def test_only_same_session_and_author_without_quote_and_no_url_in_candidates(self):
        self.remember(); self.remember('42', 10); self.remember('41', 11, 'qq:group:2')
        rows = vision.followup_candidates([], 'qq:group:1', '41')
        self.assertEqual(['9'], [r['message_id'] for r in rows])
        self.assertNotIn('qpic', json.dumps(rows))
        self.assertEqual([], vision.followup_candidates([], 'qq:private:41', '41'))

    def test_remembering_first_image_does_not_change_credentials_for_second(self):
        keys = []
        def transport(config, key, payload, timeout):
            keys.append(key)
            return response()
        vision.observe([{'kind':'image','url':color,'speaker_id':'41','messageId':i}
                        for i,color in enumerate(('red','blue'))], session_id='qq:group:1',
                       config=self.config, fetch=lambda url,*a:picture(url), transport=transport)
        self.assertEqual(['test','test'], keys)

    def test_explicit_quote_can_select_other_author_but_never_falls_back(self):
        self.remember(); self.remember('42', 10)
        quote = {'message_id':'9', 'who':'41', 'text':'[图片]'}
        rows = vision.followup_candidates([{'who':'42', 'quote':quote}], 'qq:group:1', '42')
        self.assertEqual(['9'], [r['message_id'] for r in rows])
        for changed in ({**quote,'who':'42'}, {**quote,'message_id':'404'}, {**quote,'text':'旧文字'}):
            self.assertEqual([], vision.followup_candidates([{'who':'42', 'quote':changed}], 'qq:group:1', '42'))
        rows = vision.followup_candidates([{'who':'43', 'quote':quote}], 'qq:group:1', '42')
        self.assertEqual(['10'], [r['message_id'] for r in rows], '不借同批其他人的引用')

    def test_expired_and_sensitive_images_are_not_candidates(self):
        self.remember(); self.remember(message=10, sensitive=True)
        self.assertEqual(1, len(vision.followup_candidates([], 'qq:group:1', '41')))
        self.clock[0] += 901
        self.assertEqual([], vision.followup_candidates([], 'qq:group:1', '41'))

    def test_revisit_uses_new_question_and_keeps_original_author_and_cache_usage(self):
        self.remember()
        refs = [vision.followup_candidates([], 'qq:group:1', '41')[0]['ref']]
        calls = []
        def transport(c,k,p,t):
            calls.append(p)
            return response()
        kwargs = dict(text='左边那行写了什么', who='42', session_id='qq:group:1', delivery_id='new',
                      config=self.config, fetch=lambda *a:picture('#000009'), transport=transport)
        rows = vision.revisit(refs, **kwargs)
        self.assertEqual('41', rows[0]['speaker_id'])
        self.assertEqual('42', rows[0]['questioner_id'])
        self.assertEqual('revisit', rows[0]['origin'])
        self.assertIn('左边那行写了什么', json.dumps(calls[0], ensure_ascii=False))
        self.assertIn('revisit', vision.context(rows, 0))
        again = vision.revisit(refs, **kwargs)
        self.assertEqual(1, len(calls))
        self.assertEqual({}, again[0]['usage'])
        self.assertEqual([], vision.revisit(refs, **dict(kwargs, session_id='qq:group:other')))

    def test_partial_revisit_preserves_other_images_in_same_quoted_message(self):
        base = {'kind':'image', 'session_id':'qq:group:1', 'message_id':9, 'speaker_id':'41',
                'delivery_id':'upload', 'status':'ok'}
        rows = [dict(base, index=i, image_ref='9:'+str(i), observation={
                    **dict.fromkeys(vision.FIELDS, ''), 'subject':text})
                for i,text in enumerate(('LEFT original', 'RIGHT original'))]
        updated = dict(base, index=0, image_ref='9:1', origin='revisit', delivery_id='followup',
                       observation={**dict.fromkeys(vision.FIELDS, ''), 'subject':'RIGHT detail'})
        path = Path(self.tmp.name)/'observations.jsonl'
        quote = [{'quote':{'message_id':'9','who':'41','text':'[图片][图片]'}}]
        for last in (updated, dict(updated, status='failed', reason='timeout')):
            path.write_text('\n'.join(json.dumps(r) for r in rows+[last]))
            text = vision.quoted_context(quote, 'qq:group:1')
            self.assertIn('LEFT original', text)
            self.assertNotIn('RIGHT original', text)
            self.assertEqual(last['status']=='ok', 'RIGHT detail' in text)
