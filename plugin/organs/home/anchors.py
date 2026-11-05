"""家·仓库的台词卡：人给她定的口头禅，一行一张，只追加。

**真源是实盘的那一份**（`store.home_root()` 下的 `仓库/anchors.jsonl`），别处不许留副本——
嘴注入时按需读，取用即走。

包内 `data/仓库/anchors.jsonl` 是**出厂默认（seed 用）**，不是真源：读「她现在说什么」只经
`anchors_path()`。`anchors_path()` 是构造参数 > `HOME_ROOT` > 包内 `data/` 三级解析的产物，
所以干净检出时两条路会指向同一个文件——那是兜底，不是定义。**出厂那份改了不影响实盘**；
要证明这一点，验的时候必须给一个显式的实盘根。
判据：`soul-manner`「不写具体话术、不写口癖清单」，`architecture.md` §399 台词库归家·仓库。
所以它不进魂，也不许被复制进任何配置或文档。

**为什么不并进 index.jsonl**：话术是一批语料，指认不成「一件东西」（同 `docs/plan.md:89`
判借阅条出 `home-objects` 的那条理由）；而且两者读法相反——物件按身份去重、重存只加
`mentions`，台词按 `scene` 取最新一条。混进一个 fold 会互相污染，且「仓库有几件东西」
从此答不准。文件仍在这间房内，不新开房间（`docs/modules/home.md:155`）。

**body 只许写她自己的身体**（尾巴／鳍／脸／眼），出现人称对象就拒：写别人的动作＝编事实，
与 `docs/plan.md` §3.1 第 4 条配套那条「只能写她自己、不许生成关于主人的事实」同源。
判据不是词表而是这条：body 里根本不该出现第二个人。

写盘仍只走两处：`access.check_write` 判门与上限（一处都不另判），`store.append_line` 落行，
`audit.append_entry` 记账。本文件不碰任何文件权限判定。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import access, audit
from . import store as home_store

ANCHORS_ROOM = "仓库"
ANCHORS_FILE = "anchors.jsonl"
ANCHOR_FIELDS: Tuple[str, ...] = ("say", "body", "scene", "who", "why", "ts")

# body 里出现这些就说明写到别人身上了。不用动词表是因为它必漏；人称对象是这句话的成分，
# 写自己的身体描述根本用不到它们。
BODY_PERSON_MARKERS: Tuple[str, ...] = ("你", "您", "他", "她", "主人", "大家", "别人")

CODE_BAD = "BAD_ARGUMENT"
CODE_NO_SELF = "BODY_NOT_SELF"


class AnchorError(Exception):
    """一张台词卡不合格。code 给机器，message 给人，detail 带上出问题的那一句。"""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        self.code = code
        self.message = message
        self.detail = dict(detail or {})
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def anchors_path(root=None):
    """**实盘**那份台词库在哪：一律经 `store.home_root()` 解析，本函数不认包内那条路。

    名字固定，不许按 scene 分文件——那样「一份」就成了十份。
    读路径只许走这里（以及由它派生的读取函数），谁都不许硬编码 `plugin/.../data/...`。
    """
    return home_store.home_root(root) / ANCHORS_ROOM / ANCHORS_FILE


def seed_path():
    """**出厂默认**那份：随包发的 seed 料，不是真源。

    它只在「把这五句装进一台新机器」时被读一次。实盘里已经有台词时它一点用都没有，
    改了它也不会改到任何一位她——所以读「她现在说什么」永远不许走这条路。
    """
    return home_store.factory_path(Path(ANCHORS_ROOM) / ANCHORS_FILE)


def read_seed() -> Tuple[List[Dict[str, Any]], int]:
    """读出厂默认那份，**仅供 seed**。返回值与 `read_all` 同形，但它答的不是「她现在说什么」。"""
    return home_store.read_lines(seed_path())


def check_body(body: str) -> None:
    """body 只许写她自己。命中人称对象即拒，不猜、不改写、不降级成空。"""
    for marker in BODY_PERSON_MARKERS:
        if marker in body:
            raise AnchorError(CODE_NO_SELF,
                              "body 只许写她自己的身体动作，不许写对别人的动作（命中 %r）：%s"
                              % (marker, body),
                              {"marker": marker, "body": body})


def _text(raw: Any, label: str, *, required: bool) -> str:
    if raw is None:
        if required:
            raise AnchorError(CODE_BAD, "台词卡缺 %s" % label, {"missing": label})
        return ""
    if not isinstance(raw, str):
        raise AnchorError(CODE_BAD, "台词卡的 %s 要是字符串" % label, {"field": label, "got": type(raw).__name__})
    if required and not raw.strip():
        raise AnchorError(CODE_BAD, "台词卡的 %s 不许空白" % label, {"field": label})
    return raw


def _normalise(raw: Any, *, who: str) -> Dict[str, Any]:
    """一张卡从入参变成本仓里的形状。**say 与 body 一个字都不改**——它们是主人逐句定的原文。"""
    if not isinstance(raw, dict):
        raise AnchorError(CODE_BAD, "一张台词卡要是一个对象", {"got": type(raw).__name__})
    extra = sorted(set(raw) - set(ANCHOR_FIELDS))
    if extra:
        raise AnchorError(CODE_BAD, "台词卡有不认识的字段：%s" % "、".join(extra), {"extra": extra})
    say = _text(raw.get("say"), "say", required=True)
    body = _text(raw.get("body"), "body", required=False)
    scene = _text(raw.get("scene"), "scene", required=True)
    check_body(body)
    return {
        "say": say,
        "body": body,
        "scene": scene,
        "who": _text(raw.get("who"), "who", required=False) or who,
        "why": _text(raw.get("why"), "why", required=False),
        "ts": _text(raw.get("ts"), "ts", required=False) or _now(),
    }


def append_anchors(root=None, *, role: str, by: str, anchors: List[Dict[str, Any]]) -> Dict[str, Any]:
    """整批登记台词卡：先全检后落，一张不合格整批拒（不部分落）。

    门与上限由 access 一处判：房间固定是仓库，文件名固定是 anchors.jsonl，
    所以「台词能不能写」与「仓库能不能写」是同一个答案，不另开一道门。
    """
    if not isinstance(anchors, list) or not anchors:
        raise AnchorError(CODE_BAD, "要给一张以上的台词卡；空批等于什么都没登记", {})
    prepared = [_normalise(raw, who=by) for raw in anchors]
    chars = sum(len(card["say"]) + len(card["body"]) for card in prepared)
    access.check_write(role=role, room=ANCHORS_ROOM, name=ANCHORS_FILE,
                       chars=chars, files=len(prepared), root=root)

    path = anchors_path(root)
    home_store.ensure_dir(path.parent)
    written: List[Dict[str, Any]] = []
    entries: List[audit.Entry] = []
    for card in prepared:
        home_store.append_line(path, card)
        written.append(card)
        entries.append(audit.append_entry(
            root=root, role=role, by=card["who"], room=ANCHORS_ROOM,
            name="%s#%s" % (ANCHORS_FILE, card["scene"]),
            chars=len(card["say"]) + len(card["body"]),
            bytes=len(card["say"].encode("utf-8")) + len(card["body"].encode("utf-8"))))
    return {"room": ANCHORS_ROOM, "file": str(path), "count": len(written),
            "anchors": written, "audit": entries}


def latest_by_scene(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 scene 取最新一条：**追加顺序在后者赢**，不比时间戳。

    只追加的实现里，盘上最后写下的那句就是她此刻该说的那句；ts 是人给的，可错、可重复，
    拿它当权威等于让写错的时间戳覆盖掉新台词。
    """
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        scene = str(row.get("scene") or "").strip()
        if not scene:
            continue
        latest[scene] = row
    return sorted(latest.values(), key=lambda row: (str(row.get("ts") or ""), row["scene"]))


