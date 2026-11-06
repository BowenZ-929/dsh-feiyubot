"""家的房名表：十间固定，房名与目录名一一对应，不许新建、不许改名。

**真源是这份代码**；data/rooms.json 只是盘上镜像，供人打开目录时读懂十间。
镜像对不上就报错（check_rooms_json），绝不自动改写——骨架漂移要么是人改的，要么是盘坏了，
两种都得先被人看见。
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .store import atomic_write_text, factory_path, home_root


@dataclass(frozen=True)
class Room:
    name: str            # 中文房名，如 "抽屉"
    slug: str            # ascii slug，如 "drawer"
    purpose: str         # 一句话：这间放什么
    writable_default: bool


# 顺序固定：从进门到日子，人改这里才加房
# 口径变更（2026-11-06 人拍）：十间 → **十一间**。新增「素材盒」——
# 给她把"今天的一点事"先存成草稿，将来能变 QQ 空间素材。它是独立一间，不是抽屉的格子。
ROOMS: Tuple[Room, ...] = (
    Room("玄关", "entrance", "刚进门的东西先落这儿：谁给的、哪天的", True),
    Room("客厅", "living", "家里最公开的一间，放着等人来看的东西", False),
    Room("名片盒", "cards", "一人一张卡：谁、怎么认识、他给过什么", True),
    Room("工作台", "desk", "她画出来、写出来的东西落这儿", True),
    Room("仓库", "store", "她的素材：画风、构图、参考，取用时来这儿拿", True),
    Room("信箱", "mailbox", "一封一件的信，来往的长文", True),
    Room("卧室", "bedroom", "她自己的痕迹：一天一条主日记，加零散短痕迹", False),
    Room("橱柜", "closet", "衣服与随身物件", False),
    Room("抽屉", "drawer", "她自己的资料卡，只读，只有人能直接改文件", False),
    Room("台历", "calendar", "今天是哪天、什么日子", False),
    Room("素材盒", "materials", "她自己写的今天的一点事：草稿、说说、梗（将来发空间的原料）", True),
)

ROOM_NAMES: Tuple[str, ...] = tuple(room.name for room in ROOMS)
DEFAULT_WRITABLE: frozenset = frozenset(room.name for room in ROOMS if room.writable_default)
# 结构性只读：这五间不放东西。它与 writable_default 是同一份事实的背面，所以只从 ROOMS 派生一次
READONLY_ROOMS: frozenset = frozenset(room.name for room in ROOMS if not room.writable_default)

_BY_NAME = {room.name: room for room in ROOMS}
_BY_SLUG = {room.slug: room for room in ROOMS}

ROOMS_JSON_NAME = "rooms.json"
ROOMS_JSON_NOTE = (
    "派生物：真源是 organs/home/rooms.py，这份只是盘上镜像；"
    "对不上就报错，别手改这份。"
)


class RoomMiss(KeyError):
    """房名不在十间。回执要带最接近的那一间，让写入方自己挑。"""

    def __init__(self, name) -> None:
        self.name = name
        self.nearest = nearest(name if isinstance(name, str) else str(name))
        message = "房名不在十间：%r" % (name,)
        if self.nearest:
            message += "；最接近的是 %r" % (self.nearest,)
        KeyError.__init__(self, message)

    def __str__(self) -> str:
        return self.args[0]


class RoomsJsonError(Exception):
    """盘上镜像与这份房名表对不上。"""

    def __init__(self, code: str, detail=None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.detail)


def find(name: str) -> Room:
    """中文房名与 slug 都认；都不认就抛 RoomMiss。"""
    if isinstance(name, str):
        key = name.strip()
        if key in _BY_NAME:
            return _BY_NAME[key]
        if key.lower() in _BY_SLUG:
            return _BY_SLUG[key.lower()]
    raise RoomMiss(name)


def nearest(name: str) -> Optional[str]:
    """最接近的一间（中文房名）。给不出也得给一间，因为回执上要能点得开。"""
    if not isinstance(name, str) or not name.strip():
        return None
    key = name.strip()
    pool = list(ROOM_NAMES) + list(_BY_SLUG.keys())
    hits = difflib.get_close_matches(key.lower(), [item.lower() for item in pool], n=1, cutoff=0.0)
    if not hits:
        return None
    hit = hits[0]
    if hit in _BY_NAME:
        return hit
    return _BY_SLUG[hit].name


def room_dir(name: str, root=None) -> Path:
    """房目录用中文原名：房名与目录名一一对应，这是已定判据。"""
    room = find(name)
    return home_root(root) / room.name


def rooms_json_text() -> str:
    """镜像全文。房间顺序、slug、说明都照 ROOMS 生成，一个字段都不许另写。"""
    rows = []
    for room in ROOMS:
        rows.append({
            "name": room.name,
            "slug": room.slug,
            "purpose": room.purpose,
            "writable": room.writable_default,
        })
    return json.dumps({"note": ROOMS_JSON_NOTE, "rooms": rows}, ensure_ascii=False, indent=2) + "\n"


def write_rooms_json(root=None) -> Path:
    """重新生成镜像。只由人手触发（改完 ROOMS 之后），运行时不调它。"""
    path = Path(home_root(root)) / ROOMS_JSON_NAME
    atomic_write_text(path, rooms_json_text())
    return path


def seed_path() -> Path:
    """**出厂默认**那份盘上镜像：随包发、只读参数，不是真源。真源永远是这份代码（ROOMS）。"""
    return factory_path(ROOMS_JSON_NAME)


def read_seed() -> dict:
    """读出厂默认那份镜像，**仅供对照／补件**。它答的不是「她这台机器的房名表是什么样」。"""
    path = seed_path()
    if not path.exists():
        raise RoomsJsonError("ROOMS_JSON_MISSING", str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RoomsJsonError("ROOMS_JSON_BAD", str(exc))
    if not isinstance(data, dict):
        raise RoomsJsonError("ROOMS_JSON_BAD", "顶层不是对象")
    return data


def check_rooms_json(root=None) -> dict:
    """校验盘上镜像与真源一致；不一致就抛，不改盘。"""
    path = Path(home_root(root)) / ROOMS_JSON_NAME
    if not path.exists():
        raise RoomsJsonError("ROOMS_JSON_MISSING", str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RoomsJsonError("ROOMS_JSON_BAD", str(exc))
    if not isinstance(data, dict):
        raise RoomsJsonError("ROOMS_JSON_BAD", "顶层不是对象")
    expected = json.loads(rooms_json_text())["rooms"]
    got = data.get("rooms")
    if got != expected:
        drift = []
        got_rows = got if isinstance(got, list) else []
        for want, have in zip(expected, got_rows):
            if want != have:
                drift.append({"want": want, "got": have})
        if len(got_rows) != len(expected):
            drift.append({"want": len(expected), "got": len(got_rows)})
        raise RoomsJsonError("ROOMS_JSON_DRIFT", drift)
    return data


if __name__ == "__main__":  # pragma: no cover - 人手工重生成镜像
    print(write_rooms_json())
