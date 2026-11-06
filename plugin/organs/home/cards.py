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
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import access, audit, rooms
from . import store as store_module

INDEX_NAME = "index.jsonl"

# 七种卡：物件（放着的）、痕迹（写下的经过）、名片（一人一卡）、借阅条（借来的一份语料）、
# 素材（画风与参考图）、自画像（画出来的那张）、提到（只在旧卡上加一句，不新建）。
CARD_KINDS: Tuple[str, ...] = ("物件", "痕迹", "名片", "借阅条", "素材", "自画像", "提到", "消费",
                               "进度", "日记", "梦", "人设卡")
#: 抽屉里的「格子」：这四个 kind 各自落一个子目录，各一份 index.jsonl。
#: 房名表不变（还是十间）——格子是**一间房里的抽屉分格**，不是新房间。
SHELF_KINDS: Tuple[str, ...] = ("自画像", "人设卡", "日记", "梦")
#: 只有抽屉分格。工作台上的「自画像」是画到一半的工作稿（另一件事），不分格、也不套抽屉那套严格形状。
SHELF_ROOM = access.DRAWER_ROOM
#: 她一天一张的那两类。
DATE_KINDS: Tuple[str, ...] = ("日记", "梦")
DIARY_MIN_CHARS = 80
DIARY_MAX_CHARS = 150

# 家里能当正文读的后缀；别的后缀（图、音视频）只认指纹与路径，不读正文。
TEXT_SUFFIXES: Tuple[str, ...] = (".md", ".jsonl")

MENTION_KIND = "提到"
CONSUME_KIND = "消费"
PROGRESS_KIND = "进度"

#: 进度是 **0-100 的整数**，不是文本、不是任意数。只选一套：可校验、可排序、面板能一眼读到「到哪一步」；
#: 文字说明已经在 `note` 里，再开一套自由文本就是第二个真源。
#: `None` 有确定含义：**显式「没有进度」**（把卡片上那一栏删掉），读回来就是 None，不许补成 0。
PROGRESS_MIN = 0
PROGRESS_MAX = 100
_ABSENT = object()          # 「入参里根本没这个键」，与「显式给了 None」是两件事


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


def _shelf_path(home: Path, room_name: str, kind: str) -> Tuple[Optional[str], Path]:
    """这个 kind 落哪一格：四类各一格（抽屉/自画像/…），其余落房级索引。"""
    shelf = kind if (kind in SHELF_KINDS and room_name == SHELF_ROOM) else None
    if shelf:
        return shelf, home / room_name / shelf / INDEX_NAME
    return None, home / room_name / INDEX_NAME


def _index_paths(home: Path, room_name: str) -> List[Tuple[Optional[str], Path]]:
    """一间房的全部索引：房级一份 + 每个有卡的格子各一份（抽屉就是这样装四类的）。

    只列盘上真有的；一份都没有时回到房级路径，读出来是 0 件——不是"读不到"。
    """
    base = home / room_name
    found: List[Tuple[Optional[str], Path]] = []
    room_index = base / INDEX_NAME
    if room_index.exists():
        found.append((None, room_index))
    if base.is_dir():
        for child in sorted(p for p in base.iterdir() if p.is_dir()):
            path = child / INDEX_NAME
            if path.exists():
                found.append((child.name, path))
    if not found:
        found.append((None, room_index))
    return found


def _index_path(home: Path, room_name: str) -> Path:
    return home / room_name / INDEX_NAME


def _line_count(path: Path) -> int:
    return store_module.line_count(path) if path.exists() else 0


def _read_room_rows(home: Path, room_name: str) -> Tuple[List[Dict[str, Any]], int]:
    """一间房的全部行：房级 + 各格子合起来。折叠、挑件、查重都吃这一份。"""
    rows: List[Dict[str, Any]] = []
    bad = 0
    for _shelf, path in _index_paths(home, room_name):
        part, part_bad = _read_index(path)
        bad += part_bad
        rows.extend(part)
    return rows, bad


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


