"""家的准入：写前的两道门、读一次与写一次的上限。

写前的判定只有两处门，合写在 check_write 里，别的模块（卡、住户、器官）一律调它，不许自己再判一次：
门一 结构门（角色无关，见 READONLY_ROOMS）：十间里哪几间放东西是结构决定的，只读五间对三个角色一律拒；
门二 白名单门（只管 agent）：哪几间由人拨给她，可收回也可再放开。
两道门分开是因为改它们的是不同的人事：门一要人改代码（等于改建房），门二是改一份配置。
读侧只有上限，不看任何写权；上限数值集中在这里，因为「写多少算一写」是一个决定。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .rooms import READONLY_ROOMS, ROOM_NAMES, RoomMiss, find, room_dir
from .store import factory_path, home_root, read_lines

MAX_CHARS_PER_WRITE = 2000
MAX_FILES_PER_WRITE = 20
READ_MAX_LINES = 200
READ_MAX_CHARS = 6000
MAX_WRITES_PER_ROOM_PER_DAY = 50
ROOM_CAP = {"仓库": 16000, "卧室": 32000, "橱柜": 8000}   # 只这三间有件数上限
MAX_SEGMENT = 64
MAX_DEPTH = 4
ROLES = ("agent", "organ", "human")

ENV_ACCESS = "HOME_ACCESS_FILE"
# 门一用的名字：与 rooms.READONLY_ROOMS 是同一份事实，这只给个本模块的叫法，不另写一遍
ROOM_READONLY = READONLY_ROOMS
ACCESS_NAME = "access.json"
AUDIT_NAME = "audit.jsonl"
ACCESS_VERSION = 1
_CONTROL_MAX = 32
_CONTROL_DEL = 127


@dataclass(frozen=True)
class Whitelist:
    writable: frozenset
    source: str          # 哪一级解析出来的，回执里要能说清

    def __contains__(self, room: str) -> bool:
        return room in self.writable


class AccessDenied(Exception):
    """拒收回执：code 给机器，message 给人，detail 带 used/limit/needed 让写入方知道还差多少。"""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.message)


def _deny(code: str, message: str, used: int, limit: int, needed: int, **extra: Any) -> AccessDenied:
    detail: Dict[str, Any] = {"used": used, "limit": limit, "needed": needed}
    detail.update(extra)
    return AccessDenied(code, message, detail)


def load_whitelist(root=None, explicit=None) -> Whitelist:
    """三级解析：显式参数 > HOME_ACCESS_FILE > home_root()/access.json。每次调用重读盘，不缓存。

    缺件、坏件、含未知房名一律响亮抛——「读不到」不等于「谁都能写」，也不等于「谁都不能写」，
    三种情况不许折叠成一张空表。空表只能来自一份人显式写空的配置（writable: []），
    那是一个决定，读侧据此照常工作，不由它顺推。

    这是门二的白名单：它只记「她自己（role=agent）今天能写哪几间」。
    十间里哪几间放东西是**结构**决定的（门一，见 READONLY_ROOMS），改它等于改建房，只有人能改代码；
    白名单只在这五间里点名给她哪几间——可以收回，也可以再放开。

    别的器官与人是替她把东西放进来，走门一与其余各关，不经过这条白名单：一旦把「她能写哪几间」
    当成别人做事的前提，她某天被收回一间房，眼、嘴、脑就跟着停摆，那不是权限收窄而是全屋断电。
    读侧与审计更不以「她能写」为前提——check_read / read_tail / verify 都不看白名单。
    配置里点名只读房（比如「抽屉」）是配置与结构打架，这里响亮报错，不许静默取交集。
    """
    if explicit is not None:
        if isinstance(explicit, Whitelist):
            return explicit
        if isinstance(explicit, (str, Path)):
            path = Path(explicit)
            return _read_file(path, "explicit:" + str(path))
        return _parse(explicit, "explicit")
    env = os.environ.get(ENV_ACCESS, "")
    if env:
        path = Path(env)
        return _read_file(path, "env:" + env)
    path = Path(home_root(root)) / ACCESS_NAME
    return _read_file(path, "root:" + str(path))


def seed_path() -> Path:
    """**出厂默认**那份白名单：随包发、只读参数，不是真源。

    它只回答「新机器该装成什么」，不回答「她今天能写哪几间」。读后者只经
    `load_whitelist()` → `home_root()/access.json`；改这份不影响任何一位她。
    """
    return factory_path(ACCESS_NAME)


def read_seed() -> Whitelist:
    """读出厂默认那份，**仅供补件／对照**。它答的不是「她今天能写哪几间」。"""
    path = seed_path()
    return _read_file(path, "seed:" + str(path))


def _read_file(path: Path, source: str) -> Whitelist:
    if not path.exists():
        raise _deny("ACCESS_FILE_MISSING", "白名单文件不在：%s" % path, 0, 1, 1,
                    path=str(path), source=source)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _deny("ACCESS_FILE_BAD", "白名单文件读不出来：%s" % exc, 0, 1, 1,
                    path=str(path), source=source)
    return _parse(data, source)


def _parse(data: Any, source: str) -> Whitelist:
    if not isinstance(data, dict):
        raise _deny("ACCESS_FILE_BAD", "白名单顶层必须是对象", 0, 1, 1, source=source)
    version = data.get("version", ACCESS_VERSION)
    if version != ACCESS_VERSION:
        raise _deny("ACCESS_VERSION_UNKNOWN", "白名单版本不认识：%r" % (version,), 0, 1, 1,
                    source=source, version=version)
    rows = data.get("writable")
    if not isinstance(rows, (list, tuple)):
        raise _deny("ACCESS_FILE_BAD", "writable 必须是房名数组", 0, 1, 1, source=source)
    writable = set()
    for raw in rows:
        if not isinstance(raw, str):
            raise _deny("ACCESS_FILE_BAD", "writable 里有一项不是房名：%r" % (raw,), 0, 1, 1,
                        source=source)
        try:
            room = find(raw)
        except RoomMiss as miss:
            raise _deny("ACCESS_ROOM_UNKNOWN", str(miss), 0, 1, 1,
                        source=source, room=raw, nearest=miss.nearest)
        if room.name in READONLY_ROOMS:
            raise _deny("ACCESS_ROOM_READONLY",
                        "配置点名了只读房，与结构矛盾：%s" % room.name, 0, 0, 1,
                        source=source, room=room.name, readonly=sorted(READONLY_ROOMS))
        writable.add(room.name)
    return Whitelist(writable=frozenset(writable), source=source)


def _segments(name: str):
    return [part for part in name.replace("\\", "/").split("/")]


def check_path_shape(room: str, name: str) -> None:
    """只收「房名下的相对路径」：无 .. 、非绝对、无控制字符、单段 ≤64 字、深度 ≤4。"""
    if not isinstance(name, str) or not name.strip():
        raise _deny("PATH_EMPTY", "名字是空的", 1, 0, 1, room=room)
    if any(ord(char) < _CONTROL_MAX or ord(char) == _CONTROL_DEL for char in name):
        raise _deny("PATH_CONTROL_CHAR", "名字里有控制字符", 1, 0, 1, room=room)
    if name.startswith(("/", "\\")) or os.path.isabs(name) or name[1:2] == ":":
        raise _deny("PATH_ABSOLUTE", "只收房名下的相对路径，不收绝对路径：%r" % name, 1, 0, 1, room=room)
    parts = _segments(name)
    if any(part in ("",) for part in parts):
        raise _deny("PATH_SHAPE", "路径里有空段：%r" % name, 1, 0, 1, room=room)
    if any(part in (".", "..") for part in parts):
        raise _deny("PATH_TRAVERSAL", "路径里不许有上级目录符号：%r" % name, 1, 0, 1, room=room)
    for part in parts:
        if len(part) > MAX_SEGMENT:
            raise _deny("PATH_SEGMENT_TOO_LONG",
                        "单段名字超过 %d 字：%d" % (MAX_SEGMENT, len(part)),
                        len(part), MAX_SEGMENT, len(part) - MAX_SEGMENT, room=room, segment=part)
    if len(parts) > MAX_DEPTH:
        raise _deny("PATH_TOO_DEEP", "目录深度超过 %d 层：%d" % (MAX_DEPTH, len(parts)),
                    len(parts), MAX_DEPTH, len(parts) - MAX_DEPTH, room=room, segments=parts)


def _room_files(root, room: str) -> int:
    """数这间的件数。隐藏文件（.gitkeep、.tmp-*）不算：它们不是她放的物件。"""
    directory = room_dir(room, root)
    if not directory.exists():
        return 0
    total = 0
    for path in directory.rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            total += 1
    return total


def _writes_today(root, room: str) -> int:
    """按审计账数今天这间写过几次。账头是唯一的计数真源，不另开计数器（另开会与账分叉）。"""
    path = Path(home_root(root)) / AUDIT_NAME
    rows, _bad = read_lines(path)
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    count = 0
    for row in rows:
        if row.get("room") != room or not row.get("ok", True):
            continue
        if str(row.get("at", ""))[:10] != today:
            continue
        count += 1
    return count


def check_write(*, role: str, room: str, name: str, chars: int, files: int = 1,
                root=None, whitelist: Optional[Whitelist] = None) -> None:
    """写前唯一的准入判定：两道门加五道上限，顺序固定。

    门一 结构门（角色无关）：房名在十间，且不是结构性只读的那五间。它不看任何配置文件，
    因为「这间不放东西」是房子的形状，不是谁今天的权限。
    门二 白名单门（只管 agent）：在结构可写的房里，她今天被点名了哪几间。
    organ 与 human 不经过门二——理由写在 load_whitelist 的 docstring 里。

    两道门之后：路径形状 → 单次字数 → 单次文件数 → 该房件数 → 该房日频。
    """
    try:
        found = find(room)
    except RoomMiss as miss:
        raise _deny("ROOM_MISS", str(miss), 0, len(ROOM_NAMES), 1,
                    room=room, nearest=miss.nearest)

    if found.name in READONLY_ROOMS:
        raise _deny("ROOM_READONLY", "这间只读，不放东西：%s" % found.name,
                    0, 0, 1, door="structure", room=found.name,
                    readonly=sorted(READONLY_ROOMS))

    if role == "agent":
        allowed = whitelist if whitelist is not None else load_whitelist(root)
        if found.name not in allowed.writable:
            raise _deny("ROOM_READONLY", "这间她现在不写：%s" % found.name,
                        0, len(allowed.writable), 1, door="whitelist",
                        room=found.name, writable=sorted(allowed.writable), source=allowed.source)
    elif role not in ("organ", "human"):
        raise _deny("ROLE_UNKNOWN", "不认识的角色：%r" % (role,), 0, len(ROLES), 1, role=role)

    check_path_shape(found.name, name)

    if chars > MAX_CHARS_PER_WRITE:
        raise _deny("CHARS_OVER", "一次写 %d 字，超过 %d 字，得拆" % (chars, MAX_CHARS_PER_WRITE),
                    chars, MAX_CHARS_PER_WRITE, chars - MAX_CHARS_PER_WRITE, room=found.name)
    if files > MAX_FILES_PER_WRITE:
        raise _deny("FILES_OVER", "一次写 %d 个文件，超过 %d 个，得拆批" % (files, MAX_FILES_PER_WRITE),
                    files, MAX_FILES_PER_WRITE, files - MAX_FILES_PER_WRITE, room=found.name)

    cap = ROOM_CAP.get(found.name)
    if cap is not None:
        used = _room_files(root, found.name)
        if used + files > cap:
            raise _deny("ROOM_CAP_REACHED", "这间已经 %d 件，上限 %d 件" % (used, cap),
                        used, cap, used + files - cap, room=found.name)

    today = _writes_today(root, found.name)
    if today + 1 > MAX_WRITES_PER_ROOM_PER_DAY:
        raise _deny("DAY_QUOTA_REACHED", "这间今天已经写了 %d 次，上限 %d 次" % (today, MAX_WRITES_PER_ROOM_PER_DAY),
                    today, MAX_WRITES_PER_ROOM_PER_DAY, today + 1 - MAX_WRITES_PER_ROOM_PER_DAY,
                    room=found.name)


def check_read(*, room: str, name: str, lines: int, chars: int) -> None:
    """读侧只判「读得动」：房名、路径形状、一次读多少。读不看白名单——她能写哪几间，与她或别人能读什么无关。"""
    try:
        found = find(room)
    except RoomMiss as miss:
        raise _deny("ROOM_MISS", str(miss), 0, len(ROOM_NAMES), 1,
                    room=room, nearest=miss.nearest)
    check_path_shape(found.name, name)
    if lines > READ_MAX_LINES:
        raise _deny("READ_LINES_OVER", "一次读 %d 行，超过 %d 行" % (lines, READ_MAX_LINES),
                    lines, READ_MAX_LINES, lines - READ_MAX_LINES, room=found.name)
    if chars > READ_MAX_CHARS:
        raise _deny("READ_CHARS_OVER", "一次读 %d 字，超过 %d 字" % (chars, READ_MAX_CHARS),
                    chars, READ_MAX_CHARS, chars - READ_MAX_CHARS, room=found.name)
