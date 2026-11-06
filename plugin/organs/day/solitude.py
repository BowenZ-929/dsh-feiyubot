"""独处：一个人待着的时候做点自己的事——落一行账，不打扰人。

判据（只在本文件这一处判；别处不许再判一遍）：

1. **多久没人说话** = 脑 today 里最后一条 `side != SIDE_SELF` 的 ts；今天一条都没有
   → 以当天换日点（limits.json 的 day_cutover_hour，默认 04:00）为起点。脑没载入也按
   换日点算——宁可算长，也不许凭空编一条对话出来。
2. **三档阈值全在数据文件里**（limits.json 的 solitude.thresholds_minutes）：
   心情 low 且 精力 low → double_low；两边都 ≥ mid → both_mid；其余 → otherwise。
   档位映射也在数据文件里（solitude.tier_level）：本模块只读心的档位，不自己算值。
   三条阈值都必须短于白天清醒时长（24 − 睡眠窗口 = heart_energy.DEFAULT_SLEEP_WINDOW，
   15 小时 = 900 分钟）：一条「要有自己的时间」的循环，阈值比清醒时长还长就等于永不触发
   （测试里有断言，改配置改红了就是真红）。
3. **一次只做一件**，按 a → b → c 挑这一次该做的：
   a 读一篇旧日记（抽屉/日记 里的一张，repeat_days 天内读过的不再读——去重读 idle 账；
     **读到正文才算读过**，读不到就换下一项，也不许记成读过）；
   b 记一句今天的事（草稿落仓库，kind=素材，params.draft/day/note；这句话只能由脑 today
     里的真条目按 solitude.json 的模板拼出来；今天没有真条目就跳过，**不许编**）；
   c 给自己开一张卡（工作台，kind=物件，params.owner="她自己"，**不设 due**）。
   外部要塞「这一次做什么」就写在 request 里；塞超过一项**一律拒**（一次只做一件）。
4. **不惊动人**：独处本身不发 QQ、不调核（这三件事都不需要模型）；只有这一步真的产生了
   「想跟你说一声」的念头（solitude.json 的 own_doing.actions 里那几项）才往由头池写一条
   own_doing，交给既有的 proactive 链——**每次独处最多一条**，池里已有一条没用的就不再写。
5. **夜里不做**：engage 判 REASON_NIGHT（夜里 + intent=proactive → SILENT）就什么也不做、
   不调核，idle 账落一行 held（why=night）；**同一夜只落一行**——一个 60 秒的 tick 乘一整夜
   就是几千行噪声，而那几行不改变任何一个决定。
6. 账只记**真的发生了决定**的那几次：没到阈值是常态（读数在 GET /day/solitude 里），不落账；
   到了阈值却什么都做不成，才落一行 skipped。

idle 账（proactive_idle_log.jsonl，日子线根下，一行一次决定）：
    {"at", "action", "read": {"name", "date"}, "reason_code": "idle_mood"|"idle_time",
     "threshold_minutes", "idle_minutes", "mood_tier", "energy_tier", "why"}
    action ∈ a / b / c / held / skipped
    reason_code：idle_time = 因时间够长（180 那一档）；idle_mood = 因档位把阈值缩短了
    （40 / 90 那两档）。**不写家的卡、不写动念账**（那是 proactive 的账，各记各的）。

时间一律可注入 now_ts；root 是日子线自己的根，heart_root / home_root 是心与家的根。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from organs.brain import store as brain_store
from organs.heart import energy as heart_energy
from organs.heart import engage as heart_engage
from organs.heart import model as heart_model
from organs.heart import mood as heart_mood

from .. import paths
from . import model, proactive

KIND = "solitude"
CONFIG_FILE = "solitude.json"
LOG_FILE = "proactive_idle_log.jsonl"
VERSION = 1

#: 这一次做了什么（一次触发只有其中一个）：a / b / c 是那三件事，held / skipped 是没做成。
ACT_A = "a"
ACT_B = "b"
ACT_C = "c"
ACT_HELD = "held"
ACT_SKIPPED = "skipped"
ACTION_ORDER: Tuple[str, ...] = (ACT_A, ACT_B, ACT_C)
ACTIONS: Tuple[str, ...] = ACTION_ORDER + (ACT_HELD, ACT_SKIPPED)

#: held / skipped 的那句原因（账上的 why）。
WHY_NIGHT = "night"
WHY_NOTHING = "nothing-to-do"

#: 谁说的、名字叫什么，取不到就叫这个（"不知道是谁"也是一种如实）。
WHO_UNKNOWN = "人"
#: 平台号的形状（qq:3425635473 / aiocqhttp:3425635473）与长数字串：号码任何方向都不念，
#: 也不许写进她的正文（这是全仓的规矩，不是这一条的偏好）。
_PLATFORM_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*:[0-9]+")
_LONG_DIGITS = re.compile(r"[0-9]{7,}")

#: 理由码：时间够长（180 档）/ 档位把阈值缩短了（40、90 两档）。
REASON_IDLE_TIME = "idle_time"
REASON_IDLE_MOOD = "idle_mood"

KEY_DOUBLE_LOW = "double_low"
KEY_BOTH_MID = "both_mid"
KEY_OTHERWISE = "otherwise"
THRESHOLD_KEYS: Tuple[str, ...] = (KEY_DOUBLE_LOW, KEY_BOTH_MID, KEY_OTHERWISE)

LEVEL_LOW = "low"
LEVEL_MID = "mid"
LEVEL_HIGH = "high"
LEVELS: Tuple[str, ...] = (LEVEL_LOW, LEVEL_MID, LEVEL_HIGH)
_RANK = {LEVEL_LOW: 0, LEVEL_MID: 1, LEVEL_HIGH: 2}

#: 往家里写卡时的角色与署名：独处是日子线自己在动手，不是她在跟人说话。
ROLE = "organ"
BY = "日子线·独处"
LIST_LIMIT = 500
LOG_SHOWN = 20

#: 参数文件的兜底（真源在 data/solitude.json；与 proactive.DEFAULTS 同一套规矩：
#: 文件读不到时还能跑，跑出来的行为与文件里写的那份一致）。
DEFAULTS: Dict[str, Any] = {
    "version": VERSION,
    "repeat_days": 7,
    "diary": {"room": "抽屉", "shelf": "日记", "why": "翻到一篇这些天没读过的旧日记"},
    "draft": {"room": "仓库", "kind": "素材", "name": "今天的一点事-{day}",
              "note": "今天{who}说：「{text}」", "note_chars": 60,
              "why": "今天有真素材，记一句"},
    "object": {"room": "工作台", "kind": "物件", "owner": "她自己",
               "name": "她自己想做的事 {day} #{seq}",
               "note": "她自己给自己开的卡：不设 due，也不催她",
               "why": "给自己开一张卡"},
    "own_doing": {"actions": [ACT_A, ACT_B],
                  ACT_A: "我刚翻到一篇旧日记：{name}",
                  ACT_B: "我今天记下了一句：{note}",
                  ACT_C: "我给自己开了一张卡：{name}"},
}
#: 三档阈值的兜底：真源是 limits.json 的 solitude.thresholds_minutes。
DEFAULT_THRESHOLDS: Dict[str, int] = {KEY_DOUBLE_LOW: 180, KEY_BOTH_MID: 40,
                                      KEY_OTHERWISE: 90}
#: 档位映射的兜底：真源是 limits.json 的 solitude.tier_level。
DEFAULT_TIER_LEVEL: Dict[str, Dict[str, str]] = {
    "mood": {"冰点": LEVEL_LOW, "偏冷": LEVEL_LOW, "平常": LEVEL_MID,
             "偏热": LEVEL_HIGH, "滚烫": LEVEL_HIGH},
    "energy": {"累": LEVEL_LOW, "偏累": LEVEL_LOW, "一般": LEVEL_MID, "充沛": LEVEL_HIGH},
}


class SolitudeError(ValueError):
    """参数坏了（比如一次要塞两件事）：调用方要看见，由 api 层译成 400。"""


# ---- 小工具 ---------------------------------------------------------------


def _now(now_ts: Any = None) -> str:
    return model.iso(now_ts) if now_ts is not None else model.now_iso()


def config_path(root: Any = None) -> Path:
    """参数文件：注入的根里有一份就用它，否则读包内 data/solitude.json。"""
    own = model.day_root(root) / CONFIG_FILE
    return own if own.exists() else model.DATA_DIR / CONFIG_FILE


def load_config(root: Any = None, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读参数：整份缺就兜底 DEFAULTS；坏值按 DEFAULTS 那一项修，不抛。"""
    values: Dict[str, Any] = dict(DEFAULTS)
    if isinstance(config, dict) and config:
        values.update(config)
    else:
        try:
            loaded = json.loads(config_path(root).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            values.update(loaded)
    try:
        values["repeat_days"] = float(values.get("repeat_days"))
    except (TypeError, ValueError):
        values["repeat_days"] = float(DEFAULTS["repeat_days"])
    for name in ("diary", "draft", "object", "own_doing"):
        if not isinstance(values.get(name), dict):
            values[name] = dict(DEFAULTS[name])
    return values


def block(config: Optional[Dict[str, Any]], name: str) -> Dict[str, Any]:
    """solitude.json 里的一块（diary / draft / object / own_doing）；缺哪一项就按兜底补。"""
    cfg = config or load_config()
    raw = cfg.get(name)
    merged = dict(DEFAULTS[name])
    if isinstance(raw, dict):
        merged.update(raw)
    return merged


# ---- 阈值与档位（全读数据文件） -------------------------------------------


def limits_source(root: Any = None) -> Path:
    """limits.json 在哪：注入的根里有一份就用它，否则读包内 data/limits.json。

    与 proactive.config_path 同一条规矩。这一条不是小事：?root=<临时目录> 里通常没有
    limits.json，那时要读的是**随包发的那份**，不是凭代码里的数字硬跑。
    """
    own = model.day_root(root) / model.LIMITS_FILE
    return own if own.exists() else model.DATA_DIR / model.LIMITS_FILE


def _limits_doc(root: Any = None) -> Dict[str, Any]:
    try:
        loaded = json.loads(limits_source(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def limits_section(limits: Optional[Dict[str, Any]] = None,
                   root: Any = None) -> Dict[str, Any]:
    """limits.json 的 solitude 那一节：手上这份有就用它，没有就读盘（先根、再包内）。"""
    if isinstance(limits, Mapping):
        section = limits.get(KIND)
        if isinstance(section, dict) and section:
            return dict(section)
    section = _limits_doc(root).get(KIND)
    if isinstance(section, dict) and section:
        return dict(section)
    return {}


def thresholds(limits: Optional[Dict[str, Any]] = None, root: Any = None) -> Dict[str, int]:
    """三档阈值（分钟）：真源是 limits.json 的 solitude.thresholds_minutes。"""
    raw = limits_section(limits, root).get("thresholds_minutes")
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, int] = {}
    for key in THRESHOLD_KEYS:
        try:
            value = int(raw.get(key, DEFAULT_THRESHOLDS[key]))
        except (TypeError, ValueError):
            value = int(DEFAULT_THRESHOLDS[key])
        out[key] = max(1, value)
    return out


def tier_table(limits: Optional[Dict[str, Any]] = None,
               root: Any = None) -> Dict[str, Dict[str, str]]:
    """档位映射（心的档位词 → low/mid/high）：真源是 limits.json 的 solitude.tier_level。"""
    raw = limits_section(limits, root).get("tier_level")
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, Dict[str, str]] = {}
    for kind in ("mood", "energy"):
        table = raw.get(kind)
        table = table if isinstance(table, dict) else {}
        merged = dict(DEFAULT_TIER_LEVEL[kind])
        for tier, level in table.items():
            word = str(level or "").strip().lower()
            if isinstance(tier, str) and word in _RANK:
                merged[tier] = word
        out[kind] = merged
    return out


def tier_level(kind: str, tier: Any, limits: Optional[Dict[str, Any]] = None,
               root: Any = None) -> str:
    """一个档位词落在 low / mid / high 哪一档；不认识的词按 low 算（保守：阈值最长那档）。"""
    table = tier_table(limits, root).get(kind)
    table = table if isinstance(table, dict) else {}
    level = str(table.get(str(tier or "")) or "").lower()
    return level if level in _RANK else LEVEL_LOW


def threshold(mood_tier: Any, energy_tier: Any, limits: Optional[Dict[str, Any]] = None,
              root: Any = None) -> Dict[str, Any]:
    """这一次的阈值：先命中先算——双低 180 / 两边都到 mid 40 / 其余 90。"""
    table = thresholds(limits, root)
    mood_level = tier_level("mood", mood_tier, limits, root)
    energy_level = tier_level("energy", energy_tier, limits, root)
    if _RANK[mood_level] == _RANK[LEVEL_LOW] and _RANK[energy_level] == _RANK[LEVEL_LOW]:
        key = KEY_DOUBLE_LOW
        reason = REASON_IDLE_TIME       # 时间够长才到这一档
    elif _RANK[mood_level] >= _RANK[LEVEL_MID] and _RANK[energy_level] >= _RANK[LEVEL_MID]:
        key = KEY_BOTH_MID
        reason = REASON_IDLE_MOOD      # 档位把阈值缩短了
    else:
        key = KEY_OTHERWISE
        reason = REASON_IDLE_MOOD
    return {"key": key, "minutes": int(table[key]), "reason_code": reason,
            "mood_level": mood_level, "energy_level": energy_level}


def awake_minutes(sleep_window: Optional[Sequence[int]] = None) -> int:
    """白天清醒几分钟：24 小时减去睡眠窗口（跨午夜按模 24 算）。

    睡眠窗口的真源是心的 energy.DEFAULT_SLEEP_WINDOW（23 → 8 = 9 小时睡、15 小时醒）；
    这里只做换算，不另写一份钟点。
    """
    window = sleep_window if sleep_window is not None else heart_energy.DEFAULT_SLEEP_WINDOW
    try:
        start, end = int(window[0]), int(window[1])
    except (TypeError, ValueError, IndexError):
        start, end = (int(heart_energy.DEFAULT_SLEEP_WINDOW[0]),
                      int(heart_energy.DEFAULT_SLEEP_WINDOW[1]))
    asleep = (end - start) % 24
    return (24 - asleep) * 60


# ---- 闲置多久（没人说话） --------------------------------------------------


def _today(brain: Any, at: str) -> List[Dict[str, Any]]:
    """脑的 today 在役条目（只读）；没载入脑就是空的。"""
    if brain is None:
        return []
    try:
        return list(brain.today_entries(at) or [])
    except Exception:  # noqa: BLE001 - 脑读不出来不该让独处整条断掉
        return []


def cutover_at(now_ts: Any = None, root: Any = None) -> str:
    """当天的换日点（本地时区的那个整点）：今天一条都没人说话就从这里起算。"""
    at = _now(now_ts)
    hour = int(model.load_limits(root)["day_cutover_hour"])
    day = model.day_key(at, hour)
    return model.iso(model.parse_ts("%sT00:00:00" % day) + timedelta(hours=hour))


def idle_since(now_ts: Any = None, *, brain: Any = None, root: Any = None) -> str:
    """「多久没人说话」的起点：today 里最后一条不是她说的 ts；一条都没有就回换日点。"""
    at = _now(now_ts)
    latest = ""
    for entry in _today(brain, at):
        if str(entry.get("side") or brain_store.SIDE_OTHER) == brain_store.SIDE_SELF:
            continue
        stamp = str(entry.get("last_ts") or entry.get("ts") or "")
        if not stamp or stamp <= latest:
            continue
        try:
            if model.day_key(stamp) != model.day_key(at):
                continue
        except ValueError:
            continue
        latest = stamp
    return latest or cutover_at(at, root)


def idle_minutes(now_ts: Any = None, *, brain: Any = None, root: Any = None) -> float:
    """没人说话多久了（分钟）；读不出起点就 0（宁可说"刚说过"，也不许凭空多算一截）。"""
    at = _now(now_ts)
    try:
        span = (model.parse_ts(at) - model.parse_ts(idle_since(at, brain=brain, root=root)))
    except ValueError:
        return 0.0
    return max(0.0, span.total_seconds() / 60.0)


def is_night(now_ts: Any = None, *, heart_root: Any = None, mood_read: Mapping[str, Any],
             energy_read: Mapping[str, Any], brain: Any = None) -> Tuple[bool, Dict[str, str]]:
    """engage 判这一次是不是夜里（REASON_NIGHT）：硬门第一道，判 next 的时候不许自己再算一遍。"""
    at = _now(now_ts)
    try:
        energy_value = float((energy_read or {}).get("value"))
    except (TypeError, ValueError):
        energy_value = 0.0
    inp = heart_engage.EngageInput(
        mood_now=float((mood_read or {}).get("now") or 0.0),
        energy=energy_value,
        familiarity=0.0,
        today_count=len(_today(brain, at)),
        intent=heart_model.INTENT_PROACTIVE,
        now=at,
        window=heart_engage.window_signal(heart_root, at),
    )
    verdict = heart_engage.decide(inp)
    hits = [str(item) for item in verdict.get("hits") or []]
    block_read = {"state": str(verdict.get("state") or ""),
                  "reason": str(verdict.get("reason") or "")}
    return heart_model.REASON_NIGHT in hits, block_read


# ---- 账 -------------------------------------------------------------------


def log_path(root: Any = None) -> Path:
    """idle 账：日子线数据根下的 proactive_idle_log.jsonl（一行一次决定）。"""
    return model.day_root(root) / LOG_FILE


def append_log(root: Any, row: Dict[str, Any]) -> Path:
    """追加一行 idle 账：与动念账同一条写法规矩（临时件 + append，崩了不留半行）。"""
    return proactive.append_row(log_path(root), row)


def log_lines(root: Any = None) -> List[Dict[str, Any]]:
    """idle 账的每一行（坏行原样跳过，不因为一行坏了读不了整份）。"""
    return proactive.read_rows(log_path(root))


def log_tail(root: Any = None, limit: int = LOG_SHOWN) -> List[Dict[str, Any]]:
    """账尾几行（GET /day/solitude 用）；limit <= 0 就是全部。"""
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = LOG_SHOWN
    rows = log_lines(root)
    return rows[-count:] if count > 0 else rows


def _row(at: str, action: str, read: Mapping[str, Any], why: str = "",
         read_item: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """一行 idle 账：先摆上那一刻的读数，再摆这一次的判决。

    read 把阈值从哪来（reason_code / threshold_minutes）与两个档位原样落下来——
    「为什么今天这一次触发得早/晚」只靠这一行就能答。
    """
    row: Dict[str, Any] = {"at": at, "action": action}
    if isinstance(read_item, Mapping) and read_item:
        row["read"] = {"name": str(read_item.get("name") or ""),
                       "date": str(read_item.get("date") or "")}
    for key in ("reason_code", "threshold_minutes"):
        row[key] = read.get(key)
    for key in ("idle_minutes", "mood_tier", "energy_tier"):
        row[key] = read.get(key)
    row["why"] = str(why or "")
    return row


def recent_reads(root: Any = None, now_ts: Any = None,
                 days: Optional[float] = None) -> Dict[str, set]:
    """这些天读过哪几篇旧日记（按卡名与日期两把尺）：repeat_days 天内不许再读同一篇。"""
    at = _now(now_ts)
    if days is None:
        days = float(load_config(root).get("repeat_days") or DEFAULTS["repeat_days"])
    try:
        start = model.parse_ts(at) - timedelta(days=float(days))
    except (TypeError, ValueError):
        start = model.parse_ts(at) - timedelta(days=float(DEFAULTS["repeat_days"]))
    names: set = set()
    dates: set = set()
    for row in log_lines(root):
        if str(row.get("action") or "") != ACT_A:
            continue
        try:
            when = model.parse_ts(str(row.get("at") or ""))
        except ValueError:
            continue
        if when < start:
            continue
        got = row.get("read") if isinstance(row.get("read"), dict) else {}
        if got.get("name"):
            names.add(str(got["name"]))
        if got.get("date"):
            dates.add(str(got["date"]))
    return {"names": names, "dates": dates}


# ---- 三件事（一次只做一件） -----------------------------------------------


def _render(template: Any, fields: Mapping[str, Any]) -> str:
    """占位符逐个替换（不认 str.format：模板里本来就带着别的花括号）。"""
    text = str(template if template is not None else "")
    for key, value in fields.items():
        text = text.replace("{%s}" % key, str(value if value is not None else ""))
    return text


def _clip(text: Any, chars: Any) -> str:
    body = " ".join(str(text if text is not None else "").split())
    try:
        limit = max(1, int(chars))
    except (TypeError, ValueError):
        limit = int(DEFAULTS["draft"]["note_chars"])
    return body if len(body) <= limit else body[:limit]


def _cards():
    """家的公开读写（只导入，不改它）：读日记、列房间都走这一处。"""
    from organs.home import cards      # 延迟导入：别让日子线的导入顺序被家绑住
    return cards


def _home_root(root: Any = None, home_root: Any = None) -> Path:
    """家的根：给了就用，否则与 rhythm 同一条派生规矩（<root>/home）。"""
    if home_root is not None:
        return Path(home_root)
    if root is not None:
        return Path(root) / "home"
    return paths.organ_dir("home")


def _write_card(home_root: Any, brain: Any, *, room: str,
                card: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """往家里登记一张卡：走 api.home 的 /home/write（与别的写入方同一条路）。

    这一层不判一次写权（门在 access 里）：回执说没登记成就是没成，
    **不许兜底成"就当写好了"**——账上要说真话。
    """
    from api import home as home_api       # 延迟导入：organs → api 只在用到时走一次
    body = {"role": ROLE, "by": BY, "room": room, "cards": [dict(card)]}
    try:
        status, payload = home_api.dispatch("/home/write", {}, str(home_root), brain, body=body)
    except Exception as exc:  # noqa: BLE001 - 家写不进去只该让这一项作废，不许让 tick 断
        return 500, {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
    if not isinstance(status, int):
        return 500, {"ok": False, "message": "家回了一个不认识的状态：%r" % (status,)}
    payload = dict(payload or {}) if isinstance(payload, Mapping) else {}
    if status != 200:
        return status, payload
    if not payload.get("count"):
        return 500, {"ok": False, "message": "家回了 200，但一张卡都没登记"}
    return 200, payload


def _diary_cards(home_root: Path, cfg: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """抽屉/日记 里那些卡（从最旧的一篇读起）；读不出来就是空的。"""
    diary = block(dict(cfg), "diary")
    room = str(diary.get("room") or DEFAULTS["diary"]["room"])
    shelf = str(diary.get("shelf") or DEFAULTS["diary"]["shelf"])
    try:
        listing = _cards().list_room(root=str(home_root), room=room, shelf=shelf,
                                     limit=LIST_LIMIT)
    except Exception:  # noqa: BLE001 - 抽屉读不出来就换下一项，不许当作"读过"
        return []
    items = [card for card in (listing.get("items") or []) if isinstance(card, dict)]
    items.sort(key=lambda card: (str((card.get("params") or {}).get("date") or ""),
                                 str(card.get("name") or "")))
    return items


def _read_diary(at: str, root: Any = None, brain: Any = None,
                home_root: Any = None, cfg: Optional[Dict[str, Any]] = None
                ) -> Optional[Dict[str, Any]]:
    """a 读一篇旧日记：读到正文才算数，读不到就换下一项（也不许记成读过）。

    brain 收下不用：读日记不看脑——三件事的签名一样，_DOERS 才好按码直接调。
    """
    config = cfg or load_config(root)
    diary = block(config, "diary")
    room = str(diary.get("room") or DEFAULTS["diary"]["room"])
    shelf = str(diary.get("shelf") or DEFAULTS["diary"]["shelf"])
    home = _home_root(root, home_root)
    seen = recent_reads(root, at)
    for card in _diary_cards(home, config):
        name = str(card.get("name") or "")
        date = str((card.get("params") or {}).get("date") or "")
        if not name or name in seen["names"] or (date and date in seen["dates"]):
            continue
        try:
            got = _cards().read_item(root=str(home), room=room, name=name, shelf=shelf)
        except Exception:  # noqa: BLE001 - 单张读不出来就试下一张
            continue
        body = str((got or {}).get("body") or "").strip()
        if not body:
            continue        # 没读到正文：这一张不算读过，换下一张
        return {"action": ACT_A, "why": str(diary.get("why") or ""),
                "read": {"name": name, "date": date}, "body": body}
    return None


def plain_name(value: Any) -> str:
    """可以写进她正文的一个称呼；号码／平台号一律给空串（宁可叫"人"，也不念号）。"""
    text = " ".join(str(value if value is not None else "").split())
    if not text:
        return ""
    if _PLATFORM_ID.search(text) or _LONG_DIGITS.search(text):
        return ""
    return text


def person_name(brain: Any, who: str, at: str) -> str:
    """脑的人名录里这个人怎么称呼（公开读 lookup）：nickname → display → 别名里第一个不是号的。"""
    if brain is None or not who:
        return ""
    try:
        view = brain.lookup(who, at)
    except Exception:  # noqa: BLE001 - 名录读不出来就当没有称呼，不许让独处断掉
        return ""
    if not isinstance(view, dict):
        return ""
    for key in ("nickname", "display"):
        name = plain_name(view.get(key))
        if name:
            return name
    for alias in view.get("aliases") or []:
        name = plain_name(alias)
        if name:
            return name
    return ""


def who_name(entry: Mapping[str, Any], brain: Any, at: str) -> str:
    """这一条素材里"是谁说的"：entry 上的 display 优先，取不到查名录，再取不到就叫"人"。

    **平台号绝不进她的正文**：display / 名录 / 别名里只要是 qq:3425635473 这种形状、
    或含 7 位以上连续数字，一律跳过——号码任何方向都不念；宁可写"今天人说"，
    也不许让一句"今天qq:3425635473说"落成她的草稿与由头。
    """
    who = str(entry.get("who") or "").strip()
    for candidate in (entry.get("display"), person_name(brain, who, at)):
        name = plain_name(candidate)
        if name:
            return name
    return WHO_UNKNOWN


def _material(brain: Any, at: str) -> Optional[Dict[str, str]]:
    """今天有没有真素材：脑 today 里的真条目（别人说的那一手优先）。

    "谁说的"走 who_name：平台号与长号码不进正文（display → 人名录 → "人"）。
    """
    best: Optional[Dict[str, str]] = None
    for entry in _today(brain, at):
        text = str(entry.get("text") or entry.get("quote") or "").strip()
        if not text:
            continue
        row = {"who": who_name(entry, brain, at),
               "text": text, "id": str(entry.get("id") or ""),
               "ts": str(entry.get("ts") or "")}
        best = row
        if str(entry.get("side") or brain_store.SIDE_OTHER) != brain_store.SIDE_SELF:
            return row        # 别人说的话比她自己那一手更硬：先取它
    return best


def _note_today(at: str, root: Any = None, brain: Any = None,
                home_root: Any = None, cfg: Optional[Dict[str, Any]] = None
                ) -> Optional[Dict[str, Any]]:
    """b 记一句今天的事：草稿落仓库（kind=素材）；今天没有真素材就跳过，不许编。"""
    config = cfg or load_config(root)
    draft = block(config, "draft")
    story = _material(brain, at)
    if story is None:
        return None
    day = model.day_key(at)
    note = _render(draft.get("note"), {"who": story["who"], "day": day,
                                      "text": _clip(story["text"], draft.get("note_chars"))})
    name = _render(draft.get("name"), {"day": day})
    room = str(draft.get("room") or DEFAULTS["draft"]["room"])
    card = {"kind": str(draft.get("kind") or DEFAULTS["draft"]["kind"]),
            "name": name, "note": note,
            "params": {"draft": True, "day": day, "note": note,
                       "from_entry": story["id"], "source": "solitude"}}
    status, payload = _write_card(_home_root(root, home_root), brain, room=room, card=card)
    if status != 200:
        return None
    written = (payload.get("cards") or [{}])[0]
    return {"action": ACT_B, "why": str(draft.get("why") or ""), "note": note,
            "name": str(written.get("name") or name),
            "card_id": str(written.get("card_id") or ""), "room": room,
            "from_entry": story["id"]}


def _open_card(at: str, root: Any = None, brain: Any = None,
               home_root: Any = None, cfg: Optional[Dict[str, Any]] = None
               ) -> Optional[Dict[str, Any]]:
    """c 给自己开一张卡：工作台、kind=物件、owner=她自己、**不设 due**。"""
    config = cfg or load_config(root)
    own = block(config, "object")
    home = _home_root(root, home_root)
    room = str(own.get("room") or DEFAULTS["object"]["room"])
    seq = 1
    try:
        listing = _cards().list_room(root=str(home), room=room, limit=LIST_LIMIT)
        seq = int(listing.get("total") or 0) + 1
    except Exception:  # noqa: BLE001 - 数不出张数就按第 1 张起名（名字只是给人看的）
        seq = 1
    day = model.day_key(at)
    name = _render(own.get("name"), {"day": day, "seq": seq})
    note = str(own.get("note") or "")
    card = {"kind": str(own.get("kind") or DEFAULTS["object"]["kind"]),
            "name": name, "note": note,
            "params": {"owner": str(own.get("owner") or DEFAULTS["object"]["owner"]),
                       "self": True, "day": day, "source": "solitude"}}
    status, payload = _write_card(home, brain, room=room, card=card)
    if status != 200:
        return None
    written = (payload.get("cards") or [{}])[0]
    return {"action": ACT_C, "why": str(own.get("why") or ""),
            "name": str(written.get("name") or name),
            "card_id": str(written.get("card_id") or ""), "room": room}


_DOERS = {ACT_A: _read_diary, ACT_B: _note_today, ACT_C: _open_card}


def _order(wanted: str) -> Tuple[str, ...]:
    """这一次按什么次序试：外部点了名就先试它，其余照 a → b → c 的固定次序。"""
    if wanted in ACTION_ORDER:
        return (wanted,) + tuple(code for code in ACTION_ORDER if code != wanted)
    return ACTION_ORDER


# ---- 「一次只做一件」：外部塞进来的请求 --------------------------------------


def _action_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    if code not in ACTION_ORDER:
        raise SolitudeError("不认识的独处动作：%r；只有 a（读一篇旧日记）/ b（记一句今天的事）"
                            "/ c（给自己开一张卡）" % (value,))
    return code


def parse_request(request: Any = None) -> str:
    """外部指定「这一次做什么」：最多一项；多塞的一律拒（一次只做一件）。"""
    if request is None or request == {} or request == "":
        return ""
    if isinstance(request, str):
        return _action_code(request)
    if not isinstance(request, Mapping):
        raise SolitudeError("request 要一个对象或一个动作码（a/b/c），收到 %r" % (request,))
    drafts = request.get("drafts")
    if isinstance(drafts, (list, tuple)) and len(drafts) > 1:
        names = []
        for item in drafts:
            if isinstance(item, Mapping):
                names.append(str(item.get("name") or "(无名)"))
            else:
                names.append(repr(item))
        raise SolitudeError(
            "一次只做一件：这一次塞进来 %d 张草稿（%s）。独处一次只做一件事——"
            "要么读一篇旧日记、要么记一句今天的事、要么给自己开一张卡；"
            "多出来的那几张一张都不落，要再来一次就下一次再触发"
            % (len(drafts), "、".join("第 %d 张「%s」" % (i + 1, name)
                                     for i, name in enumerate(names))))
    actions = request.get("actions")
    if isinstance(actions, (list, tuple)):
        codes = [str(item).strip().lower() for item in actions]
        if len(codes) > 1:
            raise SolitudeError(
                "一次只做一件：这一次塞进来 %d 项（%s）。独处一次只做一件事——"
                "按 a → b → c 挑一件，多塞的都不做" % (len(codes), "、".join(codes)))
        if codes:
            return _action_code(codes[0])
    for key in ("action", "do"):
        if key in request and str(request.get(key) or "").strip():
            return _action_code(request.get(key))
    if isinstance(drafts, (list, tuple)) and drafts:
        return ACT_B        # 塞了一张草稿：这一次就做"记一句"
    return ""


# ---- 一次独处 -------------------------------------------------------------


def readings(now_ts: Any = None, *, root: Any = None, heart_root: Any = None,
             brain: Any = None, limits: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """此刻的读数：两个档位、三档阈值取哪个、闲置多少分钟、是不是夜里。只读不写账。"""
    at = _now(now_ts)
    mood_read = heart_mood.read(heart_root, at)
    energy_read = heart_energy.read(heart_root, at)
    mood_tier = str(mood_read.get("tier") or "")
    energy_tier = str(energy_read.get("tier") or "")
    picked = threshold(mood_tier, energy_tier, limits, root)
    night, engage_read = is_night(at, heart_root=heart_root, mood_read=mood_read,
                                  energy_read=energy_read, brain=brain)
    return {"at": at,
            "mood_tier": mood_tier, "energy_tier": energy_tier,
            "mood_level": picked["mood_level"], "energy_level": picked["energy_level"],
            "mood_now": mood_read.get("now"), "energy_value": energy_read.get("value"),
            "threshold_key": picked["key"], "threshold_minutes": picked["minutes"],
            "reason_code": picked["reason_code"],
            "idle_since": idle_since(at, brain=brain, root=root),
            "idle_minutes": round(idle_minutes(at, brain=brain, root=root), 1),
            "night": bool(night), "engage": engage_read,
            "awake_minutes": awake_minutes(),
            "thresholds": thresholds(limits, root)}


def consider(now_ts: Any = None, *, root: Any = None, brain: Any = None,
             heart_root: Any = None, home_root: Any = None,
             limits: Optional[Dict[str, Any]] = None,
             request: Any = None) -> Dict[str, Any]:
    """一次独处：判一次"该不该、该做哪一件"，做那一件，落一行账，最多写一条由头。

    次序钉死（先夜里、再阈值、再三件事）：夜里不做也不调核；没到阈值就什么都不做、
    也不落账（那是常态）；到了阈值按 a → b → c 挑到第一件做得成的；一件都做不成才记
    skipped。**一次只做一件**：request 里多塞的项在进来时就被拒（SolitudeError）。
    """
    at = _now(now_ts)
    wanted = parse_request(request)
    cfg = load_config(root)
    read = readings(at, root=root, heart_root=heart_root, brain=brain, limits=limits)
    if read["night"]:
        row = _hold_night(root, at, read)
        return {"ok": True, "at": at, "fired": False, "skipped": WHY_NIGHT,
                "readings": read, "row": row, "own_doing": None, "tried": []}
    if float(read["idle_minutes"]) < float(read["threshold_minutes"]):
        return {"ok": True, "at": at, "fired": False, "skipped": "not-idle",
                "readings": read, "row": None, "own_doing": None, "tried": []}
    tried: List[Dict[str, Any]] = []
    done: Optional[Dict[str, Any]] = None
    for code in _order(wanted):
        candidate = _DOERS[code](at, root=root, brain=brain, home_root=home_root, cfg=cfg)
        tried.append({"action": code, "ok": candidate is not None})
        if candidate is not None:
            done = candidate
            break
    if done is None:
        row = _row(at, ACT_SKIPPED, read, why=WHY_NOTHING)
        append_log(root, row)
        return {"ok": True, "at": at, "fired": False, "skipped": WHY_NOTHING,
                "readings": read, "row": row, "own_doing": None, "tried": tried}
    row = _row(at, str(done["action"]), read, why=str(done.get("why") or ""),
               read_item=done.get("read"))
    append_log(root, row)
    pool = raise_own_doing(done, at, root=root)
    return {"ok": True, "at": at, "fired": True, "action": str(done["action"]),
            "readings": read, "did": done, "row": row, "own_doing": pool, "tried": tried}


def _hold_night(root: Any, at: str, read: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """夜里：不做事、不调核，落一行 held；**同一夜只落一行**，再判到就还回那一行。"""
    day = model.day_key(at)
    for row in reversed(log_lines(root)):
        if str(row.get("action") or "") != ACT_HELD:
            continue
        try:
            if model.day_key(str(row.get("at") or "")) != day:
                continue
        except ValueError:
            continue
        if str(row.get("why") or "") == WHY_NIGHT:
            return row
    held = _row(at, ACT_HELD, read, why=WHY_NIGHT)
    append_log(root, held)
    return held


def raise_own_doing(done: Mapping[str, Any], now_ts: Any = None, *,
                    root: Any = None) -> Optional[Dict[str, Any]]:
    """这一步若让她想跟你说一声，就往由头池写一条 own_doing（每次独处最多一条）。

    只有在 own_doing.actions 里点过名的那几项才写；句子由这一次真做的那件事的字段拼出来
    （solitude.json 的 a/b/c 模板），一个字都不许加。池里已经挂了一条没被用掉的就不再写——
    「想跟你说一声」攒多了就成了刷屏，而她要的只是说一句。
    """
    at = _now(now_ts)
    cfg = load_config(root)
    pool = block(cfg, "own_doing")
    actions = pool.get("actions")
    actions = [str(item) for item in actions] if isinstance(actions, (list, tuple))         else list(DEFAULTS["own_doing"]["actions"])
    action = str(done.get("action") or "")
    if action not in actions:
        return None
    if proactive.open_own_doing(root, at) is not None:
        return None
    fields = {"name": str(done.get("name") or (done.get("read") or {}).get("name") or ""),
              "date": str((done.get("read") or {}).get("date") or ""),
              "note": str(done.get("note") or "")}
    because = _render(pool.get(action), fields).strip()
    if not because:
        return None
    row = {"at": at, "day": model.day_key(at), "action": action, "because": because,
           "id": "od-%s-%d" % (model.day_key(at), len(proactive.own_doing_lines(root)) + 1)}
    proactive.append_own_doing(root, row)
    return row


# ---- 读数（GET /day/solitude） --------------------------------------------


def overview(now_ts: Any = None, *, root: Any = None, heart_root: Any = None,
             brain: Any = None, home_root: Any = None,
             limits: Optional[Dict[str, Any]] = None, limit: int = LOG_SHOWN
             ) -> Dict[str, Any]:
    """账尾 + 此刻的读数：三档阈值、两个档位落在哪一档、闲置几分钟、是不是夜里。

    **不动手**：GET 不该顺手做一件事（那是写动作），所以这里只有读数与账尾。
    """
    at = _now(now_ts)
    read = readings(at, root=root, heart_root=heart_root, brain=brain, limits=limits)
    cfg = load_config(root)
    return {"ok": True, "at": at, "root": str(model.day_root(root)),
            "awake_minutes": read["awake_minutes"],
            "thresholds": read["thresholds"], "tier_level": tier_table(limits, root),
            "threshold_key": read["threshold_key"],
            "threshold_minutes": read["threshold_minutes"],
            "reason_code": read["reason_code"],
            "mood_tier": read["mood_tier"], "mood_level": read["mood_level"],
            "mood_now": read["mood_now"],
            "energy_tier": read["energy_tier"], "energy_level": read["energy_level"],
            "energy_value": read["energy_value"],
            "idle_since": read["idle_since"], "idle_minutes": read["idle_minutes"],
            "night": read["night"], "engage": read["engage"],
            "repeat_days": float(cfg.get("repeat_days") or DEFAULTS["repeat_days"]),
            "home_root": str(_home_root(root, home_root)),
            "own_doing": proactive.open_own_doing(root, at),
            "actions": list(ACTION_ORDER), "counts": _counts(root), "log": log_tail(root, limit)}


def _counts(root: Any = None) -> Dict[str, int]:
    """账上每个动作各出现过几次（人一眼看得出"这些天她做了什么"）。"""
    tally: Dict[str, int] = {}
    for row in log_lines(root):
        action = str(row.get("action") or "")
        tally[action] = tally.get(action, 0) + 1
    return tally
