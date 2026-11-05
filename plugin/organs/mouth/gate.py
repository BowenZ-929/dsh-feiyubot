"""嘴的闸：开口之前先过一遍条款；命中就拦，改一次还不行就弃掉这句。

**条款不在代码里，全部来自「家·仓库」。** 调用方用 warehouse.load("clauses")["clauses"] 读出一张
条款清单，原样交给 check / enforce；本文件一条条款都不写——加一条、改一条、删一条都是改数据
（organs/mouth/data/clauses.json），代码不动。理由在 docs/reference/mouth.md：凡是要她「不说某种话」，
一律做成出口的结构闸门；判据表一旦写进代码，就成了第二处真源，改口吻要改代码。

条款形状（一条一条校验，坏一条就当场抛 ClauseBroken，并指名是哪一条）：

    {"id": 稳定字符串, "kind": 标签, "pattern": 正则, "reason": 一句人话, "note": 可选}

顶层是一份键值表 {"clauses": [...]}。kind 只是给理由用的标签（数据里现有 反例 / 可判废 / 流程泄露
三类），匹配一律只看 pattern——给一条条款换 kind 不会改变拦不拦，换 pattern 才会。

**条款顺序即优先级**：按 clauses 里的先后顺序逐条 re.search，返回第一条命中的，后面的不再看。
要调优先级就调数据里的顺序，不要在代码里加「先看哪类」的分支。

两道出口，缺一道都不算拦住：

- check(text, clauses, inject=None, policy=None) -> Hit | None：命中返回一条 Hit，里面带一句人话
  原因（含命中的那段原文）；没命中返回 None。**不返回布尔**——布尔说不出「为什么拦」，日志与面板
  也就没法复盘，只能再翻译一次。
- enforce(text, clauses, rewrite, sink=None, inject=None, policy=None) -> Verdict：命中一次 → 交给
  注入的 rewrite 改一次 → 再 check；还命中就 decision="dropped"、text 留空串，两次命中的原因都按
  发生顺序留在 hits 里。Verdict.audit 是这一轮复述审计的读数（status / hit / lcs_len / threshold）。

rewrite 是调用方注入的 callable（text -> text），本文件没有模型客户端：闸只管判，不改写。
传 None 视为「改不了」，直接弃掉——**出口不许 fail-open**，改不了就别说这句。rewrite 自己抛错、
或者改回来是空的，也只算「改不了」，错误照实写进 reason，出口本身不抛错。

sink 也是调用方注入的 callable（clause_id, reason -> None），**只在「弃掉」那一次**被叫一遍，
好让弃言留下痕迹：谁被弃、为什么。sink 为 None 就不记，闸仍是纯函数——本文件不取时钟、不写盘，
时间与落盘都在调用方（api 层与 organs/mouth/drops.py）。sink 自己抛错就照实往上抛：记账失败
也是失败，不许咽掉（否则「今天弃了几次」会悄悄变少）。

**停用的条款跳过匹配，但停用这件事必须看得见。** 数据里 enabled 为 false 的条款不参与 re.search，
它的 id 收在 Verdict.disabled 里，由 api 层原样带进返回的 disabled_ids——停用不是删除，更不是
「没命中」。空清单照旧抛 ClauseBroken；**一条生效条款都没有（全部被停用）也一样抛**：
没有闸就是没有闸，不许当成「这句没踩条款」放行。

**复述审计是另一半闸（第二轮 A）**：条款管「怎么说」，审计管「说的是不是抄来的」。注入的原文
由调用方给（inject），判据全在 organs/mouth/audit.py 与 data/audit.json——设定类（魂、锚句、
记忆原文、心档位词、规矩句）被照搬一律拦；对方原话只许带引述标记转述，标记离得太远也拦。
闸把审计拦下的那一条当成一次命中，走同一条「改一次还不行就弃」的路：审计拦下的 Hit 里，
clause_id 记违规类别、kind 记来源段那一类（两个都取自数据里的说法），所以弃言账本里看到的仍是
一个能指认的名字。注入段形状坏掉（未知 kind 之类）由 audit 当场抛 AuditBroken，照实往上抛。

**audit-unavailable 只挂在 Verdict.audit 上，check 的返回值不承载它。** 这么选有两条理由：
check 的返回类型是 Optional[Hit]，塞一条假 Hit 进去等于把「没命中」与「判不了」挤进同一条通道，
调用方再也分不开；而既有调用与断言（api 层、脑那一侧）都在用 check(...) is None 认「没踩条款」，
加一条「永远是 Hit」的旁路会把它们全部推翻。要区分 ok 与 audit-unavailable 就看 enforce 的
Verdict.audit（status 不是 "ok" 就是没真的比过），或者直接调 organs/mouth/audit.py 的 audit()。
**这条不影响「缺料不弃这句」**：审计判不了时 enforce 照常按条款走，decision 与既有五种结局一字
不变，只是 audit.status 如实写着 audit-unavailable——缺料是调用侧的问题，不拿她的沉默来兜。

纯函数：入参只有一段文本与一张只读的条款清单，出参只有结构化数据；不碰时钟、不碰随机数、
不联网、不碰宿主 IO（本文件不 import time / datetime / random），所以能离线预览，也能被单独替换。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .audit import AuditResult, audit as _audit

__all__ = ["ClauseBroken", "Hit", "Verdict", "check", "enforce"]


class ClauseBroken(ValueError):
    """条款形状不合格：缺键、正则编译不过、id 重复都算，坏一条就当场响。

    报错信息里带着那条条款的 id（连 id 都没有就带它在清单里的位置），好让人一眼找到清单里的
    哪一条写错了。**不许跳过坏条款静默降级**：跳过一条坏条款，闸就少一道，而且没人知道
    （CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("条款清单 %s %s（清单在 organs/mouth/data/clauses.json，"
                         "由 warehouse.load(\"clauses\")[\"clauses\"] 读出）" % (where, detail))


