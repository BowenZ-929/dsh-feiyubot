"""日子线的 HTTP 层：十条路由，方法与懒加载都在这里。

判据：
- 方法错一律 405（措辞在 core.handle 的 day 分支）；本模块只认表里那一个方法。
- schedule / delivery 是后续两个并行任务，本波只留调用契约：**函数内懒导入**，
  模块还没落地就回 501 + "未接入：organs.day.xxx 未落地"，绝不让 ImportError 把整个
  core 带崩（导入顺序上 core 要最先汇总各器官表，这里一崩全站都起不来）。
- 哪些路由"未接入"不是手写名单：unconnected_routes() 真的去试着导入，导不进来才算，
  模块一落地它就自动变空（于是 501 是临时状态，不是永久例外）。
- root 只做测试与验收用的数据根覆盖：绝对路径且存在才收，其余忽略并 WARN
  （与 heart.py 的规矩一致，不发明第二套）。
- now 一律透传（ISO8601，本地时区里带偏移）；查询串里的 '+' 要写成 %2B，
  坏值回 400 并点名原值，不穿成 traceback。
- 本模块不写盘的判断权在器官手里：这里只分派，不自己做闸门。
"""
from __future__ import annotations

import importlib
import inspect
import warnings
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from organs.day import model as day_model
from organs.day import night as night_mod
from organs.day import plan as plan_mod
from organs.day import rhythm as rhythm_mod

from .core import Result

DAY_ROUTES = {
    "/day/tick": "POST",
    "/day/plan": "GET",
    "/day/night": "POST",
    "/day/log": "GET",
    "/day/schedule": "GET",
    "/day/schedule/draw": "POST",
    "/day/pending": "GET",
    "/day/deliver": "POST",
    "/day/ack": "POST",
    "/day/reconcile": "POST",
}

DEFAULT_LOG_LIMIT = 20

# 哪条路由要先懒导入哪个模块：这是路由与后继模块的契约（十条里的六条）。
# 注意：这张表不是"未接入"的判定——判定一律靠 unconnected_routes() 真去导入。
_REQUIRES = {
    "/day/schedule": "organs.day.schedule",
    "/day/schedule/draw": "organs.day.schedule",
    "/day/pending": "organs.day.delivery",
    "/day/deliver": "organs.day.delivery",
    "/day/ack": "organs.day.delivery",
    "/day/reconcile": "organs.day.delivery",
}


class QueryError(ValueError):
    """查询串里的值坏了：响成一条 400，不许抛栈穿出去。"""


class NotIntegrated(Exception):
    """模块还没落地：回 501 + 一句能读的文案，不把 ImportError 穿成 500。"""

    def __init__(self, dotted: str, cause: Optional[BaseException] = None) -> None:
        message = "未接入：%s 未落地" % dotted
        if cause is not None:
            message += "（%s: %s）" % (type(cause).__name__, cause)
        super().__init__(message)
        self.dotted = dotted


# ---- 查询串 → 参数 ---------------------------------------------------------


def _root(params: Mapping[str, str]) -> Optional[Path]:
    """params["root"] 只做数据根覆盖：绝对路径且存在才收，否则忽略并 WARN。"""
    raw = (params or {}).get("root")
    if not raw:
        return None
    target = Path(str(raw))
    if not target.is_absolute() or not target.is_dir():
        warnings.warn("日子线·api：root=%r 不是绝对路径或不是目录，按默认根处理" % (raw,),
                      RuntimeWarning, stacklevel=3)
        return None
    return target


def _now(params: Mapping[str, str]) -> Optional[str]:
    """现在几点：给了就校验后透传，没给就交给各模块自己取。"""
    value = (params or {}).get("now")
    if not value:
        return None
    text = str(value)
    try:
        day_model.parse_ts(text)
    except ValueError:
        raise QueryError("now 不是合法 ISO8601：'%s'；时区偏移里的加号在查询串里要写成 %%2B"
                         "（例：2026-11-05T23:59:00%%2B08:00）" % text)
    return text


