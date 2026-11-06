"""嘴的「编记忆」判据：引用了过去说过的话，就得在这一轮真的想起来的那几条里对得上。

**这一关治的是「替自己想出一段没发生过的对话」。** 她说「你当时说要去看病，是周三上午吧？」——
标记是「你当时说」（她把自己要说的话挂到过去的一场对话上），细节是「周三上午」。这一轮的 recalled
里一条都对不上，那就是她编的：不是记忆，是叙事。判据只有一条，两样东西同时在场才成立：

- **引用标记**（把那句话挂到「哪天谁说过」上的那几种说法），在 markers 里；
- **具体细节**（数字、时间词、引号里的原话、或一段说得够长的话），在 detail 里。

**三件事必须分得开，这是这一关的全部内容**：

- **对得上 → 放行**：细节与某条 recalled 的正文对得上（贴住连续 min_run 个字，或 2-gram 覆盖率到
  bigram_overlap），那她记的就是真事，放行。两把尺子都在数据里，离线可复算：同一个输入，任何人都
  能复算出同一个结论，不取时钟、不用模型、不做相似度。
- **对不上 → 判废（走既有重写路径）**：命中先交给闸的重写器改一次，改不掉就**把那一泡换成一句实话**
  （fallback_text，取自数据）——**不是整条丢**。「说不出真话的时候，认怂比沉默强」：人那边至少知道
  该再提一句。这一格与配额剥行、冒充做过删句同族（都挂在 Verdict.salvaged 上、都记一笔账）。
- **判不了（recalled=None）→ 不判**：拿不到这一轮想起来的是什么，就没有「对不上」可言。返回 None、
  状态由调用方记 not-checked——**没判不是清白**，也不是命中了放过。**recalled=[] 是另一件事**：
  这一轮确实一条都没想起来，那么出现「标记 + 细节」就是编的，照判。一个字之差，差的是「判没判」。

**态度不是事实。** 「我没忘你」「我记得你这人」「我不会忘」说的是心，不是「哪天谁说了什么」——
在 exempt 表上的那一句整句跳出这一关：**宁可放过一句带细节的，也不冤枉一句只是在表心意的**。

词表、豁免、细节判据、两把尺子的阈值、四句人话全在 organs/mouth/data/fabricate.json，经
warehouse.load("fabricate") 只读借入；本文件不写死一个词、一个数、一句说法。缺键、坏形状当场抛
FabricateBroken，**不补默认值**：补一个默认词表，等于代码里又多了一处真源。

纯：不 import time / datetime / random，不取时钟、不联网、不写盘，不碰宿主；文件 IO 只经 warehouse
（table 给了就一次盘都不读）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .warehouse import load

STATUS_OK = "ok"                     # 真的判过了：这一轮给了 recalled（哪怕是空清单）
STATUS_NOT_CHECKED = "not-checked"   # 没给 recalled，这一关没跑；不是「没编记忆」

__all__ = ["FabricateBroken", "FabricateHit", "STATUS_OK", "STATUS_NOT_CHECKED",
           "check", "fallback_text", "fallback_reason", "clear_reason"]


class FabricateBroken(ValueError):
    """编记忆的判据表不合格：缺节、缺键、正则编译不过、阈值不是数、词表写成了一整串字，都算，坏一处就当场响。

    报错信息里带着那一项的名字：**不许拿一个空词表当「这一关没有判据」**，那等于静默放行——她可以
    接着替自己编对话，而且没人知道（CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("编记忆判据 %s%s（判据数据在 organs/mouth/data/fabricate.json，"
                         "由 warehouse.load(\"fabricate\") 读出）" % (where, detail))


