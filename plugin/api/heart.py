"""心的九个只读端点：把 organs/heart 的读数原样搬到 HTTP 上，一个字节都不写。

判据（architecture §9.2：心与脑只能看）：
- 九条路由全是 GET；写方法在 core.handle 里就被 405 挡掉，永远到不了这里。
- 合成只有一处：/heart/effective 的三块（mood / axes / voice）由
  organs/heart/effective.py 出；/heart/overview 的 effective 块、/heart/mood 的
  两个数与档位词、/heart/engage 的 mood_now 都从那里取，本模块不自己换算档位。
- 本模块只读：不调 apply_event / settle_day / apply_burst / apply_long_reply /
  apply_daily / settle_week，也不调 decide_and_log——/heart/engage 是干跑。
- 数值只留内部：/heart 与 /heart/baseline 的文本走 card.render_text，只出档位词；
  待定两轴（耐心 / 记性）显式 pending、标灰、跳过注入，不写 0。
- 坏卡不是错误页：卡坏了如实报 status="拒载" + ok=False + issues（每条带 code），
  HTTP 仍然 200——页面要能显示，500 等于把"她没载入"藏起来。
- root 只做测试用覆盖：只接受绝对路径且存在的目录，其余忽略并 WARN（不许被查询串
  指到任意路径上去写；本模块本来也不写，但读别人家的盘同样是越界）。
- now 一律透传：面板与验收都把时间往前推，各模块自己按时间戳现算。
  使用注记：now 是 ISO8601，**时区偏移里的 '+' 在查询串里必须写成 %2B**——
  parse_qs 会把裸 '+' 解成空格（"2026-11-05T09:00:00+09:00" 到手就成了
  "2026-11-05T09:00:00 09:00"），这里当场回 400 并点名原值，不让它穿成 traceback。

数据根：默认 store.DATA_DIR（各模块自己的默认值），本模块不发明第二套根路径。
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from organs.heart import card as card_module
from organs.heart import effective as effective_module
from organs.heart import energy as energy_module
from organs.heart import engage as engage_module
from organs.heart import model as heart_model
from organs.heart import mood as mood_module
from organs.heart import offset as offset_module
from organs.heart import pending as pending_module
from organs.heart import ports as ports_module

from .core import Result

HEART_ROUTES = {
    "/heart": "GET",
    "/heart/overview": "GET",
    "/heart/baseline": "GET",
    "/heart/mood": "GET",
    "/heart/energy": "GET",
    "/heart/offset": "GET",
    "/heart/engage": "GET",
    "/heart/effective": "GET",
    "/heart/pending": "GET",
}

# /heart/offset 每轴对外只承诺这四个键；offset 模块以后多出来的键原样透传。
AXIS_KEYS = ("value", "decayed_value", "day_delta", "clamped")

DEFAULT_WHO = "主人"
DEFAULT_TRAJECTORY_LIMIT = 200

# 查询串里的真假：面板与验收用的都是 0 / 1，顺手认几个常见写法。
TRUTHY = ("1", "true", "yes", "on")

STATUS_CONNECTED = "已接入"
STATUS_UNLOADABLE = "拒载"


# ---- 查询串 → 参数 -------------------------------------------------------


def _root(params: Mapping[str, str]) -> Optional[Path]:
    """params["root"] 只做测试用覆盖：绝对路径且存在才收，否则忽略并 WARN。"""
    raw = (params or {}).get("root")
    if not raw:
        return None
    target = Path(str(raw))
    if not target.is_absolute() or not target.is_dir():
        warnings.warn(
            "心·api：root=%r 不是绝对路径或不是目录，按默认根目录处理" % (raw,),
            RuntimeWarning, stacklevel=3)
        return None
    return target


class QueryError(ValueError):
    """查询串里的值坏了：要响成一条 400（说清哪个值、怎么改），不许抛栈穿出去。"""


def _now_message(value: Any) -> str:
    """坏 now 的人话：原值原样带出来，再点明 '+' → %2B 这条最常见的坑。"""
    text = "" if value is None else str(value)
    if " " in text:
        why = ("收到的值里有个空格——查询串里的 '+' 被解码成了空格；"
               "时区偏移里的加号要写成 %2B")
    else:
        why = "时区偏移里的加号在查询串里要写成 %2B"
    return "now 不是合法 ISO8601：'%s'；%s（例：2026-11-05T09:00:00%%2B09:00）" % (text, why)


def _now(params: Mapping[str, str]) -> Optional[str]:
    """现在几点：给了就校验后透传（验收把时间往前推靠它），没给就交给各模块自己取。"""
    value = (params or {}).get("now")
    if not value:
        return None
    text = str(value)
    try:
        heart_model.parse_ts(text)
    except ValueError:
        raise QueryError(_now_message(text))
    return text


def _limit(params: Mapping[str, str], default: int) -> int:
    """轨迹条数上限；坏值回默认，不炸。"""
    raw = (params or {}).get("limit")
    if not raw:
        return default
    try:
        number = int(str(raw))
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _flag(params: Mapping[str, str], key: str) -> bool:
    """0/1 这类开关；只有明确写成真才算真。"""
    raw = (params or {}).get(key)
    return str(raw).strip().lower() in TRUTHY if raw is not None else False


def _who(params: Mapping[str, str]) -> str:
    value = (params or {}).get("who")
    text = str(value).strip() if value is not None else ""
    return text or DEFAULT_WHO


# ---- 各块的读数 ----------------------------------------------------------


def _card_path(root: Optional[Path]) -> Optional[Path]:
    """卡文件：root 覆盖时落在 root 下，否则交给 card 模块自己的默认路径。

    规则只有一处：effective.card_path（organ 不能 import api，所以那边是主，
    本层直接使唤它——"root 是数据目录"这件事两边不可能理解成两套）。
    """
    return effective_module.card_path(root)


def _offsets(root: Optional[Path], now: Optional[str]) -> Dict[str, float]:
    """六轴此刻的偏移（0 附近的小数），喂给 card.view / render_text 叠档。"""
    snapshot = offset_module.overview(root=root, now=now)
    axes = snapshot.get("axes") or {}
    values: Dict[str, float] = {}
    for axis, row in axes.items():
        if not isinstance(row, dict):
            continue
        try:
            values[axis] = float(row.get("value") or 0.0)
        except (TypeError, ValueError):
            values[axis] = 0.0
    return values


def _baseline(root: Optional[Path], offsets: Dict[str, float]) -> Dict[str, Any]:
    """卡 → (ok, view, render, issues)；坏卡不抛，如实报每一条拒载码。"""
    try:
        card = card_module.load_card(_card_path(root))
    except card_module.CardError as exc:
        return {"ok": False, "baseline": {}, "render": "", "issues": list(exc.issues)}
    return {
        "ok": True,
        "baseline": card_module.view(card, offsets),
        "render": card_module.render_text(card, offsets),
        "issues": [],
    }


def _effective_blocks(root: Optional[Path], now: Optional[str],
                      mood_read: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """effective_state 的三块：mood 走 mood 的读数，offset 走 offset 的读数，卡走 load_card。

    坏卡照旧抛 CardError——本函数不兜底，由调用方决定是「拒载」还是只取 mood 那一块。
    mood_read 传进来就复用（同一次请求里不必读两遍），不传就现读。
    """
    values = mood_read if mood_read is not None else mood_module.read(root, now)
    return effective_module.effective_state(
        values["baseline"], values["now"],
        offset=offset_module.overview(root=root, now=now),
        root=root,
        now=now,
    )


def _effective_readout(root: Optional[Path], now: Optional[str],
                       values: Dict[str, Any]) -> Dict[str, Any]:
    """三块 + 只读外壳；坏卡如实拒载（ok=False、status="拒载"、issues 逐条）。"""
    readout: Dict[str, Any] = {
        "readonly": True,
        "agent": "heart",
        "as_of": values["as_of"],
    }
    try:
        blocks = _effective_blocks(root, now, mood_read=values)
    except card_module.CardError as exc:
        readout.update({"ok": False, "status": STATUS_UNLOADABLE,
                        "issues": list(exc.issues)})
        return readout
    readout.update({"ok": True, "status": STATUS_CONNECTED})
    readout.update(blocks)
    return readout


def _pending() -> List[str]:
    """待定两轴：跳过注入、面板标灰，不写数也不写 0。"""
    return list(heart_model.PENDING_AXES)


def _baseline_payload(root: Optional[Path], now: Optional[str]) -> Dict[str, Any]:
    """总览与 /heart/baseline 共用的那份。"""
    base = _baseline(root, _offsets(root, now))
    return {
        "ok": base["ok"],
        "readonly": True,
        "agent": "heart",
        "baseline": base["baseline"],
        "render": base["render"],
        # pending 一直是"待定两轴名单"，第三轮念头层不改它的意思（老键不换义）：
        # 念头块另开一个键叫 thoughts（见 _overview）——外层是块、块里的列表也叫
        # thoughts，两层各归各的，不共用一个名字。
        "pending": _pending(),
        "issues": base["issues"],
    }


# ---- 九个端点 ------------------------------------------------------------


def _overview(params: Mapping[str, str]) -> Result:
    """/heart 与 /heart/overview：一次拿齐四块读数（页面只发一次请求就够）。

    第五块 effective 是合成读数：/heart/effective 那份原样嵌进来，两个端点的数
    因此不可能分叉（都从同一个 effective_state 出）。
    """
    root, now = _root(params), _now(params)
    payload = _baseline_payload(root, now)
    payload["status"] = STATUS_CONNECTED if payload["ok"] else STATUS_UNLOADABLE
    mood_read = mood_module.read(root, now)
    payload["mood"] = mood_read
    payload["energy"] = energy_module.read(root, now)
    payload["offset"] = offset_module.overview(root, now)
    payload["effective"] = _effective_readout(root, now, mood_read)
    # 念头块与事件窗口：两块都是纯读（pending.overview 不落笔、window_signal 现算）。
    # 块名 thoughts（不叫 pending：pending 是待定轴名单，老键不换义）。
    payload["thoughts"] = pending_module.overview(root, now)
    payload["window"] = engage_module.window_signal(root, now)
    return 200, payload


def _baseline_route(params: Mapping[str, str]) -> Result:
    """/heart/baseline：六轴档位词 + 待定名单，数值一步都不出门。"""
    root, now = _root(params), _now(params)
    return 200, _baseline_payload(root, now)


def _mood(params: Mapping[str, str]) -> Result:
    """/heart/mood：mood.read 的全部键 + 轨迹账本（页面画曲线用的就是它）。

    两个数与档位词再过一遍 effective.mood_block：档位要从**公开出去的那两个数**算，
    才与 /heart/effective 的 mood / voice.tone 逐字相同（同一个合成点出的数）。
    """
    root, now = _root(params), _now(params)
    values = mood_module.read(root, now)
    payload = dict(values)
    payload.update(effective_module.mood_block(values["baseline"], values["now"]))
    payload["trajectory"] = mood_module.trajectory(root, limit=_limit(params, DEFAULT_TRAJECTORY_LIMIT))
    return 200, payload


def _energy(params: Mapping[str, str]) -> Result:
    """/heart/energy：数值 + 档位词 + 这次结算从哪算到哪。"""
    root, now = _root(params), _now(params)
    return 200, energy_module.overview(root, now)


def _offset(params: Mapping[str, str]) -> Result:
    """/heart/offset：每轴四个键（多出来的原样透传），外加读数时刻。"""
    root, now = _root(params), _now(params)
    snapshot = offset_module.overview(root=root, now=now)
    axes: Dict[str, Dict[str, Any]] = {}
    for axis, row in (snapshot.get("axes") or {}).items():
        item = dict(row) if isinstance(row, dict) else {}
        for key in AXIS_KEYS:
            if key not in item:
                warnings.warn(
                    "心·api：offset 轴 %s 少了 %s，按 0/False 占位" % (axis, key),
                    RuntimeWarning, stacklevel=3)
                item[key] = False if key == "clamped" else 0
        axes[axis] = item
    return 200, {"axes": axes, "as_of": snapshot.get("at")}


def _effective_route(params: Mapping[str, str]) -> Result:
    """/heart/effective：三块合成读数（mood / axes / voice）+ 只读外壳。

    只读：不写盘、不调任何 apply_* / settle_*；坏卡照旧 HTTP 200 + ok=False +
    status="拒载" + issues（与 /heart、/heart/baseline 一致，页面要能显示）。
    """
    root, now = _root(params), _now(params)
    return 200, _effective_readout(root, now, mood_module.read(root, now))


def _pending_route(params: Mapping[str, str]) -> Result:
    """/heart/pending：念头层的读数（config + 每条念头的 state / attempts / 剩余 TTL）。

    纯读：pending.overview 不落笔，过期的念头这里只标 expired 不标 faded。
    它不读卡，所以坏卡与它无关——卡坏了这一块照常给出念头。
    """
    root, now = _root(params), _now(params)
    payload = pending_module.overview(root, now)
    payload.update({"ok": True, "readonly": True, "agent": "heart",
                    "status": STATUS_CONNECTED})
    return 200, payload


def _engage(params: Mapping[str, str], brain: Any) -> Result:
    """/heart/engage：干跑一次判决——只调 engage.decide，不写日志、不说话。

    门的输入走 EngageInput.from_effective：mood_now 与 /heart/effective 的 mood.now
    是同一个数（面板说「还行」而她闭嘴这类分叉因此不可能发生）。
    """
    root, now = _root(params), _now(params)
    intent = str((params or {}).get("intent") or heart_model.INTENT_PASSIVE).strip()
    if intent not in heart_model.INTENTS:
        return 400, {"ok": False, "dry_run": True,
                     "message": "intent 只认 %s，收到 %r"
                                % ("、".join(heart_model.INTENTS), intent)}
    who = _who(params)
    interrupted = (params or {}).get("interrupted") or None
    # 熟稔与今日条数只从脑的公开读数来；没有脑就是 0 / 0，绝不抛。
    readout = ports_module.from_brain(brain)
    values = mood_module.read(root, now)
    try:
        eff = _effective_blocks(root, now, mood_read=values)
    except card_module.CardError:
        # 坏卡不影响门：门只读 mood.now（档位与禁令本来就不进门）。拒载由
        # /heart、/heart/baseline、/heart/effective 如实报出来，判决这里照常干跑。
        eff = {"mood": effective_module.mood_block(values["baseline"], values["now"])}
    # 第 4 道软门读的是窗口：不传 = 这道门只在单测里存在（端点永远判不出 ignored-recently）。
    window = engage_module.window_signal(root, now)
    judgement = engage_module.EngageInput.from_effective(
        eff,
        window=window,
        energy=energy_module.read(root, now)["value"],
        familiarity=readout.familiarity(who, now),
        today_count=readout.today_count(who, now),
        intent=intent,
        who=who,
        now=now,
        insulted_today=_flag(params, "insulted"),
        last_interrupted_at=str(interrupted) if interrupted else None,
    )
    payload = dict(engage_module.decide(judgement))
    payload["dry_run"] = True
    payload["who"] = who
    return 200, payload


def dispatch(route: str, params: Mapping[str, str],
             store: Any = None, brain: Any = None) -> Result:
    """心的九条只读端点；store 不用（心各模块自带默认根），brain 只喂 engage。

    只收 ValueError 这一种：查询串里的时间戳坏了一个（now / interrupted）就走 400，
    把坏值原样带出来——失败必须响，但响成一条能读的 400，不是一条 traceback。
    其余异常照旧抛：真 bug 不许被这层"兜底"吞掉。
    """
    try:
        return _dispatch(route, params, brain)
    except QueryError as exc:
        return 400, {"ok": False, "message": str(exc)}
    except ValueError as exc:
        return 400, {"ok": False,
                     "message": "%s（%s）" % (_now_message((params or {}).get("now")), exc)}


def _dispatch(route: str, params: Mapping[str, str], brain: Any = None) -> Result:
    """各端点的实际分派；干跑与只读都在这里，一个字节都不写。"""
    if route in ("/heart", "/heart/overview"):
        return _overview(params)
    if route == "/heart/baseline":
        return _baseline_route(params)
    if route == "/heart/mood":
        return _mood(params)
    if route == "/heart/energy":
        return _energy(params)
    if route == "/heart/offset":
        return _offset(params)
    if route == "/heart/engage":
        return _engage(params, brain)
    if route == "/heart/effective":
        return _effective_route(params)
    if route == "/heart/pending":
        return _pending_route(params)
    return 404, {"ok": False, "message": "没有这个端点：%s" % route}
