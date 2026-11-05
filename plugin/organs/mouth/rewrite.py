"""嘴的重写：命中之后把那一条改一次，而不是把整句吞掉。

**这一关治的是「一句毛病带走一整泡」。** 闸判她踩了条款、念了人设、复读了、动作超出配额时，
旧做法是命中即弃：一句口癖让她整泡消失，人那边只看到「她没说话」。改一次比吞掉便宜得多——
她的原话不动，只把命中的那一处换个说法，剩下的字一个都不许动。

**这里只有判据，没有模型。** 本文件是纯函数：不调模型、不联网、不取时钟、不写盘。
发请求、等时限、读配置都在 api/chat.py（那一处才有网络出口），本文件只回答三件事：

- **怎么让它改**：`prompt(original, hit, table)` 拼出一段给模型看的指令 + 待改原文。指令里写死
  三条铁律（只改命中的那一处、别整段重编、原样保留数字 / 英文串 / 引号里的原话），三条都取自
  data/rewrite.json 的 prompt.rules——改说法只改数据。
- **改成什么样才算改对了**：`accept(original, rewritten, table)` 返回（ok, why）。六条判据按顺序
  从「没字」到「整段重编」一道比一道具体：空/非字符串 → 与原文逐字相同（等于没改）→ 长度出了
  数据给的区间 → 原文里的数字少了 → 英文串少了 → 引号里的原话少了。前两条是「没改」，后四条是
  「改成别的东西了」，说法全在数据里，代码里一句都不写。
- **改不动的时候怎么说**：`failed_reason(why)` 那一段必须带 rewrite-failed——闸与账本靠它认出
  「这句本来要改、没改成、按原话发了」，而不是「这句没命中」。
- **改成了、却还是命中那一条怎么说**：`after_rewrite_reason(channel, ...)` 拼出那一行
  「<通道>-after-rewrite」的账（通道名与记号都在 data/rewrite.json 的 after_rewrite 节）。这一格
  闸不再丢整句：留进门那句原话发出，只在账本上留痕——**在最该回答的那一句上沉默，比说话带点
  毛病糟得多**。红线那类不走这一格（那类话照旧丢掉），判据在 organs/mouth/data/fatal.json。

**改不动不等于弃掉。** 本文件最大的用处是让「改不动」这条路有话说：api 层拿 `Unrewritable` 把
失败原因带出网络那一层，闸据此保留原话、记一行账，**绝不输出空**。宁可发一句略有瑕疵的话，
也不许静默变哑。

预算也在数据里：`budget_ms`（等多久算超时）与 `attempts`（一次，不重试），超时那句话在
why.timeout 里——本文件不写死一个毫秒数，也不写死一句超时说法。

判据表形状坏掉（缺节、缺键、数不是数、正则编译不过、引号对不成对、长度区间颠倒）一律当场抛
RewriteBroken 并指名是哪一项，**不补默认值**——补一个默认数，代码里就又多了一处真源。

纯：不 import time / datetime / random，不取时钟、不联网、不写盘，不碰宿主；文件 IO 只经 warehouse。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .warehouse import load

STATUS_OK = "ok"                 # 改了一次并通过 accept：新稿往下走
STATUS_FAILED = "failed"         # 改不动（超时 / 空 / 不合规 / 出口抛错）：原话留着发
STATUS_NOT_TRIED = "not-tried"   # 这一句没命中，压根没送出去改

__all__ = ["RewriteBroken", "Unrewritable", "STATUS_OK", "STATUS_FAILED", "STATUS_NOT_TRIED",
           "prompt", "accept", "failed_reason", "clear_reason", "timeout_why", "error_why",
           "no_rewriter_why", "not_text_why", "after_rewrite_reason", "budget_ms", "attempts",
           "excerpt"]


class RewriteBroken(ValueError):
    """重写判据不合格：缺节、缺键、数不是数、正则编译不过、引号对不成对、长度区间颠倒，都算，坏一处就当场响。

    报错信息里带着那一项的名字：**不许拿一个空表或一个猜出来的数当「这一关没有判据」**，
    那等于静默放行——一句本来该改的话原样发出去，而且没人知道（CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("重写判据 %s%s（判据数据在 organs/mouth/data/rewrite.json，"
                         "由 warehouse.load(\"rewrite\") 读出）" % (where, detail))


