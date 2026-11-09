"""输出文字的字面重合观察，不据此认定语义复读。

比较去标点的完整文字行和去空白的前 opening_chars 个字，窗口为最近 rounds 条。
保留 action_line 数据键以兼容旧记录，实际比较所有文字行。
RepeatHit 只描述重合；gate 记录 observed，保留原话，不触发模型改写。
明确请求复述且确有重合时返回 RepeatSkip，保留原有请求审计。
请求标记由 requested 统一供本模块与复述审计使用。
recent=None 表示未获得历史，与比过但无重合分开记录。
判据来自 warehouse.load("repeat")；本模块不联网、不记账、不取时钟。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union

from .warehouse import load

STATUS_OBSERVED = "observed"          # 字面重合只作观察，不据此改写或丢弃
STATUS_OK = "ok"                     # 真的比过了：这一轮拿到了最近几条输出
STATUS_NOT_CHECKED = "not-checked"   # 没拿到历史，这一关没跑；不是通过
STATUS_SKIPPED = "skipped-by-request"  # 撞上了重复，但对方要她再说一遍：只记账，不判废

__all__ = ["RepeatBroken", "RepeatHit", "RepeatSkip", "STATUS_OK", "STATUS_NOT_CHECKED",
           "STATUS_SKIPPED", "STATUS_OBSERVED", "check", "clear_reason", "names", "requested"]


class RepeatBroken(ValueError):
    """去重的判据表不合格：缺节、缺键、数不是正整数、字符表写成空串都算，坏一处就当场响。

    报错信息里带着那一项的名字：**不许拿一个空表当「这一关没有判据」**，那等于静默放行，
    而且没人知道她为什么突然又开始复读（CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("去重判据 %s%s（判据数据在 organs/mouth/data/repeat.json，"
                         "由 warehouse.load(\"repeat\") 读出）" % (where, detail))


@dataclass(frozen=True)
class RepeatHit:
    """一次「在反复念」的证据：哪一路、撞上的那几个字、上一次在第几条、一句人话。

    字段（api 与闸按这个接，改字段等于改接口）：
      kind        哪一路重复，取值来自数据（开场那一路 / 动作行那一路）
      matched     撞上的那几个字：动作行是归一化之后的整行，开场是那前几个字
      prev_index  上一次出现在 recent 里的下标（0 起，按调用方给的顺序）；reason 里按人话从 1 数
      reason      一句人话，里面带着撞上的那几个字、窗口大小与第几条，账本与面板直接用这句
    """

    kind: str
    matched: str
    prev_index: int
    reason: str


@dataclass(frozen=True)
class RepeatSkip:
    """一次「本来会判废、但对方明说要她再说一遍」的读数：跳过，不是命中。

    为什么单独一个类型、而不是往 RepeatHit 里加一个布尔：命中与跳过是两件事——命中要把这句拦下来，
    跳过要把这句放出去。塞进同一个类型，闸那边就得先读一个字段才知道手上是什么，早晚有一天会读反；
    而 check 的返回类型装得下两种读数、装不下「都发生了」，所以两边各一个类型，闸用 isinstance 分。

    字段（闸按这个接，改字段等于改接口）：
      hit        本来会判废的那一条（撞上的那几个字、上一次在第几条、那一句人话）
      clause_id  闸记账用的名字，取自数据（hit.skip_clause_id 那一项）：跳过记的不是判废那一笔
      status     这一关的状态，固定 STATUS_SKIPPED（skipped-by-request）
      reason     一句人话，里面带着 skipped-by-request，账本与面板直接用这句
    """

    hit: RepeatHit
    clause_id: str
    status: str
    reason: str


@dataclass(frozen=True)
class _Rules:
    """判据表拆成一堆可以直接用的值：数据里没有的一样都不补。"""

    opening_chars: int
    rounds: int
    strip: str
    blank: str
    kind_opening: str
    kind_action: str
    clause_id: str
    gate_kind: str
    skip_clause_id: str
    request_markers: Tuple[Any, ...]
    tag: str
    reason_opening: str
    reason_action: str
    skip_reason: str
    reason_clear: str