def _break_step(params: Mapping[str, str], body: Mapping[str, Any]) -> Optional[int]:
    """验收注入：?break_step=N（或 body 里的同名键）让夜的第 N 步当场抛错。

    与 night.run 的 break_step 同义，**只给验收用**：真跑时永远不传。坏值当没传。
    """
    raw = _value(body, params, "break_step")
    if raw is None:
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _int(params: Mapping[str, str], key: str, default: Optional[int] = None) -> Optional[int]:
    """整数参数；坏值回默认，不炸。"""
    raw = (params or {}).get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def _dir(params: Mapping[str, str], key: str) -> Optional[str]:
    """另一个数据根（心的）：与 _root 同一套收法，只是键名可换。"""
    raw = (params or {}).get(key)
    if not raw:
        return None
    target = Path(str(raw))
    if not target.is_absolute() or not target.is_dir():
        warnings.warn("日子线·api：%s=%r 不是绝对路径或不是目录，按默认根处理" % (key, raw),
                      RuntimeWarning, stacklevel=3)
        return None
    return str(target)


def _roots(params: Mapping[str, str]) -> Dict[str, Optional[str]]:
    """三个根：日子线自己的 root，以及家 / 心的 home_root、heart_root。

    派生是安全网：?root= 给了而 home_root / heart_root 没给，就按 root/home、root/heart
    派生——谁忘了传，卡也只会落在这棵临时树下，绝不落到真实的家与真实的心。
    """
    base = _root(params)
    derived = None if base is None else str(base)
    return {"root": derived,
            "home_root": _dir(params, "home_root") or (None if base is None else str(base / "home")),
            "heart_root": _dir(params, "heart_root") or (None if base is None else str(base / "heart"))}


def _value(body: Mapping[str, Any], params: Mapping[str, str], key: str) -> Any:
    """一个参数：body 优先（POST 惯例），其次查询串；都没有就是 None。"""
    for source in (body or {}, params or {}):
        value = source.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _text(body: Mapping[str, Any], params: Mapping[str, str], key: str) -> str:
    value = _value(body, params, key)
    return "" if value is None else str(value)


def _module(dotted: str) -> Any:
    """惰性导入后继模块；没落地就抛 NotIntegrated（由 dispatch 译成 501）。"""
    try:
        return importlib.import_module(dotted)
    except ImportError as exc:
        raise NotIntegrated(dotted, exc)


def _invoke(module: Any, name: str, **kwargs: Any) -> Any:
    """调后继模块的公开动作：只传它签名里真的收的参数，None 一概不传。

    契约（写给 B/C 的那份）只规定了 now_ts / brain / home_root；落地实现各自可以多收
    root / heart_root。按签名过滤一次，谁的口径都不必迁就对方：?root= 与 ?heart_root=
    对整条线都生效，测试也不必去碰真实的数据根（没给的参数就交给器官自己的默认根）。
    """
    func = getattr(module, name)
    params = inspect.signature(func).parameters
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()):
        accepted = dict((key, value) for key, value in kwargs.items() if value is not None)
    else:
        accepted = dict((key, value) for key, value in kwargs.items()
                        if value is not None and key in params)
    return func(**accepted)


def unconnected_routes() -> List[str]:
    """还没落地的路由：真去试着导入，导不进来才算（模块一落地这个集合自动变空）。

    所以 501 是临时状态而不是永久例外：契约测试可以断言
    "实际回 501 的 GET 集合 == unconnected_routes() ∩ GET 路由"，
    本波之后 schedule / delivery 一落地，那两条路由就自动回到"必须 200"。
    """
    out: List[str] = []
    for route, dotted in sorted(_REQUIRES.items()):
        try:
            importlib.import_module(dotted)
        except ImportError:
            out.append(route)
    return out


# ---- 十条路由 -------------------------------------------------------------


