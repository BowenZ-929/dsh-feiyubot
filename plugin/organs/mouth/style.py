"""嘴的目标长度与句末标点：这一轮「该多短」「句末别句句带句号」是两件事。

核只有一条硬上限（五档预算里那条，超了就截断）。上限管的是「不许超过」，管不了「这一轮别铺开」、
也管不了「句号别句句都收」。这里补两层：

- **目标长度**：按场景（私聊 / 群聊）与语气档 1..4 给一段该有的长度（群聊比私聊更短）。区间、
  标签、每一句人话全在 organs/mouth/data/style_target.json（经 warehouse.load 只读借入），
  代码里一个数都不写——改区间是改数据，不是改代码；数据缺键当场抛 StyleBroken，不补默认值。
- **句末标点**：一次回复里句末是「。」的比例不得超过数据里的上限（默认 0.30）；少于数据里的
  最少句数（默认 3 句）不判——两句话都带句号很正常。超了就改一次，二次仍超放行并记账：
  标点是毛病，不是红线，不许为它把话吞掉。

区间收窄过两次，旧值一直没删：它们都挪在 style_target.json 的 retired 节里（带 reason 与日期）
标作废。为什么收窄也留在那句 reason 里——她 56 字那句在旧的第 4 档（24..120）里合法 pass，
那是区间本身宽，不是闸没咬住；后来收成 8–24 / 12–36 / 16–56 / 20–72 仍然只有档位一个维度，
私聊与群聊吃同一套数，群里就显得太长，于是第三版换成按场景分表。

**场景从哪儿来**：调用方给（/chat 的 scope）。不给就取数据里的 default_scene；给了不认识的
场景名当场抛并列出数据里现有的场景——**不许回落**：回落一次，账本上就再也分不清这一句是拿
哪一套区间判的。

**两者不互相覆盖，写在这里就是钉死它**：核的五档预算是**上限**（超了由核截断），嘴的目标区间是
**目标**（超了由嘴重写一次，不截断也不发脾气）。所以：

- 嘴碰到超硬上限的那一句**不截断**，只把 over_hard_cap 举起来、原样交核——截断是核的事；
- 核也不挑目标，这一轮该短该长是嘴的事，两边各管一件事，谁也不许拿自己的数盖住另一个。

三条规则（目标长度）：
- 落在目标区间里 → decision="pass"，原样发；
- 超目标但没超硬上限 → **不截断**，用注入的 rewrite 改一次。没给 rewrite 就明说「改不了」，
  放行并记账——「改不了」和「合格」是两句话，不许混着说；改完仍在区间外也一样放行并记账；
- 超硬上限 → over_hard_cap=true，text 原样，交核。

四种结局（句末标点）：
- 少于最少句数 → judged=False、decision="pass"：没判，不是判过了；
- 没超上限 → judged=True、decision="pass"；
- 超上限、给了 rewrite → 改一次；改完落到上限里是 "rewritten"，仍在上面是 "over_mark"（放行并记账）；
- 超上限、没给 rewrite / rewrite 抛错 / 改回来是空的 → "over_mark"，放行原话并记账。

**重写的两句指令也在数据里**（prompt.length / prompt.stops）：本层只负责把它们按数据填好交出去。
改回来的新稿先过 accept（非空、与原文不同、原文里的数字一个都不许丢）；长度与句号率是否真的
合格，由 apply 与 stops 自己拿数据重算——accept 不替它们判。

记账 = 走注入的 sink（decision, reason），调用方决定写哪儿；本文件额外提供与 drops 同款的 JSONL
写入函数 record（style.jsonl），路径口径一致：参数 > 环境变量 MOUTH_TRACE_DIR > 包内 trace/。
apply 与 stops 自己不写盘、也不取时钟：要落账就由调用方的 sink 调 record，ts 由那个调用方给。

纯：不 import time / datetime / random，不碰宿主体；数据只经 warehouse 只读借阅。字数就是
len(text)（标点也算一个字）——这里要的是「这一句发出去多长」，剔除可见字符是 split 的
visible_chars 的活，不是这一层的活。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .warehouse import load

__all__ = ["StyleBroken", "StyleVerdict", "StopVerdict", "apply", "stops", "accept", "prompt",
           "target_range", "describe", "path_of", "record"]

TRACE_DIR = Path(__file__).resolve().parent / "trace"   # 包内痕迹目录（家里的仓库做出来前的临时落脚点）
ENV_VAR = "MOUTH_TRACE_DIR"                             # 环境变量：换一个痕迹根目录，不动代码
LEDGER_NAME = "style.jsonl"                             # 账本文件名：一行一条长度账
LEDGER_SUFFIX = ".jsonl"                                # 参数直接点到一个账本文件时，认这个后缀

SCENES_KEY = "scenes"                                   # 数据里按场景分的那一节
DEFAULT_SCENE_KEY = "default_scene"                     # 调用方没给场景时用哪一个（也在数据里）
RANGES_KEY = "ranges"                                   # 某个场景里目标区间那一节
REASONS_KEY = "reasons"                                 # 数据里人话模板那一节
STOPPED_KEY = "stopped"                                 # 句末标点那一节
PROMPT_KEY = "prompt"                                   # 重写指令那两个模板
ACCEPT_KEY = "accept"                                   # 新稿要过的那一关

Rewrite = Callable[[str], str]                          # 改一次：收一句、回一句
Sink = Callable[[str, str], None]                       # 记账：收（decision，人话原因）


class StyleBroken(ValueError):
    """长度/标点这边不合格：不认识的语气档或场景、数据缺键缺模板、硬上限不是数，都在这里当场响并指名。

    **不许猜一个档位继续走、也不许补一个默认区间**：目标区间一旦在代码里有了默认值，
    「数值全在数据里」这句话就成了两处真源（CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__("长度这边 %s（目标区间在 organs/mouth/data/style_target.json，"
                         "由 warehouse.load(\"style_target\") 读出）" % detail)


