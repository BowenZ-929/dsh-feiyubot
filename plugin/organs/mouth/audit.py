"""嘴的复述审计：区分照搬设定与正常使用对话原话。

按归一化后的最长公共子串比较注入文本。魂、锚句、心档位词和规矩句
达到数据中的阈值时仍产生违规；对方原话和记忆原文只报告字面重合，
不凭缺少引述标记判断冒充经历。正常确认地点、订单和转述都可能重合。
来源归属、私密话题、重复自身回复由各自检查负责，本项不替它们判语义。

返回状态、违规片段、重合长度与可读原因。缺少材料时如实返回
 audit-unavailable；未知类别抛 AuditBroken。明确请求重复设定时沿用
 requested 豁免并留痕。数据经 warehouse 读取，本模块不写盘或调用模型。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .warehouse import load

STATUS_OK = "ok"                            # 真的比过了：这一轮有注入原文，逐段比完
STATUS_UNAVAILABLE = "audit-unavailable"    # 没拿到注入原文，判不了；不弃这句，但绝不是通过

_REASON_KEYS = ("setting", "quote_unmarked", "quote_marked", "clear", "unavailable")

__all__ = ["AuditBroken", "AuditHit", "AuditResult", "STATUS_OK", "STATUS_UNAVAILABLE", "audit"]


class AuditBroken(ValueError):
    """注入段或审计数据不合格：未知 kind、缺 text、形状不对、数据缺键都算，坏一处就当场响。

    报错信息里指名是哪一段、哪个 kind、哪个键——**不许把未知 kind 静默当设定类**：那会把
    调用方的一处分类错误变成她的沉默，而且没人知道她为什么突然不说话。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("复述审计 %s%s（判据数据在 organs/mouth/data/audit.json，"
                         "由 warehouse.load(\"audit\") 读出）" % (where, detail))


@dataclass(frozen=True)
class AuditHit:
    """一次「在复述」的证据：违规类别、撞上的那几个字、来源段是哪一类、一句人话。"""

    kind: str        # 违规类别，取自数据里的 verdicts（设定段照搬 / 原话没带标记）
    matched: str     # 归一化之后撞上的那一截原文；日志与面板直接显示它
    seg_kind: str    # 来源段是哪一类（数据里的六类之一）
    reason: str      # 一句人话，里面带着来源段、片段与连着的字数


@dataclass(frozen=True)
class AuditResult:
    """一次审计的读数：状态 + 证据 + 一句人话 + 最长撞了几个字 + 这一轮用的阈值。

    字段（api 那批照这个接，改字段等于改接口）：
      status    "ok" 表示真的逐段比过了；"audit-unavailable" 表示没拿到注入原文、判不了
      hit       None 或一条 AuditHit；**status 为 "ok" 且 hit 为 None 才是「没在复述」**
      reason    一句人话：为什么拦、为什么放行、或者为什么判不了
      lcs_len   最长公共子串的长度；拦下时是那一截的长度，放行时是最长的那一截
      threshold 这一轮用的阈值（来自数据，不是代码里的常数）
      skipped   这一条命中按对方的要求没判废（他要她重复／引用）：hit 与 status 原样不变，
                变的只是闸拿它怎么办——decision 保持 pass、话照发
      skip_clause_id  skipped 为真时闸记账用的名字（取自数据），与 hit.kind 不是一回事：
                前者记「为什么没判废」，后者记「她犯了哪一条」
      skip_reason     skipped 为真时那句人话，里面带着 audit-skipped-by-request；账本与面板直接用
    """

    status: str
    hit: Optional[AuditHit]
    reason: str
    lcs_len: int
    threshold: int
    skipped: bool = False
    skip_clause_id: str = ""
    skip_reason: str = ""


@dataclass(frozen=True)
class _Seg:
    """注入的一段：它属于哪一类，原文是什么。"""

    kind: str
    text: str


@dataclass(frozen=True)
class _Rules:
    """data/audit.json 拆成一堆可以直接用的值：数据里没有的一样都不补。"""

    threshold: int
    window: int
    strip: str
    blank: str
    quote_kinds: Tuple[str, ...]
    setting_kinds: Tuple[str, ...]
    plain_kind: str
    markers: Tuple[str, ...]
    verdict_setting: str
    verdict_quote: str
    reasons: Mapping[str, str]
    skip_clause_id: str
    skip_status: str
    skip_reason: str


