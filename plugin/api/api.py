"""兼容壳：只做转发，不放业务代码。

    from api import api; api.handle(...) / api.ROUTES / api.READONLY_PAGES 照旧可用

业务代码已按器官拆开：core.py（路由表 + handle + 公共工具）、soul.py（/soul*）、
brain.py（/brain*）、pages.py（/heart 只读页与 pages/ 的 html 读取）。
"""
from __future__ import annotations

from .core import READONLY_PAGES, ROUTES, Result, handle

__all__ = ["handle", "ROUTES", "READONLY_PAGES", "Result"]