@dataclass(frozen=True)
class StyleVerdict:
    """一次长度判断的结果：发什么、目标区间多少、多少字、怎么决定的、为什么。

    字段（api 层按这个接，改字段等于改接口）：
      text             最终发出去的那句：pass 原样；rewritten 是改写后的；超目标与超硬上限也都是
                       改写后的（或原句）——**本层永不截断**
      target_lo        这一档的目标下限（字数）
      target_hi        这一档的目标上限（字数）
      chars            最后这句多少字（改写后就按改写后的算；改写前后的两个数在 reason 里说）
      decision         "pass" | "rewritten" | "over_target"
      rewrites         重写了几次（0 或 1）
      over_hard_cap    是否超了核给的硬上限。超了嘴也不截断：true 就是「交核」的那张条子
      reason           一句人话：按哪个场景哪个档、区间多少、这次算不算合格、改不了的原因是什么
      scene            判这一句用的是哪一套区间（数据里 scenes 的键）
    """

    text: str
    target_lo: int
    target_hi: int
    chars: int
    decision: str
    rewrites: int
    over_hard_cap: bool
    reason: str
    scene: str = ""


@dataclass(frozen=True)
class StopVerdict:
    """一次句末标点判断的结果：发什么、几句、几句以「。」收尾、判没判、怎么决定的、为什么。

    字段（api 层按这个接）：
      text       最终发出去的那一段：rewritten 是改写后的，其余都是原样
      sentences  这一段的句数（句末按数据里的 sentence_ends 数）
      stopped    其中句末就是 mark（默认「。」）的句数
      ratio      上面那一对的比例（保留三位小数）
      ceiling    数据里的上限
      min_sentences 数据里的最少句数：少于它不判
      judged     **这一关到底判没判**：false 是「句数不够，没判」，不是「判过了没问题」
      decision   "pass" | "rewritten" | "over_mark"
      rewrites   重写了几次（0 或 1）
      reason     一句人话：几句里有几局收「。」、上限多少、这次算不算合格
    """

    text: str
    sentences: int
    stopped: int
    ratio: float
    ceiling: float
    min_sentences: int
    judged: bool
    decision: str
    rewrites: int
    reason: str
    mark: str = ""


def target_range(tier: Any, table: Optional[Mapping[str, Any]] = None,
                 scene: Optional[str] = None) -> Tuple[int, int]:
    """取这一档的目标区间 (lo, hi)；tier 认 1..4 的整数，也认数据里那种字符串键。

    场景给了就按它那套表判，没给取数据里的 default_scene；不认识的档位或场景抛 StyleBroken
    并指名（连同数据里现有的档/场景一起列出来）。
    """
    book = table if isinstance(table, Mapping) else load("style_target")
    _name, _label, lo, hi, _tier, _tier_label = _ranges(tier, book, scene)
    return lo, hi