@dataclass(frozen=True)
class Hit:
    """一次命中：哪一条、什么标签、为什么（一句人话）、命中的那段原文。"""

    clause_id: str
    kind: str
    reason: str     # 一句人话，里面带着命中的那段原文；日志与面板直接显示这句
    matched: str    # 正则这次命中的那段原文


@dataclass(frozen=True)
class Verdict:
    """过闸的结果：最终发什么、怎么决定的、命中过什么、一句人话总结。"""

    text: str                                   # 最终文本；弃了就是空串
    decision: str                               # "pass" | "rewritten" | "dropped"
    hits: List[Hit] = field(default_factory=list)   # 按发生顺序，最多两条（改写前、改写后）
    reason: str = ""                            # 一句人话：为什么放行、为什么弃掉
    disabled: List[str] = field(default_factory=list)   # 这次被跳过（enabled=false）的条款 id，按清单顺序
    # 这一轮复述审计的读数（audit.AuditResult）：status 不是 "ok" 就是没真的比过，别当成通过；
    # 记的是**进门那一段**（调用方给的 text）的读数，改写后的那次只为决定 decision，不覆盖它。
    audit: Optional[AuditResult] = None


def check(text: str, clauses: Sequence[Mapping[str, Any]],
          inject: Any = None, policy: Optional[Mapping[str, Any]] = None) -> Optional[Hit]:
    """看这段字有没有踩条款、有没有在复述注入的原文：命中返回一条 Hit，没命中返回 None。

    **返回的不是布尔。** Hit 里带一句人话原因，原因里含这次命中的那段原文，所以日志、面板、
    复盘都能直接用这句话，不必再翻译一遍。

    先判条款（既有逻辑不动），条款没命中再看审计：按 clauses 的先后顺序逐条 re.search，返回第一
    条命中的（顺序即优先级）；零宽正则也会命中，那种情况下原因里改说位置，免得出现一对空引号。

    **没给 inject 就整段不跑审计**：没有料可比的审计既不是通过、也不是拦住，而这个返回类型装不下
    第三种状态。要区分 ok 与 audit-unavailable，看 enforce 的 Verdict.audit 或直接调 audit()。
    """
    prepared, _disabled = _prepared(clauses)
    body = text or ""
    found = _scan(prepared, body)
    if found is not None:
        return found
    return _recited(body, inject, policy)