@dataclass(frozen=True)
class FabricateHit:
    """一次「编了一段没发生过的对话」的证据：哪个标记、什么细节、那一句、一句人话、闸用的两个名字。

    字段（api 与闸按这个接，改字段等于改接口）：
      marker     撞上的那个引用标记，取自数据里的标记表（日志按它分组最好用）
      detail     与 recalled 对不上的那段具体细节（数字 / 时间词 / 引号里的原话 / 说得够长的那一截）
      detail_kind  那一段细节属于哪一路，取自数据的 detail.kinds（人话里要说出「是哪个具体」）
      matched    命中的那段原文（从标记起到这一句结束），闸把它挂到 Hit.matched 上
      reason     一句人话，里面带着标记、细节与记号（tag）；账本与面板直接用这句
      clause_id  闸用的 clause_id，取自数据——闸不自己造名字，弃言账本里看到的就是数据里那个
      kind       闸用的 kind，取自数据，理由同上
    """

    marker: str
    detail: str
    detail_kind: str
    matched: str
    reason: str
    clause_id: str
    kind: str


@dataclass(frozen=True)
class _Rules:
    """判据表拆成一堆可以直接用的值：数据里没有的一样都不补。"""

    markers: Tuple[Any, ...]
    exempt: Tuple[Any, ...]
    digits: Any
    numerals: Any
    time_words: Tuple[str, ...]
    quotes: Tuple[Tuple[str, str], ...]
    min_chars: int
    length_when_empty: bool
    kinds: Mapping[str, str]
    min_run: int
    bigram_overlap: float
    strip: frozenset
    trim: str
    sentence_marks: str
    fallback: str
    clue: str
    clear: str
    confess: str
    clause_id: str
    kind: str
    tag: str


def check(text: str, recalled: Any,
          table: Optional[Mapping[str, Any]] = None) -> Optional[FabricateHit]:
    """看这段字有没有认领一段这一轮没想起来的过去：命中给一条证据，没命中给 None。

    table 是判据表（warehouse.load("fabricate") 读出来的那份），传 None 就现读仓库里的默认表。

    recalled 是这一轮真的想起来的记忆（回包 recalled[*]，每项一份带 text 的键值表），三种形状三种
    结论，**必须分得开**：

    - **None → 判不了**：拿不到这一轮想起来的是什么，就没有「对不上」可言，返回 None，状态由调用方
      记 not-checked。**没给账不是「没编」，也不是「编了」**——这一关不替调用方猜。
    - **清单（空清单也算）→ 照实判**：空清单是「这一轮一条都没想起来」，出现「标记 + 细节」就是编的。
      清单里每一项要是一份键值表；没有 text、或 text 不是字符串的那一条，对不上任何话（不猜它的
      意思，也不拿它当抗辩）。
    - **别的形状 → 当场抛**：recalled 是机器写的，写歪了就是写歪了，猜它的意思等于替她造一份记忆。

    按句扫描，按出现顺序找第一个「有标记、有细节、又对不上」的；同一句里先报靠前的那个标记。
    在 exempt 表上的那一句整句跳过（态度不是事实）。标记后面那一截只有标点、或短得算不上具体的，
    也不算命中——**只有标记没有细节不判**。
    """
    rules = _rules(table)                 # 表先校验：坏表当场响，不许因为这一轮没给 recalled 就悄悄跳过
    if recalled is None:
        return None
    ledger = _ledger(recalled, rules)
    body = _text_of(text)
    for start, end, _mark_end in _spans(body, rules.sentence_marks):
        sentence = body[start:end]
        if _exempted(sentence, rules):
            continue                      # 态度不是事实：整句跳过，不冤枉
        found = _marker(sentence, rules)
        if found is None:
            continue
        claimed = _claimed_span(sentence, found.end(), rules)
        if not claimed:
            continue
        detail = _detail(claimed, rules, weak=_nothing_recalled(ledger))
        if detail is None:
            continue                      # 只有标记没有细节：空口引用与态度句都落在这一格
        if _aligned(claimed, ledger, rules):
            continue                      # 对得上：她记的就是真事
        return _hit(rules, sentence, found, claimed, detail)
    return None


def fallback_text(table: Optional[Mapping[str, Any]] = None) -> str:
    """那句实话（数据里的 fallback）：重写仍命中时把整泡换成它，供闸的二次命中处置用。

    为什么留一整句、而不是丢空：**在最该回话的那一句上沉默，比说一句「我没记到」糟得多**。说不出
    真话的时候认怂，人那边至少知道该再提一句。
    """
    return _rules(table).fallback


