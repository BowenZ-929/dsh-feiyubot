"""本地验收用的开发服务器：标准库 http.server，只绑 127.0.0.1。

用法（在 plugin/ 下）：
    python3 -m api.devserver --port 8787

数据根默认走 api/roots.py：魂在 <仓库>/home/organs/soul，脑在 <仓库>/home/organs/brain
（家 / 心 / 日子线由 roots 写进 HOME_ROOT / HEART_ROOT / DAY_ROOT 交给器官层）。
--root 与 --brain-root 只为兼容保留，显式给了就用，但同样要过 api/roots.py 的门禁：
她的实盘必须在 <仓库>/home 下，指向 plugin/、docs/ 或仓库外一律拒绝启动。

起身就起一个节律线程（守护线程）：每 TICK_SECONDS 秒调一次日子线的 tick，
**第一觉立刻跑**（重启后不用等一个间隔才见账面），参数永远是"现在"——不带 ?now=，
时刻由器官自己取。它调的就是 /day/tick 背后那一份 api.day.run_tick，没有第二套。
间隔不写在代码里：先看环境 QQAGENT_TICK_SECONDS，再读 data/limits.json 的 tick_seconds。
关掉它的办法是环境变量 QQAGENT_NO_TICK（非空且不是 0 就算关）：测试一律用它，
免得后台线程插手既有用例；跑通了也不许有线程拦着进程退出（daemon=True）。

顺带把面板页静态发出来：GET / 与 GET /panel 是 soul.html，GET /heart、GET /brain
是心与脑的只读页（找不到回 404；写方法走 api.handle，一律 405）。
不是产品入口；产品入口是宿主壳，它拿 api/panel.py 的 routes() 自己挂。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from contextlib import nullcontext
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from organs.brain import BrainStore, load as load_brain
from organs.soul import SoulLoadError, load
from . import api, day, inbound, roots
from .pages import PAGE_FILES, page_type, read_page

# 静态页：路径 → 文件（与 api.ROUTES 无关，这几条路径只发字节，不进 api.handle）。
# content-type 按后缀查 api/pages.py 的 PAGE_TYPES：panel-base.js 必须是 text/javascript，
# 发成 text/html 浏览器会拒绝执行（MIME 检查）。
PAGE_ROUTES = PAGE_FILES

# 节律线程的两个环境变量：一个关它（测试用），一个临时改间隔（不改盘上的配置）。
TICK_OFF_ENV = "QQAGENT_NO_TICK"
TICK_SECONDS_ENV = "QQAGENT_TICK_SECONDS"

_TICK_STOP: Optional[threading.Event] = None
_TICK_THREAD: Optional[threading.Thread] = None


def _tick_off() -> bool:
    """QQAGENT_NO_TICK 非空且不是 0 就算关：1 / true / no 都关，空、未设置、0 都开。"""
    return str(os.environ.get(TICK_OFF_ENV) or "").strip().lower() not in ("", "0")


def tick_seconds() -> float:
    """间隔从环境和数据里来，代码里不写死：QQAGENT_TICK_SECONDS > data/limits.json。

    默认值本来就住在那份配置（organs.day.model.DEFAULTS 的 tick_seconds），
    所以这里连一个 60 都不留：改节奏改配置，不改这一行。
    """
    raw = str(os.environ.get(TICK_SECONDS_ENV) or "").strip()
    if raw:
        try:
            every = float(raw)
        except ValueError:
            every = 0.0
        if every > 0:
            return every
        sys.stderr.write("[devserver] %s=%r 不是正数，改按配置里的 tick_seconds\n"
                         % (TICK_SECONDS_ENV, raw))
    from organs.day import model as day_model
    return float(day_model.load_limits()["tick_seconds"])


def _local_now() -> str:
    """本地带偏移的时刻：只给"这一 tick 抛错"那行日志用。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _tick_brief(payload: Mapping[str, Any]) -> str:
    """一行摘要：几点了、到点几条、夜跑成没跑成、动念这一步到底怎么了。

    payload 是 tick 自己的返回体，这里只挑人一眼要看的键，取不到就当空——
    摘要永远不许成为线程的新错误源。
    """
    night = payload.get("night") or {}
    actions = (payload.get("proactive") or {}).get("actions") or {}
    consider = payload.get("proactive_consider") or {}
    if consider.get("action"):
        mood = str(consider["action"])
    elif consider.get("fired"):
        mood = "fired"
    else:
        mood = "没动念(%s)" % (consider.get("skipped") or "none")
    return ("at=%s day=%s ticks=%s due=%d night=%s proactive=%s consider=%s"
            % (payload.get("now"), payload.get("day"), payload.get("ticks"),
               len(payload.get("due") or []),
               "/".join("%s:%d" % (key, len(night.get(key) or []))
                        for key in ("executed", "retried", "never_ran", "expired")),
               ",".join("%s:%s" % item for item in sorted(actions.items())) or "none",
               mood))


