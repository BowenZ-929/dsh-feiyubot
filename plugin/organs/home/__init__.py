"""家：身外之物放十间房。房名表、落盘底座、两道门、审计、索引卡、三样特殊住户。

六个文件各管一段，职责不重叠：rooms 定房名，store 管落盘，access 管门与上限，
audit 管只增的账，cards 管索引卡，residents 管抽屉／借阅条／仓库指针这类特殊住户。
门只在一处（access.check_write），写盘只在一处（store.append_line）——
本包之外（api 层、别的器官）一律调它们，不许自己再判一次写权。
"""
from __future__ import annotations

from . import access
from . import anchors
from . import audit
from . import cards
from . import residents
from . import rooms
from . import store
from .access import (AccessDenied, Whitelist, check_path_shape, check_read, check_write,
                     load_whitelist)
from .anchors import (AnchorError, append_anchors, latest_by_scene, read_all as read_anchors,
                       read_seed, seed_path)
from .audit import AuditShrunk, Entry, append_entry, read_tail, verify
from .cards import CardError, append_cards, fold, list_room, read_item
from .residents import (borrow_ledger, read_drawer, read_learned, register_artwork,
                        register_asset, register_borrow, seed_mouth_pointers,
                        warehouse_pointers)
from .rooms import (DEFAULT_WRITABLE, READONLY_ROOMS, ROOM_NAMES, ROOMS, Room, RoomMiss, find,
                    nearest, room_dir)
from .store import atomic_write_text, append_line, encode_name, home_root, sha256_file

__all__ = [
    "access", "anchors", "audit", "cards", "residents", "rooms", "store",
    "AnchorError", "append_anchors", "latest_by_scene", "read_anchors", "read_seed", "seed_path",
    "AccessDenied", "Whitelist", "check_path_shape", "check_read", "check_write",
    "load_whitelist",
    "AuditShrunk", "Entry", "append_entry", "read_tail", "verify",
    "CardError", "append_cards", "fold", "list_room", "read_item",
    "borrow_ledger", "read_drawer", "read_learned", "register_artwork", "register_asset",
    "register_borrow", "seed_mouth_pointers", "warehouse_pointers",
    "DEFAULT_WRITABLE", "READONLY_ROOMS", "ROOM_NAMES", "ROOMS", "Room", "RoomMiss", "find",
    "nearest", "room_dir",
    "atomic_write_text", "append_line", "encode_name", "home_root", "sha256_file",
]