def clear_reason(table: Optional[Mapping[str, Any]] = None) -> str:
    """放行那一句人话（数据里的 reason.clear），原样填出来。

    为什么要单独一个入口：check 的返回类型是 Optional[FabricateHit]，装不下「没命中 + 为什么放行」
    这一种读数；把放行塞成一条假 FabricateHit，会把「没命中」与「判过没问题」挤进同一条通道，
    调用方再也分不开。闸把这一句带进 Verdict.reason，面板上便看得见这一关查过了。
    """
    return _rules(table).clear


def fallback_reason(hit: FabricateHit,
                    table: Optional[Mapping[str, Any]] = None) -> str:
    """换成实话那一句人话（数据里的 reason.fallback）：带上标记、细节与换成了什么，账本靠它复盘。"""
    rules = _rules(table)
    return _fill(rules.confess, "reason.fallback", marker=hit.marker, detail=hit.detail,
                 detail_kind=hit.detail_kind, tag=rules.tag, fallback=rules.fallback)



def _hit(rules: _Rules, sentence: str, found: Any, claimed: str,
         detail: Tuple[str, str]) -> FabricateHit:
    """凑一次命中的证据：两个闸用的名字取自数据，reason 由数据里的模板填出来。"""
    marker = found.group(0)
    matched = (sentence[found.start():].strip() or sentence.strip())
    return FabricateHit(marker=marker, detail=detail[0],
                        detail_kind=rules.kinds[detail[1]], matched=matched,
                        reason=_fill(rules.clue, "reason.hit", marker=marker,
                                     detail=detail[0], detail_kind=rules.kinds[detail[1]],
                                     tag=rules.tag),
                        clause_id=rules.clause_id, kind=rules.kind)


def _ledger(recalled: Any, rules: _Rules) -> Tuple[str, ...]:
    """把这一轮想起来的那几条收成一张「对得上用」的正文表（去标点、去空白）；形状坏了当场抛。

    没有 text、或 text 不是字符串的那一条，收成空串：它不参与比对，也不拿它当抗辩——猜一条没有
    正文的记忆说的是什么，等于替她造一份能对得上的记忆。
    """
    if isinstance(recalled, (str, bytes)) or not isinstance(recalled, (list, tuple)):
        raise FabricateBroken("recalled", "这一轮想起来的记忆必须是清单（每项一份带 text 的键值表），"
                                         "读到的是 %s。" % type(recalled).__name__)
    out: List[str] = []
    for index, one in enumerate(list(recalled)):
        where = "recalled 的第 %d 项" % (index + 1)
        if not isinstance(one, Mapping):
            raise FabricateBroken(where, "不是一份键值表，读到的是 %s。" % type(one).__name__)
        text = one.get("text")
        out.append(_squash(text, rules) if isinstance(text, str) else "")
    return tuple(out)