def enforce(text: str, clauses: Sequence[Mapping[str, Any]],
            rewrite: Optional[Callable[[str], str]] = None,
            sink: Optional[Callable[[str, str], None]] = None,
            inject: Any = None, policy: Optional[Mapping[str, Any]] = None) -> Verdict:
    """过闸：命中一次 → 用注入的 rewrite 改一次 → 再 check；还命中就弃掉这句。

    rewrite 是调用方注入的 callable（text -> text），本文件没有模型客户端。五种结局：

    - 没命中：decision="pass"，text 原样发出。
    - 命中但 rewrite 是 None：decision="dropped"、text=""——改不了就不许发，不许 fail-open。
    - rewrite 抛错、或改回来是空的：同样 dropped，并把「重写失败」这件事本身写进 reason。
    - 改完不再命中：decision="rewritten"，text 是改写后的那句。
    - 改完还命中：decision="dropped"、text=""，hits 两条（第一次与第二次）都留着。

    sink 是调用方注入的 callable（clause_id, reason -> None）：只有 dropped 那五种里的三种会被叫一遍，
    参数是**最终拦下它的那条**条款 id 与这一句人话原因；pass 与 rewritten 不叫——话发出去了，
    没有弃言可记。sink=None 就不记，闸照旧是纯函数（sink 自己抛错会照实往上抛）。

    复述审计接在条款后面，走同一条路：审计拦下的那一条也当成一次命中，改一次还不行就弃。
    返回值里的 audit 是这一轮审计的读数（audit.AuditResult）：status 为 "ok" 才算真的比过，
    为 "audit-unavailable" 表示没拿到注入原文、判不了——**这时决策照常按条款走，不弃这句**，
    但状态如实带出来，api 层照实透出（缺料不是清白，也不是拦）。
    """
    prepared, disabled = _prepared(clauses)
    body = text or ""
    found = _scan(prepared, body)
    seen = _audit(body, inject, policy)
    first = found if found is not None else _from_audit(seen)
    if first is None:
        return Verdict(text=body, decision="pass", hits=[],
                       reason="没命中任何条款，这句照发。", disabled=disabled, audit=seen)
    if rewrite is None:
        return _dropped(Verdict(text="", decision="dropped", hits=[first],
                                reason="%s也没给重写器，这句改不了，弃掉不发。" % _lead(first),
                                disabled=disabled, audit=seen), sink)
    try:
        again = rewrite(body)
    except Exception as exc:          # 重写失败只算「改不了」，出口不许抛错
        return _dropped(Verdict(text="", decision="dropped", hits=[first],
                                reason="%s重写时出了错（%s：%s），这句改不了，弃掉不发。"
                                       % (_lead(first), type(exc).__name__, exc),
                                disabled=disabled, audit=seen), sink)
    rewritten = again if isinstance(again, str) else ("" if again is None else str(again))
    if not rewritten.strip():
        return _dropped(Verdict(text="", decision="dropped", hits=[first],
                                reason="%s重写回来是空的，等于改不了，弃掉不发。" % _lead(first),
                                disabled=disabled, audit=seen), sink)
    retried = _scan(prepared, rewritten)
    second = retried if retried is not None else _from_audit(_audit(rewritten, inject, policy))
    if second is None:
        return Verdict(text=rewritten, decision="rewritten", hits=[first],
                       reason="%s改写一次后不再命中，发改写后的这句。" % _lead(first),
                       disabled=disabled, audit=seen)
    return _dropped(Verdict(text="", decision="dropped", hits=[first, second],
                            reason="%s改了一次还是这样（第二次还是被条款 %r 拦下，命中「%s」），"
                                   "这句弃掉不发。" % (_lead(first), second.clause_id, second.matched),
                            disabled=disabled, audit=seen), sink)


def _dropped(verdict: Verdict, sink: Optional[Callable[[str, str], None]]) -> Verdict:
    """把弃掉的那一句交给注入的 sink 记一笔；sink 为 None 就什么也不发生。

    只传最终拦下它的那条条款与那句人话原因——账本记的是「哪条条款、为什么弃」，不存整句原文；
    reason 里会带上这次命中的那一小段（闸的判据要用它复盘），整句不会进账本。
    """
    if sink is not None and verdict.hits:
        sink(verdict.hits[-1].clause_id, verdict.reason)
    return verdict


def _recited(text: str, inject: Any, policy: Optional[Mapping[str, Any]]) -> Optional[Hit]:
    """审计这一关：判「在复述」就返回一条 Hit；没给注入原文或判不了都返回 None。

    没给 inject（None、空清单、空白字符串）就整段不跑：没有料可比的审计既不是通过、也不是拦住，
    而这个返回类型装不下第三种状态——那种情况的状态在 enforce 的 Verdict.audit 里。给了 inject
    就照实判，注入段形状坏掉（未知 kind 之类）由 audit 当场抛 AuditBroken，不许静默从严。
    """
    if not _has_inject(inject):
        return None
    return _from_audit(_audit(text, inject, policy))


def _from_audit(result: Optional[AuditResult]) -> Optional[Hit]:
    """把审计拦下的那一条翻成闸自己的 Hit：clause_id 记违规类别，kind 记来源段那一类。

    两个名字都取自数据（audit.json 的 verdicts 与六类 kind），所以弃言账本里看到的仍是一个能
    指认的名字；matched 是归一化之后撞上的那几个字，reason 是审计那句人话。
    """
    if result is None or result.hit is None:
        return None
    one = result.hit
    return Hit(clause_id=one.kind, kind=one.seg_kind, matched=one.matched, reason=one.reason)


