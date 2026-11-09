"""日子线的 HTTP 层：十三条路由，方法与懒加载都在这里。

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

from organs.mind import persona as persona_mod

import importlib
import inspect
import warnings
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from organs.day import life as life_mod
from organs.day import model as day_model
from organs.day import night as night_mod
from organs.day import plan as plan_mod
from organs.day import rhythm as rhythm_mod

from .core import Result

DAY_ROUTES = {
    "/day/growth": "GET",
    "/day/growth/config": "POST",
    "/day/growth/profile": "POST",
    "/day/life": "GET",
    "/day/life/config": "POST",
    "/day/life/adjust": "POST",
    "/day/life/generate": "POST",
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
    "/day/proactive": "GET",
    "/day/proactive/disable": "POST",
    "/day/proactive/enable": "POST",
    "/day/proactive/consider": "POST",
    "/day/proactive/outcome": "POST",
    "/day/solitude": "GET",
    "/day/solitude/consider": "POST",
}

DEFAULT_LOG_LIMIT = 20

# 哪条路由要先懒导入哪个模块：这是路由与后继模块的契约（十三条里的九条）。
# 注意：这张表不是"未接入"的判定——判定一律靠 unconnected_routes() 真去导入。
_REQUIRES = {
    "/day/schedule": "organs.day.schedule",
    "/day/schedule/draw": "organs.day.schedule",
    "/day/pending": "organs.day.delivery",
    "/day/deliver": "organs.day.delivery",
    "/day/ack": "organs.day.delivery",
    "/day/reconcile": "organs.day.delivery",
    "/day/proactive": "organs.day.proactive",
    "/day/proactive/disable": "organs.day.proactive",
    "/day/proactive/enable": "organs.day.proactive",
    "/day/proactive/consider": "organs.day.proactive",
    "/day/proactive/outcome": "organs.day.proactive",
    "/day/solitude": "organs.day.solitude",
    "/day/solitude/consider": "organs.day.solitude",
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


# ---- 十五条路由 -----------------------------------------------------------


def run_tick(now: Any = None, *, root: Any = None, brain: Any = None,
             heart_root: Any = None, home_root: Any = None,
             break_step: Optional[int] = None, store: Any = None, proactive_guard=None) -> Dict[str, Any]:
    """一次 tick 的唯一实现：HTTP 的 /day/tick 与 devserver 的节律线程都从这里走。

    now 不给就是"现在"（线程永远不传 now，只有验收与测试才指名时刻）；
    原有节律交给 rhythm.tick；有魂时再由 life.tick 补连续生活计划。
    抽这一层只有一个理由：端点与线程共用同一份，世上没有第二套 tick。
    """
    persona = persona_mod.render(store, brain, root=root)
    from organs.day import autonomy
    # 已执行活动的回执也要在关闭新模式后收尾；不发起新活动。
    activity_receipts = autonomy.settle(now, root=root, home_root=home_root)
    from organs.heart import rest, pending
    actual_heart = heart_root if heart_root is not None else (Path(root)/'heart' if root is not None else None)
    resting = rest.advance(actual_heart, now)
    from . import sleep_delivery
    sleep_notices = sleep_delivery.run(root=root, heart_root=actual_heart, brain=brain, store=store,
                                      guard=proactive_guard)
    if resting['phase'] == 'awake':
        pending.derive(actual_heart, now)
    from organs.day import proactive
    from . import proactive_delivery
    settings = proactive.load_config(root).get('delivery', {})
    runner = (partial(proactive_delivery.run, root=root, heart_root=actual_heart,
                      home_root=home_root, brain=brain, store=store, guard=proactive_guard)
              if isinstance(settings, dict) and settings.get('enabled') is True
              and store is not None and brain is not None else None)
    result = rhythm_mod.tick(now, root=root, brain=brain, heart_root=heart_root,
                             home_root=home_root, break_step=break_step, persona=persona, proactive_runner=runner,
                             store=store)
    result['rest'] = resting
    if sleep_notices:
        result['sleep_notices'] = sleep_notices
    if activity_receipts:
        result['activity_receipts'] = activity_receipts
    if store is not None:
        persona = persona_mod.render(store, brain, root=root)
        result["life"] = life_mod.tick(persona=persona, now=now, root=root, heart_root=actual_heart)
        from organs.day import phone_browse
        from organs.day import growth, autonomy
        if growth.enabled(root):
            result["autonomy"] = autonomy.tick(now, persona=persona, root=root, heart_root=actual_heart,
                                                home_root=home_root, brain=brain)
        else:
            result["phone"] = phone_browse.tick(persona=persona, now=now, root=root, heart_root=heart_root)
    return result


def _tick(params: Mapping[str, str], body: Mapping[str, Any], brain: Any, store: Any = None) -> Result:
    """/day/tick：手工 tick 一次（幂等；支持 ?now=）。

    到点的夜在这里真跑（四步固定顺序）并结账：手工触发与节律线程走同一条路，
    单飞锁挡住"同一晚跑两遍"。失败的那次不下账（留 pending 等下一 tick 只重跑失败步），
    试满次数仍失败的进 never_ran 栏——返回体的 night 段里 executed / retried /
    never_ran 三个列表都摆出来，任何"没跑成"的路都看得见。
    ?break_step=N 只给验收用（与 night.run 的同名参数同义）。
    实现只有 run_tick 那一份，这里只负责从查询串里取参数。
    """
    roots = _roots(params)
    payload = run_tick(_now(params), root=roots["root"], brain=brain,
                       heart_root=roots["heart_root"],
                       break_step=_break_step(params, body), store=store)
    return 200, payload


def _plan(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/plan：pending / consumed / blocked / expired / never_ran 五栏分开。"""
    return 200, plan_mod.listing(_root(params))


