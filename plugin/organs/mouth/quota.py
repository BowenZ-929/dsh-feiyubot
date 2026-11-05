"""嘴的动作配额兜底：装配没给写动作的资格，动作行就不许出现在这一轮的回复里。

**这一关治的是「没资格还写动作」。** 动作写得对不对是 action_note 那一关的事（它管「动作里编了
没给过的场景」）；这一关只问一件事：这一轮装配到底允不允许写动作。原句与它的毛病收在
organs/mouth/data/quota.json 的 _note 里：**判据与说法一律不进代码。**

**quota 认三种情况，其中两种是写法、一种是没标：**

- **None（装配没标）**：check 返回 None，状态是 not-checked——**「没标」不是「标了 allow」**：
  前者是这一关没跑，后者是真的判过、判过没问题。两件事必须分得开，状态由调用方挂到
  Verdict.quota_status 上（与 action_status / repeat_status 同一个写法）。
- **forbid（本轮不许写动作）**：文本里出现动作行就命中。
- **allow**：一律不命中，状态 ok。

写法本身也在数据里（quota_values 认 forbid / allow 与各自的别名）；**不认识的写法当场抛
QuotaBroken，不许猜成 allow**——猜成放行，这一关就白设了。

**动作行的定义沿用既有的两路**（与 split.py 的 _is_action 同一口径，不另造第三套）。表一律借阅：
括号对与身体部位表借自 action_note.json，人称代词表借自 policy.json 的 split 节。

- **括号那一路**：一行开头就是一整段括注（开括号一直包到配对的关括号），那一段算一个动作片段。
- **身体部位那一路**：整行（去掉行首尾空白与句末标点）以身体部位开头、且不含人称代词。
- **行内括号不算动作**：「他说（笑）就走了」的括号长在句子中间，那是台词里夹的一个字，不是动作行，
  一个字都不许剥。

**check 返回的不是布尔。** 命中给一条 QuotaHit（命中的那一段、全部动作片段、一句人话、闸用的两个
名字），没命中返回 None——布尔说不出「为什么拦」，日志、面板、账本也就没法复盘。

**strip 是兜底，不是第二道判据。** 它只删动作片段（行首那一段括注，或整行是动作的那一行），
正文里别的字一个不动：行内括号、标点、空格都不碰。删完之后整行只剩空白（或只剩句末标点）就把
这一行丢掉；剥完什么都不剩，闸那边按「没有正文可发」弃掉整句。**剥过不等于放行**：正文还要照原样
再过其余判据（条款 → 动作括注 → 内容去重 → 复述审计），命中哪一条就照旧弃掉。

判据表形状坏掉（缺节、缺键、词表写成了一整串字、括号对不成对、forbid 与 allow 的写法撞车）一律
当场抛 QuotaBroken 并指名是哪一项，**不补默认值**——补一个默认词表，代码里就又多了一处真源。

纯：不 import time / datetime / random，不取时钟、不联网、不写盘，不碰宿主；文件 IO 只经 warehouse。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .warehouse import load

STATUS_OK = "ok"                     # 真的判过了：这一轮装配标了配额（forbid 或 allow）
STATUS_NOT_CHECKED = "not-checked"   # 装配没标，这一关没跑；不是通过
FORBID = "forbid"                    # 本轮不许写动作：出现动作行就命中
ALLOW = "allow"                      # 不设限：一律不命中

__all__ = ["QuotaBroken", "QuotaHit", "STATUS_OK", "STATUS_NOT_CHECKED", "FORBID", "ALLOW",
           "check", "strip", "clear_reason", "strip_reason", "blank_reason", "leftover_reason"]


class QuotaBroken(ValueError):
    """配额判据不合格：缺节、缺键、词表写成了一整串字、括号对不成对、认不出的 quota 写法，都算，坏一处就当场响。

    报错信息里带着那一项的名字：**不许拿一个空表或一个猜出来的写法当「这一关没有判据」**，
    那等于静默放行，而且没人知道她为什么又开始写动作（CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("动作配额判据 %s%s（判据数据在 organs/mouth/data/quota.json，"
                         "由 warehouse.load(\"quota\") 读出）" % (where, detail))


