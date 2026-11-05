"""嘴的目标长度：这一轮「该多短」，和「最多多长」是两件事。

核只有一条硬上限（五档预算里那条，超了就截断）。上限管的是「不许超过」，管不了「这一轮别铺开」。
这里补一层目标区间：按语气档 1..4 给一段该有的长度（偏冷短、偏热长）。区间、标签、每一句人话全在
organs/mouth/data/style_target.json（经 warehouse.load 只读借入），代码里一个数都不写——改区间是
改数据，不是改代码；数据缺键当场抛 StyleBroken，不补默认值。

**两者不互相覆盖，写在这里就是钉死它**：核的五档预算是**上限**（超了由核截断），嘴的目标区间是
**目标**（超了由嘴重写一次，不截断也不发脾气）。所以：

- 嘴碰到超硬上限的那一句**不截断**，只把 over_hard_cap 举起来、原样交核——截断是核的事；
- 核也不挑目标，这一轮该短该长是嘴的事，两边各管一件事，谁也不许拿自己的数盖住另一个。

三条规则：
- 落在目标区间里 → decision="pass"，原样发；
- 超目标但没超硬上限 → **不截断**，用注入的 rewrite 改一次。没给 rewrite 就明说「改不了」，
  放行并记账——「改不了」和「合格」是两句话，不许混着说；改完仍在区间外也一样放行并记账；
- 超硬上限 → over_hard_cap=true，text 原样，交核。

记账 = 走注入的 sink（decision, reason），调用方决定写哪儿；本文件额外提供与 drops 同款的 JSONL
写入函数 record（style.jsonl），路径口径一致：参数 > 环境变量 MOUTH_TRACE_DIR > 包内 trace/。
apply 自己不写盘、也不取时钟：要落账就由调用方的 sink 调 record，ts 由那个调用方给。

纯：不 import time / datetime / random，不碰宿主体；数据只经 warehouse 只读借阅。字数就是
len(text)（标点也算一个字）——这里要的是「这一句发出去多长」，剔除可见字符是 split 的
visible_chars 的活，不是这一层的活。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .warehouse import load

__all__ = ["StyleBroken", "StyleVerdict", "apply", "target_range", "path_of", "record"]

TRACE_DIR = Path(__file__).resolve().parent / "trace"   # 包内痕迹目录（家里的仓库做出来前的临时落脚点）
ENV_VAR = "MOUTH_TRACE_DIR"                             # 环境变量：换一个痕迹根目录，不动代码
LEDGER_NAME = "style.jsonl"                             # 账本文件名：一行一条长度账
LEDGER_SUFFIX = ".jsonl"                                # 参数直接点到一个账本文件时，认这个后缀

RANGES_KEY = "ranges"                                   # 数据里目标区间那一节
REASONS_KEY = "reasons"                                 # 数据里人话模板那一节

Rewrite = Callable[[str], str]                          # 改一次：收一句、回一句
Sink = Callable[[str, str], None]                       # 记账：收（decision，人话原因）


class StyleBroken(ValueError):
    """长度这边不合格：不认识的语气档、数据缺键缺模板、硬上限不是数，都在这里当场响并指名。

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
      reason           一句人话：按哪个档、区间多少、这次算不算合格、改不了的原因是什么
    """

    text: str
    target_lo: int
    target_hi: int
    chars: int
    decision: str
    rewrites: int
    over_hard_cap: bool
    reason: str


def target_range(tier: Any, table: Optional[Mapping[str, Any]] = None) -> Tuple[int, int]:
    """取这一档的目标区间 (lo, hi)；tier 认 1..4 的整数，也认数据里那种字符串键。

    不认识的档位抛 StyleBroken 并指名（连同数据里现有的档一起列出来）。
    """
    book = table if isinstance(table, Mapping) else load("style_target")
    _key, lo, hi, _label = _range(tier, book)
    return lo, hi


def apply(text: str, tier: Any, hard_cap: Any,
          table: Optional[Mapping[str, Any]] = None,
          rewrite: Optional[Rewrite] = None,
          sink: Optional[Sink] = None) -> StyleVerdict:
    """按这一档的目标区间看这一句：在区间里就过，超目标就改一次，超硬上限就交核（不截断）。

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
    key, lo, hi, label = _range(tier, book)
    lines = _lines(book)
    body = text if isinstance(text, str) else ""
    cap = _cap(hard_cap)
    before = len(body)
    fields: Dict[str, Any] = {"tier": key, "label": label, "lo": lo, "hi": hi, "cap": cap,
                              "chars": before, "before": before, "after": before}
    if before > cap:
        reason = _reason(lines, "over_hard_cap", fields)
        _account(sink, "over_target", reason)
        return StyleVerdict(text=body, target_lo=lo, target_hi=hi, chars=before,
                            decision="over_target", rewrites=0, over_hard_cap=True, reason=reason)
    if before > hi:
        return _over_target(body, before, lo, hi, fields, lines, rewrite, sink)
    name = "short" if before < lo else "pass"
    reason = _reason(lines, name, fields)
    return StyleVerdict(text=body, target_lo=lo, target_hi=hi, chars=before,
                        decision="pass", rewrites=0, over_hard_cap=False, reason=reason)


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