def describe(tier: Any, table: Optional[Mapping[str, Any]] = None,
             scene: Optional[str] = None) -> Dict[str, Any]:
    """这一场景这一档的全套读数：{scene, label, lo, hi, tier, tier_label, p90}。

    api 层拿它进回包、拼重写指令：场景名、场景标签、档键、档标签、区间、p90 **全都从数据来**，
    别处不许再算一遍（算两遍迟早分叉）。p90 只是个目标数（九成的泡不该超过多少字），这里只报不判。
    """
    book = table if isinstance(table, Mapping) else load("style_target")
    name, scene_label, lo, hi, key, tier_label = _ranges(tier, book, scene)
    entry = _scene(scene, book)[1]
    value = entry.get("p90")
    if isinstance(value, bool) or not isinstance(value, int):
        raise StyleBroken("的场景 %r 缺 %r，或它不是整数（读到的 %r）" % (name, "p90", value))
    return {"scene": name, "label": scene_label, "lo": lo, "hi": hi, "tier": key,
            "tier_label": tier_label, "p90": value}


def apply(text: str, tier: Any, hard_cap: Any,
          table: Optional[Mapping[str, Any]] = None,
          rewrite: Optional[Rewrite] = None,
          sink: Optional[Sink] = None,
          scene: Optional[str] = None) -> StyleVerdict:
    """按这一场景这一档的目标区间看这一句：在区间里就过，超目标就改一次，超硬上限就交核（不截断）。

    scene 是场景名（/chat 的 scope：group / private），不给取数据里的 default_scene。

    四种结局（decision 只有三个值，区别要看 over_hard_cap 与 reason）：

    - 在区间里 / 短于下限 → decision="pass"，原样发。短不拦：目标是目标，不是配额。
    - 超目标但没超硬上限、且给了 rewrite → 改一次；改完在区间里是 "rewritten"，仍在区间外是
      "over_target"（放行并记账）。rewrite 抛错或改回来是空的，都算「改不了」：放行原句并记账。
    - 超目标但没超硬上限、且 rewrite 是 None → "over_target"，放行并记账，reason 里明说「改不了」——
      这不是「合格」，只是这一层没有别的办法。
    - 超硬上限 → "over_target" + over_hard_cap=True，text 原样，**嘴不截断**，交核。

    sink 是调用方注入的 (decision, reason -> None)，只在「记账」那几次被叫一遍（超目标、改不了、
    超硬上限）；pass 与 rewritten 不叫。sink 自己抛错就照实往上抛：记账失败也是失败，不许咽掉。
    """
    book = table if isinstance(table, Mapping) else load("style_target")
    name, scene_label, lo, hi, key, tier_label = _ranges(tier, book, scene)
    lines = _lines(book)
    body = text if isinstance(text, str) else ""
    cap = _cap(hard_cap)
    before = len(body)
    fields: Dict[str, Any] = {"tier": key, "label": tier_label, "lo": lo, "hi": hi, "cap": cap,
                              "chars": before, "before": before, "after": before,
                              "scene": scene_label}
    if before > cap:
        reason = _reason(lines, "over_hard_cap", fields)
        _account(sink, "over_target", reason)
        return StyleVerdict(text=body, target_lo=lo, target_hi=hi, chars=before,
                            decision="over_target", rewrites=0, over_hard_cap=True, reason=reason,
                            scene=name)
    if before > hi:
        return _over_target(body, before, lo, hi, fields, lines, rewrite, sink, name)
    line = "short" if before < lo else "pass"
    reason = _reason(lines, line, fields)
    return StyleVerdict(text=body, target_lo=lo, target_hi=hi, chars=before,
                        decision="pass", rewrites=0, over_hard_cap=False, reason=reason, scene=name)