def audit(text: str, inject: Any = None,
          policy: Optional[Mapping[str, Any]] = None,
          requested: bool = False) -> AuditResult:
    """判这段输出是不是在复述注入的原文：返回一句人话 + 片段 + 来源段 kind，不是布尔。

    inject 是调用方注入的原文段（[{"kind": ..., "text": ...}]，或一段纯字符串）；
    policy 是审计数据（warehouse.load("audit") 读出来的那份表），传 None 就现读仓库里的默认表；
    requested 是「对方这一轮是不是在要她重复／引用」——由闸拿 repeat.requested(asked, 表) 算一次
    传进来，**本文件不判这一件事、也不存那张标记表**（存了就是第二处真源）。

    逐段比最长公共子串，取第一段「撞够阈值又违规」的当结果：

    - 设定类段撞够阈值 → 拦（带了引述标记也拦）。
    - 对方原话或记忆原文撞够阈值 → 只记重合，不判违规。
    - 标记就在前面 → 在 reason 里点名标记；仍检查后续设定段。

    撞上了、而 requested 为真时→ **读数一样算命中**（status 仍是 "ok"、hit 原样带着、面板看得见
    她引了设定），只是另加 skipped=True 与一句取自数据的 skip_reason（带 audit-skipped-by-request）：
    这一句不判废，话照发。为什么还要标记：我们不想让她养成引用设定的习惯，只是不在这句上拦——
    标记留着，账才记得下来。requested 为假时一个字段都不多，旧行为一个字节不变。

    inject 为 None 或空（空清单、全空白字符串）→ status 是 "audit-unavailable"：判不了，
    不弃这句，但也绝不返回 ok（缺料不是清白）。这一态不受 requested 影响：判不了就是判不了。
    """
    rules = _rules(policy)
    segments = _segments(inject, rules)
    if not segments:
        return AuditResult(status=STATUS_UNAVAILABLE, hit=None,
                           reason=rules.reasons["unavailable"].format(status=STATUS_UNAVAILABLE),
                           lcs_len=0, threshold=rules.threshold)
    body = _normalize(_text_of(text), rules.strip, rules.blank)
    longest = 0
    marked: Optional[Tuple[str, str, str]] = None      # （引述标记, 撞上的那截, 来源段类别）
    overlap = None
    for segment in segments:
        length, end = _lcs(body, _normalize(segment.text, rules.strip, rules.blank))
        longest = max(longest, length)
        if length < rules.threshold:
            continue
        matched = body[end - length:end]
        if segment.kind in rules.quote_kinds:
            marker = _marker(body, end - length, rules.window, rules.markers)
            if marker:
                if marked is None:
                    marked = (marker, matched, segment.kind)
                continue
            # 原话重合只能证明用了相同字词，不能判定确认地点/订单是在冒充经历。
            # 继续检查后面的设定段，不能因遇到可用原话而提前放过照搬人设。
            if overlap is None:
                overlap = (segment.kind, matched, length)
            continue
        return _blocked(rules, rules.verdict_setting, segment.kind, matched, length,
                        rules.reasons["setting"].format(
                            seg_kind=segment.kind, matched=matched, lcs_len=length), requested)
    if overlap is not None:
        seg_kind, matched, length = overlap
        reason = rules.reasons["quote_unmarked"].format(
            seg_kind=seg_kind, matched=matched, lcs_len=length, window=rules.window)
    elif marked is not None:
        marker, matched, seg_kind = marked
        reason = rules.reasons["quote_marked"].format(seg_kind=seg_kind, matched=matched,
                                                      marker=marker)
    else:
        reason = rules.reasons["clear"].format(count=len(segments), lcs_len=longest,
                                              threshold=rules.threshold)
    return AuditResult(status=STATUS_OK, hit=None, reason=reason,
                       lcs_len=longest, threshold=rules.threshold)


