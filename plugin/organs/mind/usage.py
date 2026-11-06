"""口癖账：她最近开口，是不是老在念家·仓库里那几句。

**为什么要单开一本账**：口癖频率看的是她**回话的开头**——既不是台词库本身（那五张卡
只追加、不进任何统计），也不是最近几轮（那里存原文给连续性用，谁都不许在别人的账上
叠一层派生统计）。所以另开一份 home/organs/mind/anchor_usage.jsonl：一行一轮，只追加。

**命中口径（人定的，别再改）**：把标点与空白去掉，她回话的开头若以某张现行台词卡
的 **phrase**（≤4 字那一档，进提示词的也是它）去掉标点后的**前 prefix_chars 个字**开头，就算
命中；命中取台词库里第一条对得上的，记它的 **id** 与 phrase。判标点用的是 unicodedata.category
的前缀（P = 标点，Z = 空白），不写词表——词表必漏，而这一本账只认一条判据。
**example 不参与判命中**：整句例句是她「口吻长什么样」的证据，不是口癖；拿它判，等于把她照抄
一整句与念半个口癖算成同一件事（照抄那一档归 organs/mind/conversation.py 的照抄审计）。

**每行带 id 是为了「同一张卡最近用过几次」**：近轮禁用（anchors.repeat）按 id 数，
不是按句子数——改 when、改 example 都不改 id，所以同一张卡换过说法也还是同一张卡。
老行没有 id：读回来记空串，**当它没命中过**（猜一个 id 回去，会把从没出现过的那张卡禁掉）。

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
CARD_ID = "id"       # 命中/没命中的是哪一张卡（稳定键，改 when 与 example 都不改它）
PHRASE = "phrase"    # 命中的是那一张卡的哪一句口癖（≤4 字，进提示词的也是它）


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


def hit(text: Any, anchors: Any, prefix_chars: Any) -> Optional[Dict[str, str]]:
    """这条回话命中了哪一张现行台词卡：命中给 {card_id, phrase, scene}，没命中给 None。

    判据：对每张卡取 normalize(phrase) 的前 prefix_chars 个字当前缀，回话以它开头即命中，取
    **第一条**对得上的（台词库的现行顺序就是优先级）。前缀为空（那句全是标点、或这张卡根本没定
    phrase）跳过——空前缀会匹配一切，那是假命中。

    **按 phrase 判，不按 example 判**：phrase 是进提示词的那一档（她看见的只有它），example 是
    整句例句。拿 example 判命中，等于把「念半个口癖」与「照抄一整句」算成一件事；照抄那一档归
    organs/mind/conversation.py 的照抄审计。

    老卡没有 phrase（升级前写下的 say 那几行）：读数这一侧退回 example/say 的前缀，命中的那一笔
    **phrase 记空**——老句不该被记成「口癖命中」，但「她是不是老在念台词」这本账仍要看得见。
    scene 单独给一份：账本的 scene 栏是历史口径，一条都不改。
    """
    size = int(prefix_chars)
    head = normalize(text)
    if size <= 0 or not head:
        return None
    for row in anchors or []:
        if not isinstance(row, Mapping):
            continue
        phrase = str(row.get(PHRASE) or "")
        prefix = normalize(phrase)[:size]
        if not prefix:
            prefix = normalize(row.get("example") or row.get("say"))[:size]
            phrase = ""            # 靠老句命中的：不记成口癖命中（见上面的说明）
        if prefix and head.startswith(prefix):
            return {CARD_ID: str(row.get(CARD_ID) or ""), PHRASE: phrase,
                    SCENE: str(row.get(SCENE) or "")}
    return None


def match(text: Any, anchors: Any, prefix_chars: Any, *,
          with_card: bool = False) -> Tuple[Any, ...]:
    """同上，只是摊成元组（老调用口与面板要的是这一份形状）。

    with_card=False → (hit, scene)：老调用口（organs/mind/bench/consistency.py 的 C 轨）按两个
    变量接，所以默认仍是两元组——那一份**没有一起改**，改了它 bench 就整条读不出来。
    with_card=True  → (hit, card_id, phrase)：核那一边按卡记账要的正是这三个。
    两种形状的判据同一条（都走 hit），不另写一份匹配。
    """
    found = hit(text, anchors, prefix_chars)
    if with_card:
        if found is None:
            return False, "", ""
        return True, found[CARD_ID], found[PHRASE]
    if found is None:
        return False, ""
    return True, found[SCENE]


def repeats(window: Any, root: Optional[Any] = None) -> Dict[str, int]:
    """最近 window 轮里每张卡各命中过几次：{card_id: 次数}。

    近轮禁用（assembly.json 的 anchors.repeat）按 id 数，不按句子数：同一张卡就算换了说法，
    也还是同一张卡。只数 hit 为真、且 id 非空的行——老行读不到 id，**就当它没命中过**：
    拿 when／scene 猜一个 id 回去，会把一张从没出现过的卡禁掉，那比不禁更糟。
    window <= 0 时一行都不算（与 read 的 window 同一个口径）。
    """
    size = int(window)
    rows = _rows(path_of(root))
    rows = rows[-size:] if size > 0 else []
    tally: Dict[str, int] = {}
    for row in rows:
        if not row[HIT]:
            continue
        card = str(row.get(CARD_ID) or "")
        if not card:
            continue
        tally[card] = tally.get(card, 0) + 1
    return tally


def append(hit: Any, opening: Any, scene: Any, root: Optional[Any] = None, *,
           card_id: Any = "", phrase: Any = "") -> Dict[str, Any]:
    """追一行：{ts, hit, opening, scene, id, phrase}，只追加不重写；返回真正落下去的那一行。

    ts 用器官自己的钟（brain.model.now_iso），与装配这一轮的 at 同一个口径——
    两处时刻各用一套写法，账上的先后就会跟装配分叉。
    id / phrase 是这一轮命中的那张卡与那句口癖：近轮禁用按 id 数次数，句子换了说法也还是同一张卡。
    hit 为假时两栏都写空串——没命中的一轮没有卡可记，写一个猜出来的 id 会误禁。
    """
    row: Dict[str, Any] = {
        TS: brain_model.now_iso(),
        HIT: bool(hit),
        OPENING: str(opening if opening is not None else ""),
        SCENE: str(scene if scene is not None else ""),
        CARD_ID: str(card_id or "") if hit else "",
        PHRASE: str(phrase or "") if hit else "",
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
                     SCENE: str(item.get(SCENE) or ""),
                     CARD_ID: str(item.get(CARD_ID) or ""),
                     PHRASE: str(item.get(PHRASE) or "")})
    return rows