def stops(text: str, *,
          table: Optional[Mapping[str, Any]] = None,
          rewrite: Optional[Rewrite] = None,
          sink: Optional[Sink] = None) -> StopVerdict:
    """看这一段话的句末「。」比例：少于最少句数不判；超上限就改一次；二次仍超放行并记账。

    判的是**整段话**（她说出口的那一整段），不是单独一泡：一泡最多两句，「两三句两个句号很正常」
    这条门槛本来就是按整段话定的。句数按数据里的 sentence_ends 数（没有收尾符号的尾巴也算一句），
    句末正好是数据里的 mark（默认「。」）才算「带句号」。

    rewrite 是注入的 callable（text -> text）；不给就明说「改不了」，放行并记账。sink 与 apply 同一套
    （只在记账那几次被叫），本层自己不写盘、不取时钟。
    """
    book = table if isinstance(table, Mapping) else load("style_target")
    rules = _stopped(book)
    mark = _text_in(rules, "mark")
    ceiling = _number_in(rules, "ceiling")
    fewest = _whole_in(rules, "min_sentences")
    ends = _text_in(rules, "sentence_ends")
    lines = _section(rules, "reasons", STOPPED_KEY)
    body = text if isinstance(text, str) else ""
    rows = _sentences(body, ends)
    total, stopped = len(rows), _count(rows, mark)
    fields: Dict[str, Any] = {"mark": mark, "ceiling": _percent(ceiling),
                              "min_sentences": fewest, "sentences": total,
                              "stopped": stopped, "ratio": _percent(_ratio(stopped, total))}
    if total < fewest:
        return StopVerdict(mark=mark, text=body, sentences=total, stopped=stopped,
                           ratio=_ratio(stopped, total), ceiling=ceiling, min_sentences=fewest,
                           judged=False, decision="pass", rewrites=0,
                           reason=_reason(lines, "too_few", fields))
    here = _ratio(stopped, total)
    if here <= ceiling:
        return StopVerdict(mark=mark, text=body, sentences=total, stopped=stopped, ratio=here,
                           ceiling=ceiling, min_sentences=fewest, judged=True,
                           decision="pass", rewrites=0, reason=_reason(lines, "pass", fields))
    if rewrite is None:
        reason = _reason(lines, "no_rewrite", fields)
        _account(sink, "over_mark", reason)
        return StopVerdict(mark=mark, text=body, sentences=total, stopped=stopped, ratio=here,
                           ceiling=ceiling, min_sentences=fewest, judged=True,
                           decision="over_mark", rewrites=0, reason=reason)
    try:
        again = rewrite(body)
    except Exception as exc:          # 重写失败只算「改不了」，这一层不许把出口打断
        reason = _reason(lines, "rewrite_failed",
                         dict(fields, error="%s：%s" % (type(exc).__name__, exc)))
        _account(sink, "over_mark", reason)
        return StopVerdict(mark=mark, text=body, sentences=total, stopped=stopped, ratio=here,
                           ceiling=ceiling, min_sentences=fewest, judged=True,
                           decision="over_mark", rewrites=1, reason=reason)
    after = again if isinstance(again, str) else ("" if again is None else str(again))
    if not after.strip():
        reason = _reason(lines, "rewrite_failed", dict(fields, error="重写回来是空的"))
        _account(sink, "over_mark", reason)
        return StopVerdict(mark=mark, text=body, sentences=total, stopped=stopped, ratio=here,
                           ceiling=ceiling, min_sentences=fewest, judged=True,
                           decision="over_mark", rewrites=1, reason=reason)
    grown = _sentences(after, ends)
    now_total, now_stopped = len(grown), _count(grown, mark)
    moved = dict(fields, sentences=now_total, stopped=now_stopped,
                 ratio=_percent(_ratio(now_stopped, now_total)),
                 before_sentences=total, before_stopped=stopped, before_ratio=_percent(here))
    if _ratio(now_stopped, now_total) <= ceiling:
        return StopVerdict(mark=mark, text=after, sentences=now_total, stopped=now_stopped,
                           ratio=_ratio(now_stopped, now_total), ceiling=ceiling,
                           min_sentences=fewest, judged=True, decision="rewritten", rewrites=1,
                           reason=_reason(lines, "rewritten", moved))
    reason = _reason(lines, "still_over", moved)
    _account(sink, "over_mark", reason)
    # 二次仍超：**发原话**。这一关是「标点是毛病」，不是「换个说法就算数」——改回来还是超上限，
    # 就说明这次改写没解决问题；拿它换掉她原本那句话（哪怕句号轻了一点）是拿真话换标点。
    return StopVerdict(mark=mark, text=body, sentences=now_total, stopped=now_stopped,
                       ratio=_ratio(now_stopped, now_total), ceiling=ceiling,
                       min_sentences=fewest, judged=True, decision="over_mark", rewrites=1,
                       reason=reason)


