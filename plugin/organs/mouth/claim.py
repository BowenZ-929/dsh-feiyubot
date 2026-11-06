"""嘴的冒充做过判据：没做过的动作，不许说成做过。

**这一关治的是「演一个做完的样子」。** 魂那条红线（bounds.no-fake-doing，enforced_by
mouth·语义复核）说的是原则：没有动作账就不许说做过，做不到就说做不到。**字面不在这里抄**——
魂那边是原则，这边是词表，两边各留一份必然漂移：动词表在这份数据里（data/claim.json 的
action_verbs 与 speech_verbs），代码与注释都不抄魂的文句，这里只指路。原句与它的毛病收在同目录
data/claim.json 的 note 里：**例子可以进数据，词表与说法一律不进代码。**

**判据只有一条**：回复里出现「人称 +（前缀）+（填充）+ 动作类动词 +（后缀）」这种「说成做过的」
写法，而这一轮的动作账里对不上这个动词，就是冒充。前缀、填充词、动作类动词、说话类动词、后缀、
分句标点、命中与放行的人话、闸用的两个名字（clause_id / kind）全在数据里，经
warehouse.load("claim") 只读借入；本文件不写死一个词、一句说法。缺键、坏形状当场抛
ClaimBroken，**不补默认值**：补一个默认词表，等于代码里又多了一处真源。

**三件事必须分得开，这是这一关的全部内容**：

- **真做过（有账）→ 放行**：actions 里有一个动词与句子里那个对得上（真做过的就是实话），这一句
  就是实话，放行。放行要有据，clear_reason 那句人话由闸带进 Verdict.reason。
- **纯说话不算**：句子里那个动词落在数据里的说话类动词表上（「说过了」「记着了」这一类），
  一律豁免。说话不是动作，把说话记成冒充做过，等于逼她不许开口。
- **动作账没给（actions=None）→ 判不了**：返回 None，状态由调用方记成 not-checked。
  **没给账不是「没做过」，也不是「做过」**，所以这里既不判冒充、也不声称判过；
  **actions=[] 是给了账、账上空着**——这一轮确实什么都没做，该判冒充。两者差一个字，
  差的是「判没判」这件事，写错的代价是把一句真话拦下来、或者把一句假话放过去。

**命中返回的不是布尔。** check 返回 ClaimHit（哪个动词、命中的那段原文、那一整句、一句人话、
两个闸用的名字），一个都对不上才返回 None——布尔说不出「为什么拦」，日志、面板、账本也就没法
复盘。同一个动词在账上、在这一句里都出现时按「做过」放行，不报。

**改不了的处置是删句，不是整条丢。** strip_sentence 只删命中的那一句（连同它自己的句末标点），
别的句子一个字不动——删掉那一句之后，后面的句子连原来的标点一起留下，那句话还是她
要说的话。删完只剩空白时返回空串，闸那边按「没有正文可发」弃整条。

纯：不 import time / datetime / random，不取时钟、不联网、不写盘，不碰宿主；文件 IO 只经 warehouse。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .warehouse import load

STATUS_OK = "ok"                     # 真的判过了：这一轮给了动作账（哪怕是空账）
STATUS_NOT_CHECKED = "not-checked"   # 没给动作账，这一关没跑；不是「没冒充」

__all__ = ["ClaimBroken", "ClaimHit", "STATUS_OK", "STATUS_NOT_CHECKED",
           "check", "strip_sentence", "clear_reason", "strip_reason", "blank_reason",
           "leftover_reason"]


class ClaimBroken(ValueError):
    """冒充做过的判据表不合格：缺节、缺键、词表写成了一整串字、前缀表与豁免表撞名、句子分不出来，都算，坏一处就当场响。

    报错信息里带着那一项的名字：**不许拿一个空词表当「这一关没有判据」**，那等于静默放行，
    而且没人知道她为什么又开始演一个做完的样子（CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("冒充做过判据 %s%s（判据数据在 organs/mouth/data/claim.json，"
                         "由 warehouse.load(\"claim\") 读出）" % (where, detail))


@dataclass(frozen=True)
class ClaimHit:
    """一次「说了没做过的动作」的证据：哪个动词、命中的那段原文、那一整句、一句人话、闸用的两个名字。

    字段（api 与闸按这个接，改字段等于改接口）：
      verb      对不上账的那个动作类动词，取自数据里的词表（日志按它分组最好用）
      matched   命中的那段原文（从前缀到后缀的一整段），闸把它挂到 Hit.matched 上
      sentence  matched 所在的那一句，**不带句末标点**——strip_sentence 就是按它删句的
      reason    一句人话，里面带着命中的那一段、那个动词，账本与面板直接用这句
      clause_id 闸用的 clause_id，取自数据——闸不自己造名字，弃言账本里看到的就是数据里那个
      kind      闸用的 kind，取自数据，理由同上
    """

    verb: str
    matched: str
    sentence: str
    reason: str
    clause_id: str
    kind: str