class Unrewritable(Exception):
    """这一次改不动：为什么写在这一句里（超时 / 空 / 不合规 / 出口抛错），全文取自数据。

    为什么用异常、而不是返回一个特殊值：调用方（闸）要分得开「改了，新稿给你」与「改不动」——
    返回空串与「模型真的回来一个空串」长得一模一样，返回原文又与「模型原样抄一遍」分不开。
    异常只说一件事：这一次没改成，原因听 str(exc)。闸收到它就保留原话、记一行账，**不许弃掉**。
    """


@dataclass(frozen=True)
class _Rules:
    """判据表拆成一堆可以直接用的值：数据里没有的一样都不补。"""

    budget_ms: int
    attempts: int
    excerpt_chars: int
    min_ratio: float
    max_ratio: float
    digits: Any
    latin: Any
    quotes: Tuple[Tuple[str, str], ...]
    prompt_head: str
    prompt_rules: Tuple[str, ...]
    prompt_hit: str
    prompt_matched: str
    prompt_hit_none: str
    prompt_original: str
    rule_empty: str
    rule_same: str
    rule_length: str
    rule_digits: str
    rule_latin: str
    rule_quotes: str
    rule_passed: str
    why_timeout: str
    why_error: str
    why_no_rewriter: str
    why_not_text: str
    reason_failed: str
    reason_clear: str
    after_marks: Mapping[str, str]
    reason_after_rewrite: str
    reason_after_rewrite_unchecked: str


def prompt(original: Any, hit: Any = None,
           table: Optional[Mapping[str, Any]] = None) -> str:
    """拼一段给模型看的指令 + 待改原文：三条铁律、这一次命中的那一处、原文。

    hit 是闸那一条命中（clause_id / kind / reason / matched），也可以是一份同样键名的键值表；
    没给（None）就按「这一次没指到具体条款」写——**不许把空的名字拼进指令里**：
    一句「条款 （）」既教不动模型，也让人看不出这一次到底要改什么。
    """
    rules = _rules(table)
    lines: List[str] = [rules.prompt_head]
    lines.extend(rules.prompt_rules)
    fields = _hit_fields(hit)
    if fields is None:
        lines.append(rules.prompt_hit_none)
    else:
        lines.append(_fill(rules.prompt_hit, "prompt.hit", **fields))
        if fields["matched"].strip():
            # 撞上的那几个字单独一行、空着就不写：零宽命中时拼出一对空引号，等于什么都没指
            lines.append(_fill(rules.prompt_matched, "prompt.matched", **fields))
    lines.append("")
    lines.append(_fill(rules.prompt_original, "prompt.original", original=_text_of(original)))
    return "\n".join(lines)


def accept(original: Any, rewritten: Any,
           table: Optional[Mapping[str, Any]] = None) -> Tuple[bool, str]:
    """看新稿算不算「只把那一处改了」：合格给（True, 一句人话），不合格给（False, 为什么）。

    六条判据按顺序比，先撞上的先报——从「没字」到「整段重编」一道比一道具体：

    1. 不是字符串、或去空白之后一个字都不剩 → 等于没改；
    2. 与原文逐字相同（去首尾空白之后）→ 等于没改；
    3. 长度出了数据给的区间（默认 0.4–2.0 倍）→ 说的已经不是她那一句了；
    4. 原文里的数字串在新稿里少了 → 判整段重编；
    5. 原文里的英文串少了 → 同上；
    6. 原文引号里的原话少了 → 同上。

    为什么 4–6 三条要单列：它们最容易在「重写」里被悄悄抹掉（模型爱把「三点」写成「三个」、
    把英文缩写展开、把引号里的话改成自己的口气），而这三样恰是最不能改的——数字与英文是事实，
    引号里的话是别人的原话。丢了任何一个就按整段重编判不合规，原话留着发。
    """
    rules = _rules(table)
    body = _text_of(original)
    if not isinstance(rewritten, str) or not rewritten.strip():
        return False, _fill(rules.rule_empty, "rule.empty")
    new = rewritten.strip()
    if not body.strip():
        return False, _fill(rules.rule_empty, "rule.empty")
    if new == body:
        return False, _fill(rules.rule_same, "rule.same")
    ratio = len(new) / len(body)
    if ratio < rules.min_ratio or ratio > rules.max_ratio:
        return False, _fill(rules.rule_length, "rule.length", ratio="%.2f" % ratio,
                            min=_number(rules.min_ratio), max=_number(rules.max_ratio))
    for pattern, template, spot in ((rules.digits, rules.rule_digits, "rule.digits"),
                                    (rules.latin, rules.rule_latin, "rule.latin")):
        missing = _missing(pattern, body, new)
        if missing:
            return False, _fill(template, spot, missing="、".join(missing))
    for opener, closer in rules.quotes:
        missing = _missing_quotes(opener, closer, body, new)
        if missing:
            return False, _fill(rules.rule_quotes, "rule.quotes", missing="、".join(missing))
    return True, _fill(rules.rule_passed, "rule.passed")