@dataclass(frozen=True)
class QuotaHit:
    """一次「没资格还写动作」的证据：命中的那一段、全部动作片段、一句人话、闸用的两个名字。

    字段（api 与闸按这个接，改字段等于改接口）：
      matched   命中的那一段原文（按出现顺序的第一段），闸把它挂到 Hit.matched 上
      lines     这一段输出里全部动作片段的原文，按出现顺序——复盘时要看一共写了几处
      reason    一句人话，里面带着命中的那一段与一共几处，账本与面板直接用这句
      clause_id 闸用的 clause_id，取自数据——闸不自己造名字，弃言账本里看到的就是数据里那个
      kind      闸用的 kind，取自数据，理由同上
    """

    matched: str
    lines: Tuple[str, ...]
    reason: str
    clause_id: str
    kind: str


@dataclass(frozen=True)
class _Rules:
    """判据表拆成一堆可以直接用的值：数据里没有的一样都不补。"""

    clause_id: str
    kind: str
    forbid: Tuple[str, ...]
    allow: Tuple[str, ...]
    brackets: Tuple[Tuple[str, str], ...]
    body_parts: Tuple[str, ...]
    pronouns: str
    blank: str
    line_marks: str
    reason_hit: str
    reason_strip: str
    reason_blank: str
    reason_leftover: str
    reason_clear: str


def check(text: str, quota: Any, table: Optional[Mapping[str, Any]] = None) -> Optional[QuotaHit]:
    """看这段字有没有在「本轮不许写动作」的时候还写动作：命中给一条证据，否则 None。

    table 是判据表（warehouse.load("quota") 读出来的那份），传 None 就现读仓库里的默认表。
    quota 是这一轮装配给的配额写法，**None 表示装配没标**：这一关没跑，返回 None——那不是
    「判过没问题」，状态由调用方看 STATUS_NOT_CHECKED，两件事必须分得开。
    认不出的写法当场抛 QuotaBroken（不许猜成 allow）。
    """
    rules = _rules(table)
    if quota is None:
        return None
    if _canon(quota, rules) != FORBID:
        return None
    found = _found(_text_of(text), rules)
    if not found:
        return None
    return QuotaHit(matched=found[0], lines=tuple(found),
                    reason=_fill(rules.reason_hit, "reason.hit",
                                 matched=found[0], count=len(found)),
                    clause_id=rules.clause_id, kind=rules.kind)


def strip(text: str, table: Optional[Mapping[str, Any]] = None) -> Tuple[str, List[str]]:
    """把动作片段剥掉、只留正文：返回（剥完的正文, 剥掉的那几段原文）。

    只删「行首那一段括注」与「整行是动作的那一行」，别的字一个不动——行内括号是台词，不碰。
    剥完只剩空白（或只剩句末标点）的行整行丢掉；一行正文都不剩就返回空串，闸那边据此弃掉整句。
    removed 按出现顺序带回被删的那几段原文，账本与面板靠它复盘「到底剥掉了什么」。
    """
    rules = _rules(table)
    kept: List[str] = []
    removed: List[str] = []
    for line in _lines(_text_of(text)):
        parts, body = _cut(line, rules)
        removed.extend(parts)
        if not _blankish(body, rules):
            kept.append(body)
    return "\n".join(kept), removed


def clear_reason(table: Optional[Mapping[str, Any]] = None) -> str:
    """放行那一句人话（数据里的 reason.clear），原样填出来。

    为什么要单独一个入口：check 的返回类型是 Optional[QuotaHit]，装不下「没命中 + 为什么放行」
    这一种读数；把放行塞成一条假命中，会把「没命中」与「判过没问题」挤进同一条通道。闸把这一句
    带进 Verdict.reason，面板上便看得见这一关查过了，而不是只看见一片安静。
    """
    return _rules(table).reason_clear


