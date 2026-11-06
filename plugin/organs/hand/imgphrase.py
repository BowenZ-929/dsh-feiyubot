"""最小「想看哪张图」词表：把她那句话翻成一个图的名字。

手法照抄 timelang.py／stylelang.py：**只认一张表、认不出返回 None、绝不猜、绝不补默认值**。
表外的话（「发张好看的」「给我看看你的」「随便来一张」）一律 None，上层就反问一句——
猜错一张图，比反问一句贵得多；这里也**不认**光秃秃的一个名字（「自画像」），那是 `name` 的事。

表（去掉空格与句末标点之后，**整句**按前缀／后缀框起来，框里剩下的就是名字）：

  给我看 X ／ 给我看看 X
  让我看看 X
  看看你的 X
  发张 X 给我看
  把 X 给我看 ／ 把 X 给我看看

名字前面还挂着的「你的／你那张／你的那张／那张／一张」**只剥一次**，不反复剥：
「给我看你的自画像」→ 名字是「自画像」；剥完是空的就 None（宁可不认，也不给一个空名字）。
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

#: 框：(前缀, 后缀)。**顺序就是试的顺序**——长的框排在短的框前面，否则「给我看看 X」
#: 会被「给我看」抢走，剩一个「看 X」当名字。
FRAMES: Tuple[Tuple[str, str], ...] = (
    ("给我看看", ""),
    ("给我看", ""),
    ("让我看看", ""),
    ("看看你的", ""),
    ("发张", "给我看"),
    ("把", "给我看看"),
    ("把", "给我看"),
)
#: 名字前面可能还挂着的定语：**只剥一次**，按长的在前排。
OWNER_PREFIXES: Tuple[str, ...] = ("你的那张", "你那张", "你的", "那张", "一张")

#: 句末标点与语气尾巴：只削这些，不动字（与 stylelang 同一条口径）。
_TRAILING_PUNCT = "，,。.、；;：:！!？?～~…"
_PARTICLES = ("吧", "呀", "啊", "呢")
_INSIDE = re.compile(r"\s+")


def parse(text: Any) -> Optional[str]:
    """她那句话 → 图的名字；认不出 → None。**不返回空串**：空名字在下游只会变成另一次找不到。"""
    if not isinstance(text, str):
        return None
    piece = _normalise(text)
    if not piece:
        return None
    for prefix, suffix in FRAMES:
        if not piece.startswith(prefix):
            continue
        if suffix and not piece.endswith(suffix):
            continue
        # 框对上了就是这张表的话：框里是空的也不许退到更短的框去——「给我看看」不是「给我看 看」。
        return _inner(piece, prefix, suffix)
    return None


def _inner(piece: str, prefix: str, suffix: str) -> Optional[str]:
    """框里剩下的名字：先剪掉框，再剥一次定语；剪光了就是 None（不返回空串）。"""
    end = len(piece) - len(suffix) if suffix else len(piece)
    inner = piece[len(prefix):end]
    for owner in OWNER_PREFIXES:
        if inner.startswith(owner):
            inner = inner[len(owner):]
            break
    return inner or None


def _normalise(text: str) -> str:
    """去掉所有空格、句末标点与一个语气尾巴（只削这些，不动字）。"""
    piece = _INSIDE.sub("", str(text)).strip()
    while piece and (piece[-1] in _TRAILING_PUNCT or piece[-1] in _PARTICLES):
        if piece[-1] in _PARTICLES and len(piece) <= 1:
            break
        piece = piece[:-1]
    return piece