def check(text: str, recent: Optional[Sequence[str]],
          table: Optional[Mapping[str, Any]] = None,
          asked: str = "",
          requested: Optional[bool] = None) -> Optional[Union[RepeatHit, RepeatSkip]]:
    """看她这句是不是在最近 rounds 条里已经把同一动作行、或同一开场念过一遍：给证据、或给跳过，否则 None。

    table 是判据表（warehouse.load("repeat") 读出来的那份），传 None 就现读仓库里的默认表；
    recent 是她最近若干条输出的原文（旧的在先），**None 表示这一轮拿不到历史**；
    asked 是对方这一轮说的那句话——他明说要她重复时，撞上重复也只记账、不判废。
    requested 是「问过一次的结论」：闸拿 requested(asked, table) 算一次，再把那个布尔交给这一关
    与复述审计两处，免得同一句话被两套标记表判出两个结果。默认 None 表示调用方没算过，就现算一次
    （算的还是 requested() 那一处，标记表仍旧只有一份）。

    三种读数分开：

    - **RepeatHit**：撞上了，对方也没要求重复 → 这一关判废。
    - **RepeatSkip**：撞上了，但对方明说要她再说一遍（asked 命中 request_markers）→ 只记账、不判废。
      **先撞上、再问请求**：没撞上就没有「判废」可放，不记这一笔账，也不返回跳过。
    - **None**：没撞上，或者 recent 为 None（这一轮拿不到历史、这一关没跑）。

    recent 为 None 时返回 None——那是「没判」，不是「判过没重复」。这个返回类型装不下全部状态，
    所以判没判由调用方看状态（STATUS_OK / STATUS_NOT_CHECKED / STATUS_SKIPPED）并挂到
    Verdict.repeat_status 上；在这里塞一条假命中，等于把「没命中」与「判不了」挤进同一条通道，
    调用方再也分不开。asked 为空一律不豁免（不许默认豁免）。

    recent 的形状坏掉（不是清单、某一项不是字符串）当场抛 RepeatBroken 并指名第几条。
    """
    rules = _rules(table)
    if recent is None:
        return None
    window = _window(recent, rules.rounds)
    if not window:
        return None
    body = _text_of(text)
    found = _same_action_line(body, window, rules) or _same_opening(body, window, rules)
    if found is None:
        return None
    if not (_requested(asked, rules) if requested is None else bool(requested)):
        return found
    return RepeatSkip(hit=found, clause_id=rules.skip_clause_id, status=STATUS_SKIPPED,
                      reason=_fill(rules.skip_reason, "skip_reason",
                                   matched=found.matched, prev_no=found.prev_index + 1,
                                   rounds=rules.rounds, tag=rules.tag, kind=found.kind))


def requested(asked: Any, table: Optional[Mapping[str, Any]] = None) -> bool:
    """对方这一句是不是在要她重复刚才那句：**判定只有这一处**，闸算一次、两关共用。

    为什么公开成函数：请求标记表（request_markers）是这一条豁免的唯一真源，而豁免要落在两关上
    （内容重复、复述审计）。两关各判一次就各有一份词表，迟早会分岔成「这一关当他要重复、那一关
    不认」——同一句话在账本上就有两种说法。所以闸拿这一个布尔，同时交给 check 与 audit.check。

    table 是判据表（warehouse.load("repeat") 读出来的那份），传 None 就现读仓库里的默认表；
    asked 为空（None、空串、全空白）一律是假：**不许默认豁免**，默认豁免等于这一关白设。
    匹配规则全在数据的 request_markers 里，代码里一条都不列。
    """
    return _requested(asked, _rules(table))


def _requested(asked: Any, rules: _Rules) -> bool:
    """对方这一句是不是在要她重复刚才那句：正则表在数据里，代码里一个字都不列。

    asked 为空（None、空串、全空白）一律不算请求：**不许默认豁免**——默认豁免等于这一关白设。
    只要命中表里任何一条就算请求，不必问「他要重的是不是这一句」：这一关判的是她有没有重复，
    不是对方那句话指得准不准。
    """
    body = _text_of(asked)
    if not body.strip():
        return False
    return any(marker.search(body) is not None for marker in rules.request_markers)


def clear_reason(table: Optional[Mapping[str, Any]] = None) -> str:
    """放行那一句人话（数据里的 reason.clear），原样填出来。

    为什么要单独一个入口：check 的返回类型是 Optional[RepeatHit]，装不下「没命中 + 为什么放行」
    这一种读数；把放行塞成一条假命中，会把「没命中」与「判过没问题」挤进同一条通道。闸把这一句
    带进 Verdict.reason，面板上便看得见这一关查过了，而不是只看见一片安静。
    """
    rules = _rules(table)
    return _fill(rules.reason_clear, "reason.clear", rounds=rules.rounds)