def strip_reason(removed: Sequence[str], table: Optional[Mapping[str, Any]] = None) -> str:
    """剥行救回来那句人话（数据里的 reason.strip）：带上被剥掉的那几段，账本靠它认出 action-over-quota。"""
    return _fill(_rules(table).reason_strip, "reason.strip", removed=_joined(removed))


def blank_reason(removed: Sequence[str], table: Optional[Mapping[str, Any]] = None) -> str:
    """剥完只剩空白那句人话（数据里的 reason.blank）：没有正文可发，照样弃掉。"""
    return _fill(_rules(table).reason_blank, "reason.blank", removed=_joined(removed))


def leftover_reason(clause_id: str, matched: str,
                    table: Optional[Mapping[str, Any]] = None) -> str:
    """剥完的正文仍被别的判据拦下那句人话（数据里的 reason.leftover）：剥过不等于放行。"""
    return _fill(_rules(table).reason_leftover, "reason.leftover",
                 clause_id=clause_id, matched=matched)


def _canon(quota: Any, rules: _Rules) -> str:
    """把装配给的写法收成二者之一（forbid / allow）；认不出来就当场抛，不许猜成 allow。

    比较前去掉首尾空白、不分大小写：写法本身在数据里，代码里只有两个能指的结论。
    """
    if not isinstance(quota, str) or not quota.strip():
        raise QuotaBroken("quota", "这一轮的动作配额必须是 forbid 或 allow 之一（写法见 quota_values），"
                                   "读到的是 %r。" % (quota,))
    word = quota.strip().casefold()
    for canon, spellings in ((FORBID, rules.forbid), (ALLOW, rules.allow)):
        if word in [one.casefold() for one in spellings]:
            return canon
    raise QuotaBroken("quota", "不认这个写法 %r：认的写法在 organs/mouth/data/quota.json 的 "
                               "quota_values 里，不许猜成「允许动作」。" % (quota,))


def _found(text: str, rules: _Rules) -> List[str]:
    """整段输出里的动作片段，按出现顺序；一行最多贡献一段（行首那一段括注，或整行是动作）。"""
    out: List[str] = []
    for line in _lines(text):
        parts, _body = _cut(line, rules)
        out.extend(parts)
    return out


def _cut(line: str, rules: _Rules) -> Tuple[List[str], str]:
    """把一行切成（这一行的动作片段, 剩下的正文）。

    没有动作的行原样返回（连行首空白都不动）：不许碰正文一个字。行首那几段括注与「整行是动作」
    的那一行才算动作；剩下的正文照原文带回，只把紧跟着被删括注的那点空白去掉。
    """
    parts, rest = _leading_parts(line, rules)
    if _is_body_action(rest, rules):
        parts.append(rest.strip())
        return parts, ""
    if not parts:
        return [], line
    return parts, rest


def _leading_parts(line: str, rules: _Rules) -> Tuple[List[str], str]:
    """取行首那几段整段括注：开括号开头、且找得到配对的关括号才算一段动作。

    行内括号（前面还有字）不算：那是台词。括号没配对也不算：写歪了不等于整行是动作，
    剥掉半段括号比不剥更糟。连着的几段括注一段一段取，取完把紧跟的空白去掉。
    """
    rest = line.lstrip(rules.blank)
    parts: List[str] = []
    while True:
        span = _span(rest, rules.brackets)
        if span is None:
            return parts, rest
        one, rest = span
        parts.append(one)
        rest = rest.lstrip(rules.blank)


