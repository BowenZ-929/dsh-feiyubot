"""家·三样特殊住户：抽屉里的资料卡、名片盒里的借阅条、仓库里的嘴指针卡，外加自画像。

三样各落一间，**不串**：

1. 她的资料卡（吃/怕/以前的事）→ **抽屉**。抽屉是只读房，程序写不进去，只有人直接在盘上
   改文件；read_drawer 每次调用都重读盘、不校验、不缓存。缺件就如实回 status="未接入"
   加模板路径，**不回空正文、不编内容、不报 0 字**。本文件里没有任何一条往抽屉写的分支。
2. 咬不准的「她认得多少词」这类读数 → 借阅条，落在**名片盒**里：borrow.jsonl 一条一借，
   读数单列在 借阅读数.json（人也能改）。缺读数就 learned=None + readout_status="未接入"，
   **不报 0**——答不出和答出零是两件事。为什么不新开一间「档案」：档案是模块名，物理只落
   名片盒与信箱这两间（modules/home.md:22「名片盒与信箱，一人一卡、一信一件」）；借阅条自己
   是一张**卡片**（plan.md:89「档案里的第三类卡片」），卡片归名片盒；信箱装的是来往的长文、
   一封一件，把卡片塞信箱等于把卡片当信件。房名不变、房间不新增，只是这间房里多一个卡片种类。
3. 嘴的条款与称呼表 → **仓库**里的一张张指针卡（kind="借阅条"）：只记名字、现路径、
   sha256、bytes 与对齐口径，**不搬文件本体**。为什么用「借阅条」而不是「素材」：这三份不是
   她自己攒的素材，是嘴借去用的外部原件（家只登记指针与指纹）；「素材」这一格留给画她的
   画风与参考图（register_asset），两者混在一个 kind 里，将来「仓库里有几张素材」就答不准。
   对齐口径（依据 plugin/organs/mouth/warehouse.py:3-7）：**名字、.json 后缀、顶层是一份
   键值表**，这三件不变；嘴把 MOUTH_DATA_DIR 指进家·仓库只是换根目录，不换格式。
4. 自画像与画她的素材：画出来的那张图 → **工作台**（记谁画的 by、什么参数 params）；
   画风／参考图 → **仓库**当素材卡。**「她长什么样」的定性描述不在家**（在魂的 identity.json
   的 appearance）：本文件的入参里没有、也不认 appearance／look／长相 这类键，传了即
   CardError(code="NOT_HOME_BUSINESS")——长相有唯一真源，在第二个地方再写一遍就分叉了。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import access, audit, cards, store

DRAWER_ROOM = "抽屉"
DRAWER_CARD = "她的资料卡.md"
DRAWER_TEMPLATE = "organs/home/templates/drawer-card.md"

BORROW_ROOM = "名片盒"
BORROW_FILE = "borrow.jsonl"
READOUT_FILE = "借阅读数.json"

DESK_ROOM = "工作台"
WAREHOUSE_ROOM = "仓库"

POINTER_KIND = "借阅条"
ASSET_KIND = "素材"
ARTWORK_KIND = "自画像"

MOUTH_FILES: Tuple[str, ...] = ("clauses.json", "address_table.json", "policy.json")
MOUTH_ENV = "MOUTH_DATA_DIR"
ALIGNED_KEYS = "名字不动、.json 后缀不动、顶层仍是一份键值表"

# 长相的定性描述不在家：这几个键只要出现就整条拒，家不为它留字段、也不留默认值。
FORBIDDEN_KEYS: Tuple[str, ...] = ("appearance", "look", "长相", "外貌", "外形", "模样")

NOT_HOME = "NOT_HOME_BUSINESS"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    return store.read_lines(path)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _stat_text(path: Path) -> Dict[str, Any]:
    info = path.stat()
    return {"bytes": info.st_size, "at": datetime.fromtimestamp(info.st_mtime, timezone.utc)
            .isoformat(timespec="seconds")}


# ---------------------------------------------------------------- 1. 抽屉（只读）

def read_drawer(root=None) -> dict:
    """读抽屉里那张资料卡：只读、不校验、不缓存，每次调用都重读盘。"""
    home = store.home_root(root)
    path = home / DRAWER_ROOM / DRAWER_CARD
    base = {"room": DRAWER_ROOM, "readonly": True, "path": str(path),
            "template": DRAWER_TEMPLATE}
    if not path.is_file():
        base.update({"status": "未接入", "body": None})
        return base
    text = _read_text(path)
    base.update({"status": "已接入", "body": text, "chars": len(text)})
    base.update(_stat_text(path))
    return base


# ---------------------------------------------------------------- 2. 名片盒里的借阅条

def _readout_path(home: Path) -> Path:
    return home / BORROW_ROOM / READOUT_FILE


def _read_readouts(home: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """读读数单。坏 JSON 响亮抛——文件在但读不出来，跟「没有读数」是两件事。"""
    path = _readout_path(home)
    if not path.is_file():
        return {}, {"status": "未接入", "path": str(path), "readouts": {}}
    try:
        data = json.loads(_read_text(path))
    except ValueError as exc:
        raise cards.CardError("READOUT_UNREADABLE", {"path": str(path)},
                              "借阅读数读不出来（%s），不当作没有读数" % exc) from exc
    readouts = data.get("readouts") if isinstance(data, dict) else None
    if not isinstance(readouts, dict):
        raise cards.CardError("READOUT_UNREADABLE", {"path": str(path)},
                              "借阅读数顶层没有 readouts 键值表")
    return readouts, {"status": "已接入", "path": str(path), "readouts": readouts}


def read_learned(root=None) -> dict:
    """读一遍「她现在认得多少词」的读数单（人不改、程序不缓存）。"""
    home = store.home_root(root)
    readouts, status = _read_readouts(home)
    return {"status": status["status"], "path": status["path"], "readouts": readouts,
            "count": len(readouts)}


def register_borrow(root=None, *, role, by, source, version, total, sha256, note="") -> dict:
    """追加一条借阅条（一条一借）到名片盒，并追一行审计。

    为什么是名片盒、而不是第十一间「档案」：名片盒是十间里结构可写的五间之一，所以这一块
    本来就在房级门之内——门一放行、门二按白名单判 agent，一个字的上限也不在这里手搓第二遍
    （上限是 access 的事，自己再比一次就是第二处判定，两处迟早分叉）。

    为什么不是信箱：信箱装的是来往的长文、一封一件；借阅条自己是一张**卡片**，
    plan.md:89 原话「档案里的第三类卡片」，卡片归名片盒，塞信箱等于把卡片当信件。

    名片盒的 index.jsonl 仍只放人卡，借阅条走独立的 borrow.jsonl：房名不变、房间不新增，
    只是这间房里多一个卡片种类。它不进 index.jsonl，也就仍不占 home-objects 的单件身份
    （一批语料指认不成「一件东西」）；它也不碰读数字——读数由人改，程序只在读的时候合并。
    """
    note = note or ""
    access.check_write(role=role, room=BORROW_ROOM, name=BORROW_FILE, chars=len(note),
                       root=root)

    home = store.home_root(root)
    room_path = home / BORROW_ROOM
    store.ensure_dir(room_path)
    path = room_path / BORROW_FILE
    seq = (store.line_count(path) if path.exists() else 0) + 1
    row = {"borrow_id": "借-%d" % seq, "source": source, "version": version,
           "total": int(total), "sha256": sha256, "note": note,
           "by": by or role, "at": _now()}
    store.append_line(path, row)
    entry = audit.append_entry(root=root, role=role, by=by, room=BORROW_ROOM,
                               name=BORROW_FILE, chars=len(row["note"]),
                               bytes=len(json.dumps(row, ensure_ascii=False).encode("utf-8")))
    return {"borrow": row, "audit": entry}


def borrow_ledger(root=None) -> dict:
    """把借阅条与读数合并成一份账：答不出的一律 learned=None、readout_status="未接入"。

    借阅条落在名片盒（room 键明说它落在哪）：它是名片盒里的一种卡片，不是第十一间房。
    """
    home = store.home_root(root)
    room_path = home / BORROW_ROOM
    path = room_path / BORROW_FILE
    rows, bad = _read_jsonl(path)
    readouts, status = _read_readouts(home)

    borrows: List[Dict[str, Any]] = []
    for row in rows:
        borrow_id = row.get("borrow_id")
        readout = readouts.get(borrow_id) if isinstance(borrow_id, str) else None
        learned = readout.get("learned") if isinstance(readout, dict) else None
        if not isinstance(learned, int) or isinstance(learned, bool):
            learned = None
        borrows.append({"borrow_id": borrow_id, "source": row.get("source"),
                        "version": row.get("version"), "total": row.get("total"),
                        "learned": learned,
                        "readout_status": "未接入" if learned is None else "已接入"})

    result: Dict[str, Any] = {"room": BORROW_ROOM, "borrows": borrows, "total": len(borrows),
                              "readout_status": status["status"], "bad_lines": bad,
                              "path": str(path)}
    if not path.exists():
        result["status"] = "未接入"
    if any(item["readout_status"] == "未接入" for item in borrows):
        result["note"] = "答不出「她现在认得多少词」的那几条已标 readout_status=未接入"
    return result


# ---------------------------------------------------------------- 3. 仓库里的嘴指针卡

def mouth_data_dir() -> Path:
    """嘴的数据目录：构造参数没有，所以只看环境变量 > 包内 data/（与 warehouse.py 同口径）。"""
    env = os.environ.get(MOUTH_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "mouth" / "data"


def seed_mouth_pointers(root=None) -> dict:
    """现场算嘴那三份的 sha256，登记成三张指针卡；缺哪份就如实报哪份，不静默跳过。"""
    data_root = mouth_data_dir()
    batch: List[Dict[str, Any]] = []
    missing: List[str] = []
    for name in MOUTH_FILES:
        path = data_root / name
        if not path.is_file():
            missing.append(name)
            continue
        batch.append({"room": WAREHOUSE_ROOM, "kind": POINTER_KIND, "name": name,
                      "path": str(path), "sha256": store.sha256_file(path),
                      "bytes": path.stat().st_size,
                      "note": "嘴的数据文件指针：%s；家只记路径与指纹，不搬本体" % name,
                      "chars": 0,
                      "params": {"pointer": True, "aligned": ALIGNED_KEYS,
                                 "data_root": str(data_root)}})
    result: Dict[str, Any] = {"room": WAREHOUSE_ROOM, "data_root": str(data_root),
                              "seeded": [], "missing": missing, "ok": not missing,
                              "aligned": ALIGNED_KEYS}
    if batch:
        outcome = cards.append_cards(root=root, role="organ", by="home", cards=batch)
        result["seeded"] = [{"name": card["name"], "card_id": card["card_id"],
                             "path": card["path"], "sha256": card["sha256"]}
                            for card in outcome["cards"]]
        result["audit"] = outcome["audit"]
    if missing:
        result["note"] = "仓库缺这几份，没有登记：%s" % "、".join(missing)
    return result


def short_sha(value: Any) -> str:
    """指纹的 12 位短形，给人看的那种。空值如实说「（空）」，不拿空串冒充一个指纹。"""
    text = str(value or "")
    return text[:12] if text else "（空）"


def warehouse_pointers(root=None) -> dict:
    """读回指针卡，并对每份现场重算 sha256/字节数：对不上就 aligned=False + POINTER_STALE。

    改了就是改了：本函数只报，不自动更新卡。
    """
    home = store.home_root(root)
    rows, bad = _read_jsonl(home / WAREHOUSE_ROOM / cards.INDEX_NAME)
    pointers: List[Dict[str, Any]] = []
    for item in cards.fold(rows):
        if item.get("kind") != POINTER_KIND:
            continue
        params = item.get("params") or {}
        if not params.get("pointer"):
            continue
        entry: Dict[str, Any] = {"card_id": item.get("card_id"), "name": item.get("name"),
                                 "path": item.get("path"), "sha256": item.get("sha256"),
                                 "bytes": item.get("bytes"), "aligned_keys": params.get("aligned"),
                                 "aligned": True, "code": "ok"}
        raw = item.get("path")
        if not raw:
            entry.update({"aligned": False, "code": "POINTER_MISSING", "note": "卡上没有路径"})
            pointers.append(entry)
            continue
        path = Path(str(raw))
        if not path.is_file():
            entry.update({"aligned": False, "code": "POINTER_MISSING", "note": "文件不在了"})
            pointers.append(entry)
            continue
        digest = store.sha256_file(path)
        size = path.stat().st_size
        recorded = str(item.get("sha256") or "")
        if digest != recorded or size != int(item.get("bytes") or 0):
            entry.update({"aligned": False, "code": "POINTER_STALE",
                          "note": "现算 %s（%d 字节）跟卡上 %s（%s 字节）不一致；"
                                  "改了就是改了，不自动更新卡"
                                  % (short_sha(digest), size, short_sha(recorded),
                                     item.get("bytes")),
                          "card_sha256": recorded, "card_bytes": int(item.get("bytes") or 0),
                          "actual_sha256": digest, "actual_bytes": size})
        pointers.append(entry)

    data_root = mouth_data_dir()
    return {"room": WAREHOUSE_ROOM, "pointers": pointers, "total": len(pointers),
            "aligned": bool(pointers) and all(item["aligned"] for item in pointers),
            "missing": [item["name"] for item in pointers if item["code"] == "POINTER_MISSING"],
            "stale": [item["name"] for item in pointers if item["code"] == "POINTER_STALE"],
            "bad_lines": bad, "data_root": str(data_root)}


# ---------------------------------------------------------------- 4. 自画像与素材

def _reject_appearance(params: Dict[str, Any]) -> None:
    for key in params:
        if str(key).strip().lower() in [name.lower() for name in FORBIDDEN_KEYS]:
            raise cards.CardError(NOT_HOME, {"key": key, "where": "params"},
                                  "长相不归家：%r 这类定性描述在魂的 identity.json，"
                                  "家只记「谁画的、用什么参数」" % key)


def _reject_extra(extra: Dict[str, Any]) -> None:
    if not extra:
        return
    for key in extra:
        if str(key).strip().lower() in [name.lower() for name in FORBIDDEN_KEYS]:
            raise cards.CardError(NOT_HOME, {"key": key, "where": "kwargs"},
                                  "长相不归家：%r 这类定性描述在魂的 identity.json" % key)
    raise cards.CardError("BAD_ARGUMENT", {"keys": sorted(str(key) for key in extra)},
                          "register_artwork 只收 by/params/path/note 这几样")


def _digest_of(path: Any, code: str) -> Tuple[Path, str, int]:
    if not isinstance(path, (str, os.PathLike)) or not str(path):
        raise cards.CardError(code, {"path": path}, "没有给外面的原件路径")
    file_path = Path(str(path))
    if not file_path.is_file():
        raise cards.CardError(code, {"path": str(file_path)}, "外面的原件不在了：%s" % file_path)
    return file_path, store.sha256_file(file_path), file_path.stat().st_size


def _role_of(by: str) -> str:
    """家只认三个角色（agent／organ／human），by 里记的是原始署名（如 organ:eye）。"""
    name = str(by or "").strip()
    if name == "agent":
        return "agent"
    if name.startswith("organ"):
        return "organ"
    return "human"


def register_artwork(root=None, *, by: str, params: dict, path: str, note: str = "",
                     **extra: Any) -> dict:
    """画出来的那张图 → 工作台：卡里记谁画的（by）与什么参数（params），图本体留在外面。"""
    _reject_extra(extra)
    params = dict(params or {})
    _reject_appearance(params)
    file_path, digest, size = _digest_of(path, "ARTWORK_MISSING")
    card = {"room": DESK_ROOM, "kind": ARTWORK_KIND, "name": file_path.name,
            "path": str(file_path), "sha256": digest, "bytes": size,
            "chars": len(note or ""), "note": note or "", "by": by, "params": params}
    outcome = cards.append_cards(root=root, role=_role_of(by), by=by, cards=[card])
    return {"room": DESK_ROOM, "card": outcome["cards"][0], "audit": outcome["audit"]}


def register_asset(root=None, *, by: str, path: str, note: str = "") -> dict:
    """画她的素材（画风／参考图）→ 仓库：素材卡只记指针，不搬本体、不写长相描述。"""
    file_path, digest, size = _digest_of(path, "ASSET_MISSING")
    card = {"room": WAREHOUSE_ROOM, "kind": ASSET_KIND, "name": file_path.name,
            "path": str(file_path), "sha256": digest, "bytes": size,
            "chars": len(note or ""), "note": note or "", "by": by, "params": {}}
    outcome = cards.append_cards(root=root, role=_role_of(by), by=by, cards=[card])
    return {"room": WAREHOUSE_ROOM, "card": outcome["cards"][0], "audit": outcome["audit"]}
