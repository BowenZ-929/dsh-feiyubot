"""嘴的切分：只回答「切在哪」，不管「隔多久」。

**一条一句，一泡最多两句。** 按「换行 + 句末标点」断句（一条正则，见 _sentences），断出来的句子
按顺序攒：一泡最多 policy 的 split.sentences_per_part 句（默认 2），攒满就另起一泡。**段间延迟仍归
pace.py**：本文件只填 text / degraded / reason，gap_before 与 send_at 留给 pace.fill；两个文件互不
import，谁也不替对方算。

**整句是动作就自成一泡，不和台词挤一条。** 动作行只认两种形态，两张表都借自
organs/mouth/data/action_note.json（借阅，不另造一份）：
- 括号形态：整句去空白后以括号开头、以对应括号结尾（括号对就是 action_note.json 的 brackets）；
- 无括号形态：整句以身体部位开头（body_parts）、不含人称代词（policy 的 split.action_pronouns）、
  句长不超过 policy 的 split.action_line_max_chars。
**无括号那种形态是必要的，不是补充**：验收那句「……谁肥了。尾巴甩了一下。蓝鲸才叫肥。我这是正常
体型。」里，动作句没有括号，却必须单独成泡——只认括号，这条验收就漏了。反过来「我这是正常体型。」
带人称代词，是台词不是动作，照旧并进隔壁泡。

**两句上限优先于条数上限。** 旧口径「最多 3 泡」与「每泡最多两句」不能同时成立（300 字十来句按
两句一泡就是 6–7 泡），所以 max_parts 从判据退成防炸上限（默认 12，写在数据里）：只有泡数真超过它
才合并相邻两泡，合并优先挑还没攒满两句的那一对——两句上限是主判据，防炸上限是例外，跟旧
max_parts 压过 max_chars 是同一个道理，合并也不丢字。

**max_chars 退成兜底，语义不变。** 旧口径里「每泡不超 max_chars」不再是主判据，只在**单句自己就
超过 max_chars** 时上场：先在语义停顿处切（句末 > 分号 > 换行 > 逗号，切点落不到词中间），一片还是
超长才按字数硬切，硬切出来的那一泡带 degraded=true 与一句人话 reason，好让出口和日志一眼看出这句
是被字数逼的。这条兜底路上 split.cut_window_chars 仍有用：按总长算出来的是目标切点，窗口里还有更
好层级的合法切点就换过去（「句末优先于逗号」这句旧话就活在这一段里）；普通段落不再需要它——切点
本来就落在句末。

切点不许落在词中间：左边不许以连接词/助词收尾（的、地、得、了、着、和、与、跟、把、被、而、但、
或、且、就、都、也、还），右边不许以 的/地/得/了/着 开头。两张字表都在
organs/mouth/data/policy.json，不在代码里——加一批字是改数据，不是改算法。

句数、条数、长度上限、词表、模板一律在数据里：判据与阈值在 policy.json 的 split 节，动作的括号对
与身体部位在 action_note.json。四条约定一起成立：判据全在数据、兜底照旧带 degraded、动作自成一泡、
拼回去与原文逐字相等。

纯函数：入参只有「文本 + 只读的政策 dict + 只读的动作表」，出参只有结构化数据；不碰时钟、不碰随机
数、不写盘（本文件不 import time / datetime / random），所以能离线预览，也能被单独替换。间隔是
pace.py 的事，两个文件互不 import。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Set

from .model import Batch, Policy, PolicyMiss, Seg, need, section
from .warehouse import load

__all__ = ["split"]

ACTION_TABLE = "action_note"      # 动作行那两张表（brackets / body_parts）借自这份数据，不另造


@dataclass(frozen=True)
class _Piece:
    """切分中间的极小片：一段不再被拆的字，带上它是不是被硬切来的、以及断在它后面算第几层。

    tier 是层级序号（0 最好），取自 policy 的 cut_preference；窗口挑切点时只比这个序号。
    """

    text: str
    degraded: bool
    reason: str
    tier: int = 0


@dataclass(frozen=True)
class _Row:
    """已经切好、还没填节奏的一泡。sentences 与 solo 只给防炸上限挑合并对象用，不进 Seg。"""

    text: str
    degraded: bool = False
    reason: str = ""
    sentences: int = 1      # 这一泡里算几句：动作句算一句，超长兜底切出来的一片也算一句
    solo: bool = False      # 自成一泡：不许和邻居并（动作行与超长兜底的每一片）


@dataclass(frozen=True)
class _Action:
    """动作行要用的表：括号对与身体部位借自 action_note.json，人称代词与长度上限取自 policy。"""

    body_parts: Sequence[str]
    brackets: Sequence[Sequence[str]]
    pronouns: str
    max_chars: int


def split(text: str, policy: Policy, action_table: Optional[Mapping[str, Any]] = None) -> Batch:
    """把一段文本切成有序的几泡。

    action_table 是动作行判据表（warehouse.load("action_note") 读出来的那份）；传 None 就现读仓库里的
    默认表。只填 text / degraded / reason；gap_before 与 send_at 留 0.0、summary 留空，等 pace.fill 填。
    空白文本返回空批次（没有要发的泡）。
    """
    rules = section(policy, "split")
    body = text or ""
    if not body.strip():
        return Batch(parts=[], summary="")
    cap = int(need(rules, "max_chars"))
    max_parts = int(need(rules, "max_parts"))
    per_part = int(need(rules, "sentences_per_part"))
    action = _action_rules(rules, action_table)
    rows = _throttle(_rows(body, rules, cap, per_part, action), max_parts)
    return Batch(parts=_segs(rows), summary="")


def _rows(body: str, rules: Mapping[str, Any], cap: int, per_part: int,
          action: _Action) -> List[_Row]:
    """句子攒成一泡一泡：动作句与超长句各自成泡，其余每泡最多 per_part 句。

    超长句先把攒着的那一泡倒掉再切，切出来的几片顺序不变——拼回去仍是原文。
    """
    rows: List[_Row] = []
    group: List[str] = []
    for sentence in _sentences(body, rules):
        if len(sentence) > cap:
            rows.extend(_group_rows(group, per_part))
            group = []
            rows.extend(_overflow_rows(sentence, rules, cap))
            continue
        if _is_action(sentence, rules, action):
            rows.extend(_group_rows(group, per_part))
            group = []
            rows.append(_Row(sentence, solo=True))
            continue
        group.append(sentence)
    rows.extend(_group_rows(group, per_part))
    return rows


def _group_rows(sentences: Sequence[str], per_part: int) -> List[_Row]:
    """普通句子按 per_part 句一泡排开：一泡里句数只由数据定，代码里数的是「第几句」。"""
    rows: List[_Row] = []
    for start in range(0, len(sentences), per_part):
        group = sentences[start:start + per_part]
        rows.append(_Row("".join(group), sentences=len(group)))
    return rows


def _overflow_rows(sentence: str, rules: Mapping[str, Any], cap: int) -> List[_Row]:
    """单句自己就超过 max_chars：老判据原样上场（先在停顿处切，切不动才按字数硬切 + degraded）。

    这是兜底，所以走的还是旧那条路：语义停顿切 → 硬切 → 按长度排泡 → 窗口换更好的切点。
    """
    ranks = {str(name): index for index, name in enumerate(need(rules, "cut_preference"))}
    window = int(need(rules, "cut_window_chars"))
    max_parts = int(need(rules, "max_parts"))
    pieces = _pieces(_segments(sentence, rules), cap, rules, ranks)
    ends = _prefer(pieces, _plan(pieces, cap, max_parts), cap, window)
    return [_Row(part.text, part.degraded, part.reason, solo=True) for part in _build(pieces, ends)]


def _throttle(rows: Sequence[_Row], max_parts: int) -> List[_Row]:
    """防炸上限：泡数超过 max_parts 才合并，优先并「还没攒满两句」的相邻两泡。

    两句上限是主判据，max_parts 只是例外：真到这一步（比如一大片全是动作行），宁可让两句上限退一步，
    也不许让泡数炸开——跟旧 max_parts 压过 max_chars 是同一个道理，合并只拼字，一个字也不丢。
    """
    out = list(rows)
    while len(out) > max_parts:
        chosen = None
        for index in range(len(out) - 1):
            left, right = out[index], out[index + 1]
            if left.solo or right.solo:
                continue                                    # 动作行与超长兜底那几片先不动
            key = (left.sentences + right.sentences, len(left.text) + len(right.text))
            if chosen is None or key < chosen[0]:
                chosen = (key, index)
        index = (chosen[1] if chosen is not None else
                 min(range(len(out) - 1), key=lambda at: len(out[at].text) + len(out[at + 1].text)))
        out[index:index + 2] = [_join_rows(out[index], out[index + 1])]
    return out


def _join_rows(left: _Row, right: _Row) -> _Row:
    """两泡并一泡：degraded 取或，理由去重后接起来（被字数逼过的痕迹不许在合并里消失）。"""
    reasons = [one for one in (left.reason, right.reason) if one]
    return _Row(left.text + right.text,
                degraded=left.degraded or right.degraded,
                reason="；".join(dict.fromkeys(reasons)),
                sentences=left.sentences + right.sentences,
                solo=True)


def _segs(rows: Sequence[_Row]) -> List[Seg]:
    """把切好的泡落成 Seg：节奏留 0.0 等 pace.fill，别的字段一个不动。"""
    return [Seg(text=row.text, gap_before=0.0, send_at=0.0,
                degraded=row.degraded, reason=row.reason) for row in rows]


def _action_rules(rules: Mapping[str, Any], table: Optional[Mapping[str, Any]]) -> _Action:
    """把两张表读成一份只读判据：表借自 action_note.json，缺项当场响，不补默认值。

    补一个默认值就等于有第二处真源：括号对与身体部位只许在 organs/mouth/data/action_note.json 里长大。
    """
    book = table if isinstance(table, Mapping) else load(ACTION_TABLE)
    return _Action(body_parts=tuple(str(part) for part in _borrowed(book, "body_parts")),
                   brackets=tuple((str(pair[0]), str(pair[1]))
                                  for pair in _borrowed(book, "brackets")),
                   pronouns=str(need(rules, "action_pronouns")),
                   max_chars=int(need(rules, "action_line_max_chars")))


def _borrowed(book: Mapping[str, Any], key: str) -> Any:
    """从动作判据表里借一项：缺了就是缺了，报错里点名是哪一张表、哪一项。"""
    try:
        return book[key]
    except (KeyError, TypeError):
        raise PolicyMiss("action_note 的 %r（动作行判据借自 organs/mouth/data/%s.json）"
                         % (key, ACTION_TABLE)) from None


def _is_action(sentence: str, rules: Mapping[str, Any], action: _Action) -> bool:
    """整句是动作吗：先认括号形态（松紧无关，只看整句是不是一个括注），再认无括号形态。

    无括号形态三条一起满足才算：以身体部位开头、不含人称代词、句长不超过 action_line_max_chars。
    三条里少一条它就只是叙述或台词——「我这是正常体型。」带人称代词，不许当动作行。
    """
    body = _core(sentence, rules)
    if not body:
        return False
    for opener, closer in action.brackets:
        if len(body) > len(opener) + len(closer) and body.startswith(opener) and body.endswith(closer):
            return True
    if len(body) > action.max_chars:
        return False                                       # 太长了，那是叙述不是一下动作
    if any(char in body for char in action.pronouns):
        return False                                       # 带人称代词的是台词
    return any(body.startswith(part) for part in action.body_parts)


def _core(sentence: str, rules: Mapping[str, Any]) -> str:
    """整句去掉首尾空白与句末标点：「（尾巴甩了一下）。」与「（尾巴甩了一下）」是同一句。

    尾部引号括号不剥：括号形态要看的正是结尾那个括号；句末标点剥掉，长度比的是句子本身的字数。
    """
    body = (sentence or "").strip()
    return body.rstrip("".join(sorted(set(str(need(rules, "sentence_end")))))).strip()


def _sentences(text: str, rules: Mapping[str, Any]) -> List[str]:
    """按「换行 + 句末标点」断句：一条正则，句尾的引号括号跟着上一句走。

    正则的两个分支就是两种断点：句末标点（连着后面的引号括号）与换行。断出来的每一段都带着自己的
    标点，拼起来逐字等于原文。纯空白、纯停顿的碎片并到邻居里（「……」自己不是一句话）。
    """
    ends = _class_of(str(need(rules, "sentence_end")))
    closers = _class_of(str(need(rules, "trailing_closers")))
    newlines = _class_of(str(need(rules, "newline")))
    pattern = re.compile("[%s][%s]*|[%s]+" % (ends, closers, newlines))
    chunks: List[str] = []
    start = 0
    for found in pattern.finditer(text):
        if found.end() <= start:
            continue
        chunks.append(text[start:found.end()])
        start = found.end()
    if start < len(text):
        chunks.append(text[start:])
    return _merge_filler(chunks, rules)


def _class_of(chars: str) -> str:
    """把一张字表做成字符类里的样子：去重（同一个字不许进两次）并转义。"""
    return "".join(re.escape(char) for char in dict.fromkeys(chars))


def _stop_chars(rules: Mapping[str, Any]) -> Set[str]:
    """所有可以落在其上/其后的停顿字符：句末标点、分号、逗号、换行。"""
    marks: Set[str] = set()
    for key in ("sentence_end", "semicolon", "comma", "newline"):
        marks.update(str(need(rules, key)))
    return marks


def _filler_only(chunk: str, filler: Set[str]) -> bool:
    """这段字只有空白与停顿吗：是的话它自己不成一泡，跟着邻居走（一个字都不丢）。"""
    return not any(not char.isspace() and char not in filler for char in chunk)


def _merge_filler(chunks: Sequence[str], rules: Mapping[str, Any]) -> List[str]:
    """纯空白、纯停顿的碎片并到邻居里：前面有泡就跟前一泡，没有就跟后一泡。

    「……」这种省略号自己不是一句话：跟在前一泡后面读起来才通（「谁肥了。」后面接「……」）。
    只有整段都是碎片的极端情况才会攒出单独一泡，那时也得留着——一泡不许是空的，但一个字也不许丢。
    """
    filler = _stop_chars(rules) | set(str(need(rules, "trailing_closers")))
    merged: List[str] = []
    pending = ""
    for chunk in chunks:
        if _filler_only(chunk, filler):
            if merged:
                merged[-1] += chunk
            else:
                pending += chunk
            continue
        merged.append(pending + chunk)
        pending = ""
    if pending:
        if merged:
            merged[-1] += pending
        else:
            merged.append(pending)
    return merged


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
    return _merge_filler(chunks, rules)


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
