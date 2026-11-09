"""每人每天上限：**上限在数据文件里，用量从手账现算**。

三级取值，顺序固定（回执里的 `source` 说清是哪一级给的）：
  1. `explicit`：本次调用显式给的数（命令行 --limit）；
  2. `live`：实盘 `<手实盘>/limits.json`（人可改、改文件即生效）；
  3. `factory`：出厂默认 `plugin/organs/hand/data/limits.json`（只读参数）；
  4. `default`：两处都没有才退到的常量——它不是真源，只是「没人调过」时的兜底。
代码里不写死数值：常量常量只是第 4 级，回执里一眼能看出「这次用的是兜底值」。

用量**不另开计数器**：只数手账里今天、这个人、`state="done"` 的行。
账头是唯一计数真源——计数器与账一旦分家，一换天就谁也说不清。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from . import ledger, store

LIMITS_KEY = "per_person_per_day"
LIMITS_VERSION = 1
#: 第 4 级兜底：两处文件都没有时用这个数。真源永远在文件里。
DEFAULT_PER_PERSON_PER_DAY = 20
SOURCES = ("explicit", "live", "factory", "default")


def _pick(data: Any) -> Optional[int]:
    """从一份 limits.json 里取上限；没有／不是非负整数都算「这级没给」。"""
    if not isinstance(data, dict):
        return None
    value = data.get(LIMITS_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def resolve_limit(*, explicit: Any = None, root=None) -> Tuple[int, str]:
    """→ (上限, 来源)。来源只会是 SOURCES 里的一个。"""
    if explicit is not None:
        try:
            value = int(explicit)
        except (TypeError, ValueError):
            value = -1
        if value >= 0:
            return value, "explicit"
    got = _pick(store.read_json(store.live_limits_path(root)))
    if got is not None:
        return got, "live"
    got = _pick(store.read_json(store.factory_limits_path()))
    if got is not None:
        return got, "factory"
    return DEFAULT_PER_PERSON_PER_DAY, "default"


def used_today(who: str, *, now: Any = None, root=None) -> int:
    """今天这个人已经做成了几次：数手账里 state="done" 的行，不数别处。"""
    day = store.local_day(store.now_iso(now))
    total = 0
    for row in ledger.read_all(root=root):
        if row.get("state") != "done" or row.get("action") == "lookup":
            continue
        if str(row.get("who")) != str(who):
            continue
        try:
            if store.local_day(row.get("at")) != day:
                continue
        except (TypeError, ValueError):
            continue          # 时间坏了的行不计数：宁可少数一次，也不把坏行算成她做的
        total += 1
    return total


def remaining(who: str, *, now: Any = None, root=None, explicit: Any = None) -> Dict[str, Any]:
    """今天还能做几次。回执四栏：上限、已用、还剩、这个上限是哪一级给的。"""
    limit, source = resolve_limit(explicit=explicit, root=root)
    used = used_today(who, now=now, root=root)
    return {"limit": limit, "used": used, "remaining": max(0, limit - used), "source": source}
