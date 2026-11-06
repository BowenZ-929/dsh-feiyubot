"""取图的范围由家的房表与格子表决定：仓库看指针卡，抽屉看各格的卡，再看格目录里的文件。

三条判据写死在这里：

1. **房名与格子名一律从家 import**（`residents.WAREHOUSE_ROOM`、`cards.SHELF_ROOM`、
   `cards.SHELF_KINDS`）——手里不抄第二份中文常量，家改表这里跟着变。
2. 取图**按五步走**，名字一律**原样比**（不归一化、不认「最接近的那一个」）：
   ① 仓库的卡：素材卡优先于借阅条，同种里卡序靠前的先看（既有行为，不改）；
   ② 抽屉各格的卡（按 `SHELF_KINDS` 的顺序），图路径认 `path` 或 `params.image_path`；
   ③ 名字命中**格子名**（`SHELF_KINDS` 里的数据值，如「自画像」）→ 取该格里**最新一张有真图的**
      （卡序最后）；该格没有卡就看目录里实际存在的图片文件；
   ④ 抽屉各格目录里的**文件名**原样比（没登记成卡、文件却在的情况）；
   ⑤ 都没有 → `found=False`，`reason` 分成 `NAME_NOT_IN_HOME` 与 `FILE_MISSING` 两种，**不并成一句**。
3. **只取指针**：返回的 `path` 就是**原文件路径**，一个字节都不复制（不 copy、不落临时文件），
   `sha256` 现算自那个文件；卡在而文件不在是 `found=True, exists=False`（两件事，不折叠）。

图片后缀白名单写在手里（后缀是文件名的事，不是房名）；房名与格子名一个字都不在这里写。
读的只有家的公开读口 `cards.list_room`（一页 200，翻到没有下一页为止）。本模块不写盘、不上网。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from organs.home import cards as home_cards
from organs.home import residents as home_residents
from organs.home import rooms as home_rooms

from . import store

#: 一页读多少张：与家的读上限同量级，翻页由 truncated 决定。
PAGE = 200
#: 找图的顺序：素材卡优先于借阅条（素材是「我要用的图」，借阅条是「借来读的」）。
KIND_ORDER = (home_residents.ASSET_KIND, home_residents.POINTER_KIND)
#: 能当图发出去的后缀。它只是**文件名**的后缀，不是房名，所以写在手里。
IMAGE_SUFFIXES: Tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".gif")

#: picked_reason 的全部取值：「这张图是怎么找到的」，是证据，不是散文。
WAREHOUSE_ASSET = "warehouse_asset"
WAREHOUSE_POINTER = "warehouse_pointer"
DRAWER_CARD = "drawer_card"
SHELF_LATEST = "shelf_latest"
DRAWER_FILE = "drawer_file"

EMPTY: Dict[str, Any] = {"found": False, "name": "", "kind": None, "card_id": None,
                         "path": None, "sha256": None, "bytes": None, "exists": False,
                         "reason": "NAME_NOT_IN_HOME", "picked_reason": None, "room": None}


def _items(root, room: str) -> List[Dict[str, Any]]:
    """一间房里全部可见的卡（折叠后）。翻不动就停：宁可少找，也不死循环。"""
    items: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = home_cards.list_room(root=store.home_root(root), room=room,
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


def _shelves(items: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """抽屉的卡按格子分好：只认家格子表里那几格，别的（如旧账本的「日记-恋爱」）当没看见。"""
    grouped: Dict[str, List[Dict[str, Any]]] = {kind: [] for kind in home_cards.SHELF_KINDS}
    for item in items:
        bag = grouped.get(str(item.get("shelf") or ""))
        if bag is not None:
            bag.append(item)
    return grouped


def _shelf_dir(root, kind: str) -> Path:
    """一格配一个目录：抽屉/<格子名>。房名取自家的房表，格子名取自家的格子表。

    根要先换算成**家的**根（`store.home_root`）再交给 `rooms.room_dir`：手给的 root 是手自己的
    目录，不换算就会在手的盘上找一个叫「抽屉」的目录——那种错只会静默地找不到图。
    """
    return home_rooms.room_dir(home_cards.SHELF_ROOM, store.home_root(root)) / kind


def _image_path(card: Dict[str, Any]) -> Optional[Path]:
    """卡上那张图在哪：认 `path`，也认 `params.image_path`（家两种写法都用过，都不许漏）。"""
    params = card.get("params") or {}
    for raw in (card.get("path"), params.get("image_path")):
        if isinstance(raw, str) and raw.strip():
            return Path(raw)
    return None


def _recorded_sha(card: Dict[str, Any]) -> Optional[str]:
    params = card.get("params") or {}
    for value in (card.get("sha256"), params.get("sha256")):
        if isinstance(value, str) and value.strip():
            return value
    return None


def _recorded_bytes(card: Dict[str, Any]) -> Optional[int]:
    """卡上记的字节数：家的抽屉卡写在 params.bytes 里，顶层的 bytes 是 0（占位，不是 0 字节）。"""
    params = card.get("params") or {}
    for value in (card.get("bytes"), params.get("bytes")):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _from_card(wanted: str, card: Dict[str, Any], room: str, picked: str) -> Dict[str, Any]:
    """卡的命中：路径照卡上那个原样给，指纹**现算自那个文件**（卡上那份只在文件没了时才用）。"""
    path = _image_path(card)
    exists = bool(path is not None and path.is_file())
    digest: Optional[str] = None
    size = _recorded_bytes(card)
    if exists:
        try:
            digest = store.sha256_file(path)
            size = path.stat().st_size
        except OSError:
            exists = False            # 读不出来等于拿不到：别拿卡上那份冒充现算
    return {"found": True, "name": wanted, "kind": card.get("kind"),
            "card_id": card.get("card_id"), "path": str(path) if path else None,
            "sha256": digest if digest is not None else _recorded_sha(card),
            "bytes": size, "exists": exists,
            "reason": None if exists else "FILE_MISSING",
            "picked_reason": picked, "room": room}


def _from_file(wanted: str, path: Path, picked: str) -> Dict[str, Any]:
    """目录里的文件命中（没登记成卡的那种）：只报它自己的路径与指纹。"""
    try:
        return {"found": True, "name": wanted, "kind": None, "card_id": None,
                "path": str(path), "sha256": store.sha256_file(path),
                "bytes": path.stat().st_size, "exists": True, "reason": None,
                "picked_reason": picked, "room": home_cards.SHELF_ROOM}
    except OSError:
        return dict(EMPTY, name=wanted, reason="FILE_MISSING")


def _newest_image(directory: Path) -> Optional[Path]:
    """这一格目录里最新的那张图（按 mtime，同刻按名字）——「该格没有卡」时的兜底。"""
    try:
        files = [path for path in directory.iterdir()
                 if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES]
        if not files:
            return None
        files.sort(key=lambda path: (path.stat().st_mtime, path.name))
    except OSError:
        return None
    return files[-1]


def _plain_name(wanted: str) -> bool:
    """能直接拼进目录去找的文件名：带分隔符或上级目录的一律不拼（这不是路径，是名字）。"""
    return bool(wanted) and wanted not in (".", "..") \
        and "/" not in wanted and "\\" not in wanted


def _latest_in_shelf(root, kind: str) -> Optional[Dict[str, Any]]:
    """③ 该格「最新一张有真图的」：先看卡（卡序最后），再退到目录里实际存在的图片文件。"""
    for card in reversed(_shelves(_items(root, home_cards.SHELF_ROOM))[kind]):
        path = _image_path(card)
        if path is not None and path.is_file():
            return {"card": card, "path": path}
    newest = _newest_image(_shelf_dir(root, kind))
    if newest is not None:
        return {"card": None, "path": newest}
    return None


def find_asset(*, name: Any, root=None) -> Dict[str, Any]:
    """按名字找一张图 → 指针（路径、指纹、字节数、卡 id）与「文件还在不在」。

    取图范围由家的房表与格子表决定，不由手里的房名列表决定（见模块头那五步）。
    找不到、名字是空的都回 `found=False` 并在 `reason` 说清是哪一种；
    卡在而文件不在回 `found=True, exists=False`（**这是两件事，不许折叠**）。
    """
    wanted = "" if name is None else str(name)
    if not wanted.strip():
        return dict(EMPTY, name=wanted, reason="EMPTY_NAME")

    rows = _matches(_items(root, home_residents.WAREHOUSE_ROOM), wanted)
    if rows:
        card = rows[0][2]
        picked = (WAREHOUSE_ASSET if card.get("kind") == home_residents.ASSET_KIND
                  else WAREHOUSE_POINTER)
        return _from_card(wanted, card, home_residents.WAREHOUSE_ROOM, picked)

    shelves = _shelves(_items(root, home_cards.SHELF_ROOM))
    for kind in home_cards.SHELF_KINDS:
        for card in shelves[kind]:
            if str(card.get("name")) == wanted:
                return _from_card(wanted, card, home_cards.SHELF_ROOM, DRAWER_CARD)

    if wanted in shelves:
        hit = _latest_in_shelf(root, wanted)
        if hit is None:
            return dict(EMPTY, name=wanted, reason="FILE_MISSING")
        card = hit["card"]
        if card is not None:
            return _from_card(wanted, card, home_cards.SHELF_ROOM, SHELF_LATEST)
        return _from_file(wanted, hit["path"], SHELF_LATEST)

    if _plain_name(wanted):
        for kind in home_cards.SHELF_KINDS:
            candidate = _shelf_dir(root, kind) / wanted
            if candidate.is_file() and candidate.suffix.lower() in IMAGE_SUFFIXES:
                return _from_file(wanted, candidate, DRAWER_FILE)
    return dict(EMPTY, name=wanted, reason="NAME_NOT_IN_HOME")
