import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from organs.home import art
from organs.home.tests.test_art import picture
from organs.day import life, model
from organs.mind import paint


class PaintTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.day = self.root / "day"
        self.now = model.iso("2026-11-07T10:00:00")
        self.soul = SimpleNamespace(snapshot=lambda: SimpleNamespace(identity=SimpleNamespace(display="江迟", appearance="蓝发蓝眼睛")))
        raw = {"title": "休息", "items": [{"start": 0, "end": 720, "activity": "读书", "location": "书房", "outfit": "白色睡衣"},
                                        {"start": 720, "end": 1440, "activity": "休息"}]}
        life.generate("2026-11-07", persona="爱读书", root=self.day, now=self.now, complete=lambda messages: json.dumps(raw))
        life.configure(True, root=self.day)

    def compose(self, **body):
        return paint.compose(dict(prompt="在窗边读书", **body), soul=self.soul, root=self.root, day_root=self.day, now=self.now)

    def test_schedule_is_explicit_and_never_used_for_other_subjects(self):
        other = self.compose(follow_schedule=True)
        self.assertIsNone(other["schedule"])
        self.assertNotIn("白色睡衣", other["final_prompt"])
        own = self.compose(self_portrait=True, follow_schedule=True)
        self.assertIn("白色睡衣", own["final_prompt"])
        self.assertIn("书房", own["final_prompt"])
        self.assertEqual(1, own["schedule"]["revision"])
        life.configure(False, root=self.day)
        self.assertIsNone(self.compose(self_portrait=True, follow_schedule=True)["schedule"])

    def test_selected_outfit_and_scene_override_schedule(self):
        outfit = art.save_asset({"name": "红裙", "category": "outfits", "description": "红色连衣裙"}, root=self.root)
        scene = art.save_asset({"name": "海边", "category": "scenes", "description": "沙滩和海浪"}, root=self.root)
        out = self.compose(self_portrait=True, follow_schedule=True, assets=[outfit["id"], scene["id"]])
        self.assertNotIn("【日程中的服饰】", out["final_prompt"])
        self.assertNotIn("【日程中的场景】", out["final_prompt"])
        self.assertIn("红色连衣裙", out["final_prompt"])

    def test_refs_round_robin_roles_and_two_modes(self):
        rows = [art.save_asset({"name": category, "category": category, "images": [picture()] * 4}, root=self.root)
                for category in art.CATEGORIES]
        out = self.compose(mode="image", assets=[row["id"] for row in reversed(rows)])
        self.assertEqual("image", out["mode"])
        self.assertEqual(6, len(out["references"]))
        self.assertEqual(list(art.CATEGORIES), [ref["category"] for ref in out["references"][:4]])
        self.assertIn("忽略人设卡上的文字", out["references"][0]["role"])
        self.assertIn("参考图 1、5", out["final_prompt"])
        self.assertEqual("image", self.compose()["mode"])
        text_only = self.compose(mode="strokes", assets=[rows[0]["id"]])
        self.assertEqual([], text_only["references"])
        self.assertNotIn("参考图 1", text_only["final_prompt"])
        self.assertTrue(any("尚未读取" in note for note in text_only["notes"]))

    def test_unknown_assets_and_conflicting_scenes_rejected(self):
        with self.assertRaises(art.ArtError):
            self.compose(assets=["missing"])
        scenes = [art.save_asset({"name": str(i), "category": "scenes"}, root=self.root)["id"] for i in range(2)]
        with self.assertRaisesRegex(art.ArtError, "一个场景"):
            self.compose(assets=scenes)
        for value in ("svg_code", [], None):
            with self.assertRaises(art.ArtError):
                self.compose(mode=value)

    def test_visual_review_is_explicit_in_both_modes_and_preserved_in_draft(self):
        self.assertFalse(self.compose(mode='strokes')['visual_review'])
        for mode in ('strokes', 'image'):
            composed=self.compose(mode=mode,visual_review=True)
            draft=art.save_draft(composed,root=self.root)
            self.assertTrue(draft['visual_review'])
        for value,mode in [('yes','strokes'),(1,'image')]:
            with self.assertRaises(art.ArtError):self.compose(mode=mode,visual_review=value)