def _tick(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/tick：手工 tick 一次（幂等；支持 ?now=）。

    到点的夜在这里真跑（四步固定顺序）并结账：手工触发与节律线程走同一条路，
    单飞锁挡住"同一晚跑两遍"。失败的那次不下账（留 pending 等下一 tick 只重跑失败步），
    试满次数仍失败的进 never_ran 栏——返回体的 night 段里 executed / retried /
    never_ran 三个列表都摆出来，任何"没跑成"的路都看得见。
    ?break_step=N 只给验收用（与 night.run 的同名参数同义）。
    """
    roots = _roots(params)
    payload = rhythm_mod.tick(_now(params), root=roots["root"], brain=brain,
                              heart_root=roots["heart_root"],
                              break_step=_break_step(params, body))
    return 200, payload


def _plan(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/plan：pending / consumed / blocked / expired / never_ran 五栏分开。"""
    return 200, plan_mod.listing(_root(params))


def _night(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/night：手工跑一次夜（?now= / ?break_step=N 故意让第 N 步抛错）。"""
    roots = _roots(params)
    payload = night_mod.run(_now(params), root=roots["root"], brain=brain,
                            heart_root=roots["heart_root"],
                            break_step=_int(params, "break_step"))
    return 200, payload


def _log(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/log：夜日志尾部（?limit=）。"""
    limit = _int(params, "limit", DEFAULT_LOG_LIMIT)
    return 200, night_mod.log_tail(DEFAULT_LOG_LIMIT if limit is None else limit,
                                   root=_root(params))


def _schedule(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/schedule：今日日程（懒导入 organs.day.schedule.today）。

    只读：?now= 与其它器官的只读端点一样可注入时刻（day / slot 都跟着这个时刻走），
    它不结算心、不落日志——要把这一抽定下来走 POST /day/schedule/draw。
    """
    module = _module("organs.day.schedule")
    roots = _roots(params)
    return 200, dict(_invoke(module, "today", now_ts=_now(params), **roots))


def _schedule_draw(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/schedule/draw：抽一次并落两条痕迹（懒导入 draw_and_land）。"""
    module = _module("organs.day.schedule")
    roots = _roots(params)
    return 200, dict(_invoke(module, "draw_and_land", now_ts=_now(params), brain=brain,
                             **roots))


def _pending(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/pending：待说队列（懒导入 organs.day.delivery.pending）。"""
    module = _module("organs.day.delivery")
    return 200, dict(_invoke(module, "pending", now_ts=_now(params), root=_root(params)))


def _deliver(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/deliver：入队一条待说（懒导入 intake）。"""
    module = _module("organs.day.delivery")
    text = _text(body, params, "text")
    if not text:
        return 400, {"ok": False, "message": "deliver 要一条 text（body 或查询串）"}
    out = _invoke(module, "intake", text=text, to=_text(body, params, "to"),
                  now_ts=_now(params), root=_root(params))
    return 200, dict(out)


def _ack(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/ack：记一条通道 ack（retcode 为准；懒导入 ack）。"""
    module = _module("organs.day.delivery")
    item_id = _text(body, params, "item_id")
    if not item_id:
        return 400, {"ok": False, "message": "ack 要一个 item_id（body 或查询串）"}
    raw_code = _value(body, params, "retcode")
    if raw_code is None:
        return 400, {"ok": False, "message": "ack 要一个 retcode（通道原样回报的码）"}
    try:
        retcode = int(str(raw_code))
    except (TypeError, ValueError):
        return 400, {"ok": False, "message": "retcode 要一个整数，收到 %r" % (raw_code,)}
    return 200, dict(_invoke(module, "ack", item_id=item_id, retcode=retcode,
                             now_ts=_now(params), root=_root(params)))


def _reconcile(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/reconcile：手工对账一次（懒导入 reconcile）。"""
    module = _module("organs.day.delivery")
    return 200, dict(_invoke(module, "reconcile", now_ts=_now(params), root=_root(params)))


_ROUTES = {
    "/day/tick": _tick,
    "/day/plan": _plan,
    "/day/night": _night,
    "/day/log": _log,
    "/day/schedule": _schedule,
    "/day/schedule/draw": _schedule_draw,
    "/day/pending": _pending,
    "/day/deliver": _deliver,
    "/day/ack": _ack,
    "/day/reconcile": _reconcile,
}


def dispatch(route: str, params: Mapping[str, str], body: Optional[Mapping[str, Any]] = None,
             store: Any = None, brain: Any = None) -> Result:
    """十条路由的实际分派；store 不用（日子线自带账），brain 喂夜与日程。"""
    handler = _ROUTES.get(route)
    if handler is None:
        return 404, {"ok": False, "message": "没有这个端点：%s" % route}
    try:
        return handler(params or {}, body or {}, brain)
    except QueryError as exc:
        return 400, {"ok": False, "message": str(exc)}
    except NotIntegrated as exc:
        return 501, {"ok": False, "message": str(exc), "module": exc.dotted}
    except plan_mod.PlanError as exc:
        return 500, {"ok": False, "message": "%s（计划账坏了不许当空账跑）" % exc}
