"""面板静态页的名单（pages/ 目录的 html 读取）。

只读语义已经不在这一层：心的七条读数端点由 api/heart.py（HEART_ROUTES）全权接管，
脑由 api/brain.py（BRAIN_ROUTES）接管，405 的措辞与路由归组都在 core.handle 里按器官
表分派。"READONLY_PAGES：心还是占位页"那个化石概念随心线落地一起删掉了——两处登记
同一个 /heart 会被 core._merge 当场拒掉。
这里只剩一件事：PAGE_FILES，devserver 直接发字节的静态页名单（/heart 仍发 heart.html）。
architecture §9.2：心与脑只能看。页是活的只读视图，不含任何写入口。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

PAGE_DIR = Path(__file__).resolve().parents[1] / "pages"
# api.handle 的只读端点：路径 → 方法。心在 heart.py、脑在 brain.py，这张表留空
# （同名键重复注册会被 core._merge 当场拒掉），不再有"占位页"这类端点。
PAGE_ROUTES: Dict[str, str] = {}
# 静态页：路径 → 页面文件（devserver 直接发字节，不经 api.handle）
PAGE_FILES: Dict[str, str] = {"/": "soul.html", "/panel": "soul.html",
                              "/heart": "heart.html", "/brain": "brain.html",
                              "/home": "home.html",
                              # 说话页：GET /chat 发这一页的字节，POST /chat 走 api.handle
                              #（devserver._dispatch 只在 GET 时查 PAGE_FILES，两条不打架）。
                              "/chat": "chat.html"}


def read_page(name: str, root: Optional[Path] = None) -> Optional[bytes]:
    """读一个面板页；读不到返回 None（调用方据此回 404）。"""
    try:
        return (Path(root) if root else PAGE_DIR).joinpath(name).read_bytes()
    except OSError:
        return None
