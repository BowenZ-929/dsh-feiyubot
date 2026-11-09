"""有限笔触指令 → 确定性画布。无模型、网络、文件操作或代码执行。

借鉴 SketchAgent 的逐笔曲线表达；本项目用明确的 M/L/C/Z 指令代替 XML/曲线拟合。
"""
import io
import math
import re
from PIL import Image, ImageDraw

SIZES = {"1:1": (512, 512), "3:4": (480, 640), "4:3": (640, 480), "9:16": (360, 640), "16:9": (640, 360)}
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class StrokeError(ValueError):
    pass


def validate(value):
    if not isinstance(value, dict) or not isinstance(value.get("background"), str) or not COLOR.fullmatch(value["background"]):
        raise StrokeError("画布背景需要 #RRGGBB 颜色")
    rows = value.get("strokes")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 80:
        raise StrokeError("需要 1–80 笔")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise StrokeError("笔触需要一个对象")
        color, fill, width, label = row.get("color"), row.get("fill"), row.get("width"), row.get("label", "")
        if not isinstance(color, str) or not COLOR.fullmatch(color):
            raise StrokeError("笔触颜色需要 #RRGGBB")
        if fill is not None and (not isinstance(fill, str) or not COLOR.fullmatch(fill)):
            raise StrokeError("填色需要 #RRGGBB 或 null")
        if type(width) not in (int, float) or not math.isfinite(width) or not 1 <= width <= 30:
            raise StrokeError("笔宽需要 1–30")
        if not isinstance(label, str) or len(label) > 100:
            raise StrokeError("笔触说明限 100 字")
        commands = row.get("path")
        if not isinstance(commands, list) or not 2 <= len(commands) <= 24:
            raise StrokeError("每笔需要 2–24 个路径命令")
        for index, command in enumerate(commands):
            if not isinstance(command, list) or not command or not isinstance(command[0], str):
                raise StrokeError("路径命令形状不对")
            op = command[0]
            if len(command) != {"M": 3, "L": 3, "C": 7, "Z": 1}.get(op):
                raise StrokeError("只支持 M、L、C、Z 路径命令")
            if (index == 0) != (op == "M") or (op == "Z" and (index != len(commands) - 1 or index < 2)):
                raise StrokeError("每笔由一个 M 开始，Z 只能放在末尾")
            if any(type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1000 for n in command[1:]):
                raise StrokeError("坐标需要 0–1000 的有限数字")
        if fill is not None and commands[-1][0] != "Z":
            raise StrokeError("填色路径必须用 Z 闭合")
        out.append({"path": commands, "color": color, "fill": fill, "width": width, "label": label})
    return {"background": value["background"], "strokes": out}


def render(program, *, aspect="1:1", step=None):
    """调用方在模型/HTTP 边界验证；内部只执行已验证的数据。step 表示前 N 笔。"""
    width, height = SIZES[aspect]
    scale = 2
    image = Image.new("RGB", (width * scale, height * scale), program["background"])
    draw = ImageDraw.Draw(image)
    rows = program["strokes"] if step is None else program["strokes"][:step]
    for stroke in rows:
        points = []
        for cmd in stroke["path"]:
            op = cmd[0]
            if op in ("M", "L"):
                points.append((cmd[1], cmd[2]))
            elif op == "C":
                x0, y0 = points[-1]
                for index in range(1, 49):
                    t = index / 48
                    u = 1 - t
                    points.append((u**3*x0 + 3*u*u*t*cmd[1] + 3*u*t*t*cmd[3] + t**3*cmd[5],
                                   u**3*y0 + 3*u*u*t*cmd[2] + 3*u*t*t*cmd[4] + t**3*cmd[6]))
            else:
                points.append(points[0])
        scaled = [(x * width * scale / 1000, y * height * scale / 1000) for x, y in points]
        pen_width = max(1, round(stroke["width"] * min(width, height) * scale / 1000))
        if stroke["fill"] is not None:
            draw.polygon(scaled, fill=stroke["fill"])
        draw.line(scaled, fill=stroke["color"], width=pen_width, joint="curve")
        radius = pen_width / 2
        for x, y in (scaled[0], scaled[-1]):
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=stroke["color"])
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