def _spans(body: str, marks: str) -> List[Tuple[int, int, int]]:
    """按句末标点与换行分句：每句给（起, 句末, 标点之后）三个位置，空句也留着（位置连着才找得准）。

    与 claim 的分句同一个口径：标点本身不算进句子，但边界连它一起跳过。逗号不算句末——「你当时说
    要去看病，是周三上午吧」是一句话里的一件事，按逗号切会把细节切到下一句去。
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


def _exempted(sentence: str, rules: _Rules) -> bool:
    """这一句是不是在表心意（态度不是事实）：在 exempt 表上就整句跳过，宁可放过也不冤枉。"""
    return any(pattern.search(sentence) is not None for pattern in rules.exempt)


def _marker(sentence: str, rules: _Rules) -> Optional[Any]:
    """这一句里靠得最前的那个引用标记；一个都没有就是 None（顺序按位置，同位置按数据顺序）。"""
    best: Optional[Any] = None
    for pattern in rules.markers:
        found = pattern.search(sentence)
        if found is None:
            continue
        if best is None or found.start() < best.start():
            best = found
    return best


def _claimed_span(sentence: str, after: int, rules: _Rules) -> str:
    """标记后面那一截（她要认领的内容）：去掉两头标点与空白，空的就不算认领了什么。"""
    return sentence[after:].strip(_strip_chars(rules))


def _detail(claimed: str, rules: _Rules,
            weak: bool = True) -> Optional[Tuple[str, str]]:
    """那一截里有没有具体细节：按 digits → numerals → quote → time → length 找第一个，都没有就是 None。

    四路判据都在数据里：数字串、数目（「三遍」「两次」）、引号里的原话、时间词；前四路都没有时，
    去标点之后还剩 min_chars 个字也算「说得够具体」——「你当时说那个」不算，那一截太短。
    引号排在时间词前面：她自称复述了一句原话，是最硬的那一种具体（拿它去对 recalled 也最准）。
    找到就把（那一段原文, 哪一路）一起带回去：人话里要说出「是哪个具体」。

    **长度这一路是弱细节**（weak 为假时整段跳过）：有召回在手却对不上，很可能只是换了个说法讲
    同一件事，拿「说得够长」判就是冤枉她；这一轮一条都没想起来时（weak 为真）不存在「换了说法」
    这回事，说得够长就是在认领一段根本不存在的过去。这个开关在数据的 detail.length_needs_empty_recall 里。
    """
    found = rules.digits.search(claimed)
    if found is not None:
        return found.group(0), "digits"
    found = rules.numerals.search(claimed)
    if found is not None:
        return found.group(0), "numerals"
    quoted = _quoted(claimed, rules)
    if quoted is not None:
        return quoted, "quote"
    word = _earliest(claimed, rules.time_words)
    if word is not None:
        return word, "time"
    squashed = _squash(claimed, rules)
    if weak and len(squashed) >= rules.min_chars:
        return claimed.strip(), "length"
    return None


def _nothing_recalled(ledger: Sequence[str]) -> bool:
    """这一轮是不是一条都没想起来：收进来的那几条里一条有正文的都没有，就算一条都没有。"""
    return not any(one for one in ledger)


def _earliest(body: str, words: Sequence[str]) -> Optional[str]:
    """词表里出现得最靠前的那个词（同位置按数据顺序）：数据顺序即优先级只在打平时用得上。"""
    best: Optional[str] = None
    at = -1
    for word in words:
        spot = body.find(word)
        if spot < 0:
            continue
        if best is None or spot < at:
            best, at = word, spot
    return best


def _quoted(body: str, rules: _Rules) -> Optional[str]:
    """引号里的原话（成对取的四种引号 + 半角双引号）：开引号后面真有闭引号才算。"""
    for left, right in rules.quotes:
        start = body.find(left)
        if start < 0:
            continue
        end = body.find(right, start + len(left))
        if end < 0:
            continue
        return body[start:end + len(right)]
    return None


def _aligned(claimed: str, ledger: Sequence[str], rules: _Rules) -> bool:
    """她认领的那一截对不对得上某条 recalled：两把尺子，任一把过线就算对得上。

    一把是**连续字数**（最长连续公共子串 ≥ min_run：贴住了原话），一把是**2-gram 覆盖率**（换了说法、
    字还是那几个）。两边都先去标点去空白：逗号不该成为「她对不上」的理由。少于两个字的短截按整串
    包含判——硬套覆盖率会把这个长度上的结论交给一个没有意义的分数。
    """
    body = _squash(claimed, rules)
    if not body:
        return False
    for one in ledger:
        if not one:
            continue
        if _longest_run(body, one) >= rules.min_run:
            return True
        if _covered(body, one) >= rules.bigram_overlap:
            return True
    return False


def _longest_run(left: str, right: str) -> int:
    """两段字的最长连续公共子串有多长：一行滚动的 DP，O(两段长度之积)，短句上快得不值得记。"""
    if not left or not right:
        return 0
    best = 0
    row = [0] * (len(right) + 1)
    for one in left:
        previous = 0
        for index, other in enumerate(right, start=1):
            kept = row[index]
            row[index] = previous + 1 if one == other else 0
            if row[index] > best:
                best = row[index]
            previous = kept
    return best


def _covered(body: str, other: str) -> float:
    """这段字的 2-gram 有多大比例在那一段里出现过：换过说法也认，全是自己编的就一点都对不上。"""
    if len(body) < 2:
        return 1.0 if body in other else 0.0
    pool = {other[index:index + 2] for index in range(len(other) - 1)}
    hits = sum(1 for index in range(len(body) - 1) if body[index:index + 2] in pool)
    return hits / float(len(body) - 1)


def _squash(text: Any, rules: _Rules) -> str:
    """去掉标点与空白之后的那段字：比对只认字，逗号句号不该成为「对不上」的理由。"""
    if not isinstance(text, str):
        return ""
    return "".join(ch for ch in text if ch not in rules.strip)


def _strip_chars(rules: _Rules) -> str:
    """去两头用的字符表（数据里的 trim，按字符去重后交给 str.strip）。

    与 strip 分成两份：strip 治「比之前去掉什么」（含引号，引号不该拦住对得上），trim 治「这一截
    从哪儿算起」——**这里不能去掉引号**，否则「你说过“明天早点来”」的那句原话先被剥掉，细节这一路
    就只剩「明天」了。
    """
    return "".join(sorted(dict.fromkeys(rules.trim)))


def _text_of(text: Any) -> str:
    """要判的那段输出：不是字符串就当空串，判据只认字。"""
    return text if isinstance(text, str) else ("" if text is None else str(text))


def _rules(table: Optional[Mapping[str, Any]] = None) -> _Rules:
    """把判据表读成一份只读规则；table 为 None 就去仓库现读默认表。

    缺节缺键一律抛 FabricateBroken——数据里没有的词不在代码里补默认值（补了就是第二处真源）。
    几条硬要求：标记表与豁免表都非空、每条正则编译得过、细节四路齐、两把尺子是正数（覆盖率还是
    (0, 1] 之间的数）、四句人话都有内容。坏一处就响，不静默降级。
    """
    book = table if table is not None else load("fabricate")
    if not isinstance(book, Mapping):
        raise FabricateBroken("数据表", "不是一份键值表，读到的 %s。" % type(book).__name__)
    hit = _mapping(book, "hit")
    reason = _mapping(book, "reason")
    detail = _mapping(book, "detail")
    align = _mapping(book, "align")
    kinds = _mapping(detail, "kinds")
    rules = _Rules(markers=_patterns(book, "markers"),
                   exempt=_patterns(book, "exempt"),
                   digits=_pattern(detail, "digits", "detail.digits"),
                   numerals=_pattern(detail, "numerals", "detail.numerals"),
                   time_words=_words(detail, "time_words"),
                   quotes=_pairs(detail, "quotes"),
                   min_chars=_count(detail, "min_chars"),
                   length_when_empty=_flag(detail, "length_needs_empty_recall"),
                   kinds={name: _word(kinds, name) for name in
                          ("digits", "numerals", "time", "quote", "length")},
                   min_run=_count(align, "min_run"),
                   bigram_overlap=_ratio(align, "bigram_overlap"),
                   strip=frozenset(_chars(book, "strip")),
                   trim=_chars(book, "trim"),
                   sentence_marks=_chars(book, "sentence_marks"),
                   fallback=_word(book, "fallback"),
                   clue=_word(reason, "hit"),
                   clear=_word(reason, "clear"),
                   confess=_word(reason, "fallback"),
                   clause_id=_word(hit, "clause_id"),
                   kind=_word(hit, "kind"),
                   tag=_word(book, "tag"))
    return rules


def _patterns(data: Mapping[str, Any], key: str) -> Tuple[Any, ...]:
    """取一张正则表：非空清单、每项非空字符串、每条都编译得过；坏一条就当场响。"""
    out: List[Any] = []
    for one in _words(data, key):
        try:
            out.append(re.compile(one))
        except re.error as exc:
            raise FabricateBroken("数据", "里 %r 的 %r 编译不过：%s" % (key, one, exc)) from None
    return tuple(out)


def _pattern(data: Mapping[str, Any], key: str, spot: str) -> Any:
    """取一条正则：非空字符串、编译得过。"""
    one = _word(data, key)
    try:
        return re.compile(one)
    except re.error as exc:
        raise FabricateBroken(spot, " 编译不过：%s" % exc) from None


def _pairs(data: Mapping[str, Any], key: str) -> Tuple[Tuple[str, str], ...]:
    """取一张引号对表：非空清单、每项是两项非空字符串（开引号 / 闭引号）。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise FabricateBroken("数据", "的 %r 必须是非空清单（每项一对引号），不是一整串字。" % key)
    out: List[Tuple[str, str]] = []
    for one in value:
        if isinstance(one, str) or not isinstance(one, (list, tuple)) or len(one) != 2:
            raise FabricateBroken("数据", "的 %r 里有一项不是「开引号 + 闭引号」两个字符。" % key)
        left, right = one
        for mark in (left, right):
            if not isinstance(mark, str) or not mark.strip():
                raise FabricateBroken("数据", "的 %r 里有一个空引号。" % key)
        out.append((left, right))
    return tuple(out)