@dataclass(frozen=True)
class _Rules:
    """判据表拆成一堆可以直接用的值：数据里没有的一样都不补。"""

    person: str
    prefixes: Tuple[str, ...]
    bare_prefixes: Tuple[str, ...]
    fillers: Tuple[str, ...]
    action_verbs: Tuple[str, ...]
    speech_verbs: Tuple[str, ...]
    suffixes: Tuple[str, ...]
    sentence_marks: str
    clause_id: str
    kind: str
    reason_hit: str
    reason_clear: str
    reason_strip: str
    reason_blank: str
    reason_leftover: str


def check(text: str, actions: Any, table: Optional[Mapping[str, Any]] = None) -> Optional[ClaimHit]:
    """看这段字有没有说一个这一轮没做过的动作：命中给一条证据，没命中给 None。

    table 是判据表（warehouse.load("claim") 读出来的那份），传 None 就现读仓库里的默认表。

    actions 是这一轮真发生过的动作账，两种形状各是一种结论，**必须分得开**：
    None 表示这一轮拿不到账——这一关没跑，返回 None，状态由调用方记 not-checked；
    清单（哪怕是空清单 []）表示给了账——账上空着就是说这一轮什么都没做，该判冒充。
    清单里每一项要是一份带非空 verb 字符串的键值表（{"verb": "发", ...}），形状坏了当场抛
    ClaimBroken，不许猜成「账上没有这个动作」。

    扫描按出现顺序，第一个「动作类动词对不上账、又不是说话类动词」的就返回：说话类动词一律豁免
    （说话类动词不算动作），账上对得上的动词一律放行（真做过的就是实话）。
    """
    rules = _rules(table)                 # 表先校验：坏表当场响，不许因为这一轮没给账就悄悄跳过
    if actions is None:
        return None
    ledger = _ledger(actions)
    body = _text_of(text)
    for found in _matches(body, rules):
        verb = found.group(1)
        if verb in rules.speech_verbs:
            continue                      # 纯说话不算：说话不是动作，豁免
        if verb in ledger:
            continue                      # 真做过（有账）：放行
        return _hit(rules, body, found, verb)
    return None


def strip_sentence(text: str, hit: ClaimHit,
                   table: Optional[Mapping[str, Any]] = None) -> Tuple[str, str]:
    """只删掉命中的那一句，返回（剩下的文本, 被删掉的那一句）；删不动就原样返回、removed 是空串。

    先按 hit.matched 在文本里定位（最准：同一句话出现两次时删的是真命中的那一次），定位不到再退回
    按 hit.sentence 找第一句一模一样的。**只删那一句连同它自己的句末标点**，别的句子一个字不动，
    逗号也不当句末（一句话里夹着逗号，按逗号切会把后半截跟着那句话一起带走）。
    删完只剩空白时返回空串，闸据此整条弃掉；removed 按原文带回那一句，账本与面板靠它复盘
    「到底删掉了什么」。
    """
    rules = _rules(table)
    body = _text_of(text)
    spot = body.find(hit.matched) if hit.matched else -1
    for start, end, mark_end in _spans(body, rules.sentence_marks):
        inside = (start <= spot < end) if spot >= 0 else (body[start:end].strip() == hit.sentence)
        if not inside:
            continue
        removed = body[start:end].strip()
        rest = (body[:start] + body[mark_end:]).strip()
        return rest, removed
    return body, ""


def clear_reason(table: Optional[Mapping[str, Any]] = None) -> str:
    """放行那一句人话（数据里的 reason.clear），原样填出来。

    为什么要单独一个入口：check 的返回类型是 Optional[ClaimHit]，装不下「没命中 + 为什么放行」
    这一种读数；把放行塞成一条假 ClaimHit，会把「没命中」与「判过没问题」挤进同一条通道，
    调用方再也分不开。闸把这一句带进 Verdict.reason，面板上便看得见这一关查过了。
    """
    return _rules(table).reason_clear


def strip_reason(removed: str, table: Optional[Mapping[str, Any]] = None) -> str:
    """删句救回来那句人话（数据里的 reason.strip）：带上被删掉的那一句，账本靠它认出 fake-doing-stripped。"""
    return _fill(_rules(table).reason_strip, "reason.strip", removed=removed)


def blank_reason(removed: str, table: Optional[Mapping[str, Any]] = None) -> str:
    """删完只剩空白那句人话（数据里的 reason.blank）：没有正文可发，整条弃掉。"""
    return _fill(_rules(table).reason_blank, "reason.blank", removed=removed)