def _night(params: Mapping[str, str], body: Mapping[str, Any], brain: Any, store: Any = None) -> Result:
    """/day/night：手工跑一次夜（?now= / ?break_step=N 故意让第 N 步抛错）。"""
    roots = _roots(params)
    payload = night_mod.run(_now(params), root=roots["root"], brain=brain,
                            heart_root=roots["heart_root"],
                            break_step=_int(params, "break_step"), persona=persona_mod.render(store, brain, root=roots["root"]))
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
    roots = _roots(params)
    return 200, dict(_invoke(module, "ack", item_id=item_id, retcode=retcode,
                             now_ts=_now(params), root=roots["root"], heart_root=roots["heart_root"]))


def _reconcile(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """/day/reconcile：手工对账一次（懒导入 reconcile）。"""
    module = _module("organs.day.delivery")
    return 200, dict(_invoke(module, "reconcile", now_ts=_now(params), root=_root(params)))


def _proactive(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """GET /day/proactive：动念账尾部 + 此刻的读数（给谁、降权前后、额度、静音、streak）。

    纯读：不动念、不排计划、不调核（GET 不该写盘，那是写动作）。
    """
    module = _module("organs.day.proactive")
    roots = _roots(params)
    return 200, dict(_invoke(module, "overview", now_ts=_now(params), brain=brain,
                             limit=_int(params, "limit"), **roots))


def _proactive_disable(params, body, brain):
    module = _module('organs.day.proactive')
    return 200, module.disable_delivery(root=_roots(params)['root'])


def _proactive_enable(params, body, brain):
    if not isinstance(body, dict) or set(body) != {'private_qq'}:
        raise QueryError('只接受私聊接收人的 QQ 号')
    module = _module('organs.day.proactive')
    try:
        return 200, module.enable_delivery(body['private_qq'], root=_roots(params)['root'])
    except ValueError as exc:
        raise QueryError(str(exc)) from exc


def _proactive_consider(params: Mapping[str, str], body: Mapping[str, Any],
                        brain: Any) -> Result:
    """POST /day/proactive/consider：手工评估一次（支持 ?now=）。

    最多落一行动念账、最多排一条计划；判 SILENT 时计划仍然生成（标 held），
    静音 / 超额度则不排计划——三种"没开口"在账上是三个不同的 action。
    """
    module = _module("organs.day.proactive")
    roots = _roots(params)
    raw = _value(body, params, "idle_only")
    idle = str(raw).strip().lower() in ("1", "true", "yes") if raw is not None else None
    return 200, dict(_invoke(module, "consider", now_ts=_now(params), brain=brain,
                             idle_only=idle, **roots))


def _proactive_outcome(params: Mapping[str, str], body: Mapping[str, Any],
                       brain: Any) -> Result:
    """POST /day/proactive/outcome：结算最新那次主动开口（replied / ignored）。

    刚进静音那一次会顺带落一行 cold（同一次静音只落一次）。result 缺了是 400，
    不是 replied/ignored 也是 400——结算不认识的词只会把账改坏。
    """
    module = _module("organs.day.proactive")
    result = _text(body, params, "result")
    if not result:
        return 400, {"ok": False, "message": "outcome 要一个 result（replied 或 ignored）"}
    roots = _roots(params)
    try:
        payload = module.settle(result, _now(params), who=_text(body, params, "who"),
                                brain=brain, root=roots["root"],
                                heart_root=roots["heart_root"])
    except module.ProactiveError as exc:
        return 400, {"ok": False, "message": str(exc)}
    return 200, dict(payload)


def _solitude(params: Mapping[str, str], body: Mapping[str, Any], brain: Any) -> Result:
    """GET /day/solitude：idle 账尾部 + 此刻的读数（三档阈值、两个档位、闲置几分钟、是不是夜里）。

    纯读：不动手、不落账、不调核（GET 不该写盘，那是写动作）。
    """
    module = _module("organs.day.solitude")
    roots = _roots(params)
    return 200, dict(_invoke(module, "overview", now_ts=_now(params), brain=brain,
                             limit=_int(params, "limit"), **roots))


def _solitude_consider(params: Mapping[str, str], body: Mapping[str, Any],
                       brain: Any) -> Result:
    """POST /day/solitude/consider：手工触发一次独处（支持 ?now=）。

    body 可以点名"这一次做什么"（{"action": "b"} / 带一张草稿），**多塞的一律拒**：
    独处一次只做一件，两张草稿这种请求回 400 并说明为什么（不是默默只做第一张）。
    """
    module = _module("organs.day.solitude")
    roots = _roots(params)
    request = body if isinstance(body, dict) and body else None
    try:
        payload = _invoke(module, "consider", now_ts=_now(params), brain=brain,
                          request=request, **roots)
    except module.SolitudeError as exc:
        return 400, {"ok": False, "message": str(exc)}
    return 200, dict(payload)


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
    "/day/proactive": _proactive,
    "/day/proactive/disable": _proactive_disable,
    "/day/proactive/enable": _proactive_enable,
    "/day/proactive/consider": _proactive_consider,
    "/day/proactive/outcome": _proactive_outcome,
    "/day/solitude": _solitude,
    "/day/solitude/consider": _solitude_consider,
}


def dispatch(route: str, params: Mapping[str, str], body: Optional[Mapping[str, Any]] = None,
             store: Any = None, brain: Any = None) -> Result:
    """路由分派；store 为连续日程提供角色设定，brain 喂夜 / 日程 / 动念 / 独处。"""
    if route.startswith('/day/growth'):
        from organs.day import growth, autonomy, phone_browse, solitude
        from organs.home import intentions
        root = _root(params)
        try:
            if route == '/day/growth':
                phone_state = phone_browse.availability(root)
                opportunities = autonomy.opportunities(root)
                now = day_model.parse_ts(day_model.now_iso())
                life_day = day_model.day_key(now)
                limits = growth.limits(root)
                used = sum(day_model.parse_ts(r['at']) <= now and day_model.day_key(r['at']) == life_day
                           for r in opportunities)
                turns = {}
                for row in night_mod.log_tail(0, root=root)['lines']:
                    result = row.get('result', {})
                    turn = row.get('model_turn_id', '')
                    if (row.get('event') == 'growth_result' and result.get('status') == 'ok'
                            and result.get('changed') is True and result.get('record', {}).get('id')
                            and isinstance(turn, str) and len(turn) == 32
                            and all(c in '0123456789abcdef' for c in turn)):
                        turns[result['record']['id']] = turn
                versions = [dict(v, **({'model_turn_id': turns[v['id']]} if v.get('id') in turns else {}))
                            for v in (brain.selfnarration_versions() if brain else [])]
                return 200, {'settings': growth.config(root), 'profile': brain.current_profile() if brain else {},
                    'versions': versions,
                    'persona': persona_mod.render(store, brain, root=root),
                    'intentions': intentions.listing(solitude._home_root(root)),
                    'log': [r for r in solitude.log_lines(root) if r.get('event', '').startswith('autonomy_')][-30:],
                    **limits,
                    'usage': {'day': life_day, 'used': used, 'remaining': max(0, limits['daily_cap'] - used)},
                    'phone_available': phone_state['available'], 'phone': phone_state,
                    'opportunities': opportunities[-30:]}
            if route == '/day/growth/config':
                if (not isinstance(body, dict) or 'enabled' not in body
                        or not set(body) <= {'enabled', 'interval_minutes', 'daily_cap'}):
                    raise ValueError('只接受总开关、机会间隔与每日次数')
                return 200, growth.configure(root=root, value=body['enabled'],
                    **{k: body[k] for k in ('interval_minutes', 'daily_cap') if k in body})
            if route == '/day/growth/profile':
                if brain is None:
                    raise ValueError('脑尚未载入')
                if not isinstance(body, dict) or set(body) not in ({'patch', 'note'}, {'version', 'note'}):
                    raise ValueError('只接受 patch/note 或 version/note')
                if 'patch' in body:
                    return 200, brain.update_profile(body['patch'], note=body['note'])
                if type(body['version']) is not int:
                    raise ValueError('版本需要整数')
                old = next((v for v in brain.selfnarration_versions()
                            if v.get('version') == body['version'] and isinstance(v.get('profile'), dict)), None)
                if old is None:
                    raise ValueError('没有这个结构化成长版本')
                return 200, brain.update_profile(old['profile'], note=body['note'], sources=old.get('sources', []), replace=True)
        except ValueError as exc:
            return 400, {'ok': False, 'message': str(exc)}
    if route.startswith("/day/life"):
        try:
            roots, now = _roots(params), _now(params)
            root = roots['root']
            if route == "/day/life":
                return 200, life_mod.view(now, day=params.get("day"), root=root, heart_root=roots['heart_root'])
            if route == "/day/life/config":
                return 200, life_mod.configure((body or {}).get("enabled"), root=root, now=now)
            if route == "/day/life/adjust":
                return 200, life_mod.adjust(body or {}, root=root, now=now)
            if route == "/day/life/generate":
                if store is None:
                    raise life_mod.LifeError("魂尚未加载，不能生成日程")
                return 200, life_mod.generate((body or {}).get("day"), persona=persona_mod.render(store, brain, root=root),
                                              root=root, heart_root=roots['heart_root'], now=now, retry=True)
        except ValueError as exc:
            return 400, {"ok": False, "message": str(exc)}
    handler = _ROUTES.get(route)
    if handler is None:
        return 404, {"ok": False, "message": "没有这个端点：%s" % route}
    try:
        if route in ("/day/tick", "/day/night"):
            return handler(params or {}, body or {}, brain, store)
        return handler(params or {}, body or {}, brain)
    except QueryError as exc:
        return 400, {"ok": False, "message": str(exc)}
    except NotIntegrated as exc:
        return 501, {"ok": False, "message": str(exc), "module": exc.dotted}
    except plan_mod.PlanError as exc:
        return 500, {"ok": False, "message": "%s（计划账坏了不许当空账跑）" % exc}
