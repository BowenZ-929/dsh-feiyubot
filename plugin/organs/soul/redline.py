"""M1：把红线钉成断言脚本——校验对象是 render() 产出的红线段，不是文件行。

三条硬要求（吸收参考册 M1，按本仓的数据形状翻译，逐条都能机器判）：
1. 生效条目 ≤40 条。参考实现一条禁令占一行，所以它写「≤40 行」；本仓的红线渲染成「；」拼接的
   **一行**，物理行数恒为 1——那条照抄过来就恒真，是翻译错误，这里只数条目，不数物理行。
2. 每条禁令在红线段里恰好出现 1 次：同一条写两遍算重复，一条是另一条的子串也算重复，都拒。
3. 出现软话词即拒（默认「尽量／可以尝试／最好不要」，可配）：软话把禁令降级成建议。

落点：载入（store.load / SoulStore._reload）、每次写盘之前（store._commit、store.migrate、
store.rollback）、以及醒时自检（checks.collect）。写盘前那一次是硬要求——不过就不许落盘，
拒因跟着返回值回给调用方，并显示在面板上。

同一份校验也吃 take_pin() 的输出与压缩产物（M4）：pin 与压缩产物是红线的另一种呈现，
不校验它们，压缩就成了绕过 M1 的后门。
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from . import model
from .render import redlines

REDLINE_HEAD = "【红线】"
MAX_ITEMS = 40
SOFT_WORDS = ("尽量", "可以尝试", "最好不要")
# 三个拒因：都是 error（不过就不许落盘，不是提示）
# 条数那条的拒因码沿用旧名（面板、文档、壳都认这个字符串），判的是条目数，不是物理行。
TOO_MANY_ITEMS = "REDLINE_TOO_MANY_LINES"
DUPLICATED = "REDLINE_DUPLICATED"
SOFT_WORD = "REDLINE_SOFT_WORD"


def active_rules(bounds: model.Bounds) -> List[str]:
    """红线段里会出现的那几条：撤下的线（active=false）不进注入，也不进断言。"""
    return [item.rule for item in bounds.items if item.active]


def segment(bounds: model.Bounds) -> str:
    """render() 里的那一行【红线】：与注入同一处取词，断言才不会和注入各说各话。"""
    return REDLINE_HEAD + redlines(bounds)



def check_text(text: str, rules: Sequence[str] = (), soft_words: Sequence[str] = SOFT_WORDS,
               max_items: int = MAX_ITEMS, item_count: Optional[int] = None,
               where: str = "红线段") -> List[model.Issue]:
    """对一段红线文本跑三条断言；rules 给了就逐条数出现次数（pin 与压缩产物同样走这里）。

    item_count 是条目数：本仓渲染成一行，「≤40 行」在这里落成「≤40 条」，由调用方给。
    物理行数一律不数——本仓的物理行恒为 1，那条断言只会恒真。
    """
    issues: List[model.Issue] = []
    if item_count is not None and item_count > max_items:
        issues.append(model.Issue(
            TOO_MANY_ITEMS, model.ERROR,
            "%s有 %d 条，超过 %d 条硬顶：红线一多就没人读得完，只会被整段跳过去"
            % (where, item_count, max_items)))
    seen: List[str] = []
    for rule in rules:
        if rule in seen:
            continue
        seen.append(rule)
        count = text.count(rule)
        if count != 1:
            issues.append(model.Issue(
                DUPLICATED, model.ERROR,
                "%s里「%s」出现 %d 次（要求恰好 1 次）：重复即拒，"
                "一次禁令只许写一行" % (where, rule, count)))
    for word in soft_words:
        if word in text:
            issues.append(model.Issue(
                SOFT_WORD, model.ERROR,
                "%s里出现软话词「%s」：禁令不许写成建议，删掉软话再存" % (where, word)))
    return issues


def check_bounds(bounds: model.Bounds, soft_words: Sequence[str] = SOFT_WORDS,
                 max_items: int = MAX_ITEMS) -> List[model.Issue]:
    """写盘前/载入时的入口：三条断言都对着 render() 的红线段跑。"""
    rules = active_rules(bounds)
    return check_text(segment(bounds), rules=rules, soft_words=soft_words,
                      max_items=max_items, item_count=len(rules))
