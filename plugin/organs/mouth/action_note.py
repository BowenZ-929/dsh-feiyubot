"""检查括注中的环境词，不用身体词与地点词的共现判断普通正文。

这里只识别括注形式，不能核实图片或上下文是否提供了场景。
环境词、括号对和反馈模板从 warehouse 读取；不取时钟、不联网、不写盘。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .warehouse import load

STATUS_OK = "ok"                 # 真的判过了：这一轮给了判据表
STATUS_NOT_CHECKED = "not-checked"   # 没给判据表，这一关没跑；不是通过

__all__ = ["ActionBroken", "ActionHit", "STATUS_OK", "STATUS_NOT_CHECKED",
           "check", "clear_reason"]


class ActionBroken(ValueError):
    """动作括注的判据表不合格：缺节、缺键、词表写成了一整串字、括号对不成对，都算，坏一处就当场响。

    报错信息里带着那一项的名字：**不许拿一个空词表当「这一关没有判据」**，那等于静默放行，
    而且没人知道她为什么突然又开始编场景（CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("动作括注判据 %s%s（判据数据在 organs/mouth/data/action_note.json，"
                         "由 warehouse.load(\"action_note\") 读出）" % (where, detail))


@dataclass(frozen=True)
class ActionHit:
    """一次「动作里带了场景」的证据：命中的那一段、哪一路、哪个环境词、一句人话。

    字段（api 与闸按这个接，改字段等于改接口）：
      matched    命中的括注原文，不含括号
      where      命中位置的名称，取值来自数据
      env_word   踩到的那个环境词，同样来自数据
      reason     一句人话，里面带着命中的那一段与那个词
      clause_id  闸用的 clause_id，取自数据——闸不自己造名字，弃言账本里看到的就是数据里那个
      kind       闸用的 kind，取自数据，理由同上
    """

    matched: str
    where: str
    env_word: str
    reason: str
    clause_id: str
    kind: str


@dataclass(frozen=True)
class _Rules:
    """判据表拆成一堆可以直接用的值：数据里没有的一样都不补。"""

    env_words: Tuple[str, ...]
    body_parts: Tuple[str, ...]  # split 共用的动作行词表，不用于正文场景检查。
    brackets: Tuple[Tuple[str, str], ...]
    where_bracket: str
    clause_id: str
    kind: str
    reason_hit: str
    reason_clear: str


def check(text: str, table: Optional[Mapping[str, Any]] = None) -> Optional[ActionHit]:
    """括注命中环境词时返回证据，普通正文不在这一关判断。"""
    rules = _rules(table)
    body = _text_of(text)
    inner, word = _in_bracket(body, rules)
    if inner is not None:
        return _hit(rules, rules.where_bracket, inner, word)
    return None


def clear_reason(table: Optional[Mapping[str, Any]] = None) -> str:
    """放行那一句人话（数据里的 reason.clear），原样填出来。

    为什么要单独一个入口：check 的返回类型是 Optional[ActionHit]，装不下「没命中 + 为什么放行」
    这一种读数；把放行塞成一条假 ActionHit，会把「没命中」与「判过没问题」挤进同一条通道，
    调用方再也分不开。闸把这一句带进 Verdict.reason，面板便看得见这一关查过了。
    """
    return _fill(_rules(table).reason_clear, "reason.clear")


def _in_bracket(body: str, rules: _Rules) -> Tuple[Optional[str], str]:
    """按位置检查完整括号对；同一片段中按词表顺序取第一个命中。"""
    found: List[Tuple[int, str]] = []
    for opener, closer in rules.brackets:
        pattern = re.compile(re.escape(opener) + "(.*?)" + re.escape(closer), re.DOTALL)
        for one in pattern.finditer(body):
            found.append((one.start(), one.group(1)))
    for _at, inner in sorted(found, key=lambda pair: pair[0]):
        word = _first_word(inner, rules.env_words)
        if word:
            return inner.strip(), word
    return None, ""


def _first_word(text: str, words: Sequence[str]) -> str:
    """词表里第一个出现在这段字里的词；一个都没有返回空串（词表顺序即优先级）。"""
    for word in words:
        if word in text:
            return word
    return ""


