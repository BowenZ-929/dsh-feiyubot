"""心的常数表：版式、三个时钟、mood / energy / offset / engage 的尺，加时间小工具。

这里只放名字与数值，不放任何器官逻辑——怎么累加、怎么切片、怎么判先后，
都归后续各自的模块（mood / energy / offset / engage）。

一条铁律：时钟各有一把尺，互不引用。mood.now、mood.baseline、offset 三把尺
混用一次，数值就再也说不清是谁的节奏，所以每个常数上方写清"只给谁用"。
"""
from __future__ import annotations

from datetime import datetime, timezone

VERSION = 1

# ---- 版式与范围 ----------------------------------------------------------

# 六个轴：前四个成对（冷热两端都有意义），后两个只待定不配对。
AXES = ("服气", "心疼", "想黏人", "怕失去", "耐心", "记性")
PAIRED_AXES = ("服气", "心疼", "想黏人", "怕失去")
PENDING_AXES = ("耐心", "记性")
TIER_COUNT = 5

# ---- 三个时钟（各一把尺，互不引用） --------------------------------------

# 只给 mood.now 用：此刻心情自己往回走的那把尺。不许与 baseline、offset 共用。
MOOD_NOW_HALF_LIFE_H = 3.0

# 只给 mood.baseline 用：心情基线的长半衰。不许与 now 的 3h 那把尺混算。
# 熟稔 21 天那把尺在脑·人名录，心不持有、不引用、不抄常数。
MOOD_BASELINE_HALF_LIFE_H = 168.0

# ---- mood ----------------------------------------------------------------

MOOD_MIN = 0
MOOD_MAX = 100
# 一「档」= 10 分：说人话时按档讲，落盘仍按分。
MOOD_UNIT = 10
MOOD_BASELINE_DEFAULT = 60

MOOD_PRAISE = 1
MOOD_COLD = -1
MOOD_CONFLICT = -2

# 冲突一天最多扣两次，剩下的当天不再扣。
MOOD_CONFLICT_DAILY_CAP = 2

# 天花板：就是 MOOD_MAX 那把尺，不另造同值常数。正向增量乘余量 (1 - 当前值/天花板)，
# 越贴近顶越加不动——一顿夸顶不满，只能一路逼近。只给 mood 用。
MOOD_CEILING = MOOD_MAX
# 热度折扣的窗口与分母：同类型事件 HEAT_WINDOW_MIN 分钟内第 n+1 次，增益 × 1/(1 + n/K)。
# 只压正向；负向不折扣——疲惫与压力是代价，不打折。窗口与分母都只给 mood 用。
MOOD_HEAT_WINDOW_MIN = 10
MOOD_HEAT_K = 12.0
# 同一小时内的事件合并成一次，避免连珠炮式加分。
MOOD_EVENT_WINDOW_H = 1
# 两次采样至少隔 10 分钟，防止抖动被当成真变化。
MOOD_SAMPLE_MIN_GAP_MIN = 10
MOOD_LOW = 30

# ---- energy --------------------------------------------------------------

ENERGY_MIN = 0
ENERGY_MAX = 100

# 醒着的自然消耗（每小时）。
ENERGY_AWAKE_PER_H = -4.0
# 睡着的回血速度（每分钟）。
ENERGY_SLEEP_PER_MIN = 1.0
# 结算切片粒度：按 5 分钟一段算，不做连续积分。
ENERGY_SLICE_MIN = 5
# 一次最多补算 24 小时的账，超出的部分不追。
ENERGY_MAX_SETTLE_H = 24.0
# 两次精力采样至少隔 10 分钟。
ENERGY_MIN_GAP_MIN = 10

# 一次"用力"（短促爆发）的代价。
ENERGY_BURST_COST = 2
# 一条长回复的代价，长度门槛见 ENERGY_LONG_REPLY_CHARS。
ENERGY_LONG_REPLY_COST = 1
ENERGY_LONG_REPLY_CHARS = 200
# 一天的额外消耗封顶。
ENERGY_DAILY_CAP = 5

# 睡觉窗口：23 点到次日 8 点。
ENERGY_SLEEP_START_H = 23
ENERGY_SLEEP_END_H = 8
ENERGY_LOW = 30
# 醒来时的默认精力。
ENERGY_WAKE_DEFAULT = 80

# ---- offset --------------------------------------------------------------

# 单个轴的偏移幅度上限（正负都算）。
OFFSET_MAX = 2.0
# 单轴一天的累计偏移上限。
OFFSET_DAILY_PER_AXIS = 1.0
# 一周窗口内的最大偏移。
OFFSET_WEEK_MAX = 2.0
# 连续 3 天无事件 → 向 0 回落 1：offset 的回落尺只有这一把，单位是天，
# 判据见 docs/modules/heart.md §2 offset「会被什么改变」。
OFFSET_IDLE_DAYS = 3
# 闲置一天往回走的量。
OFFSET_IDLE_PER_DAY = 1.0
# 没有偏移时的基线。
OFFSET_BASE = 0.0

