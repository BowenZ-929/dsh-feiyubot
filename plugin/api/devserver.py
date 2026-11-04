"""本地验收用的开发服务器：标准库 http.server，只绑 127.0.0.1。

用法（在 plugin/ 下）：
    python3 -m api.devserver --root organs/soul/data --port 8787
    python3 -m api.devserver --root organs/soul/data --brain-root organs/brain/data --port 8787

顺带把面板页静态发出来：GET / 与 GET /panel 是 soul.html，GET /heart、GET /brain
是心与脑的只读页（找不到回 404；写方法走 api.handle，一律 405）。
不是产品入口；产品入口是宿主壳，它拿 api/panel.py 的 routes() 自己挂。
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from organs.brain import BrainStore, load as load_brain
from organs.soul import SoulLoadError, load
from . import api, inbound
from .pages import PAGE_FILES, read_page

# 静态页：路径 → 页面文件（与 api.ROUTES 无关，这几个路径只发 html，不进 api.handle）
PAGE_ROUTES = PAGE_FILES
PAGE_TYPE = "text/html; charset=utf-8"
DEFAULT_BRAIN_DIR = "organs/brain/data"


def _decode_target(raw: str) -> str:
    """请求行是 latin-1 解出来的；非 ASCII 路径（?audience=熟人）还原成 UTF-8。

    客户端也可能送来百分号编码，那部分是纯 ASCII，交给 api.handle 里的 parse_qs 解。
    """
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return raw


class _Handler(BaseHTTPRequestHandler):
    store: Any = None
    brain: Any = None

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    # 名片是唯一一条 PATCH（GET 读、PATCH 写白名单字段）：不接这一条，请求会落到
    # BaseHTTPRequestHandler 的 501，看着像「端点没做」。
    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        target = _decode_target(self.path)
        path = target.split("?", 1)[0]
        if method == "GET" and path in PAGE_ROUTES:
            self._page(PAGE_ROUTES[path])
            return
        body: Optional[Dict[str, Any]] = None
        if method in ("POST", "PUT", "PATCH"):
            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                self._send(400, {"ok": False, "message": "body 不是合法 JSON：%s" % exc})
                return
        if inbound.owns(method, path):
            self._send(*inbound.handle(body))
            return
        status, payload = api.handle(method, target, body, self.store, brain=self.brain)
        self._send(status, payload)

    def _page(self, name: str) -> None:
        data = read_page(name)
        if data is None:
            self._send(404, {"ok": False, "message": "面板页找不到：%s" % name})
            return
        self._send_bytes(200, PAGE_TYPE, data)

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", data)

    def _send_bytes(self, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[devserver] %s\n" % (fmt % args))


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python3 -m api.devserver")
    parser.add_argument("--root", required=True, help="魂数据目录")
    parser.add_argument("--brain-root", default=DEFAULT_BRAIN_DIR, help="脑数据目录")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--day", action="store_true",
                        help="开日子线的节律线程（60 秒一次；默认关，不开就不起任何循环）")
    return parser.parse_args(argv)


def _load_brain(path: Path) -> Optional[BrainStore]:
    try:
        return load_brain(path)
    except Exception as exc:  # noqa: BLE001 - 脑起不来也得让魂起身
        sys.stderr.write("[devserver] 脑未载入（%s）：%s\n" % (path, exc))
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    root = Path(args.root).resolve()
    try:
        _Handler.store = load(root)
    except SoulLoadError as exc:
        for issue in exc.issues:
            sys.stderr.write("[%s] %s %s\n" % (issue.level, issue.code, issue.message))
        sys.stderr.write("魂载入失败，不启动：%s\n" % root)
        return 1
    _Handler.brain = _load_brain(Path(args.brain_root).resolve())
    # 日子线：唯一的那一个循环，默认关；--day 才开，进程结束就关（不留后台线程）
    if args.day:
        from organs.day import rhythm as day_rhythm
        info = day_rhythm.start(brain=_Handler.brain)
        sys.stderr.write("[devserver] 日子线节律：%s（%s 秒一次）\n"
                         % (info.get("message") or "已启动", info.get("interval_seconds")))
    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    print("http://127.0.0.1:%d/  root=%s  brain=%s" % (
        args.port, root, "未载入" if _Handler.brain is None else "已接入"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if args.day:
            from organs.day import rhythm as day_rhythm
            day_rhythm.stop(1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
