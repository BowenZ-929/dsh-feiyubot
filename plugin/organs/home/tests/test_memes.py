import io
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from organs.home import memes


class MemeLibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'home'
        self.root.mkdir()
        self.old = Path(self.tmp.name) / 'old'
        self.old.mkdir()
        image = Image.new('RGB', (24, 24), 'blue')
        image.save(self.old / 'bird.png')
        self.caption = '以当前分类“happy”（开心）所代表的情绪、态度或用途为主体：蓝色小鸟捧着米饭。'
        self.metadata = self.old / 'semantic_metadata.json'
        self.metadata.write_text(json.dumps({'images':{'old-id':{'relative_path':'bird.png',
            'caption':self.caption, 'visible_text':'我的白饭呢', 'tags':['category:happy','米饭'],
            'category':'happy'}}}))

    def test_import_retains_source_without_recaptioning_or_changing_originals(self):
        before = self.metadata.read_bytes(), (self.old/'bird.png').read_bytes()
        result = memes.import_legacy(self.metadata, root=self.root)
        self.assertEqual(1, result['imported'])
        row = memes.catalog(self.root)['items'][0]
        self.assertEqual('蓝色小鸟捧着米饭。', row['description'])
        self.assertEqual(self.caption, row['source']['description'])
        self.assertEqual('我的白饭呢', row['visible_text'])
        self.assertEqual(['米饭'], row['keywords'])
        self.assertFalse(row['reviewed'])
        self.assertEqual(before[1], memes.image(row['id'], root=self.root)[0])
        self.assertTrue(memes.preview(row, root=self.root).startswith('data:image/jpeg;base64,'))
        self.assertEqual(0, memes.import_legacy(self.metadata, root=self.root)['imported'])
        self.assertEqual(before, (self.metadata.read_bytes(), (self.old/'bird.png').read_bytes()))

    def test_edits_are_audited_disabled_images_leave_candidates_and_reimport_keeps_edits(self):
        memes.import_legacy(self.metadata, root=self.root)
        row = memes.catalog(self.root)['items'][0]
        changed = memes.edit({'id':row['id'], 'revision':1, 'description':'在问饭在哪里',
            'visible_text':'我的白饭呢','keywords':['吃饭'], 'enabled':False, 'reviewed':True}, root=self.root)
        self.assertEqual(2, changed['revision'])
        self.assertTrue(changed['reviewed'])
        self.assertEqual([], memes.candidates('白饭', root=self.root))
        self.assertEqual(1, memes.list_items(query='白饭', root=self.root)['total'])
        with self.assertRaises(memes.MemeError):
            memes.edit({'id':row['id'], 'revision':1}, root=self.root)
        memes.import_legacy(self.metadata, root=self.root)
        current = memes.catalog(self.root)
        self.assertEqual('在问饭在哪里', current['items'][0]['description'])
        self.assertEqual(self.caption, current['items'][0]['source']['description'])
        self.assertEqual(True, current['changes'][-1]['before']['enabled'])
        self.assertEqual(False, current['changes'][-1]['after']['enabled'])

    def test_search_uses_actual_content_and_never_falls_back_to_random_mood(self):
        memes.import_legacy(self.metadata, root=self.root)
        self.assertEqual(1, len(memes.candidates('白饭', root=self.root)))
        self.assertEqual([], memes.candidates('火箭', root=self.root))
        self.assertEqual([], memes.candidates('happy', root=self.root))
        self.assertEqual([], memes.candidates('', root=self.root))
        item = memes.candidates('白饭', root=self.root)[0]
        self.assertNotIn('file', item)
        self.assertNotIn('source', item)

    def test_import_rejects_paths_outside_pack_and_reports_missing_files(self):
        data=json.loads(self.metadata.read_text())
        data['images']['escape']={'relative_path':'../private.png','caption':'不应读取'}
        data['images']['missing']={'relative_path':'missing.png','caption':'缺图'}
        self.metadata.write_text(json.dumps(data))
        result=memes.import_legacy(self.metadata, root=self.root)
        self.assertEqual(1, result['imported'])
        self.assertEqual(2, len(result['skipped']))
        with self.assertRaises(memes.MemeError):
            memes.image('../private',root=self.root)