def _span(text: str, brackets: Sequence[Sequence[str]]) -> Optional[Tuple[str, str]]:
    """拿掉行首那一段括注：返回（这一段原文, 后面剩下的字）；不是括注开头就 None。

    空括注（开括号紧挨着关括号）不算动作：里面一个字都没有，也就没有动作可剥。
    """
    for opener, closer in brackets:
        if not text.startswith(opener):
            continue
        end = text.find(closer, len(opener))
        if end <= len(opener):
            return None
        return text[:end + len(closer)], text[end + len(closer):]
    return None


def _is_body_action(text: str, rules: _Rules) -> bool:
    """整行是不是「以身体部位开头、且不含人称代词」：两条都满足才算动作行。

    带人称代词的那是台词（「我这是正常体型。」），不是动作；不以部位开头的是叙述，也不是动作。
    """
    core = _trim(text, rules)
    if not core:
        return False
    if any(char in core for char in rules.pronouns):
        return False
    return any(core.startswith(part) for part in rules.body_parts)


def _trim(text: str, rules: _Rules) -> str:
    """去掉行首尾的空白与句末标点：首尾那点标点不属于「这一行写了什么」。"""
    cut = set(rules.blank) | set(rules.line_marks)
    start, end = 0, len(text)
    while start < end and text[start] in cut:
        start += 1
    while end > start and text[end - 1] in cut:
        end -= 1
    return text[start:end]


def _blankish(text: str, rules: _Rules) -> bool:
    """这段字是不是「只剩空白」：每个字都在空白表或行末标点表里（空串也算只剩空白）。"""
    return all(char in rules.blank or char in rules.line_marks for char in text)


def _lines(text: str) -> List[str]:
    """按换行切行；三种换行写法（\\n、\\r\\n、\\r）切出来的行一样多（与 repeat.py 同一口径）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _text_of(text: Any) -> str:
    """要判的那段输出：不是字符串就当空串，判据只认字。"""
    return text if isinstance(text, str) else ("" if text is None else str(text))


def _joined(removed: Sequence[str]) -> str:
    """把被剥掉的那几段拼成一句人话：用顿号连，列表为空就照实写「（没有）」。"""
    items = [str(one) for one in removed if str(one).strip()]
    return "、".join(items) if items else "（没有）"


def _rules(table: Optional[Mapping[str, Any]] = None) -> _Rules:
    """把判据表读成一份只读规则；table 为 None 就去仓库现读默认表。

    缺节缺键一律抛 QuotaBroken——数据里没有的词不在代码里补默认值（补了就是第二处真源）。
    括号对、身体部位与人称代词三张表照 borrow 一节借阅，借不到照样当场响：
    本文件不另造第三套「什么算动作行」。
    """
    book = table if table is not None else load("quota")
    if not isinstance(book, Mapping):
        raise QuotaBroken("数据表", "不是一份键值表，读到的 %s。" % type(book).__name__)
    hit = _mapping(book, "hit")
    values = _mapping(book, "quota_values")
    borrow = _mapping(book, "borrow")
    reason = _mapping(book, "reason")
    action = _borrowed(str(_word(borrow, "action_table")), "borrow.action_table")
    pronoun = _borrowed(str(_word(borrow, "pronoun_table")), "borrow.pronoun_table")
    section = _mapping(pronoun, str(_word(borrow, "pronoun_section")))
    rules = _Rules(clause_id=_word(hit, "clause_id"),
                   kind=_word(hit, "kind"),
                   forbid=_words(values, "forbid"),
                   allow=_words(values, "allow"),
                   brackets=_pairs(action, str(_word(borrow, "brackets"))),
                   body_parts=_words(action, str(_word(borrow, "body_parts"))),
                   pronouns=_chars(section, str(_word(borrow, "pronouns"))),
                   blank=_chars(book, "blank"),
                   line_marks=_chars(book, "line_marks"),
                   reason_hit=_word(reason, "hit"),
                   reason_strip=_word(reason, "strip"),
                   reason_blank=_word(reason, "blank"),
                   reason_leftover=_word(reason, "leftover"),
                   reason_clear=_word(reason, "clear"))
    for one in rules.forbid:
        if one.casefold() in [other.casefold() for other in rules.allow]:
            raise QuotaBroken("数据", "里 quota_values 的 forbid 与 allow 都认 %r："
                                       "同一个写法不能既是禁止又是允许。" % one)
    return rules


def _borrowed(name: str, spot: str) -> Mapping[str, Any]:
    """按名字借一份数据（action_note / policy）：借不到照实往上抛，报错里点名是哪一项借的。"""
    book = load(name)
    if not isinstance(book, Mapping):
        raise QuotaBroken(spot, "借来的 %r 不是一份键值表，读到的 %s。"
                          % (name, type(book).__name__))
    return book


def _need(data: Mapping[str, Any], key: str) -> Any:
    """取一项；缺了就抛，不补默认值。"""
    try:
        return data[key]
    except (KeyError, TypeError):
        raise QuotaBroken("数据", "缺少 %r 这一项。" % key) from None


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """取一节，必须是键值表。"""
    value = _need(data, key)
    if not isinstance(value, Mapping):
        raise QuotaBroken("数据", "的 %r 不是一份键值表，读到的 %s。" % (key, type(value).__name__))
    return value


def _word(data: Mapping[str, Any], key: str) -> str:
    """取一句非空字符串；空着也算缺。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise QuotaBroken("数据", "的 %r 必须是一句非空字符串。" % key)
    return value