def names(table: Optional[Mapping[str, Any]] = None) -> Tuple[str, str]:
    """闸用的两个名字（clause_id, kind），都取自数据，闸不自己造名字。

    为什么单独开一个入口、而不是像动作括注那样塞进返回的那条证据里：RepeatHit.kind 记的是
    「哪一路重复」（开场那一路 / 动作行那一路），与闸挂在 Hit 上的 kind（数据里另一个名字）不是同一件事；
    硬塞进一个字段会让两个 kind 打架，读账本的人也就分不清看的是哪一个。
    """
    rules = _rules(table)
    return rules.clause_id, rules.gate_kind


def _same_action_line(body: str, window: Sequence[Tuple[int, str]],
                      rules: _Rules) -> Optional[RepeatHit]:
    """动作行那一路：去空白与标点之后逐字相等就算重复，报的是最近的那一次。

    比的是**整行**（换行切开的行），所以单行输出就是整句。逐字相等是为了守住她的分寸：
    「蜷了一下」与「蜷了两下」只差一个字，按相似度比会并成一条，把差别抹掉。
    """
    old = [(index, _action_lines(one, rules)) for index, one in window]
    for line in _action_lines(body, rules):
        for index, lines in reversed(old):
            if line in lines:
                return RepeatHit(kind=rules.kind_action, matched=line, prev_index=index,
                                 reason=_fill(rules.reason_action, "reason.action_line",
                                              matched=line, prev_no=index + 1,
                                              rounds=rules.rounds, tag=rules.tag))
    return None


def _same_opening(body: str, window: Sequence[Tuple[int, str]],
                  rules: _Rules) -> Optional[RepeatHit]:
    """开场那一路：去空白之后前 opening_chars 个字相等就算重复，报的是最近的那一次。

    字数不够 opening_chars 的一律不参与比较（_head 返回空串）：一句「好」不该把两句毫不相干的
    话判成同一个开场。两个开场能比，前提是它们都取满了这么多字。
    """
    head = _head(body, rules)
    if not head:
        return None
    for index in range(len(window) - 1, -1, -1):
        old_index, old_text = window[index]
        if _head(old_text, rules) == head:
            return RepeatHit(kind=rules.kind_opening, matched=head, prev_index=old_index,
                             reason=_fill(rules.reason_opening, "reason.opening",
                                          matched=head, prev_no=old_index + 1,
                                          rounds=rules.rounds, tag=rules.tag))
    return None


def _head(text: str, rules: _Rules) -> str:
    """她的开场：去掉空白之后的前 opening_chars 个字；不够这么多字就是空串（不参与比较）。"""
    clean = _drop(text, rules.blank)
    if len(clean) < rules.opening_chars:
        return ""
    return clean[:rules.opening_chars]


def _action_lines(text: str, rules: _Rules) -> List[str]:
    """把一段输出按行切开，再去空白与标点：空行（去完什么都不剩）不算一行。

    「行」就是换行：她的动作行本来就是一行一件事；单行输出因此整句算一行，同一句原样回来照样算重复。
    """
    out: List[str] = []
    for one in _lines(text):
        clean = _drop(one, rules.strip)
        if clean:
            out.append(clean)
    return out