def _check_progress(value: Any, *, where: str) -> Optional[int]:
    """进度只收 0-100 的整数或 None。**不把 None 折成 0**：「没有进度」与「进度 0」是两件事——
    一个是这栏没填，一个是填了、还在起点。折起来之后面板上就分不出「还没开始」和「没登记」。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CardError("BAD_PROGRESS", {"where": where, "progress": value},
                        "progress 只收 0-100 的整数，或 null 表示没有进度：%r" % (value,))
    if value < PROGRESS_MIN or value > PROGRESS_MAX:
        raise CardError("PROGRESS_OUT_OF_RANGE",
                        {"where": where, "progress": value,
                         "min": PROGRESS_MIN, "max": PROGRESS_MAX},
                        "progress 要在 %d-%d 之间：%r" % (PROGRESS_MIN, PROGRESS_MAX, value))
    return value


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


def _need(params: Dict[str, Any], key: str, kind: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CardError("KIND_FIELD_MISSING", {"kind": kind, "field": key},
                        "%s 这张卡缺 %s（放在 params 里，不能空）" % (kind, key))
    return value


def _check_kind_fields(raw: Dict[str, Any], kind: str, home: Path,
                       room_name: str) -> Dict[str, Any]:
    """按 kind 校验字段；返回补齐后的 params。

    日记与梦是**她写的**：一天一张、80–150 字；梦另带 `dream_of` 并标 `source_type=梦`。
    自画像只登记指针与指纹（不搬本体）；人设卡是参考卡，接口上要能一眼看出它不进提示词。
    """
    params = dict(raw.get("params") or {})
    strict = room_name == SHELF_ROOM
    if kind in DATE_KINDS and not strict:
        raise CardError("KIND_WRONG_ROOM", {"kind": kind, "room": room_name},
                        "%s 只落抽屉：它是她自己写的，不进别的房" % kind)
    if kind in DATE_KINDS:
        date = _need(params, "date", kind)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            raise CardError("BAD_DATE", {"kind": kind, "date": date},
                            "%s 的 date 要 YYYY-MM-DD：%r" % (kind, date))
        text = _need(params, "text", kind)
        count = len(text)
        if count < DIARY_MIN_CHARS or count > DIARY_MAX_CHARS:
            raise CardError("KIND_TEXT_LENGTH",
                            {"kind": kind, "length": count,
                             "min": DIARY_MIN_CHARS, "max": DIARY_MAX_CHARS},
                            "%s 要 %d–%d 字，这篇 %d 字"
                            % (kind, DIARY_MIN_CHARS, DIARY_MAX_CHARS, count))
        _need(params, "mood", kind)
        memories = params.get("from_memories")
        if memories is not None and (not isinstance(memories, list)
                                     or any(not isinstance(item, str) for item in memories)):
            raise CardError("KIND_FIELD_MISSING", {"kind": kind, "field": "from_memories"},
                            "%s 的 from_memories 要是 id 字符串数组（可空）" % kind)
        if kind == "梦":
            _need(params, "dream_of", kind)
            params["source_type"] = "梦"
        # 一天一张靠幂等：同一天再写就拒，并说清「今天已经写过了」。
        for _shelf, path in _index_paths(home, room_name):
            rows, _bad = _read_index(path)
            for card in fold(rows):
                if card.get("kind") == kind and not card.get("consumed") \
                        and str((card.get("params") or {}).get("date")) == date:
                    raise CardError("DATE_ALREADY_WRITTEN",
                                    {"kind": kind, "date": date,
                                     "card_id": card.get("card_id")},
                                    "%s %s 今天已经写过了（一天一张）" % (kind, date))
    elif kind == "自画像" and strict:
        # 抽屉那套严格形状：只登记指针与指纹，不搬本体。工作台上的工作稿不走这条。
        image = _need(params, "image_path", kind)
        digest = _need(params, "sha256", kind)
        size = params.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise CardError("KIND_FIELD_MISSING", {"kind": kind, "field": "bytes"},
                            "自画像要记 bytes（正整数）")
        target = Path(image)
        if not target.is_file():
            raise CardError("CARD_DISK_MISMATCH", {"kind": kind, "image_path": image},
                            "自画像那张图不在盘上：%s" % image)
        actual = store_module.sha256_file(target)
        if actual != digest:
            raise CardError("CARD_DISK_MISMATCH",
                            {"kind": kind, "card_sha256": digest, "actual_sha256": actual},
                            "自画像的 sha256 对不上：卡上 %s… / 盘上 %s…"
                            % (digest[:12], actual[:12]))
    elif kind == "人设卡" and strict and not params:
        raise CardError("KIND_FIELD_MISSING", {"kind": kind},
                        "人设卡要带上它的参考内容（params 不能空）")
    return params


def _new_card(raw: Dict[str, Any], *, role: str, by: str, room_name: str, seq: int,
              progress: Any = _ABSENT, prefix: Optional[str] = None) -> Dict[str, Any]:
    card = {
        "card_id": "%s-%d" % (prefix or room_name, seq),
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
    if progress is not _ABSENT:
        card["progress"] = progress          # None 也写进去：显式「没有进度」要留痕
    if card["kind"] == "梦":
        # 梦只是谈资，不是证词：读出来带 testimony=false，显示时加「（梦）」前缀。
        card["testimony"] = False
        card["display"] = "（梦）" + str((card.get("params") or {}).get("text") or "")
    if card["kind"] == "人设卡":
        # 参考卡：读得到、面板能看，但接口上一眼看出它不进提示词。
        card["in_prompt"] = False
    return card


def append_cards(root=None, *, role: str, by: str, cards: list) -> dict:
    """整批登记：先全检、再全部落、每张卡各追一行审计。

    先全检后落，是为了「超 20 张整批拒、不部分落」：检查阶段任何一张不过就直接抛，
    此时盘上一行都没写。上限本身在 access（MAX_FILES_PER_WRITE），本文件不再抄一遍，
    只把 files=len(cards) 交给 check_write 判。
    """
    home = store_module.home_root(root)
    if not cards:
        return {"cards": [], "audit": []}

    prepared: List[Tuple[Dict[str, Any], str, int, Any, str]] = []
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
        progress = _ABSENT
        if "progress" in raw:
            progress = _check_progress(raw.get("progress"), where="card")
        if kind in DATE_KINDS:
            # 日记/梦的"多大"就是正文长度：门要拿它判单次上限，所以先算出来再进门。
            text = (raw.get("params") or {}).get("text")
            if isinstance(text, str):
                chars = len(text)
        access.check_path_shape(room.name, name)
        access.check_write(role=role, room=room.name, name=name, chars=chars,
                           files=len(cards), root=root, kind=kind)
        raw = dict(raw, params=_check_kind_fields(raw, kind, home, room.name))
        prepared.append((raw, room.name, chars, progress, kind))

    written: List[Dict[str, Any]] = []
    entries: List[Any] = []
    for raw, room_name, chars, progress, kind in prepared:
        shelf, path = _shelf_path(home, room_name, kind)
        store_module.ensure_dir(path.parent)
        card = _new_card(raw, role=role, by=by, room_name=room_name,
                         seq=_line_count(path) + 1, progress=progress, prefix=shelf)
        store_module.append_line(path, card)
        entries.append(audit.append_entry(root=root, role=role, by=by, room=room_name,
                                          name=card["name"], chars=card["chars"],
                                          bytes=card["bytes"]))
        written.append(card)
    return {"cards": written, "audit": entries}


def fold(rows: list, *, stats: Optional[Dict[str, Any]] = None) -> list:
    """把「一行一张卡」的流水折成「一件东西一张卡」，重复的只加 mentions、被消费的标 consumed。

    **只追加不变**：消费也在流水里追加一行（`kind="消费"`，`params.ref` 指向被拿走那张卡的
    card_id），折的时候把目标标成 `consumed=True / left=0`。盘上一个字都不删——「少了」是读出来的
    结论，不是擦掉一行。理由与痕迹、审计同源：只增不改才追得回「谁什么时候拿走的」。

    `stats` 是个可选出参：孤儿消费（目标不在）与重复消费条数塞进去，由 list_room 如实报出来。
    读不到就报，不把「折不上」悄悄吞掉。
    """
    items: List[Dict[str, Any]] = []
    by_card: Dict[str, Dict[str, Any]] = {}
    by_key: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        card = dict(row)
        card["params"] = dict(card.get("params") or {})
        card["mentions"] = 0
        card["consumed"] = False
        card["left"] = 1

        if card.get("kind") == PROGRESS_KIND:
            target = _event_target(card, items, by_card, by_key)
            if target is None:
                if stats is not None:
                    stats["orphan_progress"] = int(stats.get("orphan_progress", 0)) + 1
                continue
            try:
                value = _check_progress(card["params"].get("progress"), where="event")
            except CardError:
                # 读路径不许因为盘上一行写坏就整间读不出来：如实计数，不降级成 0。
                if stats is not None:
                    stats["bad_progress"] = int(stats.get("bad_progress", 0)) + 1
                continue
            if value is None:
                target.pop("progress", None)      # 显式「没有进度」：不留残值、也不补 0
            else:
                target["progress"] = value
            target["progress_at"] = card.get("at")
            target["progress_by"] = card.get("by")
            target["progress_count"] = int(target.get("progress_count") or 0) + 1
            continue

        if card.get("kind") == CONSUME_KIND:
            target = _event_target(card, items, by_card, by_key)
            if target is None:
                if stats is not None:
                    stats["orphans"] = int(stats.get("orphans", 0)) + 1
                continue
            if target.get("consumed"):
                if stats is not None:
                    stats["repeats"] = int(stats.get("repeats", 0)) + 1
                continue
            target["consumed"] = True
            target["left"] = 0
            target["consumed_at"] = card.get("at")
            target["consumed_by"] = card.get("by")
            target["consume_reason"] = card["params"].get("reason") or card.get("note") or ""
            continue

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


def _event_target(card: Dict[str, Any], items: List[Dict[str, Any]],
                    by_card: Dict[str, Dict[str, Any]],
                    by_key: Dict[Tuple[Any, ...], Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """事件行（消费／进度）指向哪一件：先认 `params.ref`（card_id），退到同名且还没被拿走的那一件。

    同名多件时**不猜**——返回 None 交上去当孤儿消费报出来，比随便拿一件更诚实。
    """
    ref = card["params"].get("ref")
    target = by_card.get(str(ref)) if isinstance(ref, str) else None
    if target is not None:
        return target
    for key in _identity_keys(card):
        target = by_key.get(key)
        if target is not None:
            return target
    name = card.get("name")
    if not isinstance(name, str) or not name:
        return None
    matches = [item for item in items
               if item.get("name") == name and not item.get("consumed")]
    return matches[0] if len(matches) == 1 else None


def _pick_target(folded: List[Dict[str, Any]], *, card_id: Optional[str],
                 name: Optional[str], room_name: str) -> Dict[str, Any]:
    """从折好的卡片里挑出「就是这一件」。挑不准就响亮报，**不猜**。

    消费与改进度共用这一套：同一间房里「哪一件」的道理只有一个，
    两处各写一遍就迟早一处给候选、另一处随便挑一件。
    """
    if card_id is not None:
        every = [item for item in folded if item.get("card_id") == card_id]
        wanted: Any = card_id
    else:
        every = [item for item in folded if item.get("name") == name]
        wanted = name
    live = [item for item in every if not item.get("consumed")]
    if not live:
        # 「没有这一件」与「这一件已经被拿走了」是两件事：分开报，不然人会一直找一件拿不走的东西。
        if every:
            gone = every[0]
            raise CardError("CARD_ALREADY_CONSUMED",
                            {"room": room_name, "card_id": gone.get("card_id"),
                             "name": gone.get("name"), "consumed_at": gone.get("consumed_at"),
                             "consumed_by": gone.get("consumed_by"),
                             "reason": gone.get("consume_reason")},
                            "这件已经被拿走了（%s，由 %s）：%s"
                            % (gone.get("consumed_at"), gone.get("consumed_by"), wanted))
        raise CardError("CARD_NOT_FOUND",
                        {"room": room_name, "card_id": card_id, "name": name},
                        "这间里没有这一件：%s" % wanted)
    if len(live) > 1:
        raise CardError("CARD_AMBIGUOUS",
                        {"room": room_name, "name": name,
                         "candidates": [item.get("card_id") for item in live]},
                        "同名的不止一件，得给 card_id 才拿得准：%s" % name)
    return live[0]


def set_progress(root=None, *, role: str, by: str, room: str, progress: Any,
                 card_id: Optional[str] = None, name: Optional[str] = None,
                 reason: str = "") -> dict:
    """给她手上的那件事改进度：过两道门、追加一行 `进度` 事件、记一行审计。**卡片本体不动。**

    进度不是状态、也不是新房间：它挂在那一张卡上（`docs/modules/home.md` 的 home-objects）。
    只追加的老规矩照旧——改一次进度是往流水里写一行，读时 fold 把最新那行的值贴回卡上。
    `progress=None` 是**显式「没有进度」**：把那一栏删掉，读回来就是没有，不补 0。
    """
    target_room = _target_room(room)
    if (card_id is None) == (name is None):
        raise CardError("BAD_ARGUMENT", {"card_id": card_id, "name": name},
                        "改进度要指定一件：给 card_id 或给 name，二选一")
    if not isinstance(reason, str):
        raise CardError("BAD_ARGUMENT", {"reason": reason}, "reason 要是字符串")
    value = _check_progress(progress, where="set")

    # 门先于找件：与消费同一条纪律（否则等于允许从门外探测房间里有什么）。
    access.check_write(role=role, room=target_room.name,
                       name=str(name if name is not None else card_id), chars=len(reason),
                       root=root)
    home = store_module.home_root(root)
    path = _index_path(home, target_room.name)
    rows, _bad = _read_room_rows(home, target_room.name)
    target = _pick_target(fold(rows), card_id=card_id, name=name, room_name=target_room.name)
    access.check_path_shape(target_room.name, str(target.get("name")))

    store_module.ensure_dir(home / target_room.name)
    card = _new_card({"kind": PROGRESS_KIND, "name": target.get("name"), "note": reason,
                      "chars": len(reason),
                      "params": {"ref": target.get("card_id"), "progress": value,
                                 "reason": reason}},
                     role=role, by=by, room_name=target_room.name,
                     seq=_line_count(path) + 1)
    store_module.append_line(path, card)
    entry = audit.append_entry(root=root, role=role, by=by, room=target_room.name,
                               name="%s:%s" % (PROGRESS_KIND, card["name"]),
                               chars=len(reason), bytes=0)
    return {"room": target_room.name, "card": card, "target": target,
            "progress": value, "audit": entry}


def consume(root=None, *, role: str, by: str, room: str, card_id: Optional[str] = None,
            name: Optional[str] = None, reason: str = "") -> dict:
    """她拿走一件东西：过两道门、追加一行消费、记一行审计。**盘上一行都不删。**

    房名、门、路径形状与写入是同一套（access.check_consume）——消费也是动这间房，
    不是绕过门的一条捷径。只多了半件都做不到的事：挑出到底拿走哪一件。
    """
    target_room = _target_room(room)
    if (card_id is None) == (name is None):
        raise CardError("BAD_ARGUMENT",
                        {"card_id": card_id, "name": name},
                        "消费要指定一件：给 card_id 或给 name，二选一")
    if not isinstance(reason, str):
        raise CardError("BAD_ARGUMENT", {"reason": reason}, "reason 要是字符串")

    # 门先于找件：反过来就成了「从门外探测房间里有没有那件东西」——房名/门不对时该立刻拒，
    # 而不是先翻一遍索引再拿查询结果回话。挑件用的标识（card_id 或名字）先过路径形状。
    access.check_consume(role=role, room=target_room.name,
                         name=str(name if name is not None else card_id), root=root)

    home = store_module.home_root(root)
    path = _index_path(home, target_room.name)
    rows, _bad = _read_room_rows(home, target_room.name)
    folded = fold(rows)
    target = _pick_target(folded, card_id=card_id, name=name, room_name=target_room.name)

    access.check_path_shape(target_room.name, str(target.get("name")))
    store_module.ensure_dir(home / target_room.name)
    card = _new_card({"kind": CONSUME_KIND, "name": target.get("name"),
                      "note": reason, "chars": len(reason),
                      "params": {"ref": target.get("card_id"), "reason": reason}},
                     role=role, by=by, room_name=target_room.name,
                     seq=_line_count(path) + 1)
    store_module.append_line(path, card)
    entry = audit.append_entry(root=root, role=role, by=by, room=target_room.name,
                               name="%s%s" % (access.CONSUME_AUDIT_PREFIX, card["name"]),
                               chars=len(reason), bytes=0)
    return {"room": target_room.name, "card": card, "target": target,
            "left": 0, "audit": entry}


def list_room(root=None, *, room: str, offset: int = 0, limit: int = 200,
              include_consumed: bool = False) -> dict:
    """读一间房的索引：offset/limit 分页，再按 access 的读上限截断，截了就说截了。

    房名与 root 一样只能按关键字传：冻结契约里那句 `list_room(root=None, room: str, *)`
    在 Python 里是语法错误（有默认值的参数后面跟着没默认值的参数），所以 room 落进
    `*` 之后——名字与顺序照旧，只是不能按位置传。
    """
    target = _target_room(room)
    home = store_module.home_root(root)
    rows, bad = _read_room_rows(home, target.name)
    stats: Dict[str, Any] = {}
    items = fold(rows, stats=stats)
    shelves: Dict[str, int] = {}
    for shelf, path in _index_paths(home, target.name):
        count = _line_count(path)
        if count:
            shelves[shelf or target.name] = count
    consumed = sum(1 for card in items if card.get("consumed"))
    if not include_consumed:
        items = [card for card in items if not card.get("consumed")]

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
            "truncated": truncated, "bad_lines": bad, "items": shown,
            "shelves": shelves,
            "consumed": consumed,
            "orphan_consumes": int(stats.get("orphans", 0)),
            "repeat_consumes": int(stats.get("repeats", 0)),
            "orphan_progress": int(stats.get("orphan_progress", 0)),
            "bad_progress": int(stats.get("bad_progress", 0))}


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
