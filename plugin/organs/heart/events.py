"""事件账本：她最近遇到了什么。

这本账是"她最近遇到了什么"的**唯一投递管**：心不自己编事件，嘴/脑投递，心只读。
**禁止用它存事实条目**——事实归魂与脑的事实库，不归这本账。

规矩：
- 只追加、不改写：写完的行不再动；读的时候坏行跳过；文件不存在 → 空账。
- 本模块不持有任何时间衰减常数：窗口是调用方传进来的分钟数，
  "现在"一律读时现算（公开函数都收 now，缺省取 model.now_iso()）。
- 两个落账口子：
  deliver() 给嘴/脑投递，只认四个 DELIVER_KINDS，别的 kind 一律 ValueError；
  append() 是内部口子，mood 的四类事件（praise / cold / conflict / insult）也落同一本账。
- 行形状 {"at": iso, "kind": str, "who": str, ...extra}；下一轮的 engage 窗口与念头层读它。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from organs.heart import model, store

EVENTS_FILE = "event_log.jsonl"

KIND_IGNORED = "ignored"  # 她说了、没人回（嘴/脑投递）
KIND_HARSH = "harsh"  # 最近被凶
KIND_REPLIED = "replied"  # 一次正常互动
KIND_NORMAL = "normal"  # 背景互动（中性）
DELIVER_KINDS = (KIND_IGNORED, KIND_HARSH, KIND_REPLIED, KIND_NORMAL)


# ---- 内部小工具 ----------------------------------------------------------


def _path(root: Any) -> Path:
    """账本路径：root 给了就用 root，没给就用 store.DATA_DIR。"""
    base = Path(root) if root is not None else store.DATA_DIR
    return base / EVENTS_FILE


def _at(now: Optional[str]) -> str:
    """这一笔的时刻：调用方给了就用它，没给就是此刻。"""
    return now if isinstance(now, str) and now else model.now_iso()


def _stamp(row: Dict[str, Any]) -> Optional[datetime]:
    """行里的时刻；缺 at / 坏时间戳 → None（读的时候直接跳过）。"""
    value = row.get("at")
    if not isinstance(value, str) or not value:
        return None
    try:
        return model.parse_ts(value)
    except ValueError:
        return None


def _rows_in_window(
    root: Any, at: str, window_min: float, kinds: Optional[Tuple[str, ...]]
) -> List[Dict[str, Any]]:
    """(at − window_min, at] 里的行，按时刻升序；未来的行不算（时钟还没走到那儿）。"""
    end = model.parse_ts(at)
    horizon = end - timedelta(minutes=float(window_min))
    wanted = None if kinds is None else tuple(kinds)
    kept: List[Tuple[datetime, Dict[str, Any]]] = []
    for row in store.read_jsonl(_path(root)):
        stamp = _stamp(row)
        if stamp is None or stamp < horizon or stamp > end:
            continue
        if wanted is not None and row.get("kind") not in wanted:
            continue
        kept.append((stamp, row))
    kept.sort(key=lambda pair: pair[0])
    return [row for _stamp_value, row in kept]


def _append(
    kind: str, who: Any, root: Any, now: Optional[str], extra: Dict[str, Any]
) -> Dict[str, Any]:
    """落一行并返回它；at / kind / who 打头，extra 只补不覆盖。"""
    at = _at(now)
    label = who if isinstance(who, str) and who else "主人"
    row: Dict[str, Any] = {"at": at, "kind": kind, "who": label}
    for key, value in extra.items():
        if key not in row:
            row[key] = value
    store.append_jsonl(_path(root), row)
    return row


# ---- 对外 ----------------------------------------------------------------


def deliver(
    kind: str, who: str = "主人", root: Any = None, now: Optional[str] = None, note: str = ""
) -> Dict[str, Any]:
    """嘴/脑投递一条：她说了没人回（ignored）、被凶（harsh）、正常回了一句（replied）、
    背景互动（normal）。kind 不在 DELIVER_KINDS → ValueError，坏值原样带出，不悄悄改写。
    返回写进账本的那一行。
    """
    if kind not in DELIVER_KINDS:
        raise ValueError(
            "不认识的投递事件：%r（只认 %s）" % (kind, "、".join(DELIVER_KINDS))
        )
    extra: Dict[str, Any] = {"note": note} if note else {}
    return _append(kind, who, root, now, extra)


def append(
    kind: str, who: str = "主人", root: Any = None, now: Optional[str] = None, **extra: Any
) -> None:
    """内部口子：mood 的四类事件也落同一本账（kind 就用 praise/cold/conflict/insult）。

    不校验枚举（枚举归 mood 管），但 kind 必须是非空字符串——空名字的行读不出意义。
    """
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("事件名必须是非空字符串，收到 %r" % (kind,))
    _append(kind, who, root, now, extra)


def recent(
    root: Any = None,
    now: Optional[str] = None,
    window_min: float = 120.0,
    limit: int = 200,
    kinds: Optional[Tuple[str, ...]] = None,
) -> List[Dict[str, Any]]:
    """最近 window_min 分钟内（以 now 现算）的事件，按时刻升序，最多给最后 limit 条。

    kinds 给了就只留这几种；坏行 / 坏时间戳 / 未来的行跳过；文件不存在 → []。
    """
    rows = _rows_in_window(root, _at(now), window_min, kinds)
    if isinstance(limit, int) and limit >= 0:
        return rows[-limit:] if limit else []
    return rows


def same_kind_since(
    kind: str, root: Any = None, now: Optional[str] = None, window_min: float = 10.0
) -> int:
    """同类型事件本窗口内已发生几次（mood 的热度折扣用它，**不含即将落账的这一次**）。"""
    return len(_rows_in_window(root, _at(now), window_min, (kind,)))
