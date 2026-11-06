"""手的三个只读端点：把 organs/hand 的读数搬到 HTTP 上，一个字节都不写。

判据（第四轮手线：手这个器官已经落地，这里只把它的读数搬到只读面上）：
- 三条路由全是 GET；写方法在 core.handle 里就被 405 挡掉，永远到不了这里。
- **只读到底**：本模块不调 add_task / complete_task（那是两条动作：会落卡、会记手账），
  也不自己算额度、不自己算「到点没做」——额度走 quota.remaining、到期走 actions.due、
  手账走 ledger.read_tail、任务卡走 home.cards.list_tasks。读数只有一个来源，
  面板与验收读到的数因此不可能与动作回执分叉。
- who 缺省就用空串算一份额度读数，不 400：这是只读查询，没人点名也答得出「今天还剩几次」。
- now 一律透传（面板与验收把时间往前推），坏值当场 400 并点名原值。
  使用注记：now 是 ISO8601，**时区偏移里的 '+' 在查询串里必须写成 %2B**——
  parse_qs 会把裸 '+' 解成空格（"2026-11-06T12:00:00+08:00" 到手就成了
  "2026-11-06T12:00:00 08:00"），这里照 api/heart.py 的做法当场回 400、把收到的值
  原样摆出来，不让它穿成 traceback。
- root 只做测试用覆盖：只接受绝对路径且存在的目录，并且要过 organs/paths.py 那道门禁
  （落在仓库里却不在 <仓库>/home 下的照样拒），其余忽略并 WARN——不许被查询串指到
  任意路径上去读别人家的盘。手给的 root 是**手自己**那个目录，家是它的兄弟目录；
  这条换算只长在 organs/hand/store.py 一处，本层直接使唤它，不再拼第二遍。
- 读失败不是 500：盘读不出来就如实回 200 + ok=false + 一句人话（页面要能显示），
  只有查询串里的值坏了才是 400。
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from organs import paths as organ_paths
from organs.hand import actions as hand_actions
from organs.hand import ledger as hand_ledger
from organs.hand import permit as hand_permit
from organs.hand import quota as hand_quota
from organs.hand import store as hand_store
from organs.hand import whitelist as hand_whitelist
from organs.home import cards as home_cards

from .core import Result

#: 三条全是 GET：写方法由 core.handle 的 405 分支挡。
HAND_ROUTES = {"/hand": "GET", "/hand/tasks": "GET", "/hand/ledger": "GET"}

AGENT = "hand"

#: 手账尾读：默认最后 50 个动作，夹在 1–200 之间。
DEFAULT_LEDGER_LIMIT = 50
LEDGER_LIMIT_MIN = 1
LEDGER_LIMIT_MAX = 200

LIMITS_NOTE = ("额度上限来自数据文件（手实盘下的 limits.json，人可改、改文件即生效；"
               "回执里的 source 说清这次是哪一级给的：explicit／live／factory／default），"
               "已用次数从手账现算（只数今天、这个人、state=done 的行的最后一个状态），"
               "不另开计数器——本端点一个字节都不写盘。")


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
    return "now 不是合法 ISO8601：'%s'；%s（例：2026-11-06T09:00:00%%2B09:00）" % (text, why)


def _root(params: Mapping[str, str]) -> Optional[Path]:
    """params["root"] 只做测试用覆盖：绝对路径、存在、且过门禁才收，否则忽略并 WARN。"""
    raw = (params or {}).get("root")
    if not raw:
        return None
    target = Path(str(raw))
    if not target.is_absolute() or not target.is_dir():
        warnings.warn(
            "手·api：root=%r 不是绝对路径或不是目录，按默认根目录处理" % (raw,),
            RuntimeWarning, stacklevel=3)
        return None
    try:
        hand_store.hand_root(target)
    except organ_paths.PathError as exc:
        warnings.warn(
            "手·api：root=%r 没过实盘门禁（%s），按默认根目录处理" % (raw, exc.code),
            RuntimeWarning, stacklevel=3)
        return None
    return target


def _now(params: Mapping[str, str]) -> Optional[str]:
    """现在几点：给了就校验后透传（验收把时间往前推靠它），没给就交给各模块自己取。"""
    value = (params or {}).get("now")
    if not value:
        return None
    text = str(value)
    try:
        hand_store.parse_ts(text)
    except (TypeError, ValueError):
        raise QueryError(_now_message(text))
    return text


def _who(params: Mapping[str, str]) -> str:
    """谁：**查询串原样**（不 strip、不换称呼）；没给就是空串，照空串算一份额度读数。"""
    raw = (params or {}).get("who")
    return "" if raw is None else str(raw)


def _limit(params: Mapping[str, str]) -> int:
    """手账尾读的条数：默认 50，夹在 1–200；读不成数的不炸（回默认），这是只读查询。"""
    raw = (params or {}).get("limit")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_LEDGER_LIMIT
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_LEDGER_LIMIT
    return max(LEDGER_LIMIT_MIN, min(LEDGER_LIMIT_MAX, number))


def _home_of(root: Optional[Path]) -> Optional[Path]:
    """家的根：手给的 root 是手自己的目录，家是兄弟目录——换算只用 store 那一份算法。"""
    if root is None:
        return None
    return hand_store.home_root(root)


def _style_table() -> Dict[str, List[int]]:
    """「怎么说话」能改哪两样、各自能写多少：**从 whitelist.STYLE_KEYS 派生**。

    范围的真源在手那边的一份常量里（手按它判、这里按它显示）。本层不抄第二份：
    加一个旋钮、改一次范围，面板跟着变，而不是等谁想起来再改一行。
    """
    return {name: list(bounds) for name, bounds in hand_whitelist.STYLE_KEYS.items()}


def _actions_table() -> Dict[str, Any]:
    """判据快照的三块：名单（白名单）、风险档、要不要一次性授权。

    三块都从手自己的常量与函数派生（不在这里抄一遍名单）：加一个动作时，
    面板跟着变，而不是本文件忘改一行。
    """
    actions: List[str] = list(hand_whitelist.ACTIONS)
    return {
        "actions": actions,
        "risk": {name: hand_permit.risk_of(name) for name in actions},
        "needs_grant": {name: hand_permit.needs_grant(name) for name in actions},
    }


# ---- 三个端点 ------------------------------------------------------------


def _snapshot(params: Mapping[str, str]) -> Result:
    """/hand：判据快照 + 今天还剩几次。who 缺省按空串算，不 400。

    额度只调 quota.remaining（本层不数额度：账头是唯一计数真源）。
    多一格 `style_keys`：`set_style` 能写的那两个旋钮与闭区间（从手那边的常量派生）。
    """
    root, now = _root(params), _now(params)
    who = _who(params)
    payload: Dict[str, Any] = {"ok": True, "readonly": True, "agent": AGENT}
    payload.update(_actions_table())
    payload["style_keys"] = _style_table()
    payload["who"] = who
    payload["quota"] = hand_quota.remaining(who, now=now, root=root)
    payload["limits_note"] = LIMITS_NOTE
    return 200, payload


def _tasks(params: Mapping[str, str]) -> Result:
    """/hand/tasks：还没做完的任务卡 + 其中到点没做的那些（due）。

    tasks 直接用 home.cards.list_tasks 折好的卡（done／done_count 都是折出来的，
    本层不再算一遍）；due 用 hand.actions.due（「到点没做」的判据只在那一处）。
    """
    root, now = _root(params), _now(params)
    cards = [card for card in home_cards.list_tasks(root=_home_of(root))
             if not card.get("done")]
    due = list(hand_actions.due(now=now, root=root))
    return 200, {
        "ok": True, "readonly": True, "agent": AGENT,
        "now": hand_store.now_iso(now),
        "tasks": cards,
        "due": due,
        "count": len(cards),
        "due_count": len(due),
    }


def _ledger_tail(params: Mapping[str, str]) -> Result:
    """/hand/ledger：手账尾读（折叠后的动作，limit 数的是**动作**，不是盘上的行）。"""
    root = _root(params)
    limit = _limit(params)
    rows = hand_ledger.read_tail(limit, root=root)
    return 200, {
        "ok": True, "readonly": True, "agent": AGENT,
        "limit": limit,
        "rows": rows,
        "count": len(rows),
    }


# ---- 分派 ----------------------------------------------------------------


def _dispatch(route: str, params: Mapping[str, str]) -> Result:
    if route == "/hand":
        return _snapshot(params)
    if route == "/hand/tasks":
        return _tasks(params)
    if route == "/hand/ledger":
        return _ledger_tail(params)
    return 404, {"ok": False, "message": "没有这个端点：%s" % route}


def dispatch(route: str, params: Mapping[str, str],
             store: Any = None, brain: Any = None) -> Result:
    """手的三个只读端点；store 与 brain 都不看（手不读魂、不读脑，也不借核的 store 当根）。

    只把两类失败翻成回执：查询串里的值坏了 → 400（把坏值原样带出来）；
    盘读不出来 → 200 + ok=false（页面要能显示，「读失败」不是 500）。
    其余异常照旧抛：真 bug 不许被这层兜底吞掉。
    """
    try:
        return _dispatch(route, params)
    except QueryError as exc:
        return 400, {"ok": False, "readonly": True, "agent": AGENT,
                     "message": str(exc)}
    except organ_paths.PathError as exc:
        return 400, {"ok": False, "readonly": True, "agent": AGENT,
                     "message": "root 不能用：%s" % exc.message}
    except OSError as exc:
        return 200, {"ok": False, "readonly": True, "agent": AGENT,
                     "message": "手这边没读出来（端点只读，不会替你写一份）：%s" % exc}