def prompt(kind: str, text: str, table: Optional[Mapping[str, Any]] = None, **fields: Any) -> str:
    """把数据里那一句重写指令填好交出去：kind 认 "length"（超目标）与 "stops"（句号太密）。

    模板与占位符全在数据里（prompt 节）；缺模板、占位符对不上都抛 StyleBroken——**不许在代码里
    再写一句「说短点」**，提示词改说法是改数据。原文以 original 传进模板，调用方另外给的字段
    （lo / hi / chars / scene / tier / mark / stopped / sentences / ratio）一并填进去。
    """
    book = table if isinstance(table, Mapping) else load("style_target")
    part = book.get(PROMPT_KEY)
    if not isinstance(part, Mapping) or not part:
        raise StyleBroken("的数据里没有一节可用的 %r" % PROMPT_KEY)
    raw = part.get(kind)
    if not isinstance(raw, str) or not raw.strip():
        raise StyleBroken("的 %s 里缺 %r 这句重写指令" % (PROMPT_KEY, kind))
    return _fill(raw, "%s.%s" % (PROMPT_KEY, kind), **dict(fields, original=text))


def accept(original: Any, said: Any, table: Optional[Mapping[str, Any]] = None,
           must_shorten: bool = False) -> Tuple[bool, str]:
    """新稿先过这一关才交给下一层：非空、与原文不同、原文里的数字一个都不许丢。

    must_shorten=True 时再加一条：新稿必须比原文短（「改短一点」改长了，等于没改）。
    调用方给这个开关，是因为「长度超目标」和「句号太密」要的不是同一件事：前者必须变短，
    后者只要求换一种说法。

    返回 (过没过, 一句人话原因)。**这里不判长度也不判句号率**：那是 apply 与 stops 拿数据自己
    重算的事；这一关只管「它是不是她那句话」，两件事混在一起，改不动的原因就说不清了。
    """
    book = table if isinstance(table, Mapping) else load("style_target")
    part = book.get(ACCEPT_KEY)
    if not isinstance(part, Mapping) or not part:
        raise StyleBroken("的数据里没有一节可用的 %r" % ACCEPT_KEY)
    lines = _section(part, "reasons", ACCEPT_KEY)
    body = original if isinstance(original, str) else ""
    new = said if isinstance(said, str) else ""
    if not new.strip():
        return False, _reason(lines, "empty", {})
    if new.strip() == body.strip():
        return False, _reason(lines, "same", {})
    missing = _missing_digits(body, new, _text_in(part, "keep_digits"))
    if missing:
        return False, _reason(lines, "digits", {"missing": "、".join(missing)})
    if must_shorten and len(new) >= len(body):
        return False, _reason(lines, "longer", {"before": len(body), "after": len(new)})
    return True, ""


def path_of(path: Optional[Any] = None) -> Path:
    """定一下这次记到哪儿：参数 > 环境变量 MOUTH_TRACE_DIR > 包内 trace/（与 drops 同一口径）。

    参数给的是目录就当目录（补上 LEDGER_NAME）；直接点到 .jsonl 就照用——测试多半这么给。
    """
    if path is not None and str(path).strip():
        target = Path(str(path))
    else:
        from_env = os.environ.get(ENV_VAR, "").strip()
        target = Path(from_env) if from_env else TRACE_DIR
    if target.suffix == LEDGER_SUFFIX:
        return target
    return target / LEDGER_NAME