def _has_inject(inject: Any) -> bool:
    """有没有真的给了一段注入原文：None、空清单、空白字符串都算「没给」。

    别的形状（数字、键值表）一律当成给了——让 audit 去报错并指名形状，不在这里替它猜。
    """
    if inject is None:
        return False
    if isinstance(inject, str):
        return bool(inject.strip())
    if isinstance(inject, (list, tuple)):
        return bool(inject)
    return True


def _scan(prepared: Sequence[Tuple[str, str, str, Any]], body: str) -> Optional[Hit]:
    """按清单顺序扫一遍，第一条命中的就停——顺序即优先级。"""
    for ident, kind, reason, compiled in prepared:
        found = compiled.search(body)
        if found is not None:
            matched = found.group(0)
            return Hit(clause_id=ident, kind=kind, matched=matched,
                       reason=_sentence(reason, matched, found.start()))
    return None


def _prepared(clauses: Any) -> Tuple[List[Tuple[str, str, str, Any]], List[str]]:
    """把整张清单校验并编译一遍，返回（生效条款, 停用条款 id 名单）。

    全部条款都要合格才返回，坏一条就抛 ClauseBroken（不做部分通过）。enabled=false 的那几条
    跳过匹配，但**照旧校验、照旧编译**：停用不是藏坏数据的角落，写坏了的 pattern 今天不跑、
    明天一开就炸，不如现在响。停用名单按清单顺序带回，供 api 层挂到 disabled_ids 上。

    空清单抛 ClauseBroken（没有闸就是没有闸）；**一条生效条款都没有（全部停用）同样抛**——
    那不是「没命中」，那是这次压根没法判。
    """
    if clauses is None:
        raise ClauseBroken("没给", "（None）：一条条款都没有就等于没有闸，不许静默放行。")
    items = list(clauses)
    if not items:
        raise ClauseBroken("是空的", "：一条条款都没有就等于没有闸，不许静默放行。")
    seen: Dict[str, int] = {}
    out: List[Tuple[str, str, str, Any]] = []
    disabled: List[str] = []
    for index, raw in enumerate(items):
        ident, kind, reason, compiled, enabled = _prepared_one(index, raw)
        if ident in seen:
            raise ClauseBroken("%r" % ident, "的 id 与第 %d 条重复（id 要能唯一指认一条条款）。"
                               % (seen[ident] + 1))
        seen[ident] = index
        if not enabled:
            disabled.append(ident)
            continue
        out.append((ident, kind, reason, compiled))
    if not out:
        raise ClauseBroken("全部停用", "：%d 条条款都写了 enabled=false（%s），一条生效条款都没有，"
                           "等于没有闸，不许当成「没命中」放行。"
                           % (len(items), "、".join(repr(one) for one in disabled)))
    return out, disabled


def _prepared_one(index: int, raw: Any) -> Tuple[str, str, str, Any, bool]:
    """校验一条条款并编译它的正则；坏在哪就说哪，报错里带着这条的 id。

    enabled 可以没有（缺省 true）；有了就必须是布尔——写成 "false" 这种字符串是在赌别人的
    truthy 判断，当场响比猜它的意思便宜。
    """
    where = "第 %d 条" % (index + 1)
    if not isinstance(raw, Mapping):
        raise ClauseBroken(where, "不是一份键值表，是 %s。" % type(raw).__name__)
    ident = raw.get("id")
    ident = ident.strip() if isinstance(ident, str) else ""
    if not ident:
        raise ClauseBroken(where, "没有 id（每条条款都要有一个稳定字符串做 id）。")
    for key in ("kind", "pattern", "reason"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ClauseBroken("%r" % ident, "缺 %s，或它不是一句非空字符串。" % key)
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ClauseBroken("%r" % ident, "的 enabled 只能是 true 或 false，读到的不是布尔。")
    try:
        compiled = re.compile(raw["pattern"])
    except re.error as exc:
        raise ClauseBroken("%r" % ident, "的 pattern 正则编译不过：%s。" % exc) from None
    return ident, raw["kind"], raw["reason"], compiled, enabled


def _sentence(reason: str, matched: str, at: int) -> str:
    """把条款自带的那句人话，与这次命中的原文拼成一句可以直接读的结论。"""
    if matched:
        return "%s；命中「%s」。" % (reason.rstrip("。"), matched)
    return "%s；这次是零宽命中（位置在第 %d 字）。" % (reason.rstrip("。"), at + 1)


def _lead(hit: Hit) -> str:
    """一句话抬头：这一句是先被哪一条拦下的。"""
    return "先被条款 %r（%s）拦下：%s" % (hit.clause_id, hit.kind, hit.reason)