def failed_reason(why: Any, table: Optional[Mapping[str, Any]] = None) -> str:
    """「改不动、原话留着发」那一句人话：必须带 rewrite-failed，账本与面板靠它认门。"""
    return _fill(_rules(table).reason_failed, "reason.failed", why=_text_of(why))


def clear_reason(table: Optional[Mapping[str, Any]] = None) -> str:
    """没命中、根本不用改那一句人话（数据里的 reason.clear）。"""
    return _fill(_rules(table).reason_clear, "reason.clear")


def after_rewrite_reason(channel: str, clause_id: Any = "", why: Any = "",
                         checked: bool = True,
                         table: Optional[Mapping[str, Any]] = None) -> str:
    """改了一次还是命中（且不是红线）那一句人话：必须带 <通道>-after-rewrite，账本靠它认门。

    channel 是闸那边四条判据通道之一（条款 / 动作括注 / 内容重复 / 复述审计）：改了一次没改掉的
    是哪一关，账本上要看得出来，所以记号按通道分开。认不出的通道当场抛 RewriteBroken——猜一个
    通道，账本上就会多出一行「不知道是哪一关没改掉」的账。
    记号与那整句人话全在 data/rewrite.json 的 after_rewrite 节，本文件不写死一个记号、一句说法。
    checked=False 表示这一轮没拿到红线表（判不了是不是红线）：那一行后面再补一句取自数据的说明
    （after_rewrite.unchecked），把「判不了」写在账上——判不了不等于可以吞掉这句话，所以话照发。
    """
    rules = _rules(table)
    mark = rules.after_marks.get(channel)
    if mark is None:
        raise RewriteBroken("after_rewrite.marks", " 里没有通道 %r（只认 %s）。"
                            % (channel, "、".join(repr(one) for one in rules.after_marks)))
    said = _fill(rules.reason_after_rewrite, "reason.after_rewrite", mark=mark,
                 channel=channel, clause_id=_text_of(clause_id), why=_text_of(why))
    if checked:
        return said
    return said + _fill(rules.reason_after_rewrite_unchecked, "after_rewrite.unchecked")


def timeout_why(table: Optional[Mapping[str, Any]] = None) -> str:
    """超时那种「改不动」的说法：带上数据里的时限，人一眼看出等了多久。"""
    rules = _rules(table)
    return _fill(rules.why_timeout, "why.timeout", budget_ms=rules.budget_ms)


def error_why(error: Any, table: Optional[Mapping[str, Any]] = None) -> str:
    """出口抛错那种「改不动」的说法：异常的类型与原文都带上，别只留一句「出错了」。"""
    detail = "%s：%s" % (type(error).__name__, error)
    return _fill(_rules(table).why_error, "why.error", error=detail)


def no_rewriter_why(table: Optional[Mapping[str, Any]] = None) -> str:
    """这一轮压根没有可用的重写器（调用方没给）——改不动的一种，不是「没问题」。"""
    return _rules(table).why_no_rewriter


def not_text_why(table: Optional[Mapping[str, Any]] = None) -> str:
    """改回来不是一句话——改不动的一种。"""
    return _rules(table).why_not_text


def budget_ms(table: Optional[Mapping[str, Any]] = None) -> int:
    """一次重写最多等多少毫秒（数据里的 budget_ms）：api 层照它设硬时限。"""
    return _rules(table).budget_ms


def attempts(table: Optional[Mapping[str, Any]] = None) -> int:
    """重写最多发几次请求（数据里的 attempts）：1 就是一次，不重试。"""
    return _rules(table).attempts


def excerpt(text: Any, table: Optional[Mapping[str, Any]] = None) -> str:
    """截一小截原文给日志与面板看（长度取自数据）：要的是「改的是哪一句」，不是整段再抄一遍。"""
    rules = _rules(table)
    body = _text_of(text).strip()
    if len(body) <= rules.excerpt_chars:
        return body
    return body[:rules.excerpt_chars] + "…"


def _missing(pattern: Any, body: str, new: str) -> List[str]:
    """原文里有、新稿里没了的那些匹配（按第一次出现的顺序，去掉重复）。"""
    out: List[str] = []
    for one in pattern.findall(body):
        text = one if isinstance(one, str) else "".join(one)
        if text and text not in out and text not in new:
            out.append(text)
    return out