def record(path: Optional[Any], decision: str, reason: str, now: Any,
           target: Any = None) -> Dict[str, Any]:
    """往长度账本追一行：ts / decision / reason（给了 target 就再带一列目标区间）。时间由调用方给。

    target 是这一档的目标区间（一对整数，落成 "lo-hi"；也可直接给一个现成的词），写进来是为了
    分辨改前改后：同一句在旧区间里 pass、新区间里 over_target，账本上一眼看得见是拿哪一套判的。
    不给就不带这一列（旧行格式因此一个字都没变）。

    写不进去就照实抛——记不上账是失败，不是「这一句没超」。记的是判词与那句人话，
    整句原文不进账本（人话里已经带着字数与区间，复盘够用）。
    """
    row = {"ts": _stamp(now), "decision": str(decision or "").strip(),
           "reason": str(reason or "").strip()}
    if target is not None:
        row["target"] = _target_word(target)
    if not row["decision"]:
        raise ValueError("长度账本必须带 decision：不知道这一句最后怎么处理的，这一行记了也没用")
    if not row["reason"]:
        raise ValueError("长度账本必须带一句人话原因：只记一个 decision，回头没法复盘")
    target = path_of(path)
    target.parent.mkdir(parents=True, exist_ok=True)     # 目录不可写就在这里抛
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _target_word(target: Any) -> str:
    """把目标区间落成一个 "lo-hi" 词：一对整数就拼，现成的非空字符串就照用，别的抛。

    这一列只为分辨「改前改后」，所以它必须是一段能读的字，不是一个看不懂的结构：
    回头翻账本的人要能一眼看出这一句是拿哪一套区间判的。
    """
    if isinstance(target, str) and target.strip():
        return target.strip()
    if isinstance(target, (tuple, list)) and len(target) == 2:
        lo, hi = target
        if (isinstance(lo, int) and not isinstance(lo, bool)
                and isinstance(hi, int) and not isinstance(hi, bool)):
            return "%d-%d" % (lo, hi)
    raise ValueError("长度账本的 target 要给一对整数（这一档的 lo 与 hi），或一个现成的非空字符串；"
                     "读到的 %r 两样都不是" % (target,))


def _over_target(body: str, before: int, lo: int, hi: int, fields: Dict[str, Any],
                 lines: Mapping[str, Any], rewrite: Optional[Rewrite],
                 sink: Optional[Sink], scene: str) -> StyleVerdict:
    """超目标但没超硬上限：不截断，改一次；改不了就明说改不了、放行并记账。"""
    if rewrite is None:
        reason = _reason(lines, "no_rewrite", fields)
        _account(sink, "over_target", reason)
        return StyleVerdict(text=body, target_lo=lo, target_hi=hi, chars=before,
                            decision="over_target", rewrites=0, over_hard_cap=False,
                            reason=reason, scene=scene)
    try:
        again = rewrite(body)
    except Exception as exc:          # 重写失败只算「改不了」，这一层不许把出口打断
        reason = _reason(lines, "rewrite_failed",
                         dict(fields, error="%s：%s" % (type(exc).__name__, exc)))
        _account(sink, "over_target", reason)
        return StyleVerdict(text=body, target_lo=lo, target_hi=hi, chars=before,
                            decision="over_target", rewrites=1, over_hard_cap=False,
                            reason=reason, scene=scene)
    after = again if isinstance(again, str) else ("" if again is None else str(again))
    if not after.strip():
        reason = _reason(lines, "rewrite_failed", dict(fields, error="重写回来是空的"))
        _account(sink, "over_target", reason)
        return StyleVerdict(text=body, target_lo=lo, target_hi=hi, chars=before,
                            decision="over_target", rewrites=1, over_hard_cap=False,
                            reason=reason, scene=scene)
    after_chars = len(after)
    moved = dict(fields, chars=after_chars, after=after_chars)
    if lo <= after_chars <= hi:
        reason = _reason(lines, "rewritten", moved)
        return StyleVerdict(text=after, target_lo=lo, target_hi=hi, chars=after_chars,
                            decision="rewritten", rewrites=1, over_hard_cap=False,
                            reason=reason, scene=scene)
    reason = _reason(lines, "still_over", moved)
    _account(sink, "over_target", reason)
    return StyleVerdict(text=after, target_lo=lo, target_hi=hi, chars=after_chars,
                        decision="over_target", rewrites=1, over_hard_cap=False,
                        reason=reason, scene=scene)


def _account(sink: Optional[Sink], decision: str, reason: str) -> None:
    """把这一笔交给注入的 sink；sink 为 None 就什么也不发生（本层自己不写盘）。"""
    if sink is not None:
        sink(decision, reason)


