"""核的 HTTP 层公共件：路由注册表 + handle() + 各端点共用的小工具。

handle(method, path, body, store, query=None, brain=None) -> (status, payload)，payload 可直接 json.dumps。
path 可以带查询串（"?"后段由 handle 自己解），也可以把解好的 query 映射直接传进来。

路由按器官拆开：魂在 soul.py、脑在 brain.py、心在 heart.py、嘴在 mouth.py、家在 home.py、
日子在 day.py，静态页名单在 pages.py。心的七条只读端点由 heart.py 全权接管——"只读页只是
占位"那个概念随心线落地一起删掉了，只读语义现在由 HEART_ROUTES + 405 分支承担。
这里只做两件事——把各器官表汇总成一张 ROUTES（同名键直接抛，不许两条线各自注册同
一个端点），以及按表分发。导入顺序：本模块要汇总器官表，必须最先导入（api.api / devserver / panel 都已如此）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote

from organs.soul import model

Result = Tuple[int, Dict[str, Any]]


def _parse_query(raw: str) -> Dict[str, str]:
    """查询串 → 字典；同一个键出现多次取第一个（端点只需要单值）。"""
    if not raw:
        return {}
    return dict((key, values[0]) for key, values in parse_qs(raw, keep_blank_values=True).items())


def _issues(issues: Sequence[model.Issue]) -> List[Dict[str, str]]:
    return [item.to_dict() for item in issues]


def _failed(status: int, message: str, issues: Sequence[model.Issue] = ()) -> Result:
    return status, {"ok": False, "message": message, "issues": _issues(issues)}


def _merge(*tables: Mapping[str, str]) -> Dict[str, str]:
    """合并器官路由表；同一个路径被两条线各登记了一次就当场炸，不静默覆盖。"""
    merged: Dict[str, str] = {}
    for table in tables:
        for route, method in table.items():
            if route in merged:
                raise ValueError("路由重复注册：%s（两条线各自登记了同一个端点）" % route)
            merged[route] = method
    return merged


# 器官文件要 import 上面的公共件，所以汇总放最后：否则 core 还没定义完就被回头拿走。
from . import brain as brain_api, chat as chat_api, day as day_api, heart as heart_api  # noqa: E402
from . import home as home_api  # noqa: E402
from . import mouth as mouth_api, pages as page_api, soul as soul_api  # noqa: E402
from . import version as version_api  # noqa: E402

ROUTES = _merge(soul_api.SOUL_ROUTES, brain_api.BRAIN_ROUTES, page_api.PAGE_ROUTES,
                mouth_api.MOUTH_ROUTES, home_api.HOME_ROUTES, heart_api.HEART_ROUTES,
                version_api.VERSION_ROUTES, day_api.DAY_ROUTES, chat_api.CHAT_ROUTES)


def handle(method: str, path: str, body: Optional[Dict[str, Any]], store: Any,
           query: Optional[Mapping[str, str]] = None, brain: Any = None) -> Result:
    """唯一的入口：把一次 HTTP 请求变成 (状态码, JSON 安全的字典)。"""
    method = method.upper()
    route, _, raw_query = path.partition("?")
    route = unquote(route)   # 中文人名等百分号编码的路径段要先还原，否则匹配不到
    params = dict(query) if query else _parse_query(raw_query)
    if route.startswith("/brain/people/") and route.endswith("/card"):
        return brain_api.dispatch_card(method, route, body, brain)
    if route.startswith("/brain/memory/") and route.endswith("/note"):
        return brain_api.dispatch_note(method, route, body, brain)
    if route not in ROUTES:
        return 404, {"ok": False, "message": "没有这个端点：%s" % route}
    if method != ROUTES[route]:
        if route in heart_api.HEART_ROUTES:
            # 心与脑都只有 GET；心的措辞单独一条：这一层是她的读数，人碰不到。
            return 405, {"ok": False,
                         "message": "%s 是心的只读端点：只收 GET，POST/PUT/DELETE 一律 405"
                                    "（人不能改心）" % route}
        if route in brain_api.BRAIN_ROUTES:
            return 405, {"ok": False,
                         "message": "%s 是只读页：只收 GET，POST/PUT/DELETE 一律 405（人不能改心与脑）"
                                    % route}
        if route in mouth_api.MOUTH_ROUTES:
            return 405, {"ok": False,
                         "message": "%s 是嘴的干跑端点：只读，只收 GET，POST/PUT/DELETE 一律 405"
                                    "（干跑只算不发，不落盘）" % route}
        if route in home_api.HOME_ROUTES:
            return 405, {"ok": False,
                         "message": "%s 只收 %s：家有十条路由，只有 POST /home/write 这一条是写，"
                                    "其余九条只读，方法不对一律 405（写权限也看这张表，不看 UI）"
                                    % (route, ROUTES[route])}
        if route in day_api.DAY_ROUTES:
            return 405, {"ok": False,
                         "message": "%s 只收 %s：日子线十条路由各认一个方法，派发前先看这张表"
                                    "（换日与闸门都拦在调度层，换方法绕不过去）"
                                    % (route, ROUTES[route])}
        if route in chat_api.CHAT_ROUTES:
            return 405, {"ok": False,
                         "message": "%s 只收 %s：说话是一条写（说一句话），GET/PUT/DELETE "
                                    "一律 405（页在 GET 时由 devserver 直接发字节，不走这张表）"
                                    % (route, ROUTES[route])}
        return 405, {"ok": False, "message": "%s 只收 %s" % (route, ROUTES[route])}
    if route in chat_api.CHAT_ROUTES:
        return chat_api.dispatch(route, body, store, brain)
    if route in heart_api.HEART_ROUTES:
        return heart_api.dispatch(route, params, store, brain)
    if route in brain_api.BRAIN_ROUTES:
        return brain_api.dispatch(route, params, body, brain)
    if route in mouth_api.MOUTH_ROUTES:
        return mouth_api.dispatch(route, params, store, brain)
    if route in home_api.HOME_ROUTES:
        return home_api.dispatch(route, params, store, brain, body)
    if route in day_api.DAY_ROUTES:
        return day_api.dispatch(route, params, body, store, brain)
    if route in version_api.VERSION_ROUTES:
        return version_api.dispatch(route)
    return soul_api.dispatch(route, params, body, store)