def _blocked(rules: _Rules, verdict: str, seg_kind: str, matched: str, length: int,
             reason: str, requested: bool = False) -> AuditResult:
    """撞够了就成一条证据：违规类别、来源段那一类、撞上的那几个字、一句人话。

    requested 为真时再补上跳过那三个字段（skipped / skip_clause_id / skip_reason）——**命中照样
    留着**：豁免的是「判不判废」，不是「算不算撞上」。面板要看得出她引了设定，账本要记下这一句
    为什么没判废，两件事都靠这条读数，所以只加字段、不抹证据。
    """
    found = AuditResult(status=STATUS_OK,
                        hit=AuditHit(kind=verdict, matched=matched, seg_kind=seg_kind,
                                     reason=reason),
                        reason=reason, lcs_len=length, threshold=rules.threshold)
    if not requested:
        return found
    return replace(found, skipped=True, skip_clause_id=rules.skip_clause_id,
                   skip_reason=rules.skip_reason.format(verdict=verdict, seg_kind=seg_kind,
                                                        matched=matched, lcs_len=length))


def _rules(policy: Optional[Mapping[str, Any]] = None) -> _Rules:
    """把审计数据读成一份只读规则；policy 为 None 就去仓库现读默认表。

    缺节缺键一律抛 AuditBroken——数据里没有的数不在代码里补默认值（补了就是第二处真源）。
    skip 那一节是「按对方要求不判废」那一笔的账（记账用的名字、记号、那句人话）：它的 status 必须
    出现在 reason 里，否则账本上就认不出「这一句为什么没判废」，说不清等于没记。
    """
    table = policy if policy is not None else load("audit")
    if not isinstance(table, Mapping):
        raise AuditBroken("数据表", "不是一份键值表，读到的 %s。" % type(table).__name__)
    threshold = _above_zero(table, "threshold")
    window = _at_least_zero(table, "window")
    strip = _chars(table, "strip")
    blank = _chars(table, "blank")
    quote_kinds = _words(table, "quote_kinds")
    setting = _words(table, "setting_kinds")
    plain = _word(table, "plain_kind")
    markers = _words(table, "quote_markers")
    verdicts = _mapping(table, "verdicts")
    verdict_setting = _word(verdicts, "setting")
    verdict_quote = _word(verdicts, "quote_unmarked")
    for quote_kind in quote_kinds:
        if quote_kind in setting:
            raise AuditBroken("数据", "里 %r 同时出现在 setting_kinds 与 quote_kinds 中："
                                     "一类段只能归一边。" % quote_kind)
    if plain not in setting:
        raise AuditBroken("数据", "里的 plain_kind %r 不在 setting_kinds 中：纯字符串一律"
                                 "按设定类从严，它必须是一个设定类。" % plain)
    reasons = {key: _word(_mapping(table, "reason"), key) for key in _REASON_KEYS}
    skip = _mapping(table, "skip")
    skip_clause_id = _word(skip, "clause_id")
    skip_status = _word(skip, "status")
    skip_reason = _word(skip, "reason")
    if skip_status not in skip_reason:
        raise AuditBroken("数据", "里 skip.reason 没有带上 skip.status（%r）：账本靠这个记号认出"
                                  "「这一句为什么没判废」，说法与记号不许各走各的。" % skip_status)
    return _Rules(threshold=threshold, window=window, strip=strip, blank=blank,
                  quote_kinds=quote_kinds, setting_kinds=setting, plain_kind=plain,
                  markers=markers, verdict_setting=verdict_setting,
                  verdict_quote=verdict_quote, reasons=reasons, skip_clause_id=skip_clause_id,
                  skip_status=skip_status, skip_reason=skip_reason)


def _segments(inject: Any, rules: _Rules) -> List[_Seg]:
    """把注入的原文整理成一段一段；没有就返回空清单（调用方据此报 audit-unavailable）。

    纯字符串当一段设定类（从严）；清单逐段校验，未知 kind 当场抛并指名是哪一个。
    """
    if inject is None:
        return []
    if isinstance(inject, str):
        return [_Seg(kind=rules.plain_kind, text=inject)] if inject.strip() else []
    if not isinstance(inject, (list, tuple)):
        raise AuditBroken("inject", "只能是 [{\"kind\": ..., \"text\": ...}] 这样的清单、"
                                    "或一段纯字符串，读到的是 %s。" % type(inject).__name__)
    known = rules.setting_kinds + rules.quote_kinds
    out: List[_Seg] = []
    for index, raw in enumerate(list(inject)):
        where = "第 %d 段" % (index + 1)
        if not isinstance(raw, Mapping):
            raise AuditBroken(where, "不是一份键值表（要写成 {\"kind\": ..., \"text\": ...}），"
                                     "读到的是 %s。" % type(raw).__name__)
        kind = raw.get("kind")
        kind = kind.strip() if isinstance(kind, str) else ""
        if not kind:
            raise AuditBroken(where, "没有 kind：每一段都要说明它是哪一类（只认 %s），不许猜。"
                              % "、".join(repr(one) for one in known))
        if kind not in known:
            raise AuditBroken(where, "的 kind 是 %r，不认识；只认 %s。未知 kind 不许当成设定类"
                                     "静默从严，也不许跳过。" % (kind, "、".join(repr(one) for one in known)))
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            raise AuditBroken(where, "的 text 不是一句非空字符串（%r 这一段的原文丢了）。" % kind)
        out.append(_Seg(kind=kind, text=text))
    return out