def _scene(scene: Any, book: Mapping[str, Any]) -> Tuple[str, Mapping[str, Any]]:
    """场景名怎么落到数据上：给了就认它，没给取 default_scene，认不得就抛并列出数据里现有的。

    **不回落**：场景落到默认值一次，账本上就再也分不清这一句是拿哪一套区间判的。
    """
    scenes = book.get(SCENES_KEY) if isinstance(book, Mapping) else None
    if not isinstance(scenes, Mapping) or not scenes:
        raise StyleBroken("的数据里没有一节可用的 %r" % SCENES_KEY)
    wanted = str(scene).strip() if isinstance(scene, str) else ""
    if not wanted:
        fallback = book.get(DEFAULT_SCENE_KEY)
        if not isinstance(fallback, str) or not fallback.strip():
            raise StyleBroken("缺 %r（调用方没给场景时按它算），或它不是一句非空字符串"
                              % DEFAULT_SCENE_KEY)
        wanted = fallback.strip()
    if wanted not in scenes:
        raise StyleBroken("不认识场景 %r（数据里只认 %s）"
                          % (wanted, "、".join(repr(key) for key in scenes)))
    entry = scenes[wanted]
    if not isinstance(entry, Mapping):
        raise StyleBroken("的场景 %r 不是一份键值表，是 %s" % (wanted, type(entry).__name__))
    return wanted, entry


def _ranges(tier: Any, book: Mapping[str, Any],
            scene: Any) -> Tuple[str, str, int, int, str, str]:
    """按场景与档位取 (场景名, 场景标签, 下限, 上限, 档键, 档标签)：缺档缺键都抛并指名。"""
    name, entry = _scene(scene, book)
    scene_label = entry.get("label")
    if not isinstance(scene_label, str) or not scene_label.strip():
        raise StyleBroken("的场景 %r 缺 %r，或它不是一句非空字符串" % (name, "label"))
    ranges = entry.get(RANGES_KEY)
    if not isinstance(ranges, Mapping) or not ranges:
        raise StyleBroken("的场景 %r 里没有一节可用的 %r" % (name, RANGES_KEY))
    key = _tier_key(tier, ranges)
    row = ranges[key]
    if not isinstance(row, Mapping):
        raise StyleBroken("的场景 %r 第 %r 档不是一份键值表，是 %s"
                          % (name, key, type(row).__name__))
    lo = _int_in(row, "lo", key)
    hi = _int_in(row, "hi", key)
    label = row.get("label")
    if not isinstance(label, str) or not label.strip():
        raise StyleBroken("的场景 %r 第 %r 档缺 %r，或它不是一句非空字符串"
                          % (name, key, "label"))
    if lo > hi:
        raise StyleBroken("的场景 %r 第 %r 档下限比上限大（lo=%d > hi=%d）" % (name, key, lo, hi))
    return name, scene_label.strip(), lo, hi, key, label


def _tier_key(tier: Any, ranges: Mapping[str, Any]) -> str:
    """档位怎么落到数据的键上：整数或字符串都认，认不得就抛并列出数据里现有的档。"""
    if isinstance(tier, bool) or not isinstance(tier, (int, str)):
        wanted = ""
    else:
        wanted = str(tier).strip()
    if wanted and wanted in ranges:
        return wanted
    raise StyleBroken("不认识语气档 %r（数据里只认 %s）"
                      % (tier, "、".join(repr(key) for key in ranges)))


def _int_in(entry: Mapping[str, Any], name: str, key: str) -> int:
    """数据里的整数项：读到别的一律抛，不四舍五入也不补默认值。"""
    value = entry.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StyleBroken("的第 %r 档 %r 不是整数（读到的 %r）" % (key, name, value))
    return value


def _cap(hard_cap: Any) -> int:
    """核给的硬上限：必须是非负整数；读到别的就抛——这条数是核的事，本层不许自己猜一个。"""
    if isinstance(hard_cap, bool) or not isinstance(hard_cap, int) or hard_cap < 0:
        raise StyleBroken("要的硬上限必须是非负整数（读到的 %r）" % (hard_cap,))
    return hard_cap


def _lines(book: Mapping[str, Any]) -> Mapping[str, Any]:
    """取人话模板那一节；缺了抛，别让一句半成品判词发出去。"""
    part = book.get(REASONS_KEY) if isinstance(book, Mapping) else None
    if not isinstance(part, Mapping) or not part:
        raise StyleBroken("的数据里没有一节可用的 %r" % REASONS_KEY)
    return part