def read_all(root=None, *, offset: int = 0, limit: int = 200) -> Dict[str, Any]:
    """读回台词库：现行台词（每 scene 一条）+ 盘上有几行 + 坏了几行。

    文件不在不是错误——那只是还没有人给她定过台词（与抽屉不同：抽屉该有那张卡，
    缺件要报未接入）。所以这里如实分开报 file_exists 与 lines，不把「没有」说成「0 条台词」。
    """
    path = anchors_path(root)
    rows, bad = home_store.read_lines(path)
    current = latest_by_scene(rows)
    access.check_read(room=ANCHORS_ROOM, name=ANCHORS_FILE,
                      lines=min(len(current), access.READ_MAX_LINES),
                      chars=min(sum(len(r.get("say") or "") + len(r.get("body") or "")
                                    for r in current), access.READ_MAX_CHARS))
    window = current[offset:offset + limit]
    return {"room": ANCHORS_ROOM, "file": str(path), "file_exists": path.exists(),
            "lines": len(rows), "bad": bad, "total": len(current),
            "truncated": offset + limit < len(current),
            "anchors": window}


def count(root=None, room: str = ANCHORS_ROOM) -> int:
    """这间房里有几条现行台词。只有仓库会是非零；别间返回 0（不建空文件）。"""
    if room != ANCHORS_ROOM:
        return 0
    rows, _bad = home_store.read_lines(anchors_path(root))
    return len(latest_by_scene(rows))