def record(path: Optional[Any], decision: str, reason: str, now: Any) -> Dict[str, Any]:
    """往长度账本追一行：ts / decision / reason。时间由调用方给，本函数不取时钟。

    写不进去就照实抛——记不上账是失败，不是「这一句没超」。记的是判词与那句人话，
    整句原文不进账本（人话里已经带着字数与区间，复盘够用）。
    """
    row = {"ts": _stamp(now), "decision": str(decision or "").strip(),
           "reason": str(reason or "").strip()}
    if not row["decision"]:
        raise ValueError("长度账本必须带 decision：不知道这一句最后怎么处理的，这一行记了也没用")
    if not row["reason"]:
        raise ValueError("长度账本必须带一句人话原因：只记一个 decision，回头没法复盘")
    target = path_of(path)
    target.parent.mkdir(parents=True, exist_ok=True)     # 目录不可写就在这里抛
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _over_target(body: str, before: int, lo: int, hi: int, fields: Dict[str, Any],
                 lines: Mapping[str, Any], rewrite: Optional[Rewrite],
                 sink: Optional[Sink]) -> StyleVerdict:
    """超目标但没超硬上限：不截断，改一次；改不了就明说改不了、放行并记账。"""
    if rewrite is None:
        reason = _reason(lines, "no_rewrite", fields)
        _account(sink, "over_target", reason)
        return StyleVerdict(text=body, target_lo=lo, target_hi=hi, chars=before,
                            decision="over_target", rewrites=0, over_hard_cap=False,
                            reason=reason)
    try:
        again = rewrite(body)
    except Exception as exc:          # 重写失败只算「改不了」，这一层不许把出口打断
        reason = _reason(lines, "rewrite_failed",
                         dict(fields, error="%s：%s" % (type(exc).__name__, exc)))
        _account(sink, "over_target", reason)
        return StyleVerdict(text=body, target_lo=lo, target_hi=hi, chars=before,
                            decision="over_target", rewrites=1, over_hard_cap=False,
                            reason=reason)
    after = again if isinstance(again, str) else ("" if again is None else str(again))
    if not after.strip():
        reason = _reason(lines, "rewrite_failed", dict(fields, error="重写回来是空的"))
        _account(sink, "over_target", reason)
        return StyleVerdict(text=body, target_lo=lo, target_hi=hi, chars=before,
                            decision="over_target", rewrites=1, over_hard_cap=False,
                            reason=reason)
    after_chars = len(after)
    moved = dict(fields, chars=after_chars, after=after_chars)
    if lo <= after_chars <= hi:
        reason = _reason(lines, "rewritten", moved)
        return StyleVerdict(text=after, target_lo=lo, target_hi=hi, chars=after_chars,
                            decision="rewritten", rewrites=1, over_hard_cap=False,
                            reason=reason)
    reason = _reason(lines, "still_over", moved)
    _account(sink, "over_target", reason)
    return StyleVerdict(text=after, target_lo=lo, target_hi=hi, chars=after_chars,
                        decision="over_target", rewrites=1, over_hard_cap=False,
                        reason=reason)


def _account(sink: Optional[Sink], decision: str, reason: str) -> None:
    """把这一笔交给注入的 sink；sink 为 None 就什么也不发生（本层自己不写盘）。"""
    if sink is not None:
        sink(decision, reason)


def _range(tier: Any, book: Mapping[str, Any]) -> Tuple[str, int, int, str]:
    """按档位取 (档键, 下限, 上限, 标签)：缺档、缺键、上下限颠倒都抛并指名。"""
    ranges = book.get(RANGES_KEY) if isinstance(book, Mapping) else None
    if not isinstance(ranges, Mapping) or not ranges:
        raise StyleBroken("的数据里没有一节可用的 %r" % RANGES_KEY)
    key = _tier_key(tier, ranges)
    entry = ranges[key]
    if not isinstance(entry, Mapping):
        raise StyleBroken("的第 %r 档不是一份键值表，是 %s" % (key, type(entry).__name__))
    lo = _int_in(entry, "lo", key)
    hi = _int_in(entry, "hi", key)
    label = entry.get("label")
    if not isinstance(label, str) or not label.strip():
        raise StyleBroken("的第 %r 档缺 %r，或它不是一句非空字符串" % (key, "label"))
    if lo > hi:
        raise StyleBroken("的第 %r 档下限比上限大（lo=%d > hi=%d）" % (key, lo, hi))
    return key, lo, hi, label


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
    """数据里的区间端点必须是整数：读到别的一律抛，不四舍五入也不补默认值。"""
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


def _reason(lines: Mapping[str, Any], name: str, fields: Dict[str, Any]) -> str:
    """把这一种结局的模板填成人话；模板缺了、占位符对不上都抛。"""
    raw = lines.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise StyleBroken("的 %s 里缺 %r 这句人话" % (REASONS_KEY, name))
    try:
        return raw.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise StyleBroken("的 %s.%s 这句填不出来：%s" % (REASONS_KEY, name, exc)) from None


def _stamp(now: Any) -> str:
    """调用方给的时刻落成一个字符串：有 isoformat 就用它，没有就当字符串；空的抛。"""
    text = now.isoformat() if hasattr(now, "isoformat") else str(now if now is not None else "")
    if not text.strip():
        raise ValueError("长度账本必须带时刻：没有 ts 就没法说「今天」超了几次")
    return text
