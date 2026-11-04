"""嘴的切分：只回答「切在哪」，不管「隔多久」。

合法切点只落在语义停顿上：句末标点（。！？!?…~～，跳过尾部引号括号）、分号（；;）、
逗号（，,）、换行。**不按字数切。** 只有单段本身就超过 policy 的 max_chars 时才允许硬切，
硬切出来的那一泡带 degraded=true 与一句人话 reason，好让出口和日志一眼看出这句是被字数逼的。

**切点有层级，句末优先于逗号。** 按总长算出来的是「目标切点」——它只保证长度匀，不保证断得
好听，于是常出现一泡泡在逗号上收尾（合法但不是最好的切点）。所以在目标切点**前后各一个窗口**
（policy 的 split.cut_window_chars）以内再挑一次：窗口里有更好层级的合法切点就换过去，没有就
原样退回。层级序列本身也在数据里（split.cut_preference，按顺序从好到差：句末 > 分号 > 换行 >
逗号），代码只认「第几个」，不认「是哪一个字」。窗口换来的是切点质量，不是放松判据：一泡仍然
不超过 max_chars、条数上限照旧、拼回来逐字等于原文、硬切照旧带 degraded。

切点不许落在词中间：左边不许以连接词/助词收尾（的、地、得、了、着、和、与、跟、把、被、而、
但、或、且、就、都、也、还），右边不许以 的/地/得/了/着 开头。两张字表都在
organs/mouth/data/policy.json，不在代码里——加一批字是改数据，不是改算法。

条数上限取自 policy（默认 3 泡）：能并就并，多出来的并进最后一泡，**不丢字**——
把这批拼回去，与原文逐字相等。

纯函数：入参只有「文本 + 只读的政策 dict」，出参只有结构化数据；不碰时钟、不碰随机数、
不碰宿主 IO（本文件不 import time / datetime / random），所以能离线预览，也能被单独替换。
间隔是 pace.py 的事，两个文件互不 import。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Sequence, Set

from .model import Batch, Policy, Seg, need, section

__all__ = ["split"]


@dataclass(frozen=True)
class _Piece:
    """切分中间的极小片：一段不再被拆的字，带上它是不是被硬切来的、以及断在它后面算第几层。

    tier 是层级序号（0 最好），取自 policy 的 cut_preference；窗口挑切点时只比这个序号。
    """

    text: str
    degraded: bool
    reason: str
    tier: int = 0


def split(text: str, policy: Policy) -> Batch:
    """把一段文本切成有序的几泡。

    只填 text / degraded / reason；gap_before 与 send_at 留 0.0、summary 留空，等 pace.fill 填。
    空白文本返回空批次（没有要发的泡）。
    """
    rules = section(policy, "split")
    body = text or ""
    if not body.strip():
        return Batch(parts=[], summary="")
    cap = int(need(rules, "max_chars"))
    max_parts = int(need(rules, "max_parts"))
    ranks = {str(name): index for index, name in enumerate(need(rules, "cut_preference"))}
    window = int(need(rules, "cut_window_chars"))
    pieces = _pieces(_segments(body, rules), cap, rules, ranks)
    ends = _plan(pieces, cap, max_parts)
    return Batch(parts=_build(pieces, _prefer(pieces, ends, cap, window)))


def _stop_chars(rules: Mapping[str, Any]) -> Set[str]:
    """所有可以落在其上/其后的停顿字符：句末标点、分号、逗号、换行。"""
    marks: Set[str] = set()
    for key in ("sentence_end", "semicolon", "comma", "newline"):
        marks.update(str(need(rules, key)))
    return marks


def _breaks(text: str, rules: Mapping[str, Any]) -> List[int]:
    """所有「停顿之后」的下标（还没筛掉落在词中间的那些）。"""
    ends = set(str(need(rules, "sentence_end")))
    semis = set(str(need(rules, "semicolon")))
    commas = set(str(need(rules, "comma")))
    newlines = set(str(need(rules, "newline")))
    closers = set(str(need(rules, "trailing_closers")))
    length = len(text)
    found: List[int] = []
    index = 0
    while index < length:
        char = text[index]
        if char in newlines:
            end = index
            while end < length and text[end] in newlines:
                end += 1
            found.append(end)
            index = end
            continue
        if char in ends or char in semis or char in commas:
            end = index + 1
            if char in ends:
                while end < length and text[end] in ends:      # 「？！」「……」当一个停顿
                    end += 1
                while end < length and text[end] in closers:   # 尾部引号括号跟着上一句走
                    end += 1
            found.append(end)
            index = end
            continue
        index += 1
    return found


def _cut_ok(text: str, position: int, rules: Mapping[str, Any]) -> bool:
    """这个切点落得下吗：左右两边都要有实字，且不许切在词中间。"""
    stop = _stop_chars(rules)
    closers = set(str(need(rules, "trailing_closers")))
    left_banned = set(str(need(rules, "left_banned")))
    right_banned = set(str(need(rules, "right_banned")))
    left = text[:position].rstrip()
    while left and (left[-1] in stop or left[-1] in closers):
        left = left[:-1].rstrip()          # 跳过切点自带的标点与尾部括号，再看最后一个实字
    right = text[position:].lstrip()
    if not left or not right:
        return False
    return left[-1] not in left_banned and right[0] not in right_banned


def _tier(text: str, rules: Mapping[str, Any], ranks: Mapping[str, int]) -> int:
    """断在这段字后面算第几层：越小越好；落在字中间（谁也不认的字）算最差。

    看的是「切点前最后一个实字」属于哪一类停顿，尾部引号括号与空白跳过——「叶子。」与「叶子。」」
    是同一层。空白里含换行就额外记一次换行层，取两者中更好的那个：一段以「。\n」结尾的行，
    既是句末也是换行，按句末算。
    """
    ends = set(str(need(rules, "sentence_end")))
    semis = set(str(need(rules, "semicolon")))
    commas = set(str(need(rules, "comma")))
    newlines = set(str(need(rules, "newline")))
    closers = set(str(need(rules, "trailing_closers")))
    worst = len(ranks)
    index = len(text) - 1
    saw_newline = False
    while index >= 0 and (text[index].isspace() or text[index] in closers):
        if text[index] in newlines:
            saw_newline = True
        index -= 1
    classes: List[str] = []
    if index >= 0:
        char = text[index]
        if char in ends:
            classes.append("sentence_end")
        elif char in semis:
            classes.append("semicolon")
        elif char in commas:
            classes.append("comma")
        elif char in newlines:
            classes.append("newline")
    if saw_newline:
        classes.append("newline")
    found = [ranks[name] for name in classes if name in ranks]
    return min(found) if found else worst


def _segments(text: str, rules: Mapping[str, Any]) -> List[str]:
    """先在语义停顿处把整段切开（这一步不看字数），切点拼起来仍然是原文。"""
    cuts = [position for position in _breaks(text, rules) if _cut_ok(text, position, rules)]
    chunks: List[str] = []
    start = 0
    for position in cuts:
        if position <= start:
            continue
        chunks.append(text[start:position])
        start = position
    if start < len(text):
        chunks.append(text[start:])
    return _merge_blank(chunks)


def _merge_blank(chunks: Sequence[str]) -> List[str]:
    """纯空白的碎片并到邻居里：一泡不许是空的，但一个字也不许丢。"""
    merged: List[str] = []
    pending = ""
    for chunk in chunks:
        if chunk.strip():
            merged.append(pending + chunk)
            pending = ""
        elif merged:
            merged[-1] += chunk
        else:
            pending += chunk
    if pending:
        if merged:
            merged[-1] += pending
        else:
            merged.append(pending)
    return merged


def _pieces(segments: Sequence[str], cap: int, rules: Mapping[str, Any],
            ranks: Mapping[str, int]) -> List[_Piece]:
    """单段超过 max_chars 的，只能按字数硬切；切出来的每一片都带 degraded 与人话理由。

    层级也在这里算好：合法切点按「前一个实字属于哪类停顿」定层；硬切的每一片一律算最差
    （按字数切出来的地方没有层级可言），免得窗口挑切点时把硬切当成本事。
    """
    template = str(need(rules, "hard_cut_reason"))
    worst = len(ranks)
    pieces: List[_Piece] = []
    for segment in segments:
        if len(segment) <= cap:
            pieces.append(_Piece(segment, False, "", _tier(segment, rules, ranks)))
            continue
        reason = template.format(chars=len(segment), max_chars=cap)
        for offset in range(0, len(segment), cap):
            pieces.append(_Piece(segment[offset:offset + cap], True, reason, worst))
    return pieces


def _prefix(pieces: Sequence[_Piece]) -> List[int]:
    """原子片的前缀和：第 i 个切点在第几个字之后。"""
    prefix = [0]
    for piece in pieces:
        prefix.append(prefix[-1] + len(piece.text))
    return prefix


def _plan(pieces: Sequence[_Piece], cap: int, max_parts: int) -> List[int]:
    """每泡的结尾下标（不含）。

    先按 cap 求最少泡数、再往上加，直到切得匀且每泡不超 cap；加到 policy 的条数上限还匀不开，
    就退成「前面尽量装满、余下的并进最后一泡」——条数上限压过长度上限，一个字也不丢。
    """
    prefix = _prefix(pieces)
    total = prefix[-1]
    count = len(pieces)
    needed = max(1, -(-total // cap)) if total else 1
    want = max(1, min(needed, max_parts, count))
    ends = None
    while True:
        ends = _balanced(prefix, want, cap)
        if ends is not None or want >= max_parts or want >= count:
            break
        want += 1
    if ends is None:
        ends = _eager(prefix, max_parts, cap)
    return ends


def _prefer(pieces: Sequence[_Piece], ends: Sequence[int], cap: int,
            window: int) -> List[int]:
    """在目标切点附近挑层级更好的那个切点：句末优先于逗号。

    ends 是 _plan 按总长算出来的目标切点（只保证每泡不超 cap、长度尽量匀）。这里逐个边界再问一遍：
    前后 window 个字以内还有别的合法切点吗？有更好的层级（tier 更小）就换过去，没有就原样退回。

    换的时候三条老判据一条都不放松：一泡仍然不超过 cap（前后两泡都要核）、不越过下一条边界
    （不切出空泡、不改条数）、动的只是「在第几个原子片之后断」——拼回来逐字等于原文。
    挑选顺序：先看层级，同层级再挑离目标最近的；跟原层级一样好或更差的候选不采纳。
    """
    prefix = _prefix(pieces)
    count = len(pieces)
    total = prefix[-1]
    out = list(ends)
    start = 0
    for index in range(len(out) - 1):
        original = out[index]
        limit = out[index + 1]                 # 下一条边界还没动，就是这次能走到的最远处
        rest = len(out) - index - 2            # 这条之后再切几泡
        base = prefix[original]
        chosen = None
        chosen_key = None
        for cand in range(start + 1, limit):
            offset = prefix[cand]
            if offset - prefix[start] > cap:   # 这一泡会超长；再往后只会更长
                break
            if abs(offset - base) > window:     # 只认目标切点附近的
                continue
            if prefix[limit] - offset > cap:     # 下一泡会超长
                continue
            if total - offset > (rest + 1) * cap:   # 余下的字切不完
                continue
            if count - cand < rest + 1:             # 余下的原子片不够分
                continue
            key = (pieces[cand - 1].tier, abs(offset - base))
            if chosen_key is None or key < chosen_key:
                chosen, chosen_key = cand, key
        if chosen is not None and chosen_key[0] < pieces[original - 1].tier:
            out[index] = chosen
        start = out[index]
    return out


def _balanced(prefix: Sequence[int], want: int, cap: int) -> Any:
    """把原子片切成 want 泡、每泡不超过 cap，并让每泡长度尽量靠近平均值；切不出就返回 None。"""
    count = len(prefix) - 1
    want = max(1, min(want, count))
    total = prefix[-1]
    ends: List[int] = []
    start = 0
    for rest in range(want, 0, -1):
        if rest == 1:
            if total - prefix[start] > cap:
                return None
            ends.append(count)
            return ends
        remain = total - prefix[start]
        target = prefix[start] + -(-remain // rest)
        best = None
        for index in range(start + 1, count):
            if prefix[index] - prefix[start] > cap:
                break
            if total - prefix[index] > (rest - 1) * cap:
                continue
            if best is None or abs(prefix[index] - target) < abs(prefix[best] - target):
                best = index
        if best is None:
            return None
        ends.append(best)
        start = best
    return ends


def _eager(prefix: Sequence[int], want: int, cap: int) -> List[int]:
    """退化路径：前几泡尽量装满 cap，剩下的全部并进最后一泡。"""
    count = len(prefix) - 1
    want = max(1, min(want, count))
    ends: List[int] = []
    start = 0
    for rest in range(want, 1, -1):
        far = start + 1
        while far < count and prefix[far] - prefix[start] <= cap:
            far += 1
        end = min(max(far - 1, start + 1), count - (rest - 1))
        ends.append(end)
        start = end
    ends.append(count)
    return ends


def _build(pieces: Sequence[_Piece], ends: Sequence[int]) -> List[Seg]:
    """按结尾下标把原子片拼成泡；一泡里只要有一片是硬切的，这一泡就算硬切。"""
    parts: List[Seg] = []
    start = 0
    for end in ends:
        group = list(pieces[start:end])
        start = end
        if not group:
            continue
        reasons: List[str] = []
        degraded = False
        for piece in group:
            if piece.degraded:
                degraded = True
                if piece.reason and piece.reason not in reasons:
                    reasons.append(piece.reason)
        parts.append(Seg(text="".join(piece.text for piece in group),
                         gap_before=0.0, send_at=0.0,
                         degraded=degraded, reason="；".join(reasons)))
    return parts
