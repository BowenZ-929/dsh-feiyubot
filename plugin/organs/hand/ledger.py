"""手账：<手实盘>/ledger.jsonl，一行一动作，**只增不改**。

一次动作落两行：起手一行 `state="reserved"`（做之前先占位），做完（或拒了、或失败了）
再追加一行 `done/refused/failed`。两行共用一个 `ledger_id`，读时按它折叠，最新那行说了算。
盘上一个字都不删不改——想改结论就再加一行。

为什么要 reserved：动作做到一半进程没了，「做过没有」必须能从账上看出来，而不是靠猜。

**账写不进去 = 这次动作失败**（宁可不做，也不要做了没记账）：本文件写失败一律抛
`LedgerWriteFailed`，由 actions 改判 —— reserved 行写不进就不动手，结果行写不进就
把这次结果改判 failed，绝不对内对外说成功。

本文件与 store 是**唯二**直接碰盘的地方；别的模块要落字，都得调它们（由
tests/test_source_rules.py 看住）。
"""
from __future__ import annotations

import secrets
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from . import store

STATES = ("reserved", "done", "refused", "failed")
FIELDS = ("ledger_id", "at", "who", "action", "params", "state", "code", "elapsed_ms",
          "origin", "grant_id")
#: 参数摘要里字符串最多留这么多字：账是索引，不是正文仓库。
SUMMARY_CHARS = 80
EMPTY_PARAMS: Dict[str, Any] = {}


class LedgerWriteFailed(Exception):
    """账写不进去：code 固定 LEDGER_WRITE_FAILED，detail 带原始错误。"""

    def __init__(self, detail: Any = "") -> None:
        super().__init__("LEDGER_WRITE_FAILED")
        self.code = "LEDGER_WRITE_FAILED"
        self.detail = detail


def new_id() -> str:
    return "lg-" + secrets.token_hex(8)


def summarise(params: Any) -> Dict[str, Any]:
    """参数摘要：长文本截断，其余原样。账不必留下整段备注。"""
    out: Dict[str, Any] = {}
    for key, value in dict(params or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and len(value) > SUMMARY_CHARS:
            out[key] = value[:SUMMARY_CHARS] + "…"
        else:
            out[key] = value
    return out


def make_entry(*, who: str, action: str, params: Any = None, state: str = "reserved",
               code: str = "OK", elapsed_ms: int = 0, origin: str = "OK",
               grant_id: Optional[str] = None, at: Any = None,
               ledger_id: Optional[str] = None) -> Dict[str, Any]:
    """按 FIELDS 的顺序造一行账。字段一个不少：缺了的读账人要自己猜是哪一步，那是坏账。"""
    if state not in STATES:
        raise ValueError("不认识的手账状态：%r" % (state,))
    return {
        "ledger_id": ledger_id or new_id(),
        "at": at or store.now_iso(),
        "who": who,
        "action": action,
        "params": summarise(params),
        "state": state,
        "code": code,
        "elapsed_ms": int(elapsed_ms),
        "origin": origin,
        "grant_id": grant_id,
    }


def append(entry: Dict[str, Any], *, root=None) -> Dict[str, Any]:
    """追加一行并返回落下去的那一行（ledger_id／at 缺了就补上）。"""
    row = dict(entry)
    row.setdefault("ledger_id", new_id())
    row.setdefault("at", store.now_iso())
    for field in FIELDS:
        row.setdefault(field, None)
    row["params"] = summarise(row.get("params"))
    try:
        store.append_line(store.ledger_path(root), row)
    except OSError as exc:
        raise LedgerWriteFailed({"path": str(store.ledger_path(root)), "error": str(exc)}) from exc
    return row


def read_all(*, root=None) -> List[Dict[str, Any]]:
    """整本账，按 ledger_id 折叠成「每个动作最后的样子」（后一行盖前一行）。

    坏行由 store.read_lines 跳过并计数——一行坏了不能让整本账读不出来。
    """
    rows, _bad = store.read_lines(store.ledger_path(root))
    folded: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for row in rows:
        key = row.get("ledger_id")
        if not isinstance(key, str) or not key:
            continue
        merged = dict(folded.get(key) or {})
        merged.update(row)
        folded[key] = merged
    return list(folded.values())


def read_tail(limit: int = 50, *, root=None) -> List[Dict[str, Any]]:
    """最后 limit 个动作（折叠后）。limit 数的是**动作**，不是盘上的行。"""
    if int(limit) <= 0:
        return []
    return read_all(root=root)[-int(limit):]
