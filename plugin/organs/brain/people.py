"""人名录：熟稔读数现算不写回，只存可计数的事实（哪一天互动了几次）。

熟稔 = 每日互动次数按 λ^(age/21天) 衰减求和；这是读数，不是情绪状态，
也不许借 mood 的半衰期（architecture.md §2.3.1）。
"""
from __future__ import annotations

from datetime import date
from typing import Dict

from .model import day_key, limits


def familiarity(interactions: Dict[str, int], now_ts: str,
                lam: float = limits.familiarity_lambda,
                unit_days: int = limits.familiarity_days) -> float:
    """现算不写回：interactions 是 {"YYYY-MM-DD": 次数}，读的时候才算读数。"""
    if not interactions:
        return 0.0
    try:
        today = date.fromisoformat(day_key(now_ts))
    except ValueError:
        today = None
    total = 0.0
    for day, count in interactions.items():
        if not isinstance(count, (int, float)) or count <= 0:
            continue
        try:
            age = max(0, (today - date.fromisoformat(day)).days) if today else 0
        except ValueError:
            continue
        total += count * (lam ** (age / unit_days))
    return round(total, 4)
