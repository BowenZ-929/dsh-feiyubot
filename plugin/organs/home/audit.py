"""家的审计：一行一写、只增不改，行数比上次少只报不修。

高水位 audit.seen.json 记「上次写到第几行、第几号」。verify() 只比对不修补——
账少了要么是盘坏了，要么是有人在改账；两种都得先被人看见，程序补回去就把事故抹平了。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .store import append_line, atomic_write_text, home_root, read_lines

AUDIT_NAME = "audit.jsonl"
HIGH_WATER_NAME = "audit.seen.json"
CODE_OK = "ok"
CODE_SHRUNK = "AUDIT_SHRUNK"
CODE_MARK_MISSING = "AUDIT_MARK_MISSING"
CODE_MARK_BAD = "AUDIT_MARK_BAD"

FIELDS = ("seq", "at", "role", "by", "room", "name", "chars", "bytes", "ok", "code")


@dataclass(frozen=True)
class Entry:
    seq: int
    at: str
    role: str
    by: str
    room: str
    name: str
    chars: int
    bytes: int
    ok: bool
    code: str

    def as_row(self) -> Dict[str, Any]:
        return {key: getattr(self, key) for key in FIELDS}


class AuditShrunk(Exception):
    """账比高水位短。"""

    def __init__(self, code: str = CODE_SHRUNK, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = dict(detail or {})

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.detail)


def _root(root) -> Path:
    return Path(home_root(root))


def _audit_path(root) -> Path:
    return _root(root) / AUDIT_NAME


def _mark_path(root) -> Path:
    return _root(root) / HIGH_WATER_NAME


def _entry_from_row(row: Dict[str, Any]) -> Optional[Entry]:
    try:
        return Entry(
            seq=int(row.get("seq", 0)),
            at=str(row.get("at", "")),
            role=str(row.get("role", "")),
            by=str(row.get("by", "")),
            room=str(row.get("room", "")),
            name=str(row.get("name", "")),
            chars=int(row.get("chars", 0)),
            bytes=int(row.get("bytes", 0)),
            ok=bool(row.get("ok", True)),
            code=str(row.get("code", CODE_OK)),
        )
    except (TypeError, ValueError):
        return None


def append_entry(root=None, *, role: str, by: str, room: str, name: str,
                 chars: int, bytes: int, ok: bool = True, code: str = CODE_OK) -> Entry:
    """追加一行并立刻刷高水位。谁、哪间、何时、多大四项缺一项就不写——空账比没账更坏。"""
    for label, value in (("role", role), ("by", by), ("room", room), ("name", name)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("审计缺一项：%s" % label)
    if not isinstance(chars, int) or not isinstance(bytes, int) or chars < 0 or bytes < 0:
        raise ValueError("审计要负数大小的账算不清：chars=%r bytes=%r" % (chars, bytes))

    path = _audit_path(root)
    rows, _bad = read_lines(path)
    seq = 0
    if rows:
        try:
            seq = int(rows[-1].get("seq", 0))
        except (TypeError, ValueError):
            seq = 0
    seq += 1
    at = datetime.now().astimezone().isoformat(timespec="seconds")
    entry = Entry(seq=seq, at=at, role=role, by=by, room=room, name=name,
                  chars=int(chars), bytes=int(bytes), ok=bool(ok), code=str(code))
    lines = append_line(path, entry.as_row())
    _write_mark(root, lines, seq, at)
    return entry


def _write_mark(root, lines: int, seq: int, at: str) -> None:
    payload = {"lines": lines, "seq": seq, "at": at}
    atomic_write_text(_mark_path(root), json.dumps(payload, ensure_ascii=False) + "\n")


def read_tail(root=None, limit: int = 200) -> List[Entry]:
    """只读：取最后 limit 条，不碰高水位——读账不算写账。"""
    rows, _bad = read_lines(_audit_path(root))
    if limit is not None and limit >= 0:
        rows = rows[-limit:] if limit else []
    out: List[Entry] = []
    for row in rows:
        entry = _entry_from_row(row)
        if entry is not None:
            out.append(entry)
    return out


def verify(root=None) -> Dict[str, Any]:
    """比当前行数与序号跟高水位。少了只报（ok=False, code=AUDIT_SHRUNK），补行与重写高水位都不许做。"""
    path = _audit_path(root)
    mark_path = _mark_path(root)
    rows, bad = read_lines(path)
    lines = len(rows)
    seq = 0
    if rows:
        try:
            seq = int(rows[-1].get("seq", 0))
        except (TypeError, ValueError):
            seq = 0
    mark: Optional[Dict[str, Any]] = None
    if mark_path.exists():
        try:
            loaded = json.loads(mark_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                mark = loaded
        except (OSError, ValueError):
            mark = None
    if mark is None:
        code = CODE_OK if lines == 0 else CODE_MARK_MISSING
        return {"ok": code == CODE_OK, "lines": lines, "seq": seq, "mark": None, "code": code, "bad": bad}
    try:
        mark_lines = int(mark.get("lines", -1))
        mark_seq = int(mark.get("seq", -1))
    except (TypeError, ValueError):
        return {"ok": False, "lines": lines, "seq": seq, "mark": mark, "code": CODE_MARK_BAD, "bad": bad}
    shrunk = lines < mark_lines or seq < mark_seq
    return {
        "ok": not shrunk,
        "lines": lines,
        "seq": seq,
        "mark": mark,
        "code": CODE_SHRUNK if shrunk else CODE_OK,
        "bad": bad,
    }

