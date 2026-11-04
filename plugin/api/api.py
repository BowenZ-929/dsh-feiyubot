"""兼容壳：只做转发，不放业务代码。

    from api import api; api.handle(...) / api.ROUTES 照旧可用

READONLY_PAGES 已随"心不再是占位页"一起删除（口径变更 2026-11-05）：心的七条读数端点
在 heart.py（HEART_ROUTES），只读语义由路由表 + core.handle 的 405 分支承担。

业务代码已按器官拆开：core.py（路由表 + handle + 公共工具）、soul.py（/soul*）、
brain.py（/brain*）、heart.py（/heart*）、mouth.py（/mouth*）、home.py（/home*）、
pages.py（pages/ 的 html 读取）。
"""
from __future__ import annotations

from .core import ROUTES, Result, handle

__all__ = ["handle", "ROUTES", "Result"]
