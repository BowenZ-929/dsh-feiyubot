"""脑的参数与默认值：数值一律是默认值，不是验收线（docs/plan.md §2）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from organs.day.model import day_key  # noqa: F401  # 全仓唯一 day_key 真源


@dataclass(frozen=True)
class Limits:
    merge_window_minutes: int = 10      # 同一个人 10 分钟内连发并成一条
    day_cutover_hour: int = 4           # 换日点凌晨 4:00（默认值，作息改了要人改）
    max_today_entries: int = 60         # 水位线：热记忆形状待定，本轮不触发降级
    recall_limit: int = 3               # 本轮验收：回想给出 0–3 条带来源
    proposal_evidence: int = 3          # 每份提案必须附三条依据记忆
    proposal_nightly_max: int = 2       # 每晚 0–2 条
    familiarity_lambda: float = 0.5     # 熟稔漏积分器 λ
    familiarity_days: int = 21          # 熟稔遗忘曲线单位 21 天
    signal_scan_days: int = 3           # 提案信号回看天数（默认值）


@dataclass(frozen=True)
class PackLimits:
    """召回装包预算（都是默认值，不是验收线；名额上限将来由嘴的注入预算反推）。"""
    slots: int = 6                      # 总名额预算：一条纲占 1 个名额，普通条目 1 条占 1 个名额
    per_item_cap: int = 160             # 每项字符上限，超过截断；超预算的项整条跳过
    today_quota: int = 3                # 今日桶配额
    hot_quota: int = 3                  # 热桶配额（薄索引命中后展开的正文）
    longterm_quota: int = 3             # 长期桶配额
    outline_quota: int = 1              # 纲桶配额（纲还没有产出，桶先留）


limits = Limits()
pack_limits = PackLimits()

STATUS_ACTIVE = "active"        # 在役：进查询
STATUS_STALE = "stale"          # 已退役：不进回想，但保留在盘上（可恢复）
# 判据：每个状态枚举值必须有生产者——谁能把它置成这个值。
# 写者为 _settle_retention（读时结算：retention 低于阈值且闲置超限 → 落冷仓）。
STATUS_ARCHIVED = "archived"    # 已归档（落冷仓；原文仍在盘上可捞）
STATUS_RETIRED = (STATUS_STALE, STATUS_ARCHIVED)

# 状态 → 写者函数（谁负责把它置成这个值）。None = 未接线：只有读路径、没有任何写路径。
STATUS_WRITERS = {
    STATUS_ACTIVE: "BrainStore.ingest / BrainStore.import_rows（默认落盘）",
    STATUS_STALE: "BrainStore.retire / BrainStore.forget",
    STATUS_ARCHIVED: "BrainStore._settle_retention（读时结算：retention 低于阈值且闲置超限落冷仓）",
}


def status_writer_report() -> list:
    """可跑的断言：列出所有 status 常量，逐个指出写者；未接线的打印「未接线」。"""
    rows = []
    for status in (STATUS_ACTIVE, STATUS_STALE, STATUS_ARCHIVED):
        writer = STATUS_WRITERS.get(status)
        rows.append({"status": status,
                     "writer": writer,
                     "wired": writer is not None})
    return rows

# 人名录不建卡的占位 who：不知道是谁 ≠ 有一个人叫"人"
PLACEHOLDER_WHOS = ("", "人", "human", "unknown", "未知")

# 来源类型：亲历 = 真实对话；日程模拟 = 程序模拟她的一天；迁移 = 旧家摘要搬进来（不作证词）
SOURCE_LIVED = "亲历"
SOURCE_SCHEDULED = "日程模拟"
SOURCE_MIGRATED = "迁移"
SOURCE_TYPES = (SOURCE_LIVED, SOURCE_SCHEDULED, SOURCE_MIGRATED)

# 说话侧：他说（对方，默认可召回）／她说（她自己的话，默认不召回，引用走 selfnarration）
SIDE_OTHER = "他说"
SIDE_SELF = "她说"
SIDE_TYPES = (SIDE_OTHER, SIDE_SELF)

# α（重要度）与 β（可信度）默认值：β<0 只当线索、不作证词；虚构（日程模拟）只作谈资
ALPHA_DEFAULT = 1
BETA_DEFAULT = 0
BETA_RANGE = (-10, 10)

# 仍待定、别自己拍的项（docs/plan.md §3）
PENDING = (
    "热记忆的形状：20 行 vs 薄索引（挤掉时必须记录被挤掉的是什么）",
    "记忆滤镜：是否按情绪生成主观副本",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_ts(text: str) -> datetime:
    """ISO 时间戳 → 带时区的 datetime；naive 按 UTC 算（坏时间由调用方兜）。"""
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