def _hit(rules: _Rules, where: str, matched: str, env_word: str) -> ActionHit:
    """凑一次命中的证据：两个闸用的名字取自数据，reason 由数据里的模板填出来。"""
    return ActionHit(matched=matched, where=where, env_word=env_word,
                     reason=_fill(rules.reason_hit, "reason.hit",
                                  where=where, matched=matched, env_word=env_word),
                     clause_id=rules.clause_id, kind=rules.kind)


def _rules(table: Optional[Mapping[str, Any]] = None) -> _Rules:
    """把判据表读成一份只读规则；table 为 None 就去仓库现读默认表。

    缺节缺键一律抛 ActionBroken——数据里没有的词不在代码里补默认值（补了就是第二处真源）。
    """
    book = table if isinstance(table, Mapping) else load("action_note")
    if not isinstance(book, Mapping):
        raise ActionBroken("数据表", "不是一份键值表，读到的 %s。" % type(book).__name__)
    where = _mapping(book, "where")
    hit = _mapping(book, "hit")
    reason = _mapping(book, "reason")
    rules = _Rules(env_words=_words(book, "env_words"),
                   body_parts=_words(book, "body_parts"),
                   brackets=_pairs(book, "brackets"),
                   where_bracket=_word(where, "bracket"),
                   clause_id=_word(hit, "clause_id"),
                   kind=_word(hit, "kind"),
                   reason_hit=_word(reason, "hit"),
                   reason_clear=_word(reason, "clear"))
    return rules


def _text_of(text: Any) -> str:
    """要判的那段输出：不是字符串就当空串，判据只认字。"""
    return text if isinstance(text, str) else ("" if text is None else str(text))


def _need(data: Mapping[str, Any], key: str) -> Any:
    """取一项；缺了就抛，不补默认值。"""
    try:
        return data[key]
    except (KeyError, TypeError):
        raise ActionBroken("数据", "缺少 %r 这一项。" % key) from None


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """取一节，必须是键值表。"""
    value = _need(data, key)
    if not isinstance(value, Mapping):
        raise ActionBroken("数据", "的 %r 不是一份键值表，读到的 %s。" % (key, type(value).__name__))
    return value


def _word(data: Mapping[str, Any], key: str) -> str:
    """取一句非空字符串；空着也算缺。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise ActionBroken("数据", "的 %r 必须是一句非空字符串。" % key)
    return value


def _words(data: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    """取一张词表：非空清单、每项非空字符串；不接受「一整串字」冒充清单。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise ActionBroken("数据", "的 %r 必须是非空清单（每项一个非空字符串），不是一整串字。" % key)
    out: List[str] = []
    for one in value:
        if not isinstance(one, str) or not one.strip():
            raise ActionBroken("数据", "的 %r 里有不是字符串、或空着的项。" % key)
        out.append(one)
    return tuple(out)


def _pairs(data: Mapping[str, Any], key: str) -> Tuple[Tuple[str, str], ...]:
    """取一组括号对：非空清单，每项是两个各自非空、且互不相同的字符串。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise ActionBroken("数据", "的 %r 必须是非空清单（每项一对括号：开一个字符、关一个字符）。" % key)
    out: List[Tuple[str, str]] = []
    for index, one in enumerate(list(value)):
        where = "%r 的第 %d 对" % (key, index + 1)
        if isinstance(one, str) or not isinstance(one, (list, tuple)) or len(one) != 2:
            raise ActionBroken("数据", "的 %s 不是一对（要正好两个字符：开与关）。" % where)
        opener, closer = one[0], one[1]
        for part in (opener, closer):
            if not isinstance(part, str) or not part.strip():
                raise ActionBroken("数据", "的 %s 里有空着的一半（开与关都得写）。" % where)
        if opener == closer:
            raise ActionBroken("数据", "的 %s 开与关是同一个字符 %r：这样括号片段取不出来。"
                               % (where, opener))
        out.append((opener, closer))
    return tuple(out)


def _fill(template: str, spot: str, **fields: Any) -> str:
    """把模板填成人话；模板里有数据没填的占位符就抛，别把 {matched} 这种半成品发出去。

    spot 是模板在哪一节（reason.hit 之类），填不出来时好指名；它不叫 where，是因为命中模板
    自己就有一个 {where} 占位符，两个名字撞上会当场 TypeError。
    """
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise ActionBroken(str(spot), " 这句填不出来：%s" % exc) from None
