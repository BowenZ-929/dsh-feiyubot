import copy
import io
import unittest
from PIL import Image
from organs.hand import strokes

PROGRAM = {"background": "#FFFFFF", "strokes": [
    {"label": "三角", "color": "#224466", "width": 6, "fill": "#88AAFF", "path": [["M", 100, 100], ["L", 900, 100], ["L", 500, 900], ["Z"]]},
    {"label": "曲线", "color": "#CC4422", "width": 12, "fill": None, "path": [["M", 100, 500], ["C", 300, 100, 700, 900, 900, 500]]}]}


class StrokeTests(unittest.TestCase):
    def test_prefix_and_final_reconstruct_exactly(self):
        program = strokes.validate(PROGRAM)
        final = strokes.render(program)
        self.assertEqual(final, strokes.render(program, step=2))
        self.assertNotEqual(final, strokes.render(program, step=1))
        blank = Image.open(io.BytesIO(strokes.render(program, step=0)))
        self.assertEqual((255, 255, 255), blank.getpixel((256, 256)))
        image = Image.open(io.BytesIO(final))
        self.assertEqual((512, 512), image.size)
        self.assertNotEqual((255,255,255), image.getpixel((256, 200)))
        self.assertEqual((640,360), Image.open(io.BytesIO(strokes.render(program, aspect="16:9"))).size)

    def test_rejects_unbounded_or_executable_commands(self):
        for path in ([["M", 1, 1], ["script", "alert(1)"]], [["M", 1, 1], ["L", float("nan"), 5]],
                     [["M", 1, 1], ["L", 1001, 5]], [["M",1,1],["M",3,3]], [["M",1,1],["Z"]]):
            bad = copy.deepcopy(PROGRAM)
            bad["strokes"][0]["path"] = path
            with self.assertRaises(strokes.StrokeError): strokes.validate(bad)
        for key, value in (("width", True), ("fill", "url(http://other)"), ("color", "red")):
            bad = copy.deepcopy(PROGRAM); bad["strokes"][0][key] = value
            with self.assertRaises(strokes.StrokeError): strokes.validate(bad)
