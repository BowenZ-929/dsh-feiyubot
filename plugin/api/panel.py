"""给壳的挂载点：核里不 import 宿主，宿主自己来取这张表。

壳拿到 routes(store, brain) 的返回值就一次性把面板挂上；核不认识壳，壳不写进核。
表项是 (method, path, handler)，handler(body, query) -> (status, payload)，
payload 可直接 json.dumps，query 是解好的查询参数映射（可为空 dict）。
这是一份契约，不是服务器：没有任何 I/O、绑定端口或框架对象。
没有改心、改脑的路由：心与脑只有 GET 的只读端点（/heart、/brain、/brain/*），
写方法交给 api.handle：这些路径上 POST/PUT/DELETE 一律 405。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import api

Result = Tuple[int, Dict[str, Any]]
Handler = Callable[[Optional[Dict[str, Any]], Mapping[str, str]], Result]
Route = Tuple[str, str, Handler]


def _route(method: str, path: str, store: Any, brain: Any = None) -> Route:
    def handler(body: Optional[Dict[str, Any]],
                query: Optional[Mapping[str, str]] = None) -> Result:
        return api.handle(method, path, body, store, query, brain)

    return (method, path, handler)


def routes(store: Any, brain: Any = None) -> List[Route]:
    """面板要挂的全部路由；顺序即 api.ROUTES，壳照单挂即可。"""
    return [_route(method, path, store, brain) for path, method in api.ROUTES.items()]