# ---- engage --------------------------------------------------------------

# 夜间窗口：与 sleep 窗口同形，但这个量属 engage，别拿去算精力。
NIGHT_START_H = 23
NIGHT_END_H = 8

# 今天说话量的软上限与硬上限。
ENGAGE_TODAY_SOFT = 20
ENGAGE_TODAY_HARD = 40
# 熟稔低于这个值算"还不熟"，会少说。
ENGAGE_FAMILIARITY_LOW = 1.0
# 刚被打断后的冷却分钟数。
ENGAGE_INTERRUPT_COOLDOWN_MIN = 30

STATE_SPEAK = "说"
STATE_LESS = "少说"
STATE_SILENT = "不说"
STATES = (STATE_SPEAK, STATE_LESS, STATE_SILENT)

REASON_NIGHT = "night"
REASON_INSULTED = "insulted-today"
REASON_DOUBLE_LOW = "mood-and-energy-low"
REASON_TODAY_VOLUME = "today-volume"
REASON_FAMILIARITY = "low-familiarity"
REASON_INTERRUPTED = "recently-interrupted"
# 近期事件窗口（engage.window_signal 现算）：连续被无视 / 最近 2 小时内被凶。
REASON_IGNORED = "ignored-recently"
# 意图排序层（intent.rank 现算）：这一刻最值得开口的念头也没过阈值 → 少说。
REASON_INTENT_BELOW = "intent-below-threshold"
# 主动开口连着没人理（proactive.is_muted 现算）：只关**主动**那一侧，被动回复不受影响。
REASON_PROACTIVE_MUTED = "proactive-muted"
REASON_UNATTRIBUTED = "unattributed"

# 硬理由：一条就足够闭嘴，不看别的。
HARD_REASONS = (REASON_NIGHT, REASON_INSULTED, REASON_DOUBLE_LOW)
# 软理由：单独不足以沉默，累积才降态。
SOFT_REASONS = (REASON_TODAY_VOLUME, REASON_FAMILIARITY, REASON_INTERRUPTED,
                REASON_IGNORED, REASON_INTENT_BELOW, REASON_PROACTIVE_MUTED)
REASONS = HARD_REASONS + SOFT_REASONS + (REASON_UNATTRIBUTED,)

INTENT_PASSIVE = "passive"
INTENT_PROACTIVE = "proactive"
INTENTS = (INTENT_PASSIVE, INTENT_PROACTIVE)

EVENT_PRAISE = "praise"
EVENT_COLD = "cold"
EVENT_CONFLICT = "conflict"
EVENT_INSULT = "insult"
EVENTS = (EVENT_PRAISE, EVENT_COLD, EVENT_CONFLICT, EVENT_INSULT)

# ---- 时间工具 ------------------------------------------------------------
# 一律本地时区、秒精度；落盘串里带偏移，读回来永远 aware。


def _local_tz() -> "timezone":
    """本机当前时区偏移（固定化，够用且不依赖 tzdata）。"""
    return datetime.now().astimezone().tzinfo or timezone.utc


def now_iso() -> str:
    """此刻：本地时区、秒精度、带偏移的 ISO8601。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def parse_ts(value: str) -> datetime:
    """ISO 串 → aware datetime。

    以 "Z" 结尾按 UTC 解析；串里没时区信息就按本地时区解释；
    解析不了就抛 ValueError。返回值一定带 tzinfo。
    """
    if not isinstance(value, str):
        raise ValueError(f"时间戳必须是字符串，收到 {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError("时间戳为空")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"时间戳无法解析：{value!r}") from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_local_tz())
    return stamp


def iso(dt: datetime) -> str:
    """datetime → 与 now_iso 同格式的串；naive 按本地时区补上偏移。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz())
    return dt.astimezone().replace(microsecond=0).isoformat()


def hours_between(older: "str | datetime", newer: "str | datetime") -> float:
    """两个时刻相差几小时；负数（顺序反了）归 0。"""
    start = parse_ts(older) if isinstance(older, str) else older
    end = parse_ts(newer) if isinstance(newer, str) else newer
    if start.tzinfo is None:
        start = start.replace(tzinfo=_local_tz())
    if end.tzinfo is None:
        end = end.replace(tzinfo=_local_tz())
    delta = (end - start).total_seconds()
    if delta <= 0:
        return 0.0
    return delta / 3600.0
