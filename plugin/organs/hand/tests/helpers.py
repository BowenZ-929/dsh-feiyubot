"""手的测试小工具：临时实盘根、直接读账、伪造文件与额度、两台本地假服务。

读账一律**直接读盘**（不走被测代码），这样「盘上到底几行、最后一行是什么」才是独立证据。
假服务一律起在 127.0.0.1 的随机端口上（`http.server`），不碰外网、也不占固定端口：
通道与生图服务都是「外面」的东西，测试里就不许真有外面。
"""
from __future__ import annotations

import hashlib
import http.server
import json
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from organs.home import cards as home_cards
from organs.home import residents as home_residents
from organs.hand import store

#: 固定的「现在」：用例不看本机时钟，也就不会半夜跑挂。
NOW = "2026-11-06T09:00:00+09:00"
#: 明天中午（相对 NOW）。
TOMORROW_NOON = "2026-11-07T12:00:00+09:00"


def make_root(temp_dir: str) -> Path:
    """造 Path(temp_dir)/"hand" 并建好，返回**手**的实盘根；家的根是它的兄弟目录。"""
    root = Path(temp_dir) / "hand"
    root.mkdir(parents=True, exist_ok=True)
    return root


def home_root(root: Any) -> Path:
    return store.home_root(root)


def ledger_path(root: Any) -> Path:
    return Path(root) / "ledger.jsonl"


def ledger_lines(root: Any) -> List[Dict[str, Any]]:
    """盘上每一行（含 reserved），坏行就让它炸——测试里要的是准数。"""
    path = ledger_path(root)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def tasks(root: Any) -> list:
    """家里工作台上的卡（折叠后）。"""
    return home_cards.list_tasks(root=home_root(root))


def write_limits(root: Any, limit: int, version: int = 1) -> Path:
    """往临时实盘塞一份 limits.json。"""
    path = Path(root) / "limits.json"
    path.write_text(json.dumps({"version": version, "per_person_per_day": limit,
                                "note": "测试用"}, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def write_raw(path: Any, text: str) -> Path:
    """原样写一段文本：造坏 JSON、半截行、假账用。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# ---- 图：造一张小图、登记进仓库 -------------------------------------------

#: 一张假图（内容固定：指纹可预期，测试能直接比）。
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes" * 8


def make_image(directory: Any, name: str = "参考图.png", data: bytes = IMAGE_BYTES) -> Path:
    """在 directory 下造一个字节固定的图文件。"""
    target = Path(directory) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def register_asset(root: Any, path: Any, by: str = "测试") -> Dict[str, Any]:
    """把一张图登记进家·仓库（只记指针）。家的写口只有一个：residents.register_asset。"""
    return home_residents.register_asset(root=home_root(root), by=by, path=str(path),
                                         note="测试用")


def warehouse(root: Any) -> List[Dict[str, Any]]:
    """家·仓库里可见的卡（折叠后）。"""
    page = home_cards.list_room(root=home_root(root), room=home_residents.WAREHOUSE_ROOM,
                                limit=200)
    return list(page.get("items") or [])


# ---- 本地假服务：通道与生图服务 -------------------------------------------

#: 一个 reply：(record) → (status, headers, body_bytes)
Reply = Callable[[Dict[str, Any]], Tuple[int, Dict[str, str], bytes]]


def free_port() -> int:
    """找一个当前没人占的端口号并立刻关掉：用它演「端口是关的」。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def json_reply(payload: Any, status: int = 200) -> Reply:
    """固定回一段 JSON 的 reply。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def reply(record: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        return status, {"content-type": "application/json; charset=utf-8"}, body
    return reply


class FakeService:
    """本地假服务：起在 127.0.0.1 的随机端口，把每个请求原样记下来，用 reply 决定回什么。

    record = {"method", "path", "base", "headers", "body"}；body 能解成 JSON 就解，
    否则原样字符串——「POST 出去的 body 里到底有什么」因此是可断言的原文，不是推测。
    """

    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.records: List[Dict[str, Any]] = []
        self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def __enter__(self) -> "FakeService":
        service = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body: Any = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = raw.decode("utf-8", "replace")
                record = {"method": self.command, "path": self.path, "base": service.url,
                          "headers": dict(self.headers), "body": body}
                service.records.append(record)
                status, headers, payload = service.reply(record)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_POST = _handle
            do_GET = _handle

            def log_message(self, *args: Any) -> None:      # 测试不打访问日志
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False


# ---- 挑剔的假通道：照真通道的规矩校验请求 --------------------------------
# 教训（第五轮回炉）：假通道太宽容会漏掉集成缺陷。真通道对 scene／target 的类型很挑，
# 把 target 当字符串发过去它会回 400，图一张都没发出去——所以这里的假通道也照同样的规矩挑。

#: 真通道只认这两个场景。
SCENES = ("group", "private")


def channel_problem(body: Any) -> Optional[str]:
    """照真通道 400 的那几条逐条验；返回一句「哪一条没过 + 收到的原值」，或 None。"""
    if not isinstance(body, dict):
        return "请求体必须是一个 JSON 对象（拿到 %r）" % (body,)
    scene = body.get("scene")
    if scene not in SCENES:
        return "scene 必须是 %s 之一（拿到 %r）" % ("／".join(SCENES), scene)
    target = body.get("target")
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        return "target 必须是有限正整数（拿到 %r）" % (target,)
    chunks = body.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return "chunks 必须是非空数组（拿到 %r）" % (chunks,)
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            return "chunks[%d] 必须是对象（拿到 %r）" % (index, chunk)
        text = chunk.get("text")
        if text is not None and not isinstance(text, str):
            return "chunks[%d].text 只能是字符串或不给（拿到 %r）" % (index, text)
        image = chunk.get("image")
        if image is None:
            if not text:
                return "chunks[%d] 既没有 text 也没有 image" % index
            continue
        if not isinstance(image, dict):
            return "chunks[%d].image 必须是对象（拿到 %r）" % (index, image)
        file_uri = image.get("file")
        if not isinstance(file_uri, str) or not file_uri.startswith("file://"):
            return "chunks[%d].image.file 必须以 file:// 开头（拿到 %r）" % (index, file_uri)
        summary = image.get("summary")
        if summary is not None and not isinstance(summary, str):
            return "chunks[%d].image.summary 只能是字符串（拿到 %r）" % (index, summary)
    return None


def channel_gate(inner: Optional[Reply] = None) -> Reply:
    """先按真通道的规矩验请求；不合规 → 400 并把收到的原值写进消息，合规才交给 inner。"""
    accept = inner or json_reply({"ok": True, "id": "say-1", "sent": 1, "reason_code": "sent"})

    def reply(record: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        problem = channel_problem(record.get("body"))
        if problem is not None:
            payload = {"ok": False, "message": problem}
            return (400, {"content-type": "application/json; charset=utf-8"},
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return accept(record)
    return reply


def channel_service(*, sent: int = 1, reason_code: str = "sent",
                    channel_id: str = "say-1") -> FakeService:
    """一台挑剔的假通道：合规时按给定的 ack 回，不合规就 400。"""
    return FakeService(channel_gate(json_reply({"ok": True, "id": channel_id, "sent": sent,
                                                "reason_code": reason_code})))