def _stopped(book: Mapping[str, Any]) -> Mapping[str, Any]:
    """取句末标点那一节；缺了抛——没有判据就不许判，也不许当成「没问题」。"""
    part = book.get(STOPPED_KEY) if isinstance(book, Mapping) else None
    if not isinstance(part, Mapping) or not part:
        raise StyleBroken("的数据里没有一节可用的 %r" % STOPPED_KEY)
    return part


def _section(data: Mapping[str, Any], key: str, spot: str) -> Mapping[str, Any]:
    """数据里某一节键值表：缺了抛，并指名是哪一节。"""
    part = data.get(key)
    if not isinstance(part, Mapping) or not part:
        raise StyleBroken("的 %s 里没有一节可用的 %r" % (spot, key))
    return part


def _text_in(data: Mapping[str, Any], name: str) -> str:
    """数据里的非空字符串项：缺了、空了、不是字符串都抛。"""
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise StyleBroken("缺 %r，或它不是一句非空字符串（读到的 %r）" % (name, value))
    return value


def _number_in(data: Mapping[str, Any], name: str) -> float:
    """数据里的小数项（比例上限）：布尔与非数一律抛，不夹到 0..1 里——夹一次就是把判据改了。"""
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StyleBroken("缺 %r，或它不是数（读到的 %r）" % (name, value))
    return float(value)


def _whole_in(data: Mapping[str, Any], name: str) -> int:
    """数据里的非负整数项（最少句数）：读到别的一律抛，不补默认值。"""
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StyleBroken("缺 %r，或它不是非负整数（读到的 %r）" % (name, value))
    return value


def _sentences(text: str, ends: str) -> Sequence[str]:
    """按数据里的句末符号断句：收尾符号跟着它那一句走；没有收尾符号的尾巴也算一句。

    尾巴也算一句是有意的：一句话说到一半收住（「就这样吧」），它照样占一句的分母——
    不数它，句号率就被那些不収句号的句子稀释了。
    """
    rows: list = []
    buf: list = []
    for char in text or "":
        buf.append(char)
        if char in ends:
            piece = "".join(buf).strip()
            if piece:
                rows.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        rows.append(tail)
    return rows


def _count(rows: Sequence[str], mark: str) -> int:
    """句末正好是 mark 的句数（标点算在句子里，所以只看最后一个字符）。"""
    return len([one for one in rows if one.endswith(mark)])


def _ratio(stopped: int, total: int) -> float:
    """比例：一句都没有就是 0.0（不判那一关，但账本上得有个数），保留三位小数。"""
    return round(float(stopped) / float(total), 3) if total else 0.0


def _percent(ratio: float) -> float:
    """比例落成人话里的百分数（30.0 这样一位小数）：模板里自带那个 % 号，这里只给数。"""
    return round(float(ratio) * 100, 1)


def _missing_digits(original: str, new: str, regex: str) -> list:
    """原文里有、新稿里一个都不剩的数字串（按数据里那条正则取，半角全角都算）。"""
    try:
        pattern = re.compile(regex)
    except re.error as exc:
        raise StyleBroken("的 %s.keep_digits 不是一条能用的正则：%s" % (ACCEPT_KEY, exc)) from None
    before = pattern.findall(original)
    after = set(pattern.findall(new))
    gone = []
    for one in before:
        if one not in after and one not in gone:
            gone.append(one)
    return gone


def _fill(template: str, spot: str, **fields: Any) -> str:
    """把一句人话/指令填出来；占位符对不上就抛并指名，别把半成品发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise StyleBroken("的 %s 这句填不出来：%s" % (spot, exc)) from None


def _reason(lines: Mapping[str, Any], name: str, fields: Dict[str, Any]) -> str:
    """把这一种结局的模板填成人话；模板缺了、占位符对不上都抛。"""
    raw = lines.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise StyleBroken("的 reasons 里缺 %r 这句人话" % name)
    return _fill(raw, "reasons.%s" % name, **fields)


def _stamp(now: Any) -> str:
    """调用方给的时刻落成一个字符串：有 isoformat 就用它，没有就当字符串；空的抛。"""
    text = now.isoformat() if hasattr(now, "isoformat") else str(now if now is not None else "")
    if not text.strip():
        raise ValueError("长度账本必须带时刻：没有 ts 就没法说「今天」超了几次")
    return text
