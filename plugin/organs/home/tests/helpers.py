"""家的测试小工具：临时根、往盘上放配置与账。真 data/ 一律不碰。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, List

from organs.home import rooms as rooms_mod
from organs.home.store import ensure_dir

ACCESS_NAME = "access.json"
AUDIT_NAME = "audit.jsonl"
MARK_NAME = "audit.seen.json"


def make_root(temp_dir: str, only: Iterable[str] = ()) -> Path:
    """造一个十间齐全的空家（临时目录），返回根。"""
    root = Path(temp_dir) / "home"
    for name in tuple(only) or rooms_mod.ROOM_NAMES:
        ensure_dir(root / name)
    return root


def write_access(root: Path, writable: Iterable[str], version: int = 1) -> Path:
    path = Path(root) / ACCESS_NAME
    payload = {"version": version, "writable": list(writable), "note": "测试用"}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_raw_access(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def today_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def append_audit_row(root: Path, room: str, *, role: str = "agent", ok: bool = True,
                     at: str = "", seq: int = 1) -> Path:
    """绕过 audit.append_entry 直接塞一行：造「今天已写 N 次」这类前置状态。"""
    path = Path(root) / AUDIT_NAME
    row = {
        "seq": seq,
        "at": at or today_stamp(),
        "role": role,
        "by": "test",
        "room": room,
        "name": "x.md",
        "chars": 1,
        "bytes": 1,
        "ok": ok,
        "code": "ok",
    }
    with open(str(path), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def seed_today(root: Path, room: str, count: int) -> None:
    for index in range(count):
        append_audit_row(root, room, seq=index + 1)


def file_lines(path: Path) -> List[str]:
    """不借被测代码数行：直接读盘，好让「文件到底几行」是独立证据。"""
    text = Path(path).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
