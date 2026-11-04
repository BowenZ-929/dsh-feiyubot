"""红线条目匹配：字面或正则，命中报条目 id。

external / review 魂内不判定（词表在家·仓库、语义要模型复核），这里一律不产出命中。
"""
from __future__ import annotations

import re
from typing import Iterable, List

from . import model


def match(items: Iterable[model.BoundsItem], text: str) -> List[str]:
    hits: List[str] = []
    for item in items:
        if not item.active:
            continue
        if item.match_type == "literal":
            hit = item.pattern in text
        elif item.match_type == "regex":
            hit = re.search(item.pattern, text) is not None
        else:
            continue
        if hit:
            hits.append(item.id)
    return hits
