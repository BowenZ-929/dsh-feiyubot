"""核的 HTTP 层公共件：路由注册表 + handle() + 各端点共用的小工具。

handle(method, path, body, store, query=None, brain=None) -> (status, payload)，payload 可直接 json.dumps。
path 可以带查询串（"?"后段由 handle 自己解），也可以把解好的 query 映射直接传进来。

路由按器官拆开：魂在 soul.py、脑在 brain.py、只读页与静态页在 pages.py。
这里只做两件事——把三张表汇总成一张 ROUTES（同名键直接抛，不许两条线各自注册同一个端点），
以及按表分发。导入顺序：本模块要汇总器官表，必须最先导入（api.api / devserver / panel 都已如此）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs

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


def _readonly(route: str) -> Result:
    """心的只读页：这一层还没有代码，如实报「未接入」+ 只读。"""
    return 200, {"status": "未接入", "readonly": True, "agent": READONLY_PAGES[route]}


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
from . import brain as brain_api, mouth as mouth_api, pages as page_api, soul as soul_api  # noqa: E402

ROUTES = _merge(soul_api.SOUL_ROUTES, brain_api.BRAIN_ROUTES, page_api.PAGE_ROUTES,
                mouth_api.MOUTH_ROUTES)
READONLY_PAGES = page_api.READONLY_PAGES


def handle(method: str, path: str, body: Optional[Dict[str, Any]], store: Any,
           query: Optional[Mapping[str, str]] = None, brain: Any = None) -> Result:
    """唯一的入口：把一次 HTTP 请求变成 (状态码, JSON 安全的字典)。"""
    method = method.upper()
    route, _, raw_query = path.partition("?")
    params = dict(query) if query else _parse_query(raw_query)
    if route not in ROUTES:
        return 404, {"ok": False, "message": "没有这个端点：%s" % route}
    if method != ROUTES[route]:
        if route in READONLY_PAGES or route in brain_api.BRAIN_ROUTES:
            return 405, {"ok": False,
                         "message": "%s 是只读页：只收 GET，POST/PUT/DELETE 一律 405（人不能改心与脑）"
                                    % route}
        if route in mouth_api.MOUTH_ROUTES:
            return 405, {"ok": False,
                         "message": "%s 是嘴的干跑端点：只读，只收 GET，POST/PUT/DELETE 一律 405"
                                    "（干跑只算不发，不落盘）" % route}
        return 405, {"ok": False, "message": "%s 只收 %s" % (route, ROUTES[route])}
    if route in READONLY_PAGES:
        return _readonly(route)
    if route in brain_api.BRAIN_ROUTES:
        return brain_api.dispatch(route, params, brain)
    if route in mouth_api.MOUTH_ROUTES:
        return mouth_api.dispatch(route, params, store, brain)
    return soul_api.dispatch(route, params, body, store)
