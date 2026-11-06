"""最小时间词表：把人话里的「明天中午」「下午3点半」翻成一个带偏移的时刻。

只认一张表，**解析不出就 None**——绝不猜、绝不补默认时间。猜错一个时刻，比反问一句贵得多。
表外的东西（「下周」「三天后」「开完会」）一律 None，更全的时间路由将来归脑。

表：
  天：今天 / 明天 / 后天
  段：早上(8) / 上午(8) / 中午(12) / 下午(14) / 晚上(20) / 夜里(22)
  点：N点 / N点半（N 是阿拉伯数字）
拼法：天 + 段、天 + 点、段 + 点、单段、单点。

两点写下来的判断：
1. 已经是带偏移的 ISO8601 → 原样返回；**没带时区的 ISO 返 None**（不替它补时区，
   与家 `_check_due` 同一条规矩）。
2. **没写哪一天**（只给点或只给段）而那个时刻今天已经过了 → 顺到明天。这不是「猜哪天」，
   是「今天那个点已经过去了，提醒在今天没有意义」——不这么顺，卡片一落就已经过期。
   写了天的（今天8点）就照字面落，不顺延。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

from . import store

DAY_OFFSET = {"今天": 0, "明天": 1, "后天": 2}
#: 只说一个段时取这个整点。
PERIOD_HOUR = {"早上": 8, "上午": 8, "中午": 12, "下午": 14, "晚上": 20, "夜里": 22}
#: 「段 + N点」怎么换算：早上/上午就是 N；其余先 +12（于是「下午3点」= 15:00）。
#: 两个端口自己收：中午/下午 的 12点就是 12:00；晚上/夜里的 12点算次日 00:00。
_MORNING = ("早上", "上午")

_DAY = re.compile(r"^(今天|明天|后天)")
_POINT = re.compile(r"^(早上|上午|中午|下午|晚上|夜里)?(\d{1,2})点(半)?$")
_PERIOD_ONLY = re.compile(r"^(早上|上午|中午|下午|晚上|夜里)$")


def parse(text: Any, now: Any = None) -> Optional[str]:
    """解析成 `YYYY-MM-DDTHH:MM:SS+08:00` 这样带偏移的串；认不出就 None。"""
    if not isinstance(text, str):
        return None
    raw = text.strip().replace(" ", "")
    if not raw:
        return None

    stamp = _iso_with_offset(raw)
    if stamp is not None:
        return stamp

    base = store.now_dt(now)
    day_word, rest = _split_day(raw)
    if rest is None:
        return None

    parsed = _parse_rest(rest)
    if parsed is None:
        return None
    hour, minute, extra_days = parsed
    day_shift = DAY_OFFSET[day_word] if day_word else 0
    moment = _at(base, day_shift + extra_days, hour, minute)
    if day_word is None and moment <= base:
        moment = _at(base, 1 + extra_days, hour, minute)
    return moment.isoformat(timespec="seconds")


def _iso_with_offset(raw: str) -> Optional[str]:
    """带偏移的 ISO 原样返回；不带偏移的返回 None（**不补时区**）。"""
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None or stamp.tzinfo.utcoffset(stamp) is None:
        return None
    return raw


def _split_day(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """吃掉开头那个「今天／明天／后天」，剩下的部分留给时间。"""
    found = _DAY.match(raw)
    if not found:
        return None, raw
    rest = raw[found.end():]
    return found.group(1), (rest or None)


def _parse_rest(rest: str) -> Optional[Tuple[int, int, int]]:
    """→ (时, 分, 额外天数)；认不出 None。"""
    if _PERIOD_ONLY.match(rest):
        return PERIOD_HOUR[rest], 0, 0

    found = _POINT.match(rest)
    if not found:
        return None
    period, digits, half = found.group(1), found.group(2), found.group(3)
    number = int(digits)
    if number < 0 or number > 23:
        return None
    minute = 30 if half else 0

    if period is None:
        return number, minute, 0
    if number > 12:
        return None              # 「早上25点」这类，宁可反问
    if period in _MORNING:
        return number, minute, 0
    if number == 12:
        return (12, 0, 0) if period in ("中午", "下午") else (0, 0, 1)
    return number + 12, minute, 0


def _at(base: datetime, day_shift: int, hour: int, minute: int) -> datetime:
    """落在 base 那天之后第 day_shift 天的 hour:minute（沿用 base 的时区）。"""
    day = (base + timedelta(days=day_shift)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=base.tzinfo)