def leftover_reason(clause_id: str, matched: str,
                    table: Optional[Mapping[str, Any]] = None) -> str:
    """删句之后的正文仍被别的判据拦下那句人话（数据里的 reason.leftover）：删过不等于放行。"""
    return _fill(_rules(table).reason_leftover, "reason.leftover",
                 clause_id=clause_id, matched=matched)


def _hit(rules: _Rules, body: str, found: Any, verb: str) -> ClaimHit:
    """凑一次命中的证据：两个闸用的名字取自数据，reason 由数据里的模板填出来。"""
    matched = found.group(0)
    return ClaimHit(verb=verb, matched=matched, sentence=_sentence_at(body, found.start(), rules),
                    reason=_fill(rules.reason_hit, "reason.hit", matched=matched, verb=verb),
                    clause_id=rules.clause_id, kind=rules.kind)


def _matches(body: str, rules: _Rules) -> List[Any]:
    """这段字里所有像「说做过某个动作」的片段，按出现顺序；带不带前缀、带不带后缀两种写法都收。

    两条正则：一条是「前缀 +（填充）+ 动词 +（后缀）」的确定写法；另一条是「裸前缀 +（填充）+
    动词」，后缀可以没有——数据里那句原句说出去照样是冒充，正要去做不等于不要判。
    同一段若被两条都认出来，长的那一条先报（整段，而不是从动词起的那半截）。
    """
    out: List[Any] = []
    for pattern in _patterns(rules):
        out.extend(pattern.finditer(body))
    out.sort(key=lambda one: (one.start(), -len(one.group(0))))
    return out


def _patterns(rules: _Rules) -> Tuple[Any, ...]:
    """按数据拼出两条正则：前缀与动词都编成非捕获组，动词单独一组（group(1) 就是它）。

    词表按长度**从长到短**排序：短词先来会把一个长动词认成它的前半截，词表顺序即优先级这条规矩
    在别处是「先报靠前的那个」，在这里必须让长词先匹配，否则报出来的动词根本不是数据里那一条。
    """
    verbs = _alternation(rules.action_verbs + rules.speech_verbs)
    pre = _alternation(rules.prefixes)
    bare = _alternation(rules.bare_prefixes)
    fill = _alternation(rules.fillers)
    suffix = _alternation(rules.suffixes)
    plain = re.compile(rules.person + "(?:" + pre + ")?(?:" + fill + ")*"
                       + "(" + verbs + ")(?:" + suffix + ")")
    loose = re.compile(rules.person + "(?:" + bare + ")(?:" + fill + ")*"
                       + "(" + verbs + ")(?:" + suffix + ")?")
    return plain, loose


def _alternation(words: Sequence[str]) -> str:
    """把一张词表编成一条交替式：长词在前，各自转义（词表里有正则符号也不会当场炸）。"""
    ordered = sorted(dict.fromkeys(words), key=lambda one: (-len(one), one))
    return "|".join(re.escape(one) for one in ordered)


def _ledger(actions: Any) -> Tuple[str, ...]:
    """把这一轮的动作账收成一张动词表；形状坏了当场抛，不许猜成「账上没有这个动作」。

    每一项要是一份键值表、里面那个 verb 是一句非空字符串：账本是机器写的，写歪了就是写歪了，
    猜它的意思等于替她记一笔没发生的动作。
    """
    if isinstance(actions, (str, bytes)) or not isinstance(actions, (list, tuple)):
        raise ClaimBroken("actions", "这一轮的动作账必须是清单（每项一份带 verb 的键值表），读到的是 %s。"
                          % type(actions).__name__)
    out: List[str] = []
    for index, one in enumerate(list(actions)):
        where = "actions 的第 %d 项" % (index + 1)
        if not isinstance(one, Mapping):
            raise ClaimBroken(where, "不是一份键值表，读到的是 %s。" % type(one).__name__)
        verb = one.get("verb")
        if not isinstance(verb, str) or not verb.strip():
            raise ClaimBroken(where, "没有 verb，或它不是一句非空字符串（账本要指认做过什么动作）。")
        out.append(verb.strip())
    return tuple(out)


def _sentence_at(body: str, at: int, rules: _Rules) -> str:
    """命中的那个位置所在的那一句，**不带句末标点**——strip_sentence 就是按它删句的。

    与 strip_sentence 用同一套分句（同一个句末标点表），所以两边算出来的必然是同一句。
    """
    for start, end, _mark_end in _spans(body, rules.sentence_marks):
        if start <= at < end:
            return body[start:end].strip()
    return body.strip()


