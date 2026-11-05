"""嘴的动作括注判据：她可以写自己的动作与表情，但不许顺手写我们没给过的场景。

**这一关治的是「编场景」。** 动作是她自己的（部位名与表情词都在数据里），写下来没问题；问题出在
动作顺带描述了一个我们没给过的处境——那个处境不是她「有」的，是她补的。原句与它的毛病收在
organs/mouth/data/action_note.json 的 note 里：**例子可以进数据，词表与说法一律不进代码。**
动作括注这一关只认一件事：这一句（或这一对括号）里，有没有把「她自己的身体」和「我们没给的
场景」写在一起。

**判据全在数据里**（经 warehouse.load("action_note") 只读借入）：环境词表、身体部位与表情词表、
括注括号对、分句用的句末标点、两个 where 的说法、闸用的两个名字（clause_id 与 kind）、命中与
放行各一句人话模板。本文件不写死一个词、一句说法——改判据是改数据，代码不动。缺键、坏形状当场
抛 ActionBroken，**不补默认值**：补一个默认词表，等于代码里又多了一处真源。

**两路都认，松紧不同**（这是这一关的全部内容）：

- **括号内**（严）：取出每一对括号里的片段，片段里出现环境词表任一 → 命中。括号是她加「画外音」
  的地方，那里出现场景词，等于用括号补了一个场景。
- **括号外**（松）：先把括号片段整段挖空，再按句末标点／换行分句；某一句里**既有身体部位、
  又有环境词** → 命中。两者必须同句——只有场景没有身体部位的那句不是动作，只有动作没有场景的
  那句也不带场景，两条都放行。**放行要有据**：放行那句话也写在数据里，由 clear_reason 取出来，
  由闸带进 Verdict.reason（面板上因此看得见「这一关查过了」，而不是只看见一片安静）。

**返回的不是布尔。** check 命中返回一条 ActionHit（命中的那一段、哪一路、踩到的是哪个环境词、
一句人话），没命中返回 None——布尔说不出「为什么拦」，日志与面板也就没法复盘。词表顺序即优先级：
先报词表里靠前的那个词，先报排在前面的那一段。判据表形状坏掉当场抛 ActionBroken，照实往上抛。

纯：不 import time / datetime / random，不取时钟、不联网、不写盘；文件 IO 只经 warehouse。
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
      matched    命中的那一段原文：括号内是括号里的片段，括号外是那句话（都没有括号与句末标点）
      where      哪一路命中的，取值来自数据（括号内 / 身体句）
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
    body_parts: Tuple[str, ...]
    brackets: Tuple[Tuple[str, str], ...]
    sentence_marks: str
    where_bracket: str
    where_sentence: str
    clause_id: str
    kind: str
    reason_hit: str
    reason_clear: str


def check(text: str, table: Optional[Mapping[str, Any]] = None) -> Optional[ActionHit]:
    """看这段字有没有把身体动作和我们没给过的场景写在一起：命中给一条证据，没命中给 None。

    table 是判据表（warehouse.load("action_note") 读出来的那份），传 None 就现读仓库里的默认表。

    先查括号内（严），没中再查括号外（松）；两路都中的情况下报的是括号内——括号是她自己加的
    画外音，那里出问题更要紧。判据表缺键缺节抛 ActionBroken，报错里点名是哪一项。
    """
    rules = _rules(table)
    body = _text_of(text)
    inner, word = _in_bracket(body, rules)
    if inner is not None:
        return _hit(rules, rules.where_bracket, inner, word)
    sentence, word = _in_sentence(body, rules)
    if sentence is not None:
        return _hit(rules, rules.where_sentence, sentence, word)
    return None


def clear_reason(table: Optional[Mapping[str, Any]] = None) -> str:
    """放行那一句人话（数据里的 reason.clear），原样填出来。

    为什么要单独一个入口：check 的返回类型是 Optional[ActionHit]，装不下「没命中 + 为什么放行」
    这一种读数；把放行塞成一条假 ActionHit，会把「没命中」与「判过没问题」挤进同一条通道，
    调用方再也分不开。闸把这一句带进 Verdict.reason，面板便看得见这一关查过了。
    """
    return _fill(_rules(table).reason_clear, "reason.clear")


def _in_bracket(body: str, rules: _Rules) -> Tuple[Optional[str], str]:
    """括号内那一路：按括号出现的先后取片段，第一段带环境词的就算命中。

    同一段里踩到多个环境词时报词表里靠前的那个（词表顺序即优先级）；括号没配对（只有开没有关）
    就取不到片段，那一段的字仍留在句子里，交给括号外那一路判——不许因为括号写歪了就不判。
    """
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


def _in_sentence(body: str, rules: _Rules) -> Tuple[Optional[str], str]:
    """括号外那一路：挖掉括号片段、分句，某一句里身体部位与环境词同时出现才算命中。

    必须同句：跨句凑出来的「身体 + 场景」不是动作括注，是一句话在说事、另一句话在说环境，
    那种放行是有据的（数据里的 reason.clear 就是这么写的）。
    """
    for sentence in _sentences(_without_brackets(body, rules), rules.sentence_marks):
        if not _first_word(sentence, rules.body_parts):
            continue
        word = _first_word(sentence, rules.env_words)
        if word:
            return sentence, word
    return None, ""


def _without_brackets(body: str, rules: _Rules) -> str:
    """把括号片段整段挖空：括号里的字已由括号内那一路判过，不再算进句子，免得同一段话被两路重复算账。"""
    out = body
    for opener, closer in rules.brackets:
        pattern = re.compile(re.escape(opener) + ".*?" + re.escape(closer), re.DOTALL)
        out = pattern.sub(" ", out)
    return out


def _sentences(text: str, marks: str) -> List[str]:
    """按句末标点与换行分句，空的段丢掉；标点本身不进返回的句子（reason 里读起来才干净）。"""
    chars = "".join(dict.fromkeys(marks))          # 去重，别让同一个字符重复进字符类
    pattern = re.compile("[%s]" % "".join(re.escape(ch) for ch in chars))
    return [one.strip() for one in pattern.split(text) if one.strip()]


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
    两个 where 的说法必须不一样：写成同一个词，面板上就分不出这话是从括号里看出来的，
    还是从整句里看出来的。
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
                   sentence_marks=_chars(book, "sentence_marks"),
                   where_bracket=_word(where, "bracket"),
                   where_sentence=_word(where, "sentence"),
                   clause_id=_word(hit, "clause_id"),
                   kind=_word(hit, "kind"),
                   reason_hit=_word(reason, "hit"),
                   reason_clear=_word(reason, "clear"))
    if rules.where_bracket == rules.where_sentence:
        raise ActionBroken("数据", "里 where.bracket 与 where.sentence 是同一个词 %r："
                                   "两条路要看得出区别。" % rules.where_bracket)
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


def _chars(data: Mapping[str, Any], key: str) -> str:
    """取一串非空字符（分句用的句末标点与换行），按字符逐个用。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise ActionBroken("数据", "的 %r 必须是一串非空字符（分句就是按这里的字符切）。" % key)
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
