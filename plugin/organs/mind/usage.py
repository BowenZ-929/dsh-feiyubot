"""口癖账：她最近开口，是不是老在念家·仓库里那几句。

**为什么要单开一本账**：口癖频率看的是她**回话的开头**——既不是台词库本身（那五张卡
只追加、不进任何统计），也不是最近几轮（那里存原文给连续性用，谁都不许在别人的账上
叠一层派生统计）。所以另开一份 home/organs/mind/anchor_usage.jsonl：一行一轮，只追加。

**命中口径（人定的，别再改）**：把标点与空白去掉，她回话的开头若以某条现行锚句去掉
标点后的**前 prefix_chars 个字**开头，就算命中；命中取台词库里第一条对得上的，记它的
scene。判标点用的是 unicodedata.category 的前缀（P = 标点，Z = 空白），不写词表——
词表必漏，而这一本账只认一条判据。

**门槛全在数据里**：window / threshold / opening_chars / prefix_chars / reduced_count /
min_rounds 都在 assembly.json 的 anchors.guard 里，本文件不认识其中任何一个数，
也不给它们兜底默认值（缺了就是数据错，让 _check_anchors 当场报）。

读坏行跳过不炸：这一本账读不下去的一行，不该让她闭口。
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import paths
from ..brain import model as brain_model

FILE_NAME = "anchor_usage.jsonl"     # 落脚的文件名（与 recent_turns.jsonl 各记各的）
MARK_CHARS = ("P", "Z", "C")         # 前缀：标点 / 空白 / 控制字符（换行、制表：说话格式要换行分隔）

TS = "ts"
HIT = "hit"
OPENING = "opening"
SCENE = "scene"


def path_of(root: Optional[Any] = None) -> Path:
    """这一本账的落点：root 给定时按它（测试），否则 <器官目录>/anchor_usage.jsonl。

    实盘那份落在 home 下（不进 git）：它是她天天变的行为账，不是配置。
    """
    base = Path(root) if root is not None else paths.organ_dir("mind")
    return base / FILE_NAME


def normalize(text: Any) -> str:
    """去掉标点与空白，一个字都不替换：比较只发生在去标点之后的原文上。"""
    return "".join(ch for ch in str(text if text is not None else "")
                   if unicodedata.category(ch)[0] not in MARK_CHARS)


def opening(text: Any, chars: Any) -> str:
    """她这句话的开头：去标点后取前 chars 个字（chars 来自 anchors.guard.opening_chars）。"""
    size = int(chars)
    if size <= 0:
        return ""
    return normalize(text)[:size]


def match(text: Any, anchors: Any, prefix_chars: Any) -> Tuple[bool, str]:
    """这条回话命中了哪一条现行台词：返回 (hit, scene)，没命中是 (False, "")。

    对每条台词取 normalize(say) 的前 prefix_chars 个字当前缀；回话以它开头即命中，
    取**第一条**对得上的（台词库的现行顺序就是优先级）。前缀为空（那句全是标点）跳过——
    空前缀会匹配一切，那是假命中。
    """
    size = int(prefix_chars)
    head = normalize(text)
    if size <= 0 or not head:
        return False, ""
    for row in anchors or []:
        if not isinstance(row, Mapping):
            continue
        prefix = normalize(row.get("say"))[:size]
        if prefix and head.startswith(prefix):
            return True, str(row.get("scene") or "")
    return False, ""


def append(hit: Any, opening: Any, scene: Any, root: Optional[Any] = None) -> Dict[str, Any]:
    """追一行：{ts, hit, opening, scene}，只追加不重写；返回真正落下去的那一行。

    ts 用器官自己的钟（brain.model.now_iso），与装配这一轮的 at 同一个口径——
    两处时刻各用一套写法，账上的先后就会跟装配分叉。
    """
    row: Dict[str, Any] = {
        TS: brain_model.now_iso(),
        HIT: bool(hit),
        OPENING: str(opening if opening is not None else ""),
        SCENE: str(scene if scene is not None else ""),
    }
    path = path_of(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def read(root: Optional[Any] = None, window: Optional[Any] = None) -> Dict[str, Any]:
    """读回最近 window 行：{"rounds": n, "hits": k, "rate": 0.0..1.0, "rows": [...]}。

    window 为 None 时整份都算（面板要看全量）；一行都没有时 rate 是 0.0，不是除零。
    """
    rows = _rows(path_of(root))
    if window is not None:
        size = int(window)
        rows = rows[-size:] if size > 0 else []
    hits = sum(1 for row in rows if row[HIT])
    rounds = len(rows)
    return {"rounds": rounds, "hits": hits,
            "rate": (float(hits) / rounds) if rounds else 0.0, "rows": rows}


def report(guard: Mapping[str, Any], *, injected_available: int,
           root: Optional[Any] = None) -> Dict[str, Any]:
    """这一轮装配与面板的读数（返回八个键，**对外冻结**）。

    over = 窗口够长（rounds >= min_rounds）且命中率超过阈值；min_rounds 是防小样本误触：
    刚开口两轮都用了锚句，不该立刻降档。
    injected 是这一轮**该注入几条**：降档了就只给 reduced_count 条，否则全给。
    读数看的是**这一轮之前**的滚动窗口——先记后算会自己踩自己，所以本函数只管算，
    落账由调用方放在最后一步。
    """
    window = int(guard["window"])
    threshold = float(guard["threshold"])
    min_rounds = int(guard["min_rounds"])
    reduced = int(guard["reduced_count"])
    got = read(root=root, window=window)
    available = int(injected_available)
    over = got["rounds"] >= min_rounds and got["rate"] > threshold
    return {
        "window": window,
        "threshold": threshold,
        "min_rounds": min_rounds,
        "rounds": got["rounds"],
        "hits": got["hits"],
        "rate": got["rate"],
        "over": over,
        "injected": reduced if over else available,
        "available": available,
    }


def _rows(path: Path) -> List[Dict[str, Any]]:
    """整份读回来，坏行跳过：不是 JSON、不是对象的那一行就当没有过。"""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        rows.append({TS: str(item.get(TS) or ""),
                     HIT: bool(item.get(HIT)),
                     OPENING: str(item.get(OPENING) or ""),
                     SCENE: str(item.get(SCENE) or "")})
    return rows
