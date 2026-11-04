"""心（/heart）的只读端点，与面板静态页（pages/ 目录的 html 读取）。

心的只读页只有 GET，没有任何写方法（architecture §9.2：只能看）。
/ 与 /panel 是魂面板，/heart、/brain 是心与脑的只读页——这几条只在 devserver 上静态发出，
不是 api.handle 的路由；api.handle 只登记 /heart 这一个只读 JSON 端点。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

PAGE_DIR = Path(__file__).resolve().parents[1] / "pages"
# api.handle 的只读端点：路径 → 方法
PAGE_ROUTES = {"/heart": "GET"}
# 只读页：路径 → 器官名（响应里如实报「未接入」+ readonly）
READONLY_PAGES = {"/heart": "heart"}
# 静态页：路径 → 页面文件（devserver 直接发字节，不经 api.handle）
PAGE_FILES: Dict[str, str] = {"/": "soul.html", "/panel": "soul.html",
                              "/heart": "heart.html", "/brain": "brain.html"}


def read_page(name: str, root: Optional[Path] = None) -> Optional[bytes]:
    """读一个面板页；读不到返回 None（调用方据此回 404）。"""
    try:
        return (Path(root) if root else PAGE_DIR).joinpath(name).read_bytes()
    except OSError:
        return None
