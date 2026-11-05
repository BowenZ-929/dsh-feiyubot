"""家·索引卡：每间房一个 index.jsonl，一行一张卡，只追加。

一张卡只回答「这件东西是什么、谁给的、放在哪」：房名、种类、名字、外面那张原件的路径、
sha256、字节数、一句话说明。**家里不放二进制本体**——path 为空就是「纯说明卡」，
read_item 这时如实回 body=None、external=True，绝不编一段内容出来。

写权限**不在这里判**：check_path_shape 与 check_write 都在 access 里，本文件只是按顺序
把它们调一遍。多一处在别处再判一次写权，就是第二处真源，早晚跟 access 分叉。

重存不新建卡：fold 在**读时**折叠——同一件东西（(room, sha256) 相同，或
(room, path, name) 相同且 path 非空）第二次出现只给旧卡 mentions +1；
另有一种显式行 kind="提到"，params["ref"] 指向已有 card_id，折叠时也只加 mentions。
所以 index.jsonl 里行数会增加（append-only 的要求），但**卡数不变**。

读的时候再验一遍、不信卡：文件名要等于路径最后一段、后缀在白名单（.md/.jsonl 或卡上
记下的原件后缀）、不是符号链接、bytes 与 sha256 跟卡上两栏相等。任一不符即
CardError(code="CARD_DISK_MISMATCH")，消息里说「卡与盘对不上」，不猜。
"""
from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import access, audit, rooms
from . import store as store_module

INDEX_NAME = "index.jsonl"

# 七种卡：物件（放着的）、痕迹（写下的经过）、名片（一人一卡）、借阅条（借来的一份语料）、
# 素材（画风与参考图）、自画像（画出来的那张）、提到（只在旧卡上加一句，不新建）。
CARD_KINDS: Tuple[str, ...] = ("物件", "痕迹", "名片", "借阅条", "素材", "自画像", "提到")

# 家里能当正文读的后缀；别的后缀（图、音视频）只认指纹与路径，不读正文。
TEXT_SUFFIXES: Tuple[str, ...] = (".md", ".jsonl")

MENTION_KIND = "提到"


