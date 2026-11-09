"""面板静态页的名单（pages/ 目录的 html / js 读取）。

只读语义已经不在这一层：心的七条读数端点由 api/heart.py（HEART_ROUTES）全权接管，
脑由 api/brain.py（BRAIN_ROUTES）接管，405 的措辞与路由归组都在 core.handle 里按器官
表分派。"READONLY_PAGES：心还是占位页"那个化石概念随心线落地一起删掉了——两处登记
同一个 /heart 会被 core._merge 当场拒掉。
这里只剩一件事：PAGE_FILES，devserver 直接发字节的静态名单。
architecture §9.2：心与脑只能看。页是活的只读视图，不含任何写入口。

一页一个席位（2026-11-05）：DSH 里只有一个席位「她的面板」，页内一条导航条换
iframe 的地址；/panel 发聚合页 panel.html，"/" 仍是魂页（直开核时 / 就是魂）。

导航重切（2026-11-06，人拍板）：六项 = 魂 · 关于他 · 记忆 · 聊天 · 今天 · 设置。
心（/heart）与家（/home）仍是可用页，但不占导航位——它们是同一批只读面，
入口收在「设置」页里那两条链接上。

脑页退场（2026-11-06）：原来那张只读脑页的内容已被「关于他」（/about）与「记忆」（/memory）
整块接走，留着就是第二份会走味的副本——所以那一页整个删掉，/brain 从 PAGE_FILES 里移除，
地址回到 api/brain.py 的 JSON 端点 GET /brain（它本来就在，只是过去被静态页挡在前面）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

PAGE_DIR = Path(__file__).resolve().parents[1] / "pages"
# api.handle 的只读端点：路径 → 方法。心在 heart.py、脑在 brain.py，这张表留空
# （同名键重复注册会被 core._merge 当场拒掉），不再有"占位页"这类端点。
PAGE_ROUTES: Dict[str, str] = {}
# 静态页：路径 → 文件（devserver 直接发字节，不经 api.handle）
# "/" 与 "/panel" 不是同一张页：直开核时 / 是魂，/panel 是那条导航 + iframe 的聚合页
#（壳里只有一个席位，iframe 指向它）。/day、/settings 是占位页——没有真实页面的
# 导航项也要点得开，这是本项目的硬规矩：点不到真实路由的按钮要删，不留死按钮。
PAGE_FILES: Dict[str, str] = {"/": "soul.html", "/panel": "panel.html",
                              "/heart": "heart.html",
                              "/home": "home.html",
                              "/day": "day.html",
                              "/settings": "settings.html",
                              # 2026-11-06 信息架构重切（人拍板）：导航六项
                              #（魂 · 关于他 · 记忆 · 聊天 · 今天 · 设置），
                              # 两页新文件从脑页里分出来——「关于他」只放人名录与名片，
                              #「记忆」只放四个抽屉与冷库。脑那页因此退役（2026-11-06）：
                              # 内容搬完就删页，/brain 交回 JSON 端点，不留第二份副本。
                              "/about": "about.html",
                              "/memory": "memory.html",
                              "/galaxy": "galaxy.html",
                              "/galaxy.js": "galaxy.js",
                              "/growth-panel.js": "growth-panel.js",
                              "/model-settings.js": "model-settings.js",
                              # 说话页：GET /chat 发这一页的字节，POST /chat 走 api.handle
                              #（devserver._dispatch 只在 GET 时查 PAGE_FILES，两条不打架）。
                              "/chat": "chat.html",
                              "/observe": "observe.html",
                              "/eye": "eye.html",
                              "/voice": "voice.html",
                              "/ear": "ear.html",
                              "/studio": "studio.html",
                              "/learning": "learning.html",
                              "/expressions": "expressions.html",
                              "/memes": "memes.html",
                              "/phone": "phone.html",
                              # MOUNT/BASE 只有一份推导，就在这个片段里（各页相对地址引入）。
                              # 脚本要单独的 content-type，见下面的 PAGE_TYPES。
                              "/panel-base.js": "panel-base.js"}
# 按后缀给 content-type：一律 text/html 的话，浏览器会把 panel-base.js 当 HTML 拒掉
#（"MIME type ('text/html') is not executable"）。表里只有一样非 html。
PAGE_TYPE = "text/html; charset=utf-8"
PAGE_TYPES: Dict[str, str] = {".js": "text/javascript; charset=utf-8"}


def page_type(name: str) -> str:
    """一个静态文件的 content-type：按后缀查表，查不到按 html。"""
    return PAGE_TYPES.get(Path(name).suffix.lower(), PAGE_TYPE)


def read_page(name: str, root: Optional[Path] = None) -> Optional[bytes]:
    """读一个面板页；读不到返回 None（调用方据此回 404）。"""
    try:
        return (Path(root) if root else PAGE_DIR).joinpath(name).read_bytes()
    except OSError:
        return None
