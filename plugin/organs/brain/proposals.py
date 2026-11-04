"""每晚的改基线提案：0–2 条，各附三条依据记忆。

target 只有 manner.stance（红线永久冻结、六轴归心）；value 是待人口吻的一句话。
信号表是默认值不是验收线；真正的语义提炼可以换成宿主侧的 composer，接口保持不变。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .model import SOURCE_LIVED, day_key, limits, now_iso


@dataclass(frozen=True)
class Signal:
    id: str
    label: str
    keywords: Tuple[str, ...]
    stance: str
    reason: str


DEFAULT_SIGNALS: Tuple[Signal, ...] = (
    Signal("s-cold", "把「不熟」演成「不理人」",
           ("不理", "冷淡", "没理", "生人", "陌生"),
           "不熟就先接话，别把冷场演成不理人",
           "最近有几段记忆都指向「不熟/不理」的方向，建议把待人口吻往「先接话」调一点"),
    Signal("s-straight", "说话太直伤到人",
           ("太直", "好直", "说话直", "噎", "太冲"),
           "想到什么先停一句，把话说软一点再出口",
           "最近有几段记忆都指向「说话太直/冲」的方向，建议把待人口吻往「先软一句」调一点"),
    Signal("s-clingy", "黏人过了界",
           ("太黏", "黏人", "别黏", "想黏"),
           "黏一点可以，但先把对方要的空间留出来",
           "最近有几段记忆都指向「黏人」的方向，建议把待人口吻往「留空间」调一点"),
)


def _dedup(items: Sequence[str]) -> List[str]:
    seen: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


def evidence_for(entries: Sequence[Dict[str, Any]], signal: Signal) -> List[str]:
    found = []
    for entry in entries:
        text = entry.get("text") or ""
        # 虚构（日程模拟）只作谈资、不作证词：改基线的依据只能是亲历记忆
        if entry.get("source_type", SOURCE_LIVED) != SOURCE_LIVED:
            continue
        if any(keyword in text for keyword in signal.keywords):
            found.append(entry.get("id", ""))
    return _dedup(found)


def compose_proposals(entries: Sequence[Dict[str, Any]],
                      now_ts: Optional[str] = None,
                      signals: Sequence[Signal] = DEFAULT_SIGNALS,
                      max_proposals: Optional[int] = None) -> List[Dict[str, Any]]:
    """从记忆条目里找方向一致的证据；够三条才提，每晚最多 2 条。"""
    now_ts = now_ts or now_iso()
    max_proposals = max_proposals or limits.proposal_nightly_max
    candidates: List[Tuple[Signal, List[str]]] = []
    for signal in signals:
        evidence = evidence_for(entries, signal)
        if len(evidence) >= limits.proposal_evidence:
            candidates.append((signal, evidence))
    candidates.sort(key=lambda pair: len(pair[1]), reverse=True)

    proposals: List[Dict[str, Any]] = []
    for index, (signal, evidence) in enumerate(candidates[:max_proposals], start=1):
        proposals.append({
            "id": "bp-%s-%d" % (day_key(now_ts), index),
            "target": "manner.stance",
            "value": signal.stance,
            "evidence": evidence[:limits.proposal_evidence],
            "reason": signal.reason + "（依据记忆 %s）" % "/".join(evidence[:limits.proposal_evidence]),
        })
    return proposals
