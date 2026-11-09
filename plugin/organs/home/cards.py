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

# 这几种卡：物件（放着的）、痕迹（写下的经过）、名片（一人一卡）、借阅条（借来的一份语料）、
# 素材（画风与参考图）、自画像（画出来的那张）、提到（只在旧卡上加一句，不新建）、
# 偏好（一人一张：她想怎么对这个人说话）。
CARD_KINDS: Tuple[str, ...] = ("物件", "痕迹", "名片", "借阅条", "素材", "自画像", "提到", "消费",
                               "进度", "日记", "梦", "人设卡", "任务", "偏好",
                               "草稿", "说说", "梗")
#: 抽屉里的「格子」：这四个 kind 各自落一个子目录，各一份 index.jsonl。
#: 房名表不变（还是十间）——格子是**一间房里的抽屉分格**，不是新房间。
SHELF_KINDS: Tuple[str, ...] = ("自画像", "人设卡", "日记", "梦")
#: 只有抽屉分格。工作台上的「自画像」是画到一半的工作稿（另一件事），不分格、也不套抽屉那套严格形状。
SHELF_ROOM = access.DRAWER_ROOM
#: 她一天一张的那两类。
DATE_KINDS: Tuple[str, ...] = ("日记", "梦")
#: 列表里正文只带这么多字；全文去 read_item 取（一页才翻得完，一次也不会被几百篇日记撑爆）。
LIST_EXCERPT_CHARS = 120
DIARY_MAX_CHARS = 150

# 家里能当正文读的后缀；别的后缀（图、音视频）只认指纹与路径，不读正文。
TEXT_SUFFIXES: Tuple[str, ...] = (".md", ".jsonl")

MENTION_KIND = "提到"
CONSUME_KIND = "消费"
PROGRESS_KIND = "进度"
TASK_KIND = "任务"
#: 任务卡只落工作台：手头的事只在干活那张桌上，不进别的房（与日记只落抽屉同一条纪律）。
TASK_ROOM = "工作台"
#: 完成那一行的审计名字前缀。与「消费:」分开，免得完成任务偷吃这一间的拿取额度。
TASK_DONE_AUDIT_PREFIX = "任务完成:"

PREF_KIND = "偏好"
#: 偏好卡只落名片盒：它是**跟着人卡住**的一张卡（一人一张），不是第十一间房，也不进别处。
#: 它不走 index.jsonl（那人卡的规矩不破），落名片盒里独立的 prefs.jsonl——照 borrow.jsonl 的先例。
PREF_ROOM = "名片盒"
#: params 的键只有这四样。多一个键就是第二种「怎么说话」的写法，读的人得去猜哪个算数。
PREF_KEYS: Tuple[str, ...] = ("who", "chunks_per_reply", "max_chars", "at")
#: 这两个是旋钮：**只判类型不判范围**。范围（几个块一口、最多几个字）是手那边的判据。
PREF_INT_KEYS: Tuple[str, ...] = ("chunks_per_reply", "max_chars")

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
    for shelf, path in _index_paths(home, room_name):
        part, part_bad = _read_index(path)
        bad += part_bad
        for row in part:
            if isinstance(row, dict) and shelf is not None:
                row = dict(row, shelf=shelf)
            rows.append(row)
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
    params = card.get("params") or {}
    # current：草稿这类「同名即同一条」的现状卡（标记 used_in_diary 就是再写一行同名卡）
    return bool(params.get(LAST_WINS_PARAM) or params.get("current"))


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


def _check_due(value: str, *, kind: str) -> str:
    """截止时间只收 ISO8601 **带时区**的串（2026-11-06T12:00:00+08:00）。

    「什么时候到期」猜错就是把事误了，所以不带时区的串**不替它补一个时区**，直接拒。
    """
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise CardError("BAD_DUE", {"kind": kind, "due": value},
                        "%s 的 due 解析不了，要 2026-11-06T12:00:00+08:00 这样：%r"
                        % (kind, value)) from None
    offset = stamp.tzinfo.utcoffset(stamp) if stamp.tzinfo is not None else None
    if offset is None:
        raise CardError("BAD_DUE", {"kind": kind, "due": value, "reason": "no_timezone"},
                        "%s 的 due 没带时区，读不出是什么时候：%r" % (kind, value))
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
    params = card.get("params") or {}
    if params.get("current") and isinstance(card.get("name"), str) and card["name"]:
        # 现状卡按名字认同一条：改标记就是再写一行同名卡，读时取最新。
        keys.append(("current", room, card["name"]))
    return keys


