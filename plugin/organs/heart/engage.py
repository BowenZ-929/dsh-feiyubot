"""心的 engage：说 / 少说 / 不说的三态判决，外加一组固定的枚举理由码。

判据（**数值是默认值，不是验收线**；线只许在 model.py 改，本文件只用名字）：
- 硬门先于软门，次序固定：① 夜间窗口 → ② 被骂当天 → ③ mood 与 energy 双低。
- 硬门命中一条就不再算软门（soft_skipped=True、soft=[]）：夜里、被骂、双低，
  这三件事发生的时候再去数今天说了几条没有意义。
- 没有硬门才把软门三条全算一遍（不短路）：今日量硬线判不说 / 软线判少说、
  熟稔偏低判少说、刚被打断判少说。命中集全列，最强的那门定三态。
- 每次出结论都要能说出"哪几个门命中"：hits 为空而态不是"说"，那就是
  "判了但不知道为什么"，属失败——塞 model.REASON_UNATTRIBUTED 并 WARN 出来。

本模块持有的"尺"：**一条半衰都没有**。engage 的每个量都是当刻快照——
夜间窗口按 now 的小时现算（跨午夜 23→8），被打断的冷却按时间戳差值现算，
既不累加也不衰减，所以这里不写任何半衰常数，也不许借 mood 的 3h/7d 或
offset 的 6h/4h 那把尺。熟稔 21 天那把尺在脑：心只搬运 ports 给的读数，
不持有、不重算、不抄常数；今日条数同样是脑给的，心不自己数文件。

纯函数：decide 不读盘、不写盘、不产生任何句子、不掷骰子、不调模型；
唯一落盘的是 decide_and_log 的那行日志，而日志写成败都不影响结论。
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from organs.heart import model, store

# 判决日志的文件名：一行一次判决，只看不判。
LOG_FILE = "engage_log.jsonl"

# 强弱的次序：不说 > 少说 > 说。
_STATE_RANK = {
    model.STATE_SPEAK: 0,
    model.STATE_LESS: 1,
    model.STATE_SILENT: 2,
}


@dataclass
class EngageInput:
    """一次判决的全部输入：读数和立场，一个句子都不带。

    now 给了就按它算（测试把时间往前推就靠这个），None → 此刻。
    """

    mood_now: float
    energy: float
    familiarity: float
    today_count: int
    intent: str = model.INTENT_PASSIVE
    who: str = "主人"
    now: Optional[str] = None
    insulted_today: bool = False
    last_interrupted_at: Optional[str] = None

    @classmethod
    def from_effective(cls, eff: Dict[str, Any], energy: float, familiarity: float,
                       today_count: int, **kw: Any) -> "EngageInput":
        """从 effective_state 的读数建判决输入：mood_now 取 eff["mood"]["now"]。

        门的输入必须来自 effective_state（organs/heart/effective.py，全仓唯一合成点）：
        面板显示的档位、嘴读的语气档与这里的 mood_now 是同一个数，任何一处自己再算
        一遍就是第二处真源。eff 只需要 "mood" 这一块，其余两块（axes / voice）不进门；
        其余输入原样转给 **kw，门逻辑一个字不改。
        """
        mood_read = eff.get("mood") if isinstance(eff, dict) else None
        if not isinstance(mood_read, dict) or "now" not in mood_read:
            raise ValueError(
                "from_effective 要 effective_state 的读数（缺 mood.now）：%r" % (eff,))
        return cls(
            mood_now=float(mood_read["now"]),
            energy=energy,
            familiarity=familiarity,
            today_count=today_count,
            **kw
        )


# —— 门：硬门固定次序，软门三条全算不短路 ——


def _is_night(now: str) -> bool:
    """now 的小时是否落在夜间窗口里；窗口按 model 的起点/终点算，跨午夜也对。"""
    hour = model.parse_ts(now).hour
    start = model.NIGHT_START_H
    end = model.NIGHT_END_H
    if start <= end:  # 不跨午夜
        return start <= hour < end
    return hour >= start or hour < end


def _hard_hits(inp: EngageInput, now: str) -> List[str]:
    """按固定次序把三道硬门各算一遍，命中的理由码按次序全列。"""
    hits: List[str] = []
    if _is_night(now):
        hits.append(model.REASON_NIGHT)
    if inp.insulted_today:
        hits.append(model.REASON_INSULTED)
    if inp.mood_now <= model.MOOD_LOW and inp.energy <= model.ENERGY_LOW:
        hits.append(model.REASON_DOUBLE_LOW)
    return hits


def _hard_state(reason: str, intent: str) -> str:
    """硬门 → 三态：夜里主动不提话头但被动还回；被骂当天少数；双低不出声。"""
    if reason == model.REASON_NIGHT:
        if intent == model.INTENT_PROACTIVE:
            return model.STATE_SILENT
        return model.STATE_LESS
    if reason == model.REASON_INSULTED:
        return model.STATE_LESS
    if reason == model.REASON_DOUBLE_LOW:
        return model.STATE_SILENT
    return model.STATE_LESS


def _minutes_since(stamp: Optional[str], now: str) -> Optional[float]:
    """stamp 距 now 几分钟；没有 stamp 或读不懂就 None（当作没打断过）。"""
    if not stamp:
        return None
    try:
        return model.hours_between(stamp, now) * 60.0
    except ValueError as exc:
        warnings.warn(
            "心·engage：last_interrupted_at 读不懂（%s：%s），按没有打断处理"
            % (type(exc).__name__, exc),
            RuntimeWarning, stacklevel=3)
        return None


def _soft_hits(inp: EngageInput, now: str) -> List[Tuple[str, str]]:
    """三道软门全算一遍（不短路），返回 (理由码, 它主张的态)。

    今日量一档只出一条码：过硬线判不说，只过软线判少说。
    """
    hits: List[Tuple[str, str]] = []
    if inp.today_count >= model.ENGAGE_TODAY_HARD:
        hits.append((model.REASON_TODAY_VOLUME, model.STATE_SILENT))
    elif inp.today_count >= model.ENGAGE_TODAY_SOFT:
        hits.append((model.REASON_TODAY_VOLUME, model.STATE_LESS))
    if inp.familiarity < model.ENGAGE_FAMILIARITY_LOW:
        hits.append((model.REASON_FAMILIARITY, model.STATE_LESS))
    minutes = _minutes_since(inp.last_interrupted_at, now)
    if minutes is not None and minutes < model.ENGAGE_INTERRUPT_COOLDOWN_MIN:
        hits.append((model.REASON_INTERRUPTED, model.STATE_LESS))
    return hits


def _strongest(hits: List[Tuple[str, str]]) -> Tuple[str, Optional[str], List[str]]:
    """命中集里取最强的那门定态；同强按列出次序取先者当主因。"""
    if not hits:
        return model.STATE_SPEAK, None, []
    best = hits[0]
    for item in hits[1:]:
        if _STATE_RANK[item[1]] > _STATE_RANK[best[1]]:
            best = item
    return best[1], best[0], [reason for reason, _ in hits]


def _attributed(state: str, reason: Optional[str],
                hits: List[str]) -> Tuple[Optional[str], List[str]]:
    """归因不变式：态不是"说"却没有任何理由码，就是判了但不知道为什么。"""
    if state != model.STATE_SPEAK and not hits:
        warnings.warn(
            "心·engage：判成「%s」却没有任何理由码命中——这是"
            "'判了但不知道为什么'，按 %s 记账并报出来"
            % (state, model.REASON_UNATTRIBUTED),
            RuntimeWarning, stacklevel=3)
        hits = [model.REASON_UNATTRIBUTED]
        return model.REASON_UNATTRIBUTED, hits
    if reason is None and hits:
        return hits[0], hits
    return reason, hits


def _score(inp: EngageInput) -> float:
    """面板看的解释性读数；三态由门定、不由分数定（分数不参与任何分支）。"""
    return (
        22.0
        + 0.22 * inp.energy
        + 0.24 * min(1.0, inp.familiarity / 5.0)
        + 0.12 * min(1.0, inp.today_count / 40.0)
        + 0.08 * inp.mood_now
    )


def _readouts(inp: EngageInput) -> Dict[str, Any]:
    """判决时手里握着的那几个数，原样记下来，方便事后对账。"""
    return {
        "mood_now": inp.mood_now,
        "energy": inp.energy,
        "familiarity": inp.familiarity,
        "today_count": inp.today_count,
        "intent": inp.intent,
    }


def decide(inp: EngageInput) -> Dict[str, Any]:
    """纯函数：给一次判决，说清态与理由码，不读盘、不写盘、不说话。

    返回 {"state", "reason", "hits", "hard", "soft", "soft_skipped",
          "readouts", "score"}；reason 是主因（最强那门的码，无门则 None），
    hits 是所有命中门（硬门时等于 hard），score 只做读数。
    """
    now = inp.now or model.now_iso()
    hard = _hard_hits(inp, now)
    if hard:
        # 硬门命中：一道就够，软门不算（soft_skipped=True、soft=[]）。
        reason: Optional[str] = hard[0]
        state = _hard_state(reason, inp.intent)
        hits = list(hard)
        soft: List[str] = []
        soft_skipped = True
    else:
        soft_hits = _soft_hits(inp, now)
        state, reason, hits = _strongest(soft_hits)
        soft = [code for code, _ in soft_hits]
        soft_skipped = False
    reason, hits = _attributed(state, reason, hits)
    return {
        "state": state,
        "reason": reason,
        "hits": hits,
        "hard": hard,
        "soft": soft,
        "soft_skipped": soft_skipped,
        "readouts": _readouts(inp),
        "score": _score(inp),
    }


def _log_path(root: Any = None) -> Path:
    """日志落 root（默认 store.DATA_DIR），测试一律给临时目录。"""
    base = Path(root) if root is not None else store.DATA_DIR
    return base / LOG_FILE


def decide_and_log(inp: EngageInput, root: Any = None) -> Dict[str, Any]:
    """decide 之后把这一行追加进日志；日志写失败只 WARN，不动结论。"""
    result = decide(inp)
    row = {
        "at": inp.now or model.now_iso(),
        "state": result["state"],
        "reason": result["reason"],
        "hits": list(result["hits"]),
        "readouts": dict(result["readouts"]),
    }
    try:
        store.append_jsonl(_log_path(root), row)
    except Exception as exc:  # 日志是旁证，不是判决的前提
        warnings.warn(
            "心·engage：判决日志写失败（%s：%s），结论不受影响"
            % (type(exc).__name__, exc),
            RuntimeWarning, stacklevel=2)
    return result


def attribution_report(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一批判决行算归因账：总判决数、判了却无理由的次数、按理由分的账。"""
    total = 0
    unattributed = 0
    by_reason: Dict[str, int] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        total += 1
        state = row.get("state")
        hits = row.get("hits") or []
        reason = row.get("reason")
        key = reason if isinstance(reason, str) and reason else "(none)"
        by_reason[key] = by_reason.get(key, 0) + 1
        if state != model.STATE_SPEAK and not hits:
            unattributed += 1
    return {"total": total, "unattributed": unattributed, "by_reason": by_reason}
