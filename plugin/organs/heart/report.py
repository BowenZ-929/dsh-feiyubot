"""情绪的契约与两个投递口：核说「发生了什么」，心说「推多少」。

立场（人定的，别发挥）：
1. 心只定义「情绪被什么推动」——**不识别文本、不读聊天**，识别在核那边。
   本模块只收一个已经判好的 kind，不提供任何"从一句话里猜情绪"的口子。
2. 投递是**建议**不是指令：核不许直接写 mood 数值（本模块不提供写 mood 的入口，
   也不转发任何一个能写 mood 数字的参数），推多少由心按**冷却与权重**自己拍。
3. 权重是「档」：一档 = model.MOOD_UNIT 分。口径**正向小幅、负向明显**——
   praise +1 / care +0.5 / cold −1 / insult −2 / rejected −2：
   「被骂 > 被冷落」这件事由数自己说清，不靠形容词。

契约在数据文件里，不写死在代码里（emotion_contract.json）：
- 出厂一份在 store.DATA_DIR/emotion_contract.json（data/.gitignore 里放行了它：
  人得能看见默认长什么样、怎么改）；
- 运行期读 store.default_root()/emotion_contract.json（root 参数能指到别处，测试一律给临时根）；
- 读不到 → 内置默认 + WARN 一行；坏 JSON / 缺键 / 越界 → 补默认 + 夹回 + WARN，
  **永不抛**（M8：参数表读入不许把调用方炸掉）。DEFAULT_* 只是"没文件时的出厂值"。

冷却按 kind 各算：同一类 cooldown_min 分钟内只推一次（10 分钟内被夸三次只算一次）。
冷却中的投递**不丢也不藏**：记账但不推，账上 applied:false / reason:"cooldown"。

两本账，各记各的：
- event_log.jsonl：她最近遇到了什么。五类事件的落账口子只有一个——mood.apply_event
  里的 events.append。本模块**不再自己投一遍**：同一件事落两行会让 mood 的热度折扣
  （events.same_kind_since 数同 kind 的行数）把一次投递数成两次。
- emotion_log.jsonl：本模块自己的账，每投一次落一行（applied / reason /
  mood_before / mood_after 都在里面），冷却就靠读它现算——没有定时器、没有线程。
  行的字段与顺序照契约每类的 ledger 清单，不再把一份字段表写死在这里。

两个投递口（包级导出，核与通道调这两个就够）：
- report_event(kind, who, at, note, root, now)：五类情绪事件，一次一件；
- report_reaction(kind, who, at, root, now)：她主动开口之后有没有人理（warm / cold）。
  它不直接改 mood：先交给 proactive 记 streak；真进了静音才**额外**推一条 cold 情绪
  ——静音是行为，cold 是情绪，不能只有一件（见 report_reaction 的 docstring）。
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

from organs.heart import model, mood, proactive, store

CONTRACT_FILE = "emotion_contract.json"
LOG_FILE = "emotion_log.jsonl"
VERSION = 1

#: 五类情绪事件的名字就是 model.EVENTS——枚举只有那一份，本模块不另抄一遍。
EVENT_KINDS = model.EVENTS

#: 两种反馈：有人接了她的话 / 没人理。它们不直接改 mood，只结算 streak。
REACTION_WARM = "warm"
REACTION_COLD = "cold"
REACTION_KINDS = (REACTION_WARM, REACTION_COLD)
#: 反馈 → proactive 的结算词（warm 有人接 → replied；cold 没人理 → ignored）。
REACTION_OUTCOME = {
    REACTION_WARM: proactive.OUTCOME_REPLIED,
    REACTION_COLD: proactive.OUTCOME_IGNORED,
}

REASON_COOLDOWN = "cooldown"
REASON_MUTE = "mute-triggered"
#: 静音触发的那条情绪：连着没人理 = 被冷落。
MUTE_EVENT = model.EVENT_COLD

#: 契约的账格式出厂清单；文件里的 ledger 说了算，这里只是"没文件时"那一份。
LEDGER_FIELDS = ("at", "kind", "who", "note", "weight", "applied", "reason",
                 "mood_before", "mood_after")
#: 冷却分钟数的取值范围：0 就是"不冷却"，上限一天（再长人也不会真想要）。
_COOLDOWN_RANGE = (0.0, 1440.0)
#: 权重的取值范围（档）。口径是人定的，越界就夹回，不抛。
_WEIGHT_RANGE = (-10.0, 10.0)

#: 出厂那份契约里的说明：与 data/emotion_contract.json 的 note 逐字一致。
NOTE = ("这是出厂默认值，不是验收线；改这份即生效，不用重启。"
        "（运行期读 store.default_root()/emotion_contract.json，"
        "home/organs/heart/ 里那份就是你能改的那份）")

#: 没文件时的出厂契约：与 data/emotion_contract.json 逐字对齐（测试钉住不许漂）。
DEFAULT_CONTRACT: Dict[str, Any] = {
    "note": NOTE,
    "version": VERSION,
    "cooldown_min": 10.0,
    "events": {
        model.EVENT_PRAISE: {
            "name": "被夸",
            "meaning": "对方明确夸她、谢她、说她做得好",
            "weight": 1.0,
        },
        model.EVENT_CARE: {
            "name": "被关心",
            "meaning": "对方问她吃没吃、累不累，让她早点睡这类照顾",
            "weight": 0.5,
        },
        model.EVENT_COLD: {
            "name": "被冷落",
            "meaning": "她说了话没人接，或对方明显敷衍",
            "weight": -1.0,
        },
        model.EVENT_INSULT: {
            "name": "被骂",
            "meaning": "针对她的辱骂、贬损",
            "weight": -2.0,
        },
        model.EVENT_REJECTED: {
            "name": "被拒绝",
            "meaning": "对方拒了这个话题／拒了她的请求",
            "weight": -2.0,
        },
    },
    "reactions": {
        REACTION_WARM: {"name": "有人接了她的话"},
        REACTION_COLD: {"name": "没人理"},
    },
}


# ---- 内部小工具 ----------------------------------------------------------


def _path(root: Any = None) -> Path:
    """契约路径：root 给了就用 root，没给就是**运行期那一份** store.default_root()。"""
    base = Path(root) if root is not None else store.default_root()
    return base / CONTRACT_FILE


def _log_path(root: Any = None) -> Path:
    """本模块的账：root 给了就用 root，没给就是 store.default_root()（实盘）。"""
    base = Path(root) if root is not None else store.default_root()
    return base / LOG_FILE


def _at(at: Optional[str], now: Optional[str]) -> str:
    """这一笔的时刻：now 优先（判冷却用的"此刻"），没给就用 at，两个都没给就是现在。"""
    for value in (now, at):
        if isinstance(value, str) and value:
            return value
    return model.now_iso()


def _warn(notes: List[str], target: Path) -> None:
    """契约读入只 WARN 一行，且永不抛错（M8）。"""
    if not notes:
        return
    shown = "；".join(notes[:4])
    if len(notes) > 4:
        shown += "（等 %d 处）" % len(notes)
    warnings.warn(
        "心·report：情绪契约 %s %s" % (target.name, shown),
        RuntimeWarning, stacklevel=3)


def _clamped(value: Any, default: float, low: float, high: float,
             label: str, notes: List[str]) -> float:
    """缺键补默认、坏值回默认、越界夹回；每一处记一条 note（合成一行 WARN）。"""
    num: Optional[float] = None
    if value is not None and not isinstance(value, bool):
        try:
            num = float(value)
        except (TypeError, ValueError):
            num = None
    if num is None:
        if value is not None:
            notes.append("%s 读不懂（%r），用默认 %g" % (label, value, default))
        else:
            notes.append("缺 %s，用默认 %g" % (label, default))
        return float(default)
    if num < low or num > high:
        notes.append("%s=%g 越界，夹回 [%g, %g]" % (label, num, low, high))
        return min(high, max(low, num))
    return num


def _text(value: Any, default: str, label: str, notes: List[str]) -> str:
    """名字与释义：非空字符串就用，否则补默认（缺键也是缺，记一条 note）。"""
    if isinstance(value, str) and value.strip():
        return value
    if value is None:
        notes.append("缺 %s，用默认 %r" % (label, default))
    else:
        notes.append("%s 不是非空字符串（%r），用默认 %r" % (label, value, default))
    return default


def _ledger(value: Any, default: List[str], label: str, notes: List[str]) -> List[str]:
    """账格式：一串字段名；坏的、空的整条退回默认（空表等于不落账，不接受）。"""
    if isinstance(value, list):
        fields = [item for item in value if isinstance(item, str) and item.strip()]
        if fields:
            return fields
        notes.append("%s 没有可用字段名，用默认" % label)
        return list(default)
    if value is None:
        notes.append("缺 %s，用默认" % label)
    else:
        notes.append("%s 不是数组，用默认" % label)
    return list(default)


def _factory() -> Dict[str, Any]:
    """内置默认的深拷一份：调用方改了也不污染出厂值。"""
    return {
        "note": DEFAULT_CONTRACT["note"],
        "version": VERSION,
        "cooldown_min": float(DEFAULT_CONTRACT["cooldown_min"]),
        "events": {
            kind: {
                "name": entry["name"],
                "meaning": entry["meaning"],
                "weight": float(entry["weight"]),
                "ledger": list(LEDGER_FIELDS),
            }
            for kind, entry in DEFAULT_CONTRACT["events"].items()
        },
        "reactions": {
            name: {"name": cfg["name"]}
            for name, cfg in DEFAULT_CONTRACT["reactions"].items()
        },
    }


# ---- 契约读入 ------------------------------------------------------------


def load_contract(root: Any = None) -> Dict[str, Any]:
    """读情绪契约：{note, version, cooldown_min, events, reactions}。

    顶层不是对象 / 读不到 / 坏 JSON → 整张表用内置默认；缺键补默认、越界夹回；
    三种情况都只 WARN 一行，**永不抛错**（M8：参数表读入不许把调用方炸掉）。
    返回的 dict 一定是完整形状：五类事件（name / meaning / weight / ledger）与
    两条反馈一个不缺——调用方不用再判存在性。
    """
    target = _path(root)
    notes: List[str] = []
    raw: Optional[Dict[str, Any]] = None
    try:
        parsed = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        notes.append("读不到（%s：%s），整张表用内置默认"
                     % (type(exc).__name__, exc))
    else:
        if isinstance(parsed, dict):
            raw = parsed
        else:
            notes.append("顶层不是 JSON 对象（收到 %s），整张表用内置默认"
                         % type(parsed).__name__)
    if raw is None:
        _warn(notes, target)
        return _factory()

    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        version = VERSION
    note = raw.get("note")
    table: Dict[str, Any] = {
        "note": note if isinstance(note, str) and note.strip()
        else DEFAULT_CONTRACT["note"],
        "version": int(version),
        "cooldown_min": _clamped(raw.get("cooldown_min"),
                                 DEFAULT_CONTRACT["cooldown_min"],
                                 _COOLDOWN_RANGE[0], _COOLDOWN_RANGE[1],
                                 "cooldown_min", notes),
        "events": {},
        "reactions": {},
    }

    events_raw = raw.get("events")
    if not isinstance(events_raw, dict):
        events_raw = {}
        notes.append("缺 events 一节，五类全用默认")
    for kind in EVENT_KINDS:
        fallback = DEFAULT_CONTRACT["events"][kind]
        entry = events_raw.get(kind)
        if not isinstance(entry, dict):
            notes.append("缺 events.%s（%s），用默认" % (kind, fallback["name"]))
            entry = {}
        table["events"][kind] = {
            "name": _text(entry.get("name"), fallback["name"],
                          "events.%s.name" % kind, notes),
            "meaning": _text(entry.get("meaning"), fallback["meaning"],
                             "events.%s.meaning" % kind, notes),
            "weight": _clamped(entry.get("weight"), fallback["weight"],
                               _WEIGHT_RANGE[0], _WEIGHT_RANGE[1],
                               "events.%s.weight" % kind, notes),
            "ledger": _ledger(entry.get("ledger"), list(LEDGER_FIELDS),
                              "events.%s.ledger" % kind, notes),
        }

    reactions_raw = raw.get("reactions")
    if not isinstance(reactions_raw, dict):
        reactions_raw = {}
        notes.append("缺 reactions 一节，两条反馈全用默认")
    for name in REACTION_KINDS:
        fallback = DEFAULT_CONTRACT["reactions"][name]
        entry = reactions_raw.get(name)
        if not isinstance(entry, dict):
            notes.append("缺 reactions.%s，用默认 %r" % (name, fallback["name"]))
            entry = {}
        table["reactions"][name] = {
            "name": _text(entry.get("name"), fallback["name"],
                          "reactions.%s.name" % name, notes),
        }

    _warn(notes, target)
    return table


# ---- 冷却 ----------------------------------------------------------------


def _last_applied_at(root: Any, kind: str) -> Optional[str]:
    """本类最近一次**真推过**（applied is True）的时刻；冷却中的行不算，别自己续命。"""
    for row in reversed(store.read_jsonl(_log_path(root))):
        if row.get("kind") != kind or row.get("applied") is not True:
            continue
        at = row.get("at")
        if isinstance(at, str) and at:
            return at
    return None


def _cooling(root: Any, kind: str, at: str, cooldown_min: float) -> bool:
    """同类距上次落账不到 cooldown_min 分钟 → 冷却中（0 分钟就是不冷却）。"""
    if cooldown_min <= 0.0:
        return False
    last = _last_applied_at(root, kind)
    if last is None:
        return False
    try:
        minutes = model.hours_between(last, at) * 60.0
    except ValueError:
        return False
    return minutes < float(cooldown_min)


# ---- 落账 ----------------------------------------------------------------


def _write_log(root: Any, contract: Dict[str, Any], kind: str,
               values: Dict[str, Any]) -> Dict[str, Any]:
    """按契约给的字段名与顺序落一行；没列的字段不写（账格式由文件说了算）。"""
    row = {field: values.get(field)
           for field in contract["events"][kind]["ledger"]}
    store.append_jsonl(_log_path(root), row)
    return row


# ---- 投递口 1：情绪事件 --------------------------------------------------


def report_event(
    kind: str,
    who: str = "主人",
    at: Optional[str] = None,
    note: str = "",
    root: Any = None,
    now: Optional[str] = None,
    force: bool = False,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """核/通道投一件情绪事件：**建议**，推多少由心按契约权重与冷却自己拍。

    - kind 不在五类（model.EVENTS）里 → ValueError，消息列清五类并原样带出收到的值
      ——不许静默当成"没发生"；
    - 冷却中（同类距上次落账 < cooldown_min）→ 只记账不推：返回 applied=False、
      reason="cooldown"，账上照样落一行（applied:false / reason:cooldown）；
    - 通过 → ① 事件账（mood.apply_event 里的 events.append，一次投递只落一行）
      ② mood.apply_event(kind, tiers=契约权重)——权重从这里进 mood，核碰不到数值
      ③ emotion_log.jsonl 落一行（applied:true + mood_before / mood_after）。
    - force=True 绕过冷却（只给"静音触发那条 cold"用，reason 写进账说明来由）；
    - mood 自己的两道旧闸（同一人同一小时 windowed、负向日上限 capped）照样在前面：
      被它们挡下时返回 applied=False、reason=那道的名字（"windowed"/"capped"），
      账上照落——被挡也是一件事，不能悄悄消失。
    - at 是记账时刻，now 是判冷却的此刻；只给一个就两个都用它。

    返回 {"applied", "kind", "who", "weight", "mood_before", "mood_after",
    "reason", "at"}：weight 是这次用的契约权重（档），mood_before/after 是 mood
    那一侧的真实前后（被挡下时两个都是 None：没推，就没有前后）。
    """
    contract = load_contract(root)
    if kind not in EVENT_KINDS:
        raise ValueError(
            "不认识的投递事件：%r（只收这五类：%s）"
            % (kind, "、".join(EVENT_KINDS)))
    moment = _at(at, now)
    label = who if isinstance(who, str) and who else "主人"
    entry = contract["events"][kind]
    weight = float(entry["weight"])
    values: Dict[str, Any] = {
        "at": moment,
        "kind": kind,
        "who": label,
        "note": note if isinstance(note, str) else "",
        "weight": weight,
        "applied": None,
        "reason": reason,
        "mood_before": None,
        "mood_after": None,
    }

    if not force and _cooling(root, kind, moment, float(contract["cooldown_min"])):
        values.update({"applied": False, "reason": REASON_COOLDOWN})
        _write_log(root, contract, kind, values)
        return {
            "applied": False,
            "kind": kind,
            "who": label,
            "weight": weight,
            "mood_before": None,
            "mood_after": None,
            "reason": REASON_COOLDOWN,
            "at": moment,
        }

    # ② 权重交给 mood：核只递 kind，数值由这里给（契约权重 = 名义档）。
    result = mood.apply_event(kind, root=root, now=moment, who=label, tiers=weight)
    applied = bool(result.get("ok"))
    if applied:
        values.update({"applied": True,
                       "mood_before": result["before"],
                       "mood_after": result["after"]})
    else:
        values.update({"applied": False, "reason": result.get("skipped")})
    _write_log(root, contract, kind, values)
    return {
        "applied": applied,
        "kind": kind,
        "who": label,
        "weight": weight,
        "mood_before": values["mood_before"],
        "mood_after": values["mood_after"],
        "reason": values["reason"],
        "at": moment,
    }


# ---- 投递口 2：她开口之后有没有人理 --------------------------------------


def report_reaction(
    kind: str,
    who: str = "主人",
    at: Optional[str] = None,
    root: Any = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """她主动开口之后有没有人理：warm（有人接了）/ cold（没人理），别的拒。

    **不直接改 mood**——情绪得由"发生了什么"推出来，不由"有没有人理"直接写数：
    1. 先交给 proactive 结算 streak（warm → replied 清零，cold → ignored +1）。
       结算的是"她刚开口那一次"，所以先补一条未结算的开口（proactive.sent 幂等：
       已经有一条 pending 就不重复记），这样子账本里一次开口只长一行；
    2. 如果这一次让 proactive **由不静音变成静音**（is_muted 的 muted 由 False 变 True）
       → 额外推一条 cold 情绪事件：force=True 绕过冷却，reason="mute-triggered"。
       一句话钉死：**静音是行为，cold 是情绪，不能只有一件**——静音由 proactive 自己
       落盘，情绪由本模块经 report_event 落到 mood 与两本账上；
    3. 已经在静音里继续被无视不再重复推 cold：只认"由 False 变 True"那一次。

    kind 不在 {warm, cold} 里 → ValueError，消息列清两个并原样带出收到的值。
    返回 {"kind", "streak", "muted", "muted_until", "mood_event"}：mood_event 是
    那条被推上去的 cold 事件的返回（没推就是 None）。
    """
    if kind not in REACTION_KINDS:
        raise ValueError(
            "不认识的反馈：%r（只收 %s 这两个）"
            % (kind, "、".join(REACTION_KINDS)))
    moment = _at(at, now)
    label = who if isinstance(who, str) and who else "主人"

    muted_before = proactive.is_muted(root=root, now=moment)["muted"]
    proactive.sent(root=root, now=moment)
    proactive.outcome(REACTION_OUTCOME[kind], root=root, now=moment)
    view = proactive.is_muted(root=root, now=moment)

    pushed: Optional[Dict[str, Any]] = None
    if view["muted"] and not muted_before:
        pushed = report_event(
            MUTE_EVENT,
            who=label,
            root=root,
            now=moment,
            note="连着 %d 次主动开口没人理 → 安静到 %s"
                 % (int(view["streak"]), view["muted_until"]),
            force=True,
            reason=REASON_MUTE,
        )
    return {
        "kind": kind,
        "streak": int(view["streak"]),
        "muted": bool(view["muted"]),
        "muted_until": view["muted_until"],
        "mood_event": pushed,
    }
