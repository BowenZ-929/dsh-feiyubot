"""嘴的节奏：只回答「隔多久」，不管「切在哪」。

首条立即（gap_before = 0.0）；后续每一条的间隔 = 夹取(log(可见字数 + 1, 底数), 最小, 最大)，
再按 sha256(seed + 序号) 取到的分数抖一点，抖完再夹一次，保证落在 policy 的夹取范围里。
底数、上下限、抖动幅度、保留位数全在 organs/mouth/data/policy.json，代码里不写死数值。

抖动用 sha256 而不是随机数：同一个 seed 两次调用必须拿到完全一样的间隔——面板预览算出来的
就是实发时用的那一个，对不上就等于预览在骗人。seed 用这条回复自己的 id。

「可见字数」= 去掉空白与标点之后的字数（标点表在 policy 的 visible.strip 里）。停顿本身不发声，
算进字数就会把「……」这种沉默当成话多；数的也只是这一条自己的字数，不含前面的。

**15ms 是通道往返，不是节奏。** 那个 15ms 是面板请求往返的实测（plugin/api/README.md:130
记的是传输），说的是把这一泡送出去要多久；本文件算的是「说一句、停一下、再说一句」的说话
节奏，量级在秒。两者既不互相替代，也不许拿 15ms 去卡节奏或反推节奏。

纯函数：入参只有「几段文本 + 只读的政策 dict + 一个 seed」，不碰时钟（不 import time/datetime）、
不碰随机数（不 import random）、不碰宿主 IO。切分是 split.py 的事，两个文件互不 import。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from typing import List, Mapping, Sequence

from .model import Batch, Policy, Seg, need, section

__all__ = ["fill", "gaps", "visible_chars"]


def visible_chars(text: str, policy: Policy) -> int:
    """可见字数：去掉空白与标点之后的字数（标点表取自 policy 的 visible.strip）。"""
    strip = set(str(need(section(policy, "visible"), "strip")))
    return sum(1 for char in (text or "") if not char.isspace() and char not in strip)


def _fraction(seed: str, index: int) -> float:
    """sha256(seed + 序号) 取 [0, 1) 的分数：确定的抖动，不是随机数。"""
    digest = hashlib.sha256(("%s:%d" % (seed, index)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def gaps(texts: Sequence[str], policy: Policy, seed: str) -> List[float]:
    """每一条之前的间隔（秒）：首项 0.0，其余都落在 policy 的夹取范围里。"""
    rules = section(policy, "pace")
    first = float(need(rules, "first_gap"))
    base = float(need(rules, "log_base"))
    low = float(need(rules, "min_gap"))
    high = float(need(rules, "max_gap"))
    span = float(need(rules, "jitter_span"))
    digits = int(need(rules, "round_digits"))
    out: List[float] = []
    for index, text in enumerate(texts):
        if index == 0:
            out.append(float(round(first, digits)))
            continue
        raw = math.log(visible_chars(text, policy) + 1, base)
        value = min(max(raw, low), high)
        value = value * (1.0 - span + 2.0 * span * _fraction(seed, index))   # 对称抖动，再夹一次
        out.append(round(min(max(value, low), high), digits))
    return out


def fill(batch: Batch, policy: Policy, seed: str) -> Batch:
    """给一批填上 gap_before / send_at / summary；text 一个字符都不改。"""
    digits = int(need(section(policy, "pace"), "round_digits"))
    values = gaps([part.text for part in batch.parts], policy, seed)
    parts: List[Seg] = []
    clock = 0.0
    for part, gap in zip(batch.parts, values):
        clock = round(clock + gap, digits)
        parts.append(replace(part, gap_before=gap, send_at=clock))
    return Batch(parts=parts, summary=_summary(values, policy))


def _summary(values: Sequence[float], policy: Policy) -> str:
    """逐条摘要：哪一泡、什么时候发（「1/3 立即 · 2/3 +1.4s · 3/3 +1.1s」）。"""
    fmt = section(policy, "summary")
    item = str(need(fmt, "item"))
    immediate = str(need(fmt, "immediate"))
    gap_format = str(need(fmt, "gap"))
    separator = str(need(fmt, "sep"))
    total = len(values)
    rows = []
    for index, gap in enumerate(values, 1):
        when = immediate if gap == 0.0 else gap_format.format(gap=gap)
        rows.append(item.format(i=index, n=total, when=when))
    return separator.join(rows)