def _count(data: Mapping[str, Any], key: str) -> int:
    """取一个正整数（字数门槛、连续字数门槛）：零与负数都算坏表。"""
    value = _need(data, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FabricateBroken("数据", "的 %r 必须是正整数，读到的是 %r。" % (key, value))
    return value


def _flag(data: Mapping[str, Any], key: str) -> bool:
    """取一个开关：只认真正的布尔，其余形状算坏表（写 "true" 的字符串不算数）。"""
    value = _need(data, key)
    if not isinstance(value, bool):
        raise FabricateBroken("数据", "的 %r 必须是 true / false，读到的是 %r。" % (key, value))
    return value


def _ratio(data: Mapping[str, Any], key: str) -> float:
    """取一个 (0, 1] 之间的比例（2-gram 覆盖率门槛）：不在这个区间就算坏表。"""
    value = _need(data, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        raise FabricateBroken("数据", "的 %r 必须是 (0, 1] 之间的数，读到的是 %r。" % (key, value))
    return float(value)


def _need(data: Mapping[str, Any], key: str) -> Any:
    """取一项；缺了就抛，不补默认值。"""
    try:
        return data[key]
    except (KeyError, TypeError):
        raise FabricateBroken("数据", "缺少 %r 这一项。" % key) from None


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """取一节，必须是键值表。"""
    value = _need(data, key)
    if not isinstance(value, Mapping):
        raise FabricateBroken("数据", "的 %r 不是一份键值表，读到的 %s。" % (key, type(value).__name__))
    return value


def _word(data: Mapping[str, Any], key: str) -> str:
    """取一句非空字符串；空着也算缺。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise FabricateBroken("数据", "的 %r 必须是一句非空字符串。" % key)
    return value


def _chars(data: Mapping[str, Any], key: str) -> str:
    """取一串非空字符（去标点的表、分句用的句末标点）。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise FabricateBroken("数据", "的 %r 必须是一串非空字符。" % key)
    return value


def _words(data: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    """取一张词表：非空清单、每项非空字符串；不接受「一整串字」冒充清单。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise FabricateBroken("数据", "的 %r 必须是非空清单（每项一个非空字符串），不是一整串字。" % key)
    out: List[str] = []
    for one in value:
        if not isinstance(one, str) or not one.strip():
            raise FabricateBroken("数据", "的 %r 里有不是字符串、或空着的项。" % key)
        out.append(one)
    return tuple(out)


def _fill(template: str, spot: str, **fields: Any) -> str:
    """把模板填成人话；模板里有数据没填的占位符就抛，别把 {matched} 这种半成品发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise FabricateBroken(str(spot), " 这句填不出来：%s" % exc) from None
