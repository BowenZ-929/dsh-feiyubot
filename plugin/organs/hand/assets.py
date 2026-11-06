"""从家·仓库按名字取一张图：**只取指针**（path 与 sha256），一个字节都不复制。

仓库里能当图用的卡有两种：素材卡（`residents.ASSET_KIND`）与借阅条
（`residents.POINTER_KIND`）；名字按**原样**比——不归一化、不认「最接近的那一个」，
素材卡优先于借阅条，同种里卡序靠前的先看。

读的只有家的公开读口 `cards.list_room`（一页 200，翻到没有下一页为止），
房间与种类的名字仍从家取（不在手里抄第二份常量）。本模块不写盘、不上网。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from organs.home import cards as home_cards
from organs.home import residents as home_residents

from . import store

#: 一页读多少张：与家的读上限同量级，翻页由 truncated 决定。
PAGE = 200
#: 找图的顺序：素材卡优先于借阅条（素材是「我要用的图」，借阅条是「借来读的」）。
KIND_ORDER = (home_residents.ASSET_KIND, home_residents.POINTER_KIND)
EMPTY: Dict[str, Any] = {"found": False, "name": "", "kind": None, "card_id": None,
                         "path": None, "sha256": None, "bytes": None, "exists": False,
                         "reason": "NAME_NOT_IN_WAREHOUSE"}


def _items(root) -> List[Dict[str, Any]]:
    """仓库里全部可见的卡（折叠后）。翻不动就停：宁可少找，也不死循环。"""
    items: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = home_cards.list_room(root=store.home_root(root),
                                    room=home_residents.WAREHOUSE_ROOM,
                                    offset=offset, limit=PAGE)
        batch = list(page.get("items") or [])
        items.extend(batch)
        if not page.get("truncated") or not batch:
            return items
        offset += len(batch)


def _matches(items: List[Dict[str, Any]], wanted: str) -> List[Tuple[int, int, Dict[str, Any]]]:
    """同名的素材卡／借阅条，按（种类优先，卡序）排好。"""
    found: List[Tuple[int, int, Dict[str, Any]]] = []
    for order, kind in enumerate(KIND_ORDER):
        for index, item in enumerate(items):
            if item.get("kind") != kind:
                continue
            if str(item.get("name")) != wanted:
                continue
            found.append((order, index, item))
    found.sort(key=lambda row: (row[0], row[1]))
    return found


def find_asset(*, name: Any, root=None) -> Dict[str, Any]:
    """按名字找一张图 → 指针（路径、指纹、字节数、卡 id）与「文件还在不在」。

    找不到、名字是空的都回 `found=False` 并在 `reason` 说清是哪一种；
    卡在而文件不在回 `found=True, exists=False`（**这是两件事，不许折叠**）。
    """
    wanted = "" if name is None else str(name)
    if not wanted.strip():
        return dict(EMPTY, name=wanted, reason="EMPTY_NAME")
    rows = _matches(_items(root), wanted)
    if not rows:
        return dict(EMPTY, name=wanted, reason="NAME_NOT_IN_WAREHOUSE")

    card = rows[0][2]
    raw = card.get("path")
    path = Path(str(raw)) if raw else None
    exists = bool(path is not None and path.is_file())
    digest = card.get("sha256")
    if exists and not digest:
        try:
            digest = store.sha256_file(path)
        except OSError:
            digest = None
    return {"found": True, "name": wanted, "kind": card.get("kind"),
            "card_id": card.get("card_id"), "path": str(path) if path else None,
            "sha256": None if digest in (None, "") else str(digest),
            "bytes": card.get("bytes"), "exists": exists,
            "reason": None if exists else "FILE_MISSING"}
