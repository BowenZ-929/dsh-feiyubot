import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from organs.home import art


def picture(color="blue"):
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode()


class ArtTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_read_does_not_seed_empty_library(self):
        self.assertEqual([], art.library(self.root)["assets"])
        self.assertEqual([], art.drafts(self.root))
        self.assertEqual([], list(self.root.iterdir()))

    def test_upload_edit_and_snapshot_preserve_old_images(self):
        row = art.save_asset({"name": "角色", "category": "characters", "images": [picture()]}, root=self.root)
        self.assertTrue(art.thumbnail(row, root=self.root).startswith("data:image/jpeg;base64,"))
        first = row["images"][0]
        revised = art.save_asset(dict(row, name="角色新版", images=[picture("red")]), root=self.root)
        self.assertNotEqual(first["sha256"], revised["images"][0]["sha256"])
        self.assertTrue((art.library_root(self.root) / "images" / first["file"]).exists())
        self.assertEqual(row, art.library(self.root)["changes"][-1]["before"])
        with self.assertRaisesRegex(art.ArtError, "已被修改"):
            art.save_asset(row, root=self.root)
        keep = dict(revised, description="蓝头发")
        del keep["images"]
        kept = art.save_asset(keep, root=self.root)
        self.assertEqual(revised["images"], kept["images"])

    def test_all_uploads_validate_before_any_write(self):
        with self.assertRaises(art.ArtError):
            art.save_asset({"name": "坏图", "category": "characters", "images": [picture(), "aW52YWxpZA=="]}, root=self.root)
        self.assertEqual([], list(self.root.iterdir()))
        for invalid in (None, [], {"category": []}, {"name": "", "category": "props"},
                        {"name": "a", "category": "props", "images": ["!!!"]}):
            with self.assertRaises(art.ArtError):
                art.save_asset(invalid, root=self.root)

    def test_draft_is_not_a_generated_image_and_is_immutable(self):
        row = art.save_asset({"name": "衣服", "category": "outfits"}, root=self.root)
        draft = art.save_draft({"mode": "strokes", "selected": [row], "prompt": "画猫"}, root=self.root)
        art.save_asset(dict(row, name="换个名字"), root=self.root)
        saved = art.drafts(self.root)[0]
        self.assertEqual("draft", saved["status"])
        self.assertEqual("衣服", saved["selected"][0]["name"])
        self.assertEqual(draft, saved)