def _chars(data: Mapping[str, Any], key: str) -> str:
    """取一串非空字符（空白表、行末标点表、人称代词表），按字符逐个用。

    只要求「非空」，不要求里面有非空白字符：空白表本来就是一整串空白，按 strip 后非空来卡它
    会把自己卡死；而空串才是真的缺表（没有空白可认，剥行时会一个字都删不干净）。
    """
    value = _need(data, key)
    if not isinstance(value, str) or not value:
        raise QuotaBroken("数据", "的 %r 必须是一串非空字符。" % key)
    return value


def _words(data: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    """取一张词表：非空清单、每项非空字符串；不接受「一整串字」冒充清单。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise QuotaBroken("数据", "的 %r 必须是非空清单（每项一个非空字符串），不是一整串字。" % key)
    out: List[str] = []
    for one in value:
        if not isinstance(one, str) or not one.strip():
            raise QuotaBroken("数据", "的 %r 里有不是字符串、或空着的项。" % key)
        out.append(one)
    return tuple(out)


def _pairs(data: Mapping[str, Any], key: str) -> Tuple[Tuple[str, str], ...]:
    """取一组括号对（借自 action_note.json）：非空清单，每项是两个各自非空、且互不相同的字符串。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise QuotaBroken("数据", "的 %r 必须是非空清单（每项一对括号：开一个字符、关一个字符）。" % key)
    out: List[Tuple[str, str]] = []
    for index, one in enumerate(list(value)):
        where = "%r 的第 %d 对" % (key, index + 1)
        if isinstance(one, str) or not isinstance(one, (list, tuple)) or len(one) != 2:
            raise QuotaBroken("数据", "的 %s 不是一对（要正好两个字符：开与关）。" % where)
        opener, closer = one[0], one[1]
        for part in (opener, closer):
            if not isinstance(part, str) or not part.strip():
                raise QuotaBroken("数据", "的 %s 里有空着的一半（开与关都得写）。" % where)
        if opener == closer:
            raise QuotaBroken("数据", "的 %s 开与关是同一个字符 %r：这样括号片段取不出来。"
                              % (where, opener))
        out.append((opener, closer))
    return tuple(out)


def _fill(template: str, spot: str, **fields: Any) -> str:
    """把模板填成人话；模板里有数据没填的占位符就抛，别把 {matched} 这种半成品发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise QuotaBroken(str(spot), " 这句填不出来：%s" % exc) from None