def _missing_quotes(opener: str, closer: str, body: str, new: str) -> List[str]:
    """原文引号里那些原话里、新稿里没了的（连引号一起比：少一个字也算少了）。"""
    pattern = re.compile("%s([^%s]*)%s" % (re.escape(opener), re.escape(closer), re.escape(closer)))
    out: List[str] = []
    for inner in pattern.findall(body):
        span = opener + inner + closer
        if inner and span not in out and span not in new:
            out.append(span)
    return out


def _hit_fields(hit: Any) -> Optional[dict]:
    """把闸那一条命中摊成四个占位符要用的值；没给命中就是 None（不许拼出一对空引号）。"""
    if hit is None:
        return None
    def _get(name: str) -> str:
        value = hit.get(name) if isinstance(hit, Mapping) else getattr(hit, name, "")
        return _text_of(value)
    return {"clause_id": _get("clause_id"), "kind": _get("kind"),
            "reason": _get("reason"), "matched": _get("matched")}


def _text_of(value: Any) -> str:
    """拿来拼人话的那段字：不是字符串就当空串（判据只认字）。"""
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _number(value: float) -> str:
    """把区间端点写成人话里的样子：2.0 写成 2，0.4 还是 0.4。"""
    return ("%f" % value).rstrip("0").rstrip(".")


def _rules(table: Optional[Mapping[str, Any]] = None) -> _Rules:
    """把判据表读成一份只读规则；table 为 None 就去仓库现读默认表。

    缺节缺键一律抛 RewriteBroken——数据里没有的数不在代码里补默认值（补了就是第二处真源）。
    长度区间要正、且低的一头不能高过高的那头；引号对要成对、两半不同；两条正则都要能编译。
    """
    book = table if table is not None else load("rewrite")
    if not isinstance(book, Mapping):
        raise RewriteBroken("数据表", "不是一份键值表，读到的 %s。" % type(book).__name__)
    length = _mapping(book, "length")
    keep = _mapping(book, "keep")
    said = _mapping(book, "prompt")
    rule = _mapping(book, "rule")
    why = _mapping(book, "why")
    reason = _mapping(book, "reason")
    after = _mapping(book, "after_rewrite")
    low = _positive(keep=length, key="min", spot="length.min", allow_zero=False)
    high = _positive(keep=length, key="max", spot="length.max", allow_zero=False)
    if low > high:
        raise RewriteBroken("数据", "里 length.min（%s）比 length.max（%s）还大：区间反了。"
                            % (_number(low), _number(high)))
    rules = _Rules(budget_ms=_above_zero(book, "budget_ms"),
                   attempts=_above_zero(book, "attempts"),
                   excerpt_chars=_above_zero(book, "excerpt_chars"),
                   min_ratio=low,
                   max_ratio=high,
                   digits=_regex(keep, "digits"),
                   latin=_regex(keep, "latin"),
                   quotes=_pairs(keep, "quotes"),
                   prompt_head=_word(said, "head"),
                   prompt_rules=_words(said, "rules"),
                   prompt_hit=_word(said, "hit"),
                   prompt_matched=_word(said, "matched"),
                   prompt_hit_none=_word(said, "hit_none"),
                   prompt_original=_word(said, "original"),
                   rule_empty=_word(rule, "empty"),
                   rule_same=_word(rule, "same"),
                   rule_length=_word(rule, "length"),
                   rule_digits=_word(rule, "digits"),
                   rule_latin=_word(rule, "latin"),
                   rule_quotes=_word(rule, "quotes"),
                   rule_passed=_word(rule, "passed"),
                   why_timeout=_word(why, "timeout"),
                   why_error=_word(why, "error"),
                   why_no_rewriter=_word(why, "no_rewriter"),
                   why_not_text=_word(why, "not_text"),
                   reason_failed=_word(reason, "failed"),
                   reason_clear=_word(reason, "clear"),
                   after_marks=_marks(after, "marks"),
                   reason_after_rewrite=_word(after, "reason"),
                   reason_after_rewrite_unchecked=_word(after, "unchecked"))
    if "rewrite-failed" not in rules.reason_failed:
        raise RewriteBroken("数据", "里 reason.failed 必须带 rewrite-failed：账本靠它认出这一笔。")
    if "{mark}" not in rules.reason_after_rewrite:
        raise RewriteBroken("数据", "里 after_rewrite.reason 必须带 {mark}："
                                    "记账那一行看不出是哪一关没改掉，等于没记。")
    return rules