def _lines(text: str) -> List[str]:
    """按换行切行；三种换行写法（\\n、\\r\\n、\\r）切出来的行一样多。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _drop(text: str, chars: str) -> str:
    """把表里的字符逐个去掉：表来自数据，代码里一个字都不列。"""
    table = set(chars)
    return "".join(ch for ch in text if ch not in table)


def _window(recent: Sequence[str], rounds: int) -> List[Tuple[int, str]]:
    """只看最近 rounds 条，带回每一条在 recent 里的下标（0 起）；形状坏了当场抛并指名第几条。

    recent 是一串字符串，不是字符串本身——传一整段话进来是在赌「按字切」，那会把她的历史读成
    一个字一条，判出来的重复全是假的，所以当坏形状抛。
    """
    if isinstance(recent, str) or not isinstance(recent, (list, tuple)):
        raise RepeatBroken("recent", "只能是她最近几条输出的原文清单（旧的在先），读到的是 %s。"
                           % type(recent).__name__)
    rows = list(recent)
    start = max(0, len(rows) - rounds)
    out: List[Tuple[int, str]] = []
    for index in range(start, len(rows)):
        one = rows[index]
        if not isinstance(one, str):
            raise RepeatBroken("recent 的第 %d 条（从 0 数起）" % index,
                               "不是字符串，读到的是 %s。" % type(one).__name__)
        out.append((index, one))
    return out


def _text_of(text: Any) -> str:
    """要判的那段输出：不是字符串就当空串，判据只认字。"""
    return text if isinstance(text, str) else ("" if text is None else str(text))


def _rules(table: Optional[Mapping[str, Any]] = None) -> _Rules:
    """把判据表读成一份只读规则；table 为 None 就去仓库现读默认表。

    缺节缺键一律抛 RepeatBroken——数据里没有的数不在代码里补默认值（补了就是第二处真源）。
    两条判据的名字必须不一样：写成同一个词，面板上就分不出这句是「开场没换」还是「整行照抄」。
    """
    book = table if table is not None else load("repeat")
    if not isinstance(book, Mapping):
        raise RepeatBroken("数据表", "不是一份键值表，读到的 %s。" % type(book).__name__)
    hit = _mapping(book, "hit")
    kinds = _mapping(book, "kinds")
    reason = _mapping(book, "reason")
    rules = _Rules(opening_chars=_above_zero(book, "opening_chars"),
                   rounds=_above_zero(book, "rounds"),
                   strip=_chars(book, "strip"),
                   blank=_chars(book, "blank"),
                   kind_opening=_word(kinds, "opening"),
                   kind_action=_word(kinds, "action_line"),
                   clause_id=_word(hit, "clause_id"),
                   gate_kind=_word(hit, "kind"),
                   skip_clause_id=_word(hit, "skip_clause_id"),
                   request_markers=_patterns(book, "request_markers"),
                   tag=_word(book, "tag"),
                   reason_opening=_word(reason, "opening"),
                   reason_action=_word(reason, "action_line"),
                   skip_reason=_word(book, "skip_reason"),
                   reason_clear=_word(reason, "clear"))
    if rules.kind_opening == rules.kind_action:
        raise RepeatBroken("数据", "里 kinds.opening 与 kinds.action_line 是同一个词 %r："
                                   "两条路要看得出区别。" % rules.kind_opening)
    return rules


def _need(data: Mapping[str, Any], key: str) -> Any:
    """取一项；缺了就抛，不补默认值。"""
    try:
        return data[key]
    except (KeyError, TypeError):
        raise RepeatBroken("数据", "缺少 %r 这一项。" % key) from None


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """取一节，必须是键值表。"""
    value = _need(data, key)
    if not isinstance(value, Mapping):
        raise RepeatBroken("数据", "的 %r 不是一份键值表，读到的 %s。" % (key, type(value).__name__))
    return value


def _word(data: Mapping[str, Any], key: str) -> str:
    """取一句非空字符串；空着也算缺。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise RepeatBroken("数据", "的 %r 必须是一句非空字符串。" % key)
    return value


def _chars(data: Mapping[str, Any], key: str) -> str:
    """取一张字符表（标点表、空白表）：非空字符串，按字符逐个用。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value:
        raise RepeatBroken("数据", "的 %r 必须是一串非空字符（表里的字符会被逐个去掉）。" % key)
    return value


def _patterns(data: Mapping[str, Any], key: str) -> Tuple[Any, ...]:
    """取一张正则表（请求标记），逐条编译好；非清单、空项、编译不过都算坏数据，当场抛。

    为什么当坏数据抛、而不是跳过那一条：跳过一条标记，就等于有一种请求方式永远不豁免——
    这种漏法没有声音，人只会觉得「她怎么又不听我的」。报错里点名是哪一条。
    """
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise RepeatBroken("数据", "的 %r 必须是非空清单（每项一条正则），不是一整串字。" % key)
    out: List[Any] = []
    for index, one in enumerate(list(value)):
        if not isinstance(one, str) or not one.strip():
            raise RepeatBroken("数据", "的 %r 里第 %d 条不是一句非空正则。" % (key, index + 1))
        try:
            out.append(re.compile(one))
        except re.error as exc:
            raise RepeatBroken("数据", "的 %r 里第 %d 条正则编译不过：%s。"
                               % (key, index + 1, exc)) from None
    return tuple(out)


def _above_zero(data: Mapping[str, Any], key: str) -> int:
    """取一个 1 以上的整数（开场几个字、看最近几轮）；布尔不算整数。"""
    value = _need(data, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RepeatBroken("数据", "的 %r 必须是 1 以上的整数，读到的不是。" % key)
    return value


def _fill(template: str, spot: str, **fields: Any) -> str:
    """把模板填成人话；模板里有数据没填的占位符就抛，别把 {matched} 这种半成品发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise RepeatBroken(str(spot), " 这句填不出来：%s" % exc) from None