class CardError(Exception):
    """卡本身或卡与盘的关系有问题：code 说明哪一类，detail 带定位用的字段。"""

    def __init__(self, code: str, detail: Optional[Dict[str, Any]] = None, message: str = "") -> None:
        self.code = code
        self.detail = dict(detail or {})
        self.message = message or code
        super().__init__(self.message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _target_room(name: Any) -> rooms.Room:
    """房名或 slug 都能认；不在十间里抛 CardError，并把最接近的一间带在 detail 里。"""
    try:
        return rooms.find(str(name))
    except rooms.RoomMiss as exc:
        nearest = getattr(exc, "nearest", None)
        raise CardError("ROOM_UNKNOWN", {"name": str(name), "nearest": nearest},
                        "房名不在十间里：%r（最接近的一间是 %r）" % (name, nearest)) from exc


def _index_path(home: Path, room_name: str) -> Path:
    return home / room_name / INDEX_NAME


def _line_count(path: Path) -> int:
    return store_module.line_count(path) if path.exists() else 0


def _read_index(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    return store_module.read_lines(path)


#: 打了这个 params 标记的卡是「现状卡」：同件后追加的那一行顶掉前一行，而不是并成 mentions。
LAST_WINS_PARAM = "pointer"


def _last_wins(card: Dict[str, Any]) -> bool:
    """这张卡是不是「只追加、读时取最新」的那种（指针卡）。

    指针卡记的是「盘上那份现在是什么指纹」，旧行是历史。若把新行当成「又被提到一次」
    并进旧行，读回的就永远是第一次登记的那个 sha——文件明明改对了也报对不上，
    只能靠人翻流水自己找。口径与台词卡（`anchors`）一致：**按追加顺序取最新，不比 ts**
    ——`ts` 是人给的，可错、可重复，拿它当权威等于让写错的时间戳盖掉后写的那行。
    """
    return bool((card.get("params") or {}).get(LAST_WINS_PARAM))


def _replace_with_later(items: List[Dict[str, Any]], by_card: Dict[str, Dict[str, Any]],
                        by_key: Dict[Tuple[Any, ...], Dict[str, Any]],
                        existing: Dict[str, Any], card: Dict[str, Any]) -> None:
    """用后追加的那一行顶掉旧行的内容；位置不动（房内顺序不跳），mentions 累加。"""
    merged = dict(card)
    merged["mentions"] = int(existing.get("mentions") or 0) + 1
    index = next((i for i, item in enumerate(items) if item is existing), None)
    if index is None:            # 理论上不可达：existing 一定来自 items
        items.append(merged)
    else:
        items[index] = merged
    for source in (existing, merged):
        if source.get("card_id"):
            by_card[str(source["card_id"])] = merged
        for key in _identity_keys(source):
            by_key[key] = merged


def _identity_keys(card: Dict[str, Any]) -> List[Tuple[Any, ...]]:
    """判同件：sha256 相同算同一件；没有 sha256 时退到 (房、路径、名字)。"""
    keys: List[Tuple[Any, ...]] = []
    room = card.get("room")
    sha = card.get("sha256")
    if sha:
        keys.append(("sha256", room, sha))
    path = card.get("path")
    if path:
        keys.append(("path", room, path, card.get("name")))
    return keys


def _new_card(raw: Dict[str, Any], *, role: str, by: str, room_name: str, seq: int) -> Dict[str, Any]:
    return {
        "card_id": "%s-%d" % (room_name, seq),
        "room": room_name,
        "kind": raw["kind"],
        "name": raw["name"],
        "path": raw.get("path") or None,
        "sha256": raw.get("sha256") or None,
        "bytes": int(raw.get("bytes") or 0),
        "chars": int(raw.get("chars") or 0),
        "note": raw.get("note") or "",
        "by": raw.get("by") or by,
        "role": raw.get("role") or role,
        "at": raw.get("at") or _now(),
        "params": dict(raw.get("params") or {}),
    }


def append_cards(root=None, *, role: str, by: str, cards: list) -> dict:
    """整批登记：先全检、再全部落、每张卡各追一行审计。

    先全检后落，是为了「超 20 张整批拒、不部分落」：检查阶段任何一张不过就直接抛，
    此时盘上一行都没写。上限本身在 access（MAX_FILES_PER_WRITE），本文件不再抄一遍，
    只把 files=len(cards) 交给 check_write 判。
    """
    home = store_module.home_root(root)
    if not cards:
        return {"cards": [], "audit": []}

    prepared: List[Tuple[Dict[str, Any], str, int]] = []
    for raw in cards:
        if not isinstance(raw, dict):
            raise CardError("BAD_CARD", {"card": raw}, "卡必须是字典")
        kind = raw.get("kind")
        if kind not in CARD_KINDS:
            raise CardError("BAD_KIND", {"kind": kind, "kinds": list(CARD_KINDS)},
                            "kind 只能是这七种：%s" % "、".join(CARD_KINDS))
        room = _target_room(raw.get("room") or "")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise CardError("BAD_NAME", {"room": room.name, "name": name}, "卡上必须有一个名字")
        chars = raw.get("chars")
        chars = 0 if chars is None else chars
        if not isinstance(chars, int) or isinstance(chars, bool) or chars < 0:
            raise CardError("BAD_CHARS", {"chars": chars}, "chars 必须是 >=0 的整数")
        access.check_path_shape(room.name, name)
        access.check_write(role=role, room=room.name, name=name, chars=chars,
                           files=len(cards), root=root)
        prepared.append((raw, room.name, chars))

    written: List[Dict[str, Any]] = []
    entries: List[Any] = []
    for raw, room_name, chars in prepared:
        store_module.ensure_dir(home / room_name)
        path = _index_path(home, room_name)
        card = _new_card(raw, role=role, by=by, room_name=room_name, seq=_line_count(path) + 1)
        store_module.append_line(path, card)
        entries.append(audit.append_entry(root=root, role=role, by=by, room=room_name,
                                          name=card["name"], chars=card["chars"],
                                          bytes=card["bytes"]))
        written.append(card)
    return {"cards": written, "audit": entries}


def fold(rows: list) -> list:
    """把「一行一张卡」的流水折成「一件东西一张卡」，重复的只加 mentions。"""
    items: List[Dict[str, Any]] = []
    by_card: Dict[str, Dict[str, Any]] = {}
    by_key: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        card = dict(row)
        card["params"] = dict(card.get("params") or {})
        card["mentions"] = 0

        if card.get("kind") == MENTION_KIND:
            ref = card["params"].get("ref") or card.get("card_id")
            target = by_card.get(ref) if isinstance(ref, str) else None
            for key in ([] if target is not None else _identity_keys(card)):
                target = by_key.get(key)
                if target is not None:
                    break
            if target is not None:
                target["mentions"] += 1
            continue

        existing = None
        for key in _identity_keys(card):
            existing = by_key.get(key)
            if existing is not None:
                break
        if existing is not None:
            if _last_wins(card):
                _replace_with_later(items, by_card, by_key, existing, card)
            else:
                existing["mentions"] += 1
            continue

        items.append(card)
        if card.get("card_id"):
            by_card[str(card["card_id"])] = card
        for key in _identity_keys(card):
            by_key[key] = card
    return items


def list_room(root=None, *, room: str, offset: int = 0, limit: int = 200) -> dict:
    """读一间房的索引：offset/limit 分页，再按 access 的读上限截断，截了就说截了。

    房名与 root 一样只能按关键字传：冻结契约里那句 `list_room(root=None, room: str, *)`
    在 Python 里是语法错误（有默认值的参数后面跟着没默认值的参数），所以 room 落进
    `*` 之后——名字与顺序照旧，只是不能按位置传。
    """
    target = _target_room(room)
    home = store_module.home_root(root)
    rows, bad = _read_index(_index_path(home, target.name))
    items = fold(rows)

    offset = max(0, int(offset))
    limit = max(0, int(limit))
    capped = min(limit, access.READ_MAX_LINES)
    shown: List[Dict[str, Any]] = []
    chars = 0
    truncated = False
    for card in items[offset:]:
        if len(shown) >= capped:
            truncated = True
            break
        cost = len(json.dumps(card, ensure_ascii=False))
        if chars + cost > access.READ_MAX_CHARS:
            truncated = True
            break
        shown.append(card)
        chars += cost
    if offset + len(shown) < len(items):
        truncated = True

    return {"room": target.name, "slug": target.slug, "offset": offset, "limit": limit,
            "total": len(items), "lines": len(shown), "chars": chars,
            "truncated": truncated, "bad_lines": bad, "items": shown}


def _allowed_suffixes(card: Dict[str, Any]) -> set:
    allowed = set(TEXT_SUFFIXES)
    for source in (card.get("name"), card["params"].get("suffix")):
        if isinstance(source, str):
            suffix = Path(source).suffix.lower()
            if suffix:
                allowed.add(suffix)
    return allowed


def _mismatch(card: Dict[str, Any], reason: str) -> CardError:
    return CardError("CARD_DISK_MISMATCH",
                     {"card_id": card.get("card_id"), "room": card.get("room"),
                      "name": card.get("name"), "path": card.get("path"), "reason": reason},
                     "卡与盘对不上（%s）：%s" % (card.get("card_id"), reason))


def _verify(card: Dict[str, Any], file_path: Path) -> str:
    """验一遍再读：路径形状、后缀、非符号链接、字节数、sha256。返回后缀。"""
    raw = str(card.get("path") or "")
    segments = [piece for piece in raw.replace("\\", "/").split("/") if piece not in ("", ".")]
    if ".." in segments:
        raise _mismatch(card, "路径里有上级目录符号")
    if not segments:
        raise _mismatch(card, "路径里没有文件名")
    if segments[-1] != file_path.name:
        raise _mismatch(card, "文件名 %r 不等于路径最后一段 %r" % (file_path.name, segments[-1]))

    suffix = file_path.suffix.lower()
    allowed = _allowed_suffixes(card)
    if suffix not in allowed:
        raise _mismatch(card, "后缀 %r 不在白名单内（%s）" % (suffix, "、".join(sorted(allowed))))

    try:
        info = file_path.lstat()
    except OSError:
        raise _mismatch(card, "盘上没有这个文件") from None
    if stat.S_ISLNK(info.st_mode):
        raise _mismatch(card, "是符号链接，不认")
    if not stat.S_ISREG(info.st_mode):
        raise _mismatch(card, "不是普通文件")

    expected_bytes = int(card.get("bytes") or 0)
    if info.st_size != expected_bytes:
        raise _mismatch(card, "字节数对不上：盘 %d，卡 %d" % (info.st_size, expected_bytes))
    digest = store_module.sha256_file(file_path)
    if digest != (card.get("sha256") or ""):
        raise _mismatch(card, "sha256 对不上：盘 %s，卡 %s" % (digest, card.get("sha256")))
    return suffix


def read_item(root=None, *, room: str, name: str) -> dict:
    """读一件东西：先找到卡，再对着盘重验一遍，验过了才交正文。"""
    target = _target_room(room)
    home = store_module.home_root(root)
    rows, bad = _read_index(_index_path(home, target.name))
    items = fold(rows)

    card = None
    for item in items:
        if item.get("card_id") == name:
            card = item
            break
    if card is None:
        for item in items:
            if item.get("name") == name:
                card = item
                break
    if card is None:
        raise CardError("CARD_NOT_FOUND", {"room": target.name, "name": name},
                        "%s 里没有这张卡：%r" % (target.name, name))

    base = {"card": card, "room": target.name, "bad_lines": bad}
    if not card.get("path"):
        base.update({"path": None, "body": None, "external": True, "verified": False,
                     "truncated": False, "lines": 0, "chars": 0})
        return base

    file_path = Path(str(card["path"]))
    suffix = _verify(card, file_path)
    if suffix not in TEXT_SUFFIXES:
        base.update({"path": str(file_path), "body": None, "external": True, "verified": True,
                     "truncated": False, "lines": 0, "chars": 0})
        return base

    text = file_path.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    body = "\n".join(all_lines[:access.READ_MAX_LINES])
    truncated = len(all_lines) > access.READ_MAX_LINES
    if len(body) > access.READ_MAX_CHARS:
        body = body[:access.READ_MAX_CHARS]
        truncated = True
    access.check_read(room=target.name, name=file_path.name,
                      lines=len(body.splitlines()), chars=len(body))
    base.update({"path": str(file_path), "body": body, "external": False, "verified": True,
                 "truncated": truncated, "lines": len(body.splitlines()), "chars": len(body)})
    return base