def _need(params: Dict[str, Any], key: str, kind: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CardError("KIND_FIELD_MISSING", {"kind": kind, "field": key},
                        "%s 这张卡缺 %s（放在 params 里，不能空）" % (kind, key))
    return value


def _check_at(value: Any) -> str:
    """at 只收「能当 ISO8601 读」的字符串。读不出来 → BAD_AT，不猜、不替它补一个当前时间。"""
    text = value.strip() if isinstance(value, str) else ""
    if text:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            datetime.fromisoformat(iso)
        except ValueError:
            pass
        else:
            return text
    raise CardError("BAD_AT", {"at": value},
                    "at 要能当 ISO8601 读（可省）：%r" % (value,))


def _check_pref_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """偏好卡的字段校验：**只做结构与类型，不判取值范围**。

    为什么范围不在这里：几个块拼一口回、一条最多几个字，是**她怎么对这个人说话**的判据，长在
    手那边；家只保证这张卡立得住（键认得、类型对、时间读得出来）。范围写进家，等于手的判断
    多一个真源，改上限要改两处，两处早晚分叉。

    为什么 bool 特别点出来：bool 是 int 的子类，True 落盘就成了 1，读回来没人分得清
    「一个块」与「打开这个开关」。

    为什么没给的旋钮不进 params：**空卡 = 两个旋钮都没设过**，跟「设成 0」是两件事；
    补 0 就把「没设过」写成了一条决定。
    """
    extra = sorted(str(key) for key in params if key not in PREF_KEYS)
    if extra:
        raise CardError("BAD_KEY", {"keys": extra, "allowed": list(PREF_KEYS)},
                        "%s 的 params 只许这几样：%s；多出来的是：%s"
                        % (PREF_KIND, "、".join(PREF_KEYS), "、".join(extra)))
    for key in PREF_INT_KEYS:
        value = params.get(key)
        if value is None:              # 没给 = 这个旋钮没设过
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise CardError("BAD_VALUE", {"field": key, "value": value},
                            "%s 的 %s 只收 int（bool 也不算）：%r；"
                            "不合法不静默改成 0" % (PREF_KIND, key, value))
    params["who"] = _need(params, "who", PREF_KIND).strip()   # 谁这张卡是给他的
    if params.get("at") is not None:
        params["at"] = _check_at(params.get("at"))
    return params


def _check_kind_fields(raw: Dict[str, Any], kind: str, home: Path,
                       room_name: str) -> Dict[str, Any]:
    """按 kind 校验字段；返回补齐后的 params。

    日记与梦是**她写的**：一天一张、非空且最多150字；梦另带 `dream_of` 并标 `source_type=梦`。
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
        if count > DIARY_MAX_CHARS:
            raise CardError("KIND_TEXT_LENGTH",
                            {"kind": kind, "length": count,
                             "min": 1, "max": DIARY_MAX_CHARS},
                            "%s 最多 %d 字，这篇 %d 字"
                            % (kind, DIARY_MAX_CHARS, count))
        _need(params, "mood", kind)
        memories = params.get("from_memories")
        if memories is not None and (not isinstance(memories, list)
                                     or any(not isinstance(item, str) for item in memories)):
            raise CardError("KIND_FIELD_MISSING", {"kind": kind, "field": "from_memories"},
                            "%s 的 from_memories 要是 id 字符串数组（可空）" % kind)
        if kind == "梦":
            _need(params, "dream_of", kind)
            params["source_type"] = "梦"
        # 旧账本（from_old_home）不受「一天一张」约束：那是搬迁，不是她今天写的。
        if params.get("from_old_home"):
            return params
        # 一天一张靠幂等：同一天再写就拒，并说清「今天已经写过了」。
        # 只跟她**自己写的**那几篇比——旧账本同一天有好几篇是合法的，她当天再写一张也允许并存。
        for _shelf, path in _index_paths(home, room_name):
            rows, _bad = _read_index(path)
            for card in fold(rows):
                stored = card.get("params") or {}
                if card.get("kind") == kind and not card.get("consumed") \
                        and not stored.get("from_old_home") \
                        and str(stored.get("date")) == date:
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
    elif kind == TASK_KIND:
        # 任务卡：截止时间必填、done 必填且只收 bool、note 可选。**缺就报、错就报，不补默认值**——
        # 替她补一个 due 等于替她决定什么时候到期。
        if room_name != TASK_ROOM:
            raise CardError("KIND_WRONG_ROOM", {"kind": kind, "room": room_name},
                            "%s 只落%s：手头的事只在干活那张桌上" % (kind, TASK_ROOM))
        params["due"] = _check_due(_need(params, "due", kind), kind=kind)
        if "done" not in params:
            raise CardError("KIND_FIELD_MISSING", {"kind": kind, "field": "done"},
                            "%s 这张卡缺 done（true/false）" % kind)
        if not isinstance(params.get("done"), bool):
            raise CardError("BAD_DONE", {"kind": kind, "done": params.get("done")},
                            "%s 的 done 只收 true/false：%r" % (kind, params.get("done")))
        note = params.get("note")
        if note is not None and not isinstance(note, str):
            raise CardError("BAD_NOTE", {"kind": kind, "note": note},
                            "%s 的 note 只收字符串：%r" % (kind, note))
    elif kind == PREF_KIND:
        # 偏好卡只落名片盒。它跟着人卡住——一人一张，落名片盒里独立的 prefs.jsonl，
        # **不进 index.jsonl**（那间房的索引仍只放人卡）。字段规矩全在 _check_pref_params。
        if room_name != PREF_ROOM:
            raise CardError("KIND_WRONG_ROOM", {"kind": kind, "room": room_name},
                            "%s 只落%s（落 %s/prefs.jsonl，不进 index.jsonl）："
                            "一人一张的旋钮跟着人卡住" % (kind, PREF_ROOM, PREF_ROOM))
        params = _check_pref_params(params)
    return params


def _new_card(raw: Dict[str, Any], *, role: str, by: str, room_name: str, seq: int,
              progress: Any = _ABSENT, prefix: Optional[str] = None) -> Dict[str, Any]:
    card = {
        # 现状卡改标记时要保住 id（mark_note 追加一行同名卡，卡的 id 不许漂）。
        "card_id": raw.get("card_id") or "%s-%d" % (prefix or room_name, seq),
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

        if card.get("kind") == TASK_KIND:
            # done 是**折出来的**：创建那一行写着 false，完成那一行（params.ref 指着这张卡）在后面。
            card["done"] = bool(card["params"].get("done"))

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

        if card.get("kind") == TASK_KIND and isinstance(card["params"].get("ref"), str):
            # 完成也是一行事件（与消费、进度同一手法）：指向那张卡，折的时候把它标成 done。
            target = by_card.get(card["params"]["ref"])
            if target is None:
                if stats is not None:
                    stats["orphan_tasks"] = int(stats.get("orphan_tasks", 0)) + 1
                continue
            target["done"] = True
            target["done_at"] = card.get("at")
            target["done_by"] = card.get("by")
            target["done_count"] = int(target.get("done_count") or 0) + 1
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


SPACE_RULES_NAME = "space_rules.json"
NOTE_KINDS: Tuple[str, ...] = ("草稿", "说说", "梗")
NOTE_TEXT_MAX = 120


def space_rules_path(root=None) -> Path:
    """「能不能发」的词表与阈值：只在数据里，代码不写死。"""
    return store_module.home_root(root) / SPACE_RULES_NAME


def load_space_rules(root=None) -> Dict[str, Any]:
    """读取能否发空间的判据：缺件就报错（fail-closed），不默认放行。"""
    path = space_rules_path(root)
    if not path.exists():
        raise CardError("SPACE_RULES_MISSING", {"path": str(path)},
                        "发空间的判据文件不在：%s（没有判据就不给 space_ready=true）" % path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CardError("SPACE_RULES_BAD", {"path": str(path), "detail": str(exc)},
                        "发空间的判据文件读不出来：%s" % exc)
    if not isinstance(data, dict) or not isinstance(data.get("private_markers"), list):
        raise CardError("SPACE_RULES_BAD", {"path": str(path)},
                        "判据文件要有 private_markers 数组")
    return data


def judge_space_ready(text: str, rules: Dict[str, Any]) -> Dict[str, Any]:
    """三条：有具体的事 ／ 不涉私密 ／ 不评价别人。词表全部来自数据文件。"""
    text = text or ""
    reasons = []
    if len(text) < int(rules.get("min_chars", 0)):
        reasons.append("太短：没有具体的事")
    for marker in rules.get("private_markers") or []:
        if marker and marker in text:
            reasons.append("涉私密：命中 %r" % marker)
            break
    for marker in rules.get("judge_others_markers") or []:
        if marker and marker in text:
            reasons.append("评价别人：命中 %r" % marker)
            break
    return {"space_ready": not reasons, "reasons": reasons}


def add_note(root=None, *, role: str, by: str, text: str, source: str = "",
             mood: str = "", at: str = "") -> dict:
    """记一句今天的小事：落一张 kind=草稿。**只追加，不覆盖**。

    space_ready 由数据文件里的词表算，默认 false；
    家**不判「能不能发」**——发之前要人点头。
    """
    if not isinstance(text, str) or not text.strip():
        raise CardError("BAD_ARGUMENT", {"text": text}, "草稿要有一句话")
    if len(text) > NOTE_TEXT_MAX:
        raise CardError("NOTE_TEXT_LENGTH", {"length": len(text), "max": NOTE_TEXT_MAX},
                        "草稿最多 %d 字，这句 %d 字" % (NOTE_TEXT_MAX, len(text)))
    decision = judge_space_ready(text, load_space_rules(root))
    params = {"text": text, "source": source, "mood": mood,
              "space_ready": bool(decision["space_ready"]),
              "used_in_diary": False, "space_reasons": decision["reasons"],
              "current": True}
    stamp = at or _now()
    # 名字要唯一：同一天（甚至同一秒）写多张是正常事，靠时间戳当名字会被「同名即同一条」折掉。
    seq = _line_count(_index_path(store_module.home_root(root), "素材盒")) + 1
    out = append_cards(root=root, role=role, by=by, cards=[{
        "room": "素材盒", "kind": "草稿", "name": "草稿 %s #%d" % (stamp[:16], seq),
        "chars": len(text), "at": stamp, "params": params}])
    card = out["cards"][0]
    card["space_ready"] = params["space_ready"]
    card["space_reasons"] = decision["reasons"]
    return card


def mark_note(root=None, *, role: str, by: str, note_id: str, used_in_diary: bool = True) -> dict:
    """草稿被夜跑日记引用 → 追加一行同名卡（used_in_diary=true），**不删**。"""
    home = store_module.home_root(root)
    rows, _bad = _read_room_rows(home, "素材盒")
    target = _pick_target(fold(rows), card_id=note_id, name=None, room_name="素材盒")
    params = dict(target.get("params") or {})
    params["used_in_diary"] = bool(used_in_diary)
    out = append_cards(root=root, role=role, by=by, cards=[{
        "room": "素材盒", "kind": "草稿", "name": target.get("name"),
        "card_id": target.get("card_id"),
        "chars": int(target.get("chars") or 0), "params": params}])
    return out["cards"][0]


def notes(root=None, *, day: str) -> dict:
    """取某一天的草稿（夜跑写日记时用）。按卡上的 at 前 10 位对日。"""
    got = list_room(root=root, room="素材盒", limit=access.READ_MAX_LINES, excerpt_chars=0)
    items = [card for card in got["items"]
             if card.get("kind") == "草稿" and str(card.get("at") or "")[:10] == day]
    return {"day": day, "room": "素材盒", "total": len(items),
            "notes": items,
            "used": [card["card_id"] for card in items
                     if (card.get("params") or {}).get("used_in_diary")]}


BATCH_KIND = "批量入档"


def ingest_batch(root=None, *, role: str, by: str, batch: str, script: str = "",
                 snapshot_sha: str = "", cards_in: Optional[List[Dict[str, Any]]] = None,
                 note: str = "") -> dict:
    """**批量入档**：给旧账本用的合法入口。一个批次只落**一行**审计，但**不许没有行**。

    为什么不能走 append_cards：它一张卡一行审计，343 篇就是 343 行流水，把账淹了。
    这里反过来——先过门（整批按一批算），逐篇只落卡，最后落**一行汇总审计**：
    `kind=批量入档 / 批 / 篇数 / 快照sha / 脚本 / at`。少写行可以，没有行不行。

    内容形状只做最小校验（kind 在白名单、有名字、params 是对象）：
    旧账本的字数与日期不该按「她自己今天写的那张」那把尺子量。
    """
    if not isinstance(cards_in, list) or not cards_in:
        raise CardError("BAD_ARGUMENT", {"batch": batch}, "批量入档要给一个非空的 cards 列表")
    if not isinstance(batch, str) or not batch.strip():
        raise CardError("BAD_ARGUMENT", {"batch": batch}, "批量入档要给一个批次名（批）")
    home = store_module.home_root(root)

    prepared: List[Tuple[Dict[str, Any], str, str]] = []
    rooms_touched: Dict[str, int] = {}
    for raw in cards_in:
        if not isinstance(raw, dict):
            raise CardError("BAD_CARD", {"card": raw}, "卡必须是字典")
        kind = raw.get("kind")
        if kind not in CARD_KINDS:
            raise CardError("BAD_KIND", {"kind": kind}, "kind 只能是：%s" % "、".join(CARD_KINDS))
        room = _target_room(raw.get("room") or "")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise CardError("BAD_NAME", {"room": room.name}, "卡上必须有一个名字")
        params = raw.get("params") or {}
        if not isinstance(params, dict):
            raise CardError("BAD_CARD", {"params": params}, "params 要是对象")
        access.check_path_shape(room.name, name)
        rooms_touched[room.name] = rooms_touched.get(room.name, 0) + 1
        prepared.append((raw, room.name, kind))

    for room_name in rooms_touched:
        access.check_ingest(role=role, room=room_name, name="%s.jsonl" % batch)

    written: List[Dict[str, Any]] = []
    for raw, room_name, kind in prepared:
        shelf, path = _shelf_path(home, room_name, kind)
        store_module.ensure_dir(path.parent)
        card = _new_card(raw, role=role, by=by, room_name=room_name,
                         seq=_line_count(path) + 1, prefix=shelf)
        store_module.append_line(path, card)
        written.append(card)

    rooms = sorted(rooms_touched)
    entry = audit.append_entry(
        root=root, role=role, by=by, room=rooms[0] if len(rooms) == 1 else "、".join(rooms),
        name="%s:%s" % (BATCH_KIND, batch), chars=0, bytes=0,
        extra={"kind": BATCH_KIND, "批": batch, "篇数": len(written),
               "快照sha": snapshot_sha, "脚本": script, "note": note})
    return {"room": rooms, "batch": batch, "count": len(written),
            "cards": [{"card_id": card["card_id"], "shelf": card.get("shelf"),
                       "name": card["name"]} for card in written],
            "audit": entry}


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


def add_task(root=None, *, role: str, by: str, title: str, due: str, note: str = "",
             at: Optional[str] = None) -> dict:
    """往工作台放一张带截止时间的任务卡：过 append_cards 那两道门，落**一行**、记一行审计。

    校验走 _check_kind_fields 的 任务 分支，所以 due／done／note 的规矩只有那一处。
    """
    size = len(title) + len(note) if isinstance(title, str) and isinstance(note, str) else 0
    written = append_cards(root, role=role, by=by, cards=[
        {"room": TASK_ROOM, "kind": TASK_KIND, "name": title, "chars": size, "at": at,
         "params": {"due": due, "done": False, "note": note}}])
    return written["cards"][0]


def complete_task(root=None, *, role: str, by: str, card_id: str,
                  at: Optional[str] = None) -> dict:
    """把一张任务卡标成完成：往同一份流水**再追加一行**，读时折成 done=true。**盘上一个字不删。**

    set_progress／consume 的老手法：卡片本体不动，完成是流水里的一行事件（params.ref）。
    """
    if not isinstance(card_id, str) or not card_id.strip():
        raise CardError("BAD_ARGUMENT", {"card_id": card_id},
                        "完成任务要指定一张卡：给 card_id")
    # 门先于找件：与消费、改进度同一条纪律（否则等于允许从门外探测房间里有什么）。
    access.check_write(role=role, room=TASK_ROOM, name=card_id, chars=0, root=root)

    home = store_module.home_root(root)
    path = _index_path(home, TASK_ROOM)
    rows, _bad = _read_room_rows(home, TASK_ROOM)
    tasks = [card for card in fold(rows) if card.get("kind") == TASK_KIND]
    target = _pick_target(tasks, card_id=card_id, name=None, room_name=TASK_ROOM)

    store_module.ensure_dir(home / TASK_ROOM)
    event = _new_card({"kind": TASK_KIND, "name": target.get("name"), "at": at,
                       "params": {"ref": target.get("card_id"), "done": True}},
                      role=role, by=by, room_name=TASK_ROOM, seq=_line_count(path) + 1)
    store_module.append_line(path, event)
    entry = audit.append_entry(root=root, role=role, by=by, room=TASK_ROOM,
                               name="%s%s" % (TASK_DONE_AUDIT_PREFIX, event["name"]),
                               chars=0, bytes=0)

    after, _bad = _read_room_rows(home, TASK_ROOM)
    done = next((card for card in fold(after) if card.get("card_id") == card_id), None)
    if done is None:            # 行刚落下去必然折得回来；折不上是盘坏了，如实报，不拿旧卡充数。
        raise CardError("CARD_DISK_MISMATCH", {"room": TASK_ROOM, "card_id": card_id},
                        "完成行写下去了，读回来却折不上这张卡：%s" % card_id)
    return {"room": TASK_ROOM, "card": done, "event": event, "audit": entry}


def list_tasks(root=None, *, room: str = TASK_ROOM) -> list:
    """这一间里还没被拿走的任务卡（折叠后）。

    done／done_at／done_by 都是折出来的：流水只增，读时才成为「完成了」。
    """
    target = _target_room(room)
    home = store_module.home_root(root)
    rows, _bad = _read_room_rows(home, target.name)
    return [card for card in fold(rows)
            if card.get("kind") == TASK_KIND and not card.get("consumed")]


def _excerpt(card: Dict[str, Any], chars: int) -> Dict[str, Any]:
    """列表用的轻量视图：正文只带前 `chars` 字，全文去 read_item 取。

    不这么做就没法既「一页真能翻完」又「不把几百份日记一次塞回去」——一篇日记光正文就几百字，
    整条卡上千字，按旧写法一页只装得下六条。
    """
    if not chars or chars < 0:
        return card
    params = dict(card.get("params") or {})
    text = params.get("text")
    if isinstance(text, str) and len(text) > chars:
        params["text"] = text[:chars]
        params["text_truncated"] = True
    if isinstance(text, str):
        params["text_chars"] = len(text)
    view = dict(card)
    view["params"] = params
    return view


def list_room(root=None, *, room: str, offset: int = 0, limit: int = 200,
              include_consumed: bool = False, shelf: Optional[str] = None,
              excerpt_chars: int = LIST_EXCERPT_CHARS) -> dict:
    """读一间房的索引：**房级 + 各格子**合起来折叠，offset/limit 真能翻完。

    三个口径在这里定死，免得再出现「数得出 345、翻不出 345」：

    · `total` = **房内可见总数**（折叠后、按 `include_consumed` 过滤之后的卡数）；
    · `shelves` = **同一个可见集按格子分组的卡数**，与 `total` 严格对齐（加起来就是 total）；
    · `truncated` = **还有没翻到的卡**（`offset + len(items) < total`），最后一页一定是 false。

    分页只受 `limit`（上限 `access.READ_MAX_LINES`）约束，**不拿单次读取的字数上限当页上限**——
    那是 `check_read` 管「读一件东西」的尺子，用在枚举上就会把一页压到六条。
    正文按 `excerpt_chars` 截断并标 `text_truncated`，全文去 read_item 取。
    """
    target = _target_room(room)
    home = store_module.home_root(root)
    rows, bad = _read_room_rows(home, target.name)
    stats: Dict[str, Any] = {}
    items = fold(rows, stats=stats)
    if shelf:
        items = [card for card in items if (card.get("shelf") or target.name) == shelf]
    consumed = sum(1 for card in items if card.get("consumed"))
    if not include_consumed:
        items = [card for card in items if not card.get("consumed")]

    offset = max(0, int(offset))
    limit = max(0, int(limit))
    capped = min(limit, access.READ_MAX_LINES)
    shown = [_excerpt(card, excerpt_chars) for card in items[offset:offset + capped]]
    chars = sum(len(json.dumps(card, ensure_ascii=False)) for card in shown)

    shelves: Dict[str, int] = {}
    for card in items:
        name = card.get("shelf") or target.name
        shelves[name] = shelves.get(name, 0) + 1

    return {"room": target.name, "slug": target.slug, "offset": offset, "limit": limit,
            "shelf": shelf,
            "total": len(items), "lines": len(shown), "chars": chars,
            "truncated": offset + len(shown) < len(items), "bad_lines": bad, "items": shown,
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


def read_item(root=None, *, room: str, name: str,
              shelf: Optional[str] = None) -> dict:
    """读一件东西：先找到卡，再交正文。**会进格子**——抽屉里的日记与人设卡都在各自的格子里。

    只给 (room, name) 时在整间房（房级 + 各格子）里找；给 shelf 就只翻那一格。
    正文优先取卡上的 `params.text`（旧账本搬来的日记正文就存在卡里，没有外部文件），
    没有才回落到 `path` 指向的外部文件并重验 sha／字节。
    """
    target = _target_room(room)
    home = store_module.home_root(root)
    if shelf:
        path = home / target.name / shelf / INDEX_NAME
        if not path.exists():
            raise CardError("SHELF_NOT_FOUND", {"room": target.name, "shelf": shelf},
                            "%s 里没有这个格子：%r" % (target.name, shelf))
        rows, bad = _read_index(path)
        items = fold([dict(row, shelf=shelf) if isinstance(row, dict) else row
                      for row in rows])
    else:
        rows, bad = _read_room_rows(home, target.name)
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

    base = {"card": card, "room": target.name, "shelf": card.get("shelf"),
            "bad_lines": bad}
    inline = (card.get("params") or {}).get("text")
    if isinstance(inline, str) and inline:
        # 正文就在卡上（旧账本搬来的日记）。这不是「编内容」——它是这张卡自己的正文。
        lines = inline.splitlines()
        body = "\n".join(lines[:access.READ_MAX_LINES])
        truncated = len(lines) > access.READ_MAX_LINES or len(body) > access.READ_MAX_CHARS
        if len(body) > access.READ_MAX_CHARS:
            body = body[:access.READ_MAX_CHARS]
        base.update({"path": card.get("path"), "body": body, "external": False,
                     "verified": True, "truncated": truncated,
                     "lines": len(lines), "chars": len(body)})
        return base
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
