"""嘴的弃言账本：闸把哪句话吃了，要留下痕迹。

「弃言」= 闸判 dropped 的那一句。它跟「人自己不想说」在出口上长得一模一样（都是发空串），
所以必须有第三处证据把两件事分开：一句「今天因闸弃言 N 次，最近一次命中 <clause-id>」。
没有这本账，人就只能看到「她没说话」，分不清「她不想说」和「闸把话吃了」
（CONTRIBUTING.md §1.1 第三条：失败必须响；读不到 ≠ 0 次）。

**三件事必须分开，不许折叠**：

- 文件不存在 = 今天还没记过：计数 0、exists=false，人话照给一句「今天还没有因闸弃言」。
  「文件还没建」不是错误，但也不能省成一句「0」——那和「读了、结果是 0」看不出区别。
- 文件在、但有些行读不出来 = 跳过坏行，把坏行数报在 errors 里。跳过可以，静默不行：
  少读一行就是少记一次弃言，账本自己开始骗人的时候最该被看见。
- 目录不可写、文件读不动 = 当场抛 OSError，绝不返回 0。写不进账本却报「今天 0 次」，
  正是这本账要防的那件事。

**只换路径、不换格式**（与 warehouse 同一套口径）：一行一条 JSON（JSONL），字段至少
ts / clause_id / reason，追加写、不改旧行。ts 由调用方给（record 收 now，本文件不取时钟），
格式用时刻自己的 isoformat，好让「今天」直接拿前几个字符比。根目录三级：
参数 > 环境变量 MOUTH_TRACE_DIR > 包内 trace/。

写盘只在本文件（record）。gate.py 与 split.py 一行 IO 都没有：闸只叫注入的 sink，
时间与落盘都在 api 层与这里。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TRACE_DIR = Path(__file__).resolve().parent / "trace"   # 包内痕迹目录（家里的仓库做出来前的临时落脚点）
ENV_VAR = "MOUTH_TRACE_DIR"                             # 环境变量：换一个痕迹根目录，不动代码
LEDGER_NAME = "drops.jsonl"                             # 账本文件名：一行一条弃言
LEDGER_SUFFIX = ".jsonl"                                # 参数直接点到一个账本文件时，认这个后缀

__all__ = ["record", "report", "count_today", "last_today", "today_line", "path_of"]


def path_of(path: Optional[Any] = None) -> Path:
    """定一下这次记到哪儿：构造参数 > 环境变量 MOUTH_TRACE_DIR > 包内 trace/。

    参数给的是目录就当目录（补上 LEDGER_NAME）；直接点到 .jsonl 就照用——测试多半这么给。
    """
    if path is not None and str(path).strip():
        target = Path(str(path))
    else:
        from_env = os.environ.get(ENV_VAR, "").strip()
        target = Path(from_env) if from_env else TRACE_DIR
    if target.suffix == LEDGER_SUFFIX:
        return target
    return target / LEDGER_NAME


def record(path: Optional[Any], clause_id: str, reason: str, now: Any) -> Dict[str, Any]:
    """追加一行弃言：ts / clause_id / reason。时间由调用方给，本函数不取时钟。

    目录不在就建（mkdir -p），写不进去就照实抛——账本记不上是失败，不是「今天 0 次」。
    记的是「哪条条款、为什么弃」，不存整句原文（reason 是闸那句人话，里面会带命中的那一小段，
    复盘要用它；整句不进账本）。
    """
    stamp = _stamp(now)
    row = {"ts": stamp, "clause_id": str(clause_id or ""), "reason": str(reason or "")}
    if not row["clause_id"].strip():
        raise ValueError("弃言必须带 clause_id：不知道是哪条条款拦的，这一行记了也没用")
    target = path_of(path)
    target.parent.mkdir(parents=True, exist_ok=True)     # 目录不可写就在这里抛
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def report(path: Optional[Any], today: Any) -> Dict[str, Any]:
    """一次读完账本，给出一整节读数：today / last_clause_id / line / trace / exists / errors。

    这是给出口用的合并版：计数、最近一条、那句人话、账本路径、账本在不在、坏行几行。
    count_today / last_today / today_line 三个单问走的是同一条读法，只是各取一项。
    """
    rows, errors, exists = _rows(path, today)
    target = path_of(path)
    last = rows[-1] if rows else None
    return {
        "today": len(rows),
        "last_clause_id": str(last["clause_id"]) if last else None,
        "line": _line(len(rows), last, str(target), exists, errors),
        "trace": str(target),
        "exists": exists,
        "errors": errors,
    }


def count_today(path: Optional[Any], today: Any) -> int:
    """今天因闸弃言几次；账本不存在就是 0 次（exists 由 report 单独报）。"""
    return len(_rows(path, today)[0])


def last_today(path: Optional[Any], today: Any) -> Optional[Dict[str, Any]]:
    """今天最近一次弃言那一条记录；今天没有就 None（不是空 dict）。"""
    rows = _rows(path, today)[0]
    return rows[-1] if rows else None


def today_line(path: Optional[Any], today: Any) -> str:
    """一句人话：「今天因闸弃言 N 次，最近一次命中 <clause-id>」；N=0 也有话说。"""
    return report(path, today)["line"]


def _rows(path: Optional[Any], today: Any) -> Tuple[List[Dict[str, Any]], int, bool]:
    """读出账本里属于「今天」的那些行，返回（今天的记录, 坏行数, 文件在不在）。

    文件不存在 = （[], 0, False），不是错；坏行跳过并计数，不静默；读不动就抛，不返回空。
    """
    target = path_of(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], 0, False
    day = str(today)
    rows: List[Dict[str, Any]] = []
    errors = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            errors += 1                                 # 坏行跳过，但要让调用方看见
            continue
        if not _well_formed(row):
            errors += 1
            continue
        if _same_day(str(row["ts"]), day):
            rows.append(row)
    return rows, errors, True


def _well_formed(row: Any) -> bool:
    """一条记录至少要有 ts / clause_id / reason 三个字符串，缺一就当坏行。"""
    if not isinstance(row, dict):
        return False
    return all(isinstance(row.get(key), str) for key in ("ts", "clause_id", "reason"))


def _same_day(stamp: str, day: str) -> bool:
    """同一个「今天」：拿时刻的前面几个字符与 today 比，不比时区也不解析日期。"""
    if not day:
        return False
    return stamp[:len(day)] == day


def _stamp(now: Any) -> str:
    """调用方给的时刻落成一个字符串：有 isoformat 就用它，没有就当字符串。"""
    text = now.isoformat() if hasattr(now, "isoformat") else str(now or "")
    if not text.strip():
        raise ValueError("弃言必须带时刻：没有 ts 就没法说「今天」弃了几次")
    return text


def _line(count: int, last: Optional[Dict[str, Any]], trace: str,
          exists: bool, errors: int) -> str:
    """把一次读数写成一句人话；坏行有就补一句，不让它藏在计数背后。"""
    if not exists:
        text = "今天还没有因闸弃言（账本 %s 还不存在，等于今天还没记过）。" % trace
    elif count:
        text = "今天因闸弃言 %d 次，最近一次命中 %s。" % (count, last["clause_id"])
    else:
        text = "今天还没有因闸弃言（账本 %s 在，今天一条弃言都没有）。" % trace
    if errors:
        text += "另有 %d 行读不出来，已跳过。" % errors
    return text