def _tick_loop(stop: threading.Event, every: float, brain: Any, store: Any = None) -> None:
    """一次一觉的循环：第一觉**立刻**跑（重启后 60 秒内就能看到账），之后每 every 秒一次。

    线程里的错只记一行、不吃掉循环，也绝不把进程带走：一件坏事不许让节律停摆。
    """
    while True:
        try:
            print("[devserver] tick %s" % _tick_brief(day.run_tick(brain=brain, proactive_guard=_Handler.business_lock,
                  **({"store": store} if store is not None else {}))), flush=True)
        except Exception as exc:  # noqa: BLE001 - 线程里的错不许无声无息
            sys.stderr.write("[devserver] tick 失败 at=%s：%s: %s\n"
                             % (_local_now(), type(exc).__name__, exc))
            sys.stderr.flush()
        from organs.mind import rice
        rice.refresh()
        if stop.wait(every):
            return


def start_tick_thread(every: Optional[float] = None, brain: Any = None, store: Any = None) -> Dict[str, Any]:
    """起节律线程；被 QQAGENT_NO_TICK 关掉或已经在跑就不起第二个。"""
    global _TICK_STOP, _TICK_THREAD
    if _tick_off():
        return {"started": False, "interval_seconds": None,
                "message": "%s 已置，节律线程不开" % TICK_OFF_ENV}
    thread = _TICK_THREAD
    if thread is not None and thread.is_alive():
        return {"started": False, "interval_seconds": None, "message": "节律线程已经在跑"}
    interval = float(every if every is not None else tick_seconds())
    stop = threading.Event()
    thread = threading.Thread(target=_tick_loop, args=(stop, interval, brain, store),
                              name="day-tick", daemon=True)
    _TICK_STOP, _TICK_THREAD = stop, thread
    thread.start()
    return {"started": True, "interval_seconds": interval,
            "message": "节律线程已启动（%s 秒一次）" % interval}


def stop_tick_thread(timeout: float = 2.0) -> bool:
    """关节律线程：置停止位并等它走完当前这一觉（不 kill，不留半截）。"""
    global _TICK_STOP, _TICK_THREAD
    stop, thread = _TICK_STOP, _TICK_THREAD
    _TICK_STOP, _TICK_THREAD = None, None
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout)
        return not thread.is_alive()
    return True


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
    # 慢模型调用不能堵住守护探活；其他业务保持原来逐个处理的顺序。
    business_lock = threading.Lock()

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
        immediate = (method == 'GET' and path in ('/soul/check', '/healthz', '/observe/channel', '/observe/channel-runtime',
                                                '/eye/status', '/ear/status', '/voice/status') or
                     method == 'POST' and path == '/day/proactive/disable')
        guard = nullcontext() if immediate else self.business_lock
        with guard:
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
        self._send_bytes(200, page_type(name), data)

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
    parser.add_argument("--root", default=None,
                        help="魂数据目录（默认 <仓库>/home/organs/soul；兼容保留，同样过门禁）")
    parser.add_argument("--brain-root", default=None,
                        help="脑数据目录（默认 <仓库>/home/organs/brain；兼容保留，同样过门禁）")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--day", action="store_true",
                        help="兼容保留：节律线程现在默认就开（环境 QQAGENT_NO_TICK 可关）")
    return parser.parse_args(argv)


def _load_brain(path: Path) -> Optional[BrainStore]:
    try:
        return load_brain(path)
    except Exception as exc:  # noqa: BLE001 - 脑起不来也得让魂起身
        sys.stderr.write("[devserver] 脑未载入（%s）：%s\n" % (path, exc))
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    # 用户要求保留模型调用记录；DSH 与命令行启动保持一致，显式设 0 仍可关闭。
    os.environ.setdefault("QQAGENT_TRACE", "1")
    try:
        info = roots.bootstrap()
    except roots.RootsError as exc:
        sys.stderr.write("[devserver] 拒不起身：%s\n" % exc)
        return 2
    for organ, count in sorted(info["seeded"].items()):
        if count:
            sys.stderr.write("[devserver] 出厂默认补件 %s：%d 个\n" % (organ, count))
    try:
        root = roots.organ_root("soul", args.root)
        brain_root = roots.organ_root("brain", args.brain_root)
    except roots.RootsError as exc:
        sys.stderr.write("[devserver] 拒不起身：%s\n" % exc)
        return 2
    try:
        _Handler.store = load(root)
    except SoulLoadError as exc:
        for issue in exc.issues:
            sys.stderr.write("[%s] %s %s\n" % (issue.level, issue.code, issue.message))
        sys.stderr.write("魂载入失败，不启动：%s\n" % root)
        return 1
    _Handler.brain = _load_brain(brain_root)
    # 日子线：唯一的那一个循环，默认就开（QQAGENT_NO_TICK 关得掉），进程结束就跟它一起走
    started = start_tick_thread(brain=_Handler.brain, store=_Handler.store)
    print("[devserver] 日子线节律：%s" % started["message"], flush=True)
    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    print("http://127.0.0.1:%d/  root=%s  brain=%s" % (
        args.port, root, "未载入" if _Handler.brain is None else "已接入"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        stopped = stop_tick_thread(1.0)
        if started["started"] and stopped:
            print("[devserver] 日子线节律：已停", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