def _normalize(text: str, strip: str, blank: str) -> str:
    """比之前先归一化：把空白与标点整张表里的字符都去掉，只留下实字。"""
    drop = set(strip) | set(blank)
    return "".join(ch for ch in text if ch not in drop)


def _lcs(body: str, segment: str) -> Tuple[int, int]:
    """最长公共**子串**（连续，不是子序列）：返回（长度, 它在 body 里的结束位置）。

    结束位置是 1 基的右开下标，撞上的那截就是 body[end - 长度:end]；判「连着几个字一样」
    必须用子串，子序列会把跳着撞字的句子也算进去，那不是照搬。
    """
    if not body or not segment:
        return 0, 0
    prev = [0] * (len(body) + 1)
    best = 0
    end = 0
    for j in range(1, len(segment) + 1):
        cur = [0] * (len(body) + 1)
        mark = segment[j - 1]
        for i in range(1, len(body) + 1):
            if body[i - 1] == mark:
                value = prev[i - 1] + 1
                cur[i] = value
                if value > best:
                    best = value
                    end = i
        prev = cur
    return best, end


def _marker(body: str, start: int, window: int, markers: Sequence[str]) -> str:
    """命中片段前面 window 个字以内有没有引述标记：有就返回那一个，没有返回空串。

    按归一化之后的字算（空白与标点已经不在里面）。标记跨在窗口边界上、一半在窗外就算没有：
    宁可多拦一句，也不把「离得挺远」当成「刚说过」。
    """
    head = body[max(0, start - window):start]
    for word in markers:
        if word in head:
            return word
    return ""


def _text_of(text: Any) -> str:
    """要判的那段输出：不是字符串就当空串，判据只认字。"""
    return text if isinstance(text, str) else ("" if text is None else str(text))


def _need(data: Mapping[str, Any], key: str) -> Any:
    """取一项；缺了就抛，不补默认值。"""
    try:
        return data[key]
    except (KeyError, TypeError):
        raise AuditBroken("数据", "缺少 %r 这一项。" % key) from None


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """取一节，必须是键值表。"""
    value = _need(data, key)
    if not isinstance(value, Mapping):
        raise AuditBroken("数据", "的 %r 不是一份键值表，读到的 %s。" % (key, type(value).__name__))
    return value


def _word(data: Mapping[str, Any], key: str) -> str:
    """取一句非空字符串；空着也算缺。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise AuditBroken("数据", "的 %r 必须是一句非空字符串。" % key)
    return value


def _chars(data: Mapping[str, Any], key: str) -> str:
    """取一张字符表（标点表、空白表）：非空字符串，按字符逐个用。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value:
        raise AuditBroken("数据", "的 %r 必须是一串非空字符（表里的字符会被逐个去掉）。" % key)
    return value


def _words(data: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    """取一张词表：非空清单、每项非空字符串；不接受「一整串字」冒充清单。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise AuditBroken("数据", "的 %r 必须是非空清单（每项一句非空字符串），不是一整串字。"
                          % key)
    out: List[str] = []
    for one in value:
        if not isinstance(one, str) or not one:
            raise AuditBroken("数据", "的 %r 里有不是字符串、或空着的项。" % key)
        out.append(one)
    return tuple(out)


def _above_zero(data: Mapping[str, Any], key: str) -> int:
    """取一个 1 以上的整数（阈值）；布尔不算整数。"""
    value = _need(data, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AuditBroken("数据", "的 %r 必须是 1 以上的整数，读到的不是。" % key)
    return value


def _at_least_zero(data: Mapping[str, Any], key: str) -> int:
    """取一个 0 以上的整数（窗口）；布尔不算整数。"""
    value = _need(data, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuditBroken("数据", "的 %r 必须是 0 以上的整数，读到的不是。" % key)
    return value