def _need(data: Mapping[str, Any], key: str) -> Any:
    """取一项；缺了就抛，不补默认值。"""
    try:
        return data[key]
    except (KeyError, TypeError):
        raise RewriteBroken("数据", "缺少 %r 这一项。" % key) from None


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """取一节，必须是键值表。"""
    value = _need(data, key)
    if not isinstance(value, Mapping):
        raise RewriteBroken("数据", "的 %r 不是一份键值表，读到的 %s。" % (key, type(value).__name__))
    return value


def _word(data: Mapping[str, Any], key: str) -> str:
    """取一句非空字符串；空着也算缺。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise RewriteBroken("数据", "的 %r 必须是一句非空字符串。" % key)
    return value


def _marks(data: Mapping[str, Any], key: str) -> Mapping[str, str]:
    """取一张「通道 → 记号」表：非空、键与记号都是非空字符串，不接受一整串字冒充。"""
    value = _need(data, key)
    if not isinstance(value, Mapping) or not value:
        raise RewriteBroken("数据", "的 %r 必须是一份非空的键值表（通道 → 记号）。" % key)
    out: dict = {}
    for one, mark in value.items():
        if not isinstance(one, str) or not one.strip():
            raise RewriteBroken("数据", "的 %r 里有一个不是字符串、或空着的通道名。" % key)
        if not isinstance(mark, str) or not mark.strip():
            raise RewriteBroken("数据", "的 %r 里通道 %r 的记号不是一句非空字符串。"
                                % (key, one))
        out[one] = mark
    return out


def _words(data: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    """取一张说法清单（三条铁律）：非空清单、每项非空字符串；不接受一整串字冒充清单。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise RewriteBroken("数据", "的 %r 必须是非空清单（每项一句说法），不是一整串字。" % key)
    out: List[str] = []
    for one in value:
        if not isinstance(one, str) or not one.strip():
            raise RewriteBroken("数据", "的 %r 里有不是字符串、或空着的项。" % key)
        out.append(one)
    return tuple(out)


def _above_zero(data: Mapping[str, Any], key: str) -> int:
    """取一个 1 以上的整数（时限、次数、截几个字）；布尔不算整数。"""
    value = _need(data, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RewriteBroken("数据", "的 %r 必须是 1 以上的整数，读到的不是。" % key)
    return value


def _positive(keep: Mapping[str, Any], key: str, spot: str,
              allow_zero: bool = True) -> float:
    """取一个倍数（长度区间的两头）：数、且大于 0；布尔不算数。"""
    value = _need(keep, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RewriteBroken(spot, " 必须是数，读到的不是。")
    number = float(value)
    if number < 0 or (not allow_zero and number == 0):
        raise RewriteBroken(spot, " 必须大于 0，读到的是 %s。" % _number(number))
    return number


def _regex(data: Mapping[str, Any], key: str) -> Any:
    """取一条编译好的正则（数字串、英文串的取法）；编译不过当场抛。"""
    pattern = _need(data, key)
    if not isinstance(pattern, str) or not pattern.strip():
        raise RewriteBroken("数据", "的 %r 必须是一句非空正则。" % key)
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise RewriteBroken("数据", "的 %r 正则编译不过：%s。" % (key, exc)) from None


def _pairs(data: Mapping[str, Any], key: str) -> Tuple[Tuple[str, str], ...]:
    """取一组引号对：非空清单，每项两个各自非空、且互不相同的字符。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise RewriteBroken("数据", "的 %r 必须是非空清单（每项一对引号）。" % key)
    out: List[Tuple[str, str]] = []
    for index, one in enumerate(list(value)):
        where = "%r 的第 %d 对" % (key, index + 1)
        if isinstance(one, str) or not isinstance(one, (list, tuple)) or len(one) != 2:
            raise RewriteBroken("数据", "的 %s 不是一对（要正好两个字符：开与关）。" % where)
        opener, closer = one[0], one[1]
        for part in (opener, closer):
            if not isinstance(part, str) or not part.strip():
                raise RewriteBroken("数据", "的 %s 里有空着的一半（开与关都得写）。" % where)
        if opener == closer:
            raise RewriteBroken("数据", "的 %s 开与关是同一个字符 %r：这样取不出引号片段。"
                                % (where, opener))
        out.append((opener, closer))
    return tuple(out)


def _fill(template: str, spot: str, **fields: Any) -> str:
    """把模板填成人话；模板里有数据没填的占位符就抛，别把 {matched} 这种半成品发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise RewriteBroken(str(spot), " 这句填不出来：%s" % exc) from None
