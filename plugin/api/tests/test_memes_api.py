import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from api import api
from organs.home import memes


class MemeApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.home=self.root/'home'; self.home.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict('os.environ',{'HOME_ROOT':str(self.home),'QQAGENT_HOME':str(self.home)}).start()
        patch('organs.mind.llm.complete',side_effect=AssertionError('图库管理不调用模型')).start()
        patch('organs.hand.channel.say_image',side_effect=AssertionError('图库管理不发送QQ')).start()
        images={}
        for i in range(23):
            Image.new('RGB',(20,20),(i*10,50,80)).save(self.root/f'{i}.png')
            images[str(i)]={'relative_path':f'{i}.png','caption':f'第{i}张小鸟','visible_text':'吃饭' if i==22 else '', 'tags':[]}
        path=self.root/'semantic_metadata.json';path.write_text(json.dumps({'images':images}))
        memes.import_legacy(path)

    def test_gallery_can_reach_all_images_and_edit_one_without_modifying_picture(self):
        seen=[]
        for offset in (0,20):
            status,page=api.handle('GET',f'/memes/list?offset={offset}',None,None)
            self.assertEqual(200,status)
            self.assertEqual(23,page['total'])
            seen.extend(r['id'] for r in page['items'])
            self.assertTrue(page['items'][0]['preview'].startswith('data:image/jpeg;base64,'))
        self.assertEqual(23,len(set(seen)))
        status,page=api.handle('GET','/memes/list?q=吃饭',None,None)
        self.assertEqual(1,page['total'])
        ident=page['items'][0]['id']
        status,detail=api.handle('GET','/memes/read?id='+ident,None,None)
        self.assertEqual(200,status)
        raw=memes.image(ident)[0]
        self.assertTrue(detail['image'].startswith('data:image/png;base64,'))
        body={'id':ident,'revision':1,'description':'小鸟想吃饭','visible_text':'吃饭','keywords':['米饭'],'enabled':False,'reviewed':False}
        self.assertEqual(200,api.handle('POST','/memes/save',body,None)[0])
        updated=api.handle('GET','/memes/read?id='+ident,None,None)[1]
        self.assertFalse(updated['enabled'])
        self.assertFalse(updated['reviewed'],'停用不等于确认旧描述正确')
        self.assertEqual('小鸟想吃饭',updated['description'])
        self.assertEqual(1,len(updated['changes']))
        self.assertEqual(raw,memes.image(ident)[0])
        self.assertEqual([],memes.candidates('米饭'))
        self.assertEqual(400,api.handle('POST','/memes/save',body,None)[0])

    def test_invalid_query_and_unknown_image_are_rejected(self):
        for path in ['/memes/list?offset=-1','/memes/list?limit=101','/memes/list?offset=x','/memes/read?id=../private']:
            self.assertEqual(400,api.handle('GET',path,None,None)[0])
        self.assertEqual(405,api.handle('POST','/memes/list',{},None)[0])

    def test_chat_switch_is_explicit_and_keeps_library_edits(self):
        status, page = api.handle('GET','/memes/list',None,None)
        self.assertIs(False, page.get('chat_enabled'))
        for value in ('true', 1, None):
            self.assertEqual(400,api.handle('POST','/memes/settings',{'chat_enabled':value},None)[0])
        self.assertEqual(200,api.handle('POST','/memes/settings',{'chat_enabled':True},None)[0])
        self.assertIs(True,api.handle('GET','/memes/list',None,None)[1]['chat_enabled'])
        saved = memes.catalog()
        self.assertEqual(23,len(saved['items']))
        self.assertEqual({'chat_enabled':False},saved['changes'][-1]['before'])
        self.assertEqual({'chat_enabled':True},saved['changes'][-1]['after'])
