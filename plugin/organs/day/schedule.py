"""日程：按精力、时段、有没有人陪，从权重表里抽「她今天做什么」。

五条判据：

1. **参数全在 data/schedule.json**：时段边界、能量的三档、有人陪的默认档、每张权重表、
   兜底条目与三个触发条件、卡落在哪间房哪一种，全在那份数据里。本文件一个条目名都没有——
   想把某一条调重一点就改数据文件，diff 里看得见，不用回来改代码。
2. **抽法可复现**：同一个 (seed, now, energy, company) 永远抽出同一件。随机数发生器可以
   注入（传 rng=random.Random(7)）；不注入时由这四个值拼出一把定值的 Random，**不碰全局
   random**——所以「今天抽到哪一条」在重跑、换进程、换机器上都是同一件。
3. **只写她自己**：条目全是「她自己做了什么」这一类的事，具体有哪些全在数据文件里。
   关于主人的事实（"主人今天来过"）只能来自真实对话，本模块一条都不生成
   （docs/plan.md §3.1 第 4 条配套）。
4. **一次抽要给两条痕迹，缺一条就算没抽成**：
   - 脑：brain.ingest(who="她自己", source_type="日程模拟")，evidence_key 由 store 留空＝不可作证；
   - 家：走家的公开动作 api.home.dispatch("/home/write")，在「工作台」落一张卡
     （role=agent、by=day），这就是「每件日程要改一处真实状态」。
   家的 403／结构门拒绝如实上抛，不改写成成功，也不重试一版更宽的写法。
5. **来源只有两个词**：plan（她自己定的）／fallback（早上的兜底）。触发条件写在
   schedule.json 的 fallback 里；标记同时落在家的卡（note 首行）与 data/schedule_log.jsonl。

两个入口都不抛未捕获异常（契约测试会拿空参数各调一遍）：坏 now、坏参数、缺脑、家拒绝，
一律回一个 json 安全的 dict，ok=False 带 code 与 message。
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import model

SCHEDULE_FILE = Path(__file__).resolve().parent / "data" / "schedule.json"
LOG_NAME = "schedule_log.jsonl"

# 与嘴／脑对外的词对齐，不在两处各写一套：日程模拟的条目由 store 决定 evidence_key 留空。
BRAIN_WHO = "她自己"
CARD_BY = "day"
CARD_ROLE = "agent"

# 参数文件只在人改它的时候变，所以按 (路径, mtime) 缓存一次读盘结果
_CACHE: Dict[str, Any] = {"key": None, "values": None}


class ScheduleError(ValueError):
    """日程参数读不出来／自相矛盾：调用方要看见，不许当空表继续抽。"""


# ---- 参数 -----------------------------------------------------------------


def _fail(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ok": False, "code": code, "message": message}
    payload.update(extra)
    return payload


def _validate(raw: Any, where: Any) -> Dict[str, Any]:
    """参数自检：缺键、权重指向不存在的条目，一律响亮报错（静默抽空比报错难查）。"""
    if not isinstance(raw, dict):
        raise ScheduleError("日程参数的顶层要是对象：%s" % where)
    for key in ("slots", "tier_to_band", "company_by_slot", "company_keys", "markers",
                "energy", "fallback", "card", "items", "weights"):
        if key not in raw:
            raise ScheduleError("日程参数缺 %r：%s" % (key, where))

    items = raw.get("items")
    if not isinstance(items, dict) or not items:
        raise ScheduleError("items 要是「条目名 → 一句话」的对象：%s" % where)
    for name, row in items.items():
        if not isinstance(name, str) or not name.strip():
            raise ScheduleError("items 里有个空条目名：%s" % where)
        if not isinstance(row, str) or not row.strip():
            raise ScheduleError("条目 %r 要一句「她自己做什么」的话：%s" % (name, where))

    slots = raw.get("slots")
    if not isinstance(slots, list) or not slots:
        raise ScheduleError("slots 要是一个非空数组：%s" % where)
    names: List[str] = []
    for row in slots:
        if not isinstance(row, dict) or not str(row.get("name") or "").strip():
            raise ScheduleError("slots 里有一项没名字：%s" % where)
        for edge in ("from", "to"):
            hour = row.get(edge)
            if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 24:
                raise ScheduleError("时段 %r 的 %s 要一个 0–24 的整点：%s"
                                    % (row.get("name"), edge, where))
        names.append(str(row["name"]))
    if len(set(names)) != len(names):
        raise ScheduleError("slots 里有重名时段：%s" % where)

    markers = raw.get("markers")
    for key in ("plan", "fallback"):
        if not isinstance(markers, dict) or not str(markers.get(key) or "").strip():
            raise ScheduleError("markers 缺 %r：%s" % (key, where))

    card = raw.get("card")
    if not isinstance(card, dict) or not str(card.get("room") or "").strip() \
            or not str(card.get("kind") or "").strip():
        raise ScheduleError("card 要给 room 与 kind：%s" % where)
    for key in ("source_line", "detail_line"):
        if not str(card.get(key) or "").strip():
            raise ScheduleError("card 缺 %r（卡上那两行文案）：%s" % (key, where))

    company_keys = raw.get("company_keys")
    if not isinstance(company_keys, dict) or not company_keys \
            or not any(bool(v) for v in company_keys.values()) \
            or not any(not bool(v) for v in company_keys.values()):
        raise ScheduleError("company_keys 要把 true/false 各映射到一个键名：%s" % where)

    fallback = raw.get("fallback")
    if not isinstance(fallback, dict) or not str(fallback.get("slot") or "").strip():
        raise ScheduleError("fallback 要给一个 slot（哪一段一进来就兜底）：%s" % where)
    if str(fallback["slot"]) not in names:
        raise ScheduleError("fallback.slot=%r 不在 slots 里：%s" % (fallback["slot"], where))
    reasons = fallback.get("reasons")
    for key in ("slot", "energy", "weights"):
        if not isinstance(reasons, dict) or not str(reasons.get(key) or "").strip():
            raise ScheduleError("fallback.reasons 缺 %r：%s" % (key, where))
    if not isinstance(fallback.get("items"), dict) or not fallback["items"]:
        raise ScheduleError("fallback.items 要是一张非空权重表：%s" % where)

    weights = raw.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise ScheduleError("weights 要是一个非空对象：%s" % where)
    bands = set(str(v) for v in raw["tier_to_band"].values())
    for table_name, table in (("fallback.items", fallback["items"]),):
        _check_table(table, items, table_name, where)
    for band in sorted(bands):
        rows = weights.get(band)
        if not isinstance(rows, dict):
            raise ScheduleError("weights 缺档 %r：%s" % (band, where))
        for slot_name in names:
            per_slot = rows.get(slot_name)
            if not isinstance(per_slot, dict):
                raise ScheduleError("weights[%r] 缺时段 %r：%s" % (band, slot_name, where))
            for company_key in company_keys:
                _check_table(per_slot.get(company_key), items,
                             "weights[%s][%s][%s]" % (band, slot_name, company_key), where)
    return raw


def _check_table(table: Any, items: Dict[str, Any], label: str, where: Any) -> None:
    if not isinstance(table, dict):
        raise ScheduleError("%s 要是一张「条目 → 权重」的表：%s" % (label, where))
    for name, weight in table.items():
        if name not in items:
            raise ScheduleError("%s 里的 %r 不在 items 里（权重指向一个不存在的条目）：%s"
                                % (label, name, where))
        if not isinstance(weight, int) or isinstance(weight, bool) or weight < 0:
            raise ScheduleError("%s 里 %r 的权重要是 >=0 的整数：%s" % (label, name, where))


def load_config(explicit: Any = None) -> Dict[str, Any]:
    """读参数：dict 原样验一遍，路径按 mtime 缓一次；读坏了抛 ScheduleError。

    返回的是缓存里那份，调用方只读不改。
    """
    if isinstance(explicit, dict):
        return _validate(dict(explicit), "explicit")
    path = Path(explicit) if explicit is not None else SCHEDULE_FILE
    try:
        key: Any = (str(path), path.stat().st_mtime_ns)
    except OSError:
        key = (str(path), None)
    if _CACHE["key"] == key and _CACHE["values"] is not None:
        return _CACHE["values"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScheduleError("日程参数读不出来：%s（%s）" % (path, exc))
    values = _validate(raw, path)
    _CACHE["key"] = key
    _CACHE["values"] = values
    return values


def log_path(root: Any = None) -> Path:
    """每次抽落的那一行：日子线数据根下的 schedule_log.jsonl。"""
    return model.day_root(root) / LOG_NAME


# ---- 时刻与档 -------------------------------------------------------------


def _now(now_ts: Any = None) -> str:
    return model.iso(now_ts) if now_ts is not None else model.now_iso()


def slot_of(now_ts: Any, cfg: Dict[str, Any]) -> str:
    """这一刻落在哪一段：按 slots 的整点边界判，跨零点的段也算得对。"""
    stamp = model.parse_ts(now_ts)
    hour = stamp.hour
    for row in cfg["slots"]:
        start, end = int(row["from"]), int(row["to"])
        if start <= end:
            if start <= hour < end:
                return str(row["name"])
        elif hour >= start or hour < end:
            return str(row["name"])
    raise ScheduleError("这一刻不在任何时段里（slots 没盖住 24 小时）：%r" % (now_ts,))


def slot_window(slot: str, cfg: Dict[str, Any]) -> Dict[str, int]:
    for row in cfg["slots"]:
        if str(row["name"]) == slot:
            return {"from": int(row["from"]), "to": int(row["to"])}
    raise ScheduleError("时段不在表里：%r" % (slot,))


def _company_key(company: bool, cfg: Dict[str, Any]) -> str:
    for key, value in cfg["company_keys"].items():
        if bool(value) == bool(company):
            return str(key)
    raise ScheduleError("company_keys 里没有对应 %r 的键名" % (company,))


def company_of(slot: str, cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """有人陪的默认档：只看时段。调用方传了就以传入的为准（不读脑的对话状态）。"""
    row = cfg["company_by_slot"]
    if slot not in row:
        raise ScheduleError("company_by_slot 缺时段 %r" % (slot,))
    return bool(row[slot]), "slot"


def read_energy(now_ts: Any, *, heart_root: Any = None, injected: Any = None,
                cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """此刻的精力：默认走心的公开读 heart.energy.read；读不到／档不认识就是「不可读」。

    injected 是给调用方注入的快照（{value, tier}）：传了就不再读盘，缺 tier 算读不到。
    read 自己会结算并落盘，所以只在 POST 这条抽的路上调它——GET 那条不结算心。
    """
    conf = cfg if cfg is not None else load_config()
    label = str(conf["energy"]["unreadable"])
    if injected is not None:
        row = injected if isinstance(injected, dict) else {}
        tier = row.get("tier")
        return {"readable": isinstance(tier, str) and bool(tier), "tier": tier if isinstance(tier, str) and tier else label,
                "value": row.get("value"), "source": "injected"}
    try:
        from organs.heart import energy as energy_mod
        snap = energy_mod.read(root=heart_root, now=now_ts)
    except Exception as exc:                      # 心读不到就是读不到，不许猜一个值
        return {"readable": False, "tier": label, "value": None, "source": "heart",
                "error": "%s: %s" % (type(exc).__name__, exc)}
    tier = snap.get("tier") if isinstance(snap, dict) else None
    value = snap.get("value") if isinstance(snap, dict) else None
    return {"readable": isinstance(tier, str) and bool(tier),
            "tier": tier if isinstance(tier, str) and tier else label,
            "value": value, "source": "heart"}


# ---- 抽 -------------------------------------------------------------------


def _rng(seed_key: str) -> random.Random:
    """一把定值的 Random：同一把钥匙永远同一个序列，与全局 random 无关。"""
    digest = hashlib.sha256(seed_key.encode("utf-8")).hexdigest()[:16]
    return random.Random(int(digest, 16))


def _seed_key(seed: Any, now_ts: str, tier: str, company_key: str) -> str:
    """(seed, now, energy, company) → 钥匙：这四样一样，抽出来的就一样。"""
    return "%r|%s|%s|%s" % (seed, now_ts, tier, company_key)


def _weighted(generator: random.Random, table: Any) -> str:
    """按权重抽一条：表空或权重全零就不抽（由调用方转兜底，不在这里编一个条目）。"""
    rows = []
    for name, weight in (table or {}).items():
        if int(weight) > 0:
            rows.append((str(name), int(weight)))
    rows.sort()
    total = sum(weight for _, weight in rows)
    if not rows or total <= 0:
        raise ScheduleError("权重表是空的／全零：抽不出来")
    point = generator.random() * total
    upto = 0.0
    for name, weight in rows:
        upto += weight
        if point < upto:
            return name
    return rows[-1][0]


def _item_of(name: str, cfg: Dict[str, Any]) -> Dict[str, str]:
    return {"name": name, "text": str(cfg["items"][name])}


def _decide(decision: Dict[str, Any], cfg: Dict[str, Any], name: str,
            source: str, reason: str, **extra: Any) -> Dict[str, Any]:
    decision["source"] = source
    decision["reason"] = reason
    decision["item"] = _item_of(name, cfg)
    decision.update(extra)
    return decision


def pick(now_ts: Any = None, *, energy: Any = None, company: Any = None, seed: Any = None,
         rng: Any = None, heart_root: Any = None,
         config: Any = None) -> Dict[str, Any]:
    """抽一件「她今天做什么」：只看参数，不落任何痕迹（今天的分布靠它看）。

    返回 ok=True 的 dict：now/day/slot/slot_window/company/company_key/energy/
    source/reason/item/table/seed。source 是 plan 或 fallback，reason 说明为什么兜底。
    """
    try:
        cfg = load_config(config)
        now = _now(now_ts)
        slot = slot_of(now, cfg)
        if company is None:
            company_value, company_source = company_of(slot, cfg)
        else:
            company_value, company_source = bool(company), "explicit"
        company_key = _company_key(company_value, cfg)
        snap = read_energy(now, heart_root=heart_root, injected=energy, cfg=cfg)
        decision: Dict[str, Any] = {
            "ok": True, "now": now, "day": model.day_key(now), "slot": slot,
            "slot_window": slot_window(slot, cfg), "company": company_value,
            "company_key": company_key, "company_source": company_source,
            "energy": snap, "seed": seed, "markers": dict(cfg["markers"]),
        }
        generator = rng if rng is not None else _rng(
            _seed_key(seed, now, str(snap["tier"]), company_key))
        fallback = cfg["fallback"]
        band = cfg["tier_to_band"].get(str(snap["tier"])) if snap["readable"] else None
        if slot == str(fallback["slot"]):
            return _fallback(decision, cfg, generator, str(fallback["reasons"]["slot"]))
        if not snap["readable"] or band is None:
            return _fallback(decision, cfg, generator, str(fallback["reasons"]["energy"]))
        table = ((cfg["weights"].get(band) or {}).get(slot) or {}).get(company_key) or {}
        try:
            name = _weighted(generator, table)
        except ScheduleError:
            return _fallback(decision, cfg, generator, str(fallback["reasons"]["weights"]))
        return _decide(decision, cfg, name, str(cfg["markers"]["plan"]), "",
                       band=band, table=dict(table))
    except ScheduleError as exc:
        return _fail("SCHEDULE_CONFIG_BAD", str(exc))
    except ValueError as exc:
        return _fail("BAD_NOW", "现在几点读不出来：%s" % exc)


def _fallback(decision: Dict[str, Any], cfg: Dict[str, Any], generator: random.Random,
              reason: str) -> Dict[str, Any]:
    """兜底：条目与触发条件都来自参数文件，这里只是照着抽一条。"""
    table = cfg["fallback"]["items"]
    name = _weighted(generator, table)
    return _decide(decision, cfg, name, str(cfg["markers"]["fallback"]), reason,
                   band=None, table=dict(table))


# ---- 今日视图 -------------------------------------------------------------


def log_lines(root: Any = None, *, day: Optional[str] = None) -> List[Dict[str, Any]]:
    """日程日志的每一行（坏行原样跳过，不因此读不了整份）。"""
    target = log_path(root)
    if not target.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for text in target.read_text(encoding="utf-8").splitlines():
        text = text.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if isinstance(row, dict) and (day is None or row.get("day") == day):
            rows.append(row)
    return rows


def today(now_ts: Any = None, *, company: Any = None, config: Any = None,
          root: Any = None) -> Dict[str, Any]:
    """GET /day/schedule：今日日程的视图——现在落在哪一段、谁陪、会走哪条路。

    **不在 GET 里结算心**：心的 read 会把 value 与 updated_at 落盘，那是写动作。
    所以这里只报结构与今天的日志尾巴；要把这一抽真正定下来（落脑与家两条痕迹），
    走 POST /day/schedule/draw。

    root 只决定日志在哪读（日子线自己的根），缺省就是日子线的默认根。
    """
    try:
        cfg = load_config(config)
        now = _now(now_ts)
        slot = slot_of(now, cfg)
        if company is None:
            company_value, company_source = company_of(slot, cfg)
        else:
            company_value, company_source = bool(company), "explicit"
        company_key = _company_key(company_value, cfg)
        day = model.day_key(now)
        rows = log_lines(root, day=day)
        return {
            "ok": True, "now": now, "day": day, "slot": slot,
            "slot_window": slot_window(slot, cfg),
            "company": company_value, "company_key": company_key,
            "company_source": company_source,
            "fallback_slot": str(cfg["fallback"]["slot"]),
            "source_hint": str(cfg["markers"]["fallback"])
            if slot == str(cfg["fallback"]["slot"]) else str(cfg["markers"]["plan"]),
            "markers": dict(cfg["markers"]),
            "tier_to_band": dict(cfg["tier_to_band"]),
            "slots": [str(row["name"]) for row in cfg["slots"]],
            "items": sorted(str(name) for name in cfg["items"]),
            "counts": {"items": len(cfg["items"]),
                       "fallback_items": len(cfg["fallback"]["items"]),
                       "drawn_today": len(rows)},
            "log_path": str(log_path(root)),
            "last": rows[-1] if rows else None,
            "note": "GET 只报结构、不结算心；要把这一抽定下来并落两条痕迹，走 POST /day/schedule/draw。",
        }
    except ScheduleError as exc:
        return _fail("SCHEDULE_CONFIG_BAD", str(exc))
    except ValueError as exc:
        return _fail("BAD_NOW", "现在几点读不出来：%s" % exc)


# ---- 落两条痕迹 -----------------------------------------------------------


def _note(decision: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    card = cfg["card"]
    first = str(card["source_line"]) % decision["source"]
    detail = str(card["detail_line"]) % (
        decision["slot"], decision["energy"]["tier"],
        "有" if decision["company"] else "没", decision["seed"])
    return "%s\n%s\n%s" % (first, decision["item"]["text"], detail)


def _home_target(home_root: Any) -> str:
    """家的根：给了就用给的；没给就按家自己的三级解析（构造参数 > HOME_ROOT > 包内 data/）。"""
    if home_root is not None:
        return str(Path(home_root))
    from organs.home import store as home_store
    return str(home_store.home_root(None))


def _write_home(decision: Dict[str, Any], cfg: Dict[str, Any], brain: Any,
                home_root: Any) -> Tuple[int, Dict[str, Any]]:
    """落家那张卡：只走家的公开动作 /home/write，门在 access 里，本层不判一次写权。"""
    from api import home as home_api
    note = _note(decision, cfg)
    card = {
        "room": str(cfg["card"]["room"]),
        "kind": str(cfg["card"]["kind"]),
        "name": decision["item"]["name"],
        "note": note,
        "chars": len(note),
        "role": CARD_ROLE,
        "by": CARD_BY,
        "at": decision["now"],
        "params": {"day": decision["day"], "slot": decision["slot"],
                   "source": decision["source"], "reason": decision["reason"],
                   "company": decision["company"], "energy_tier": decision["energy"]["tier"],
                   "seed": decision["seed"], "text": decision["item"]["text"]},
    }
    body = {"role": CARD_ROLE, "by": CARD_BY, "room": str(cfg["card"]["room"]),
            "cards": [card]}
    status, payload = home_api.dispatch("/home/write", {}, _home_target(home_root), brain,
                                        body=body)
    return int(status), dict(payload or {})


def _append_log(decision: Dict[str, Any], root: Any) -> Dict[str, Any]:
    """每次抽一行：ts/day/item/source/energy_tier/slot/company/seed（多一个字段都不加）。"""
    line = {
        "ts": decision["now"], "day": decision["day"], "item": decision["item"]["name"],
        "source": decision["source"], "energy_tier": decision["energy"]["tier"],
        "slot": decision["slot"], "company": bool(decision["company"]),
        "seed": decision["seed"],
    }
    target = log_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    return line


def draw_and_land(now_ts: Any = None, *, brain: Any = None, root: Any = None,
                  home_root: Any = None, heart_root: Any = None, company: Any = None,
                  seed: Any = None, energy: Any = None, rng: Any = None,
                  config: Any = None) -> Dict[str, Any]:
    """POST /day/schedule/draw：抽一次，落两条痕迹（脑一条 + 家一张卡）并记一行日志。

    三个根各管一处，谁也不替谁：root 是日子线自己的根（schedule_log.jsonl 写这儿）、
    home_root 是家的根（那张卡落这儿）、heart_root 是心的根（energy.read 读这儿）。
    三个都缺省 None 就各走各的默认根，与不带覆盖时一模一样——所以契约测试可以把
    三个都指到临时目录，真实数据根一行都不动。

    顺序是刻意的：先抽 → 先落家那张卡 → 再进脑 → 最后记日志。家拒了就不进脑，
    也不记日志（落半份比没落更难查），并把家的原始拒绝原样带回去。
    """
    decision = pick(now_ts, energy=energy, company=company, seed=seed, rng=rng,
                    heart_root=heart_root, config=config)
    if not decision.get("ok"):
        return decision
    card = {"now": decision["now"], "day": decision["day"],
            "item": decision["item"]["name"], "source": decision["source"]}
    if brain is None:
        return _fail("BRAIN_MISSING",
                     "没有脑（brain=None）：日程模拟那条痕迹落不下去，本模块不落半份——"
                     "家那张卡也不写。请带 brain 再调。", stage="brain", **card)
    try:
        cfg = load_config(config)
    except ScheduleError as exc:
        return _fail("SCHEDULE_CONFIG_BAD", str(exc), stage="config", **card)

    status, payload = _write_home(decision, cfg, brain, home_root)
    if status != 200 or not payload.get("ok"):
        return _fail("HOME_WRITE_REJECTED",
                     "家那边拒了这次登记（HTTP %d）：%s。本模块不吞、不改写成成功，"
                     "也不重试一版更宽的写法。" % (status, payload.get("message") or payload.get("code")),
                     stage="home", http_status=status, home=payload, **card)

    written = (payload.get("cards") or [{}])[0]
    try:
        from organs.brain import store as brain_store
        result = brain.ingest(who=BRAIN_WHO, text=decision["item"]["text"],
                              source_type=brain_store.SOURCE_SCHEDULED, ts=decision["now"])
    except Exception as exc:
        return _fail("BRAIN_INGEST_FAILED",
                     "家那张卡已经落了，脑这条进不去：%s: %s（如实报，不把这次当成功）"
                     % (type(exc).__name__, exc), stage="brain", card=written, **card)
    if not result.get("ok"):
        return _fail("BRAIN_INGEST_FAILED",
                     "脑拒了这一条：%s（家那张卡已经落了，如实报）" % result.get("message"),
                     stage="brain", card=written, **card)

    try:
        line = _append_log(decision, root)
    except OSError as exc:
        return _fail("LOG_IO_ERROR", "两条痕迹都落了，但日志那一行没写成：%s" % exc,
                     stage="log", card=written, **card)

    entry = result.get("entry") or {}
    return {
        "ok": True, "now": decision["now"], "day": decision["day"],
        "slot": decision["slot"], "slot_window": decision["slot_window"],
        "company": decision["company"], "company_key": decision["company_key"],
        "company_source": decision["company_source"],
        "energy": decision["energy"], "source": decision["source"],
        "reason": decision["reason"], "item": decision["item"], "seed": decision["seed"],
        "band": decision.get("band"), "table": decision.get("table"),
        "card": {"card_id": written.get("card_id"), "room": written.get("room"),
                 "kind": written.get("kind"), "name": written.get("name"),
                 "note": written.get("note"), "by": written.get("by"),
                 "role": written.get("role"), "at": written.get("at")},
        "brain": {"entry_id": entry.get("id"), "source_type": entry.get("source_type"),
                  "evidence_key": entry.get("evidence_key"),
                  "merged": bool(result.get("merged"))},
        "log": line, "log_path": str(log_path(root)),
        "traces": {"brain": 1, "home": 1, "log": 1},
    }