def _spans(body: str, marks: str) -> List[Tuple[int, int, int]]:
    """按句末标点与换行分句：每句给（起, 句末, 标点之后）三个位置，空句也留着（位置要连续才删得准）。

    标点本身不算进句子（起与句末之间的那段），但删句时要连它一起删（标点之后那个位置），
    否则删完会剩下一个孤零零的句号。
    """
    chars = "".join(dict.fromkeys(marks))
    pattern = re.compile("[%s]" % "".join(re.escape(ch) for ch in chars))
    out: List[Tuple[int, int, int]] = []
    start = 0
    for found in pattern.finditer(body):
        out.append((start, found.start(), found.end()))
        start = found.end()
    out.append((start, len(body), len(body)))
    return out


def _rules(table: Optional[Mapping[str, Any]] = None) -> _Rules:
    """把判据表读成一份只读规则；table 为 None 就去仓库现读默认表。

    缺节缺键一律抛 ClaimBroken——数据里没有的词不在代码里补默认值（补了就是第二处真源）。
    三条硬要求：前缀表非空、裸前缀必须是前缀表里的一员（不然那条宽松写法永远匹配不上，写的人
    以为开了、其实没开）、动作类动词与说话类动词不许撞名（同一个词既算动作又算说话，判成什么
    就看查表的先后，那是运气不是判据）。
    """
    book = table if table is not None else load("claim")
    if not isinstance(book, Mapping):
        raise ClaimBroken("数据表", "不是一份键值表，读到的 %s。" % type(book).__name__)
    hit = _mapping(book, "hit")
    reason = _mapping(book, "reason")
    rules = _Rules(person=_word(book, "person"),
                   prefixes=_words(book, "prefixes"),
                   bare_prefixes=_words(book, "bare_prefixes"),
                   fillers=_words(book, "fillers"),
                   action_verbs=_words(book, "action_verbs"),
                   speech_verbs=_words(book, "speech_verbs"),
                   suffixes=_words(book, "suffixes"),
                   sentence_marks=_chars(book, "sentence_marks"),
                   clause_id=_word(hit, "clause_id"),
                   kind=_word(hit, "kind"),
                   reason_hit=_word(reason, "hit"),
                   reason_clear=_word(reason, "clear"),
                   reason_strip=_word(reason, "strip"),
                   reason_blank=_word(reason, "blank"),
                   reason_leftover=_word(reason, "leftover"))
    for one in rules.bare_prefixes:
        if one not in rules.prefixes:
            raise ClaimBroken("数据", "里 bare_prefixes 的 %r 不在 prefixes 里：那条宽松写法永远匹配不上。"
                              % one)
    for one in rules.action_verbs:
        if one in rules.speech_verbs:
            raise ClaimBroken("数据", "里动作类动词与说话类动词都收了 %r：同一个词算哪一边要看查表顺序，"
                                      "那是运气不是判据。" % one)
    return rules


def _text_of(text: Any) -> str:
    """要判的那段输出：不是字符串就当空串，判据只认字。"""
    return text if isinstance(text, str) else ("" if text is None else str(text))


def _need(data: Mapping[str, Any], key: str) -> Any:
    """取一项；缺了就抛，不补默认值。"""
    try:
        return data[key]
    except (KeyError, TypeError):
        raise ClaimBroken("数据", "缺少 %r 这一项。" % key) from None


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """取一节，必须是键值表。"""
    value = _need(data, key)
    if not isinstance(value, Mapping):
        raise ClaimBroken("数据", "的 %r 不是一份键值表，读到的 %s。" % (key, type(value).__name__))
    return value


def _word(data: Mapping[str, Any], key: str) -> str:
    """取一句非空字符串；空着也算缺。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise ClaimBroken("数据", "的 %r 必须是一句非空字符串。" % key)
    return value


def _chars(data: Mapping[str, Any], key: str) -> str:
    """取一串非空字符（分句用的句末标点与换行），按字符逐个用。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise ClaimBroken("数据", "的 %r 必须是一串非空字符（分句就是按这里的字符切）。" % key)
    return value


def _words(data: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    """取一张词表：非空清单、每项非空字符串；不接受「一整串字」冒充清单。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise ClaimBroken("数据", "的 %r 必须是非空清单（每项一个非空字符串），不是一整串字。" % key)
    out: List[str] = []
    for one in value:
        if not isinstance(one, str) or not one.strip():
            raise ClaimBroken("数据", "的 %r 里有不是字符串、或空着的项。" % key)
        out.append(one)
    return tuple(out)


def _fill(template: str, spot: str, **fields: Any) -> str:
    """把模板填成人话；模板里有数据没填的占位符就抛，别把 {matched} 这种半成品发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise ClaimBroken(str(spot), " 这句填不出来：%s" % exc) from None
