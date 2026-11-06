"""通道：把一张图（连同它的一句话）交给通道去发。**手唯一的对外出口**都在这里。

手不改字、不管通道那边的门与配额，也**不替通道下结论**：本模块只把请求发出去、把回答
原样带回来。判据不在这里（「算不算发出去了」由 actions 判），这里只把四种答案分开：

- `OK`：通道回 `ok == true` 且 `sent > 0`——只有这一种才算发出去了；
- `CHANNEL_BLOCKED`：通道回 `sent == 0`（它自己拦下了），它的 `reason_code` 原样带上；
- `CHANNEL_UNREACHABLE`：连不上／超时（没有回答，不是「没发」也不是「发了」）；
- `SEND_FAILED`：回的东西不对劲（HTTP 4xx/5xx、不是 JSON、没有能读的 sent），
  或者**根本没能出站**（本地闸拦下：`local=True`，此时通道什么都没收到）。

出站前有一道本地断言（`guard`）：`scene` 只认 `group`／`private`，`target` 必须是
**JSON 数字正整数**。这条是实测补的：把 target 当字符串发过去，真通道回 400
（`"target 必须是有限正整数（拿到 \"100000001\"）"`），图一张都没发出去。

**手自己绝不声明「发过了」**：done 只认通道的 `ok` 与 `sent`，人话里必须带它们。

地址与超时一律在**调用时**从环境变量读（不在 import 时读死，测试与冒烟才换得动）：
`HAND_CHANNEL_URL`（默认 http://127.0.0.1:6200）、`HAND_CHANNEL_TIMEOUT`（秒，默认 10）。
本模块与 draw.py 是**仅有的两个能上网的文件**，由 tests/test_source_rules.py 看住。
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

CHANNEL_ENV = "HAND_CHANNEL_URL"
TIMEOUT_ENV = "HAND_CHANNEL_TIMEOUT"
DEFAULT_CHANNEL_URL = "http://127.0.0.1:6200"
DEFAULT_TIMEOUT = 10.0
SAY_ROUTE = "/say"

OK = "OK"
CHANNEL_UNREACHABLE = "CHANNEL_UNREACHABLE"
SEND_FAILED = "SEND_FAILED"
CHANNEL_BLOCKED = "CHANNEL_BLOCKED"
CODES = (OK, CHANNEL_UNREACHABLE, SEND_FAILED, CHANNEL_BLOCKED)

#: 通道只认这两个场景；别的值它一律 400。
SCENES = ("group", "private")


class Unreachable(Exception):
    """没有回答：连不上、超时、DNS 挂了。**它不等于「没发出去」**，只是「不知道」。"""


def base_url(value: Any = None) -> str:
    """通道地址：显式给 > 环境变量 > 默认。末尾的斜杠去掉，免得拼出 '//say'。"""
    raw = value
    if raw is None or not str(raw).strip():
        raw = os.environ.get(CHANNEL_ENV)
    text = str(raw or "").strip() or DEFAULT_CHANNEL_URL
    return text.rstrip("/")


def timeout_seconds(value: Any = None) -> float:
    """超时（秒）：显式给 > 环境变量 > 默认。读不成数就用默认，不猜一个更大的。"""
    raw = value
    if raw is None:
        raw = os.environ.get(TIMEOUT_ENV)
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return number if number > 0 else DEFAULT_TIMEOUT


def file_uri(path: Any) -> str:
    """绝对路径 → `file://` 开头的 URI。路径里的中文原样带出去，不在这里转义。"""
    text = os.path.abspath(str(path))
    return text if text.startswith("file://") else "file://" + text


def _post_json(endpoint: str, body: Dict[str, Any], timeout: float) -> Tuple[int, bytes]:
    """POST 一段 JSON。**HTTP 4xx/5xx 也算回答**（返回码与 body）；连不上才抛 Unreachable。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=data, method="POST",
        headers={"content-type": "application/json; charset=utf-8",
                 "accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 0) or 0), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code or 0), (exc.read() or b"")
    except urllib.error.URLError as exc:
        raise Unreachable(str(exc.reason)) from exc
    except (socket.timeout, TimeoutError, OSError) as exc:
        raise Unreachable(str(exc)) from exc


def _outcome(code: str, *, endpoint: str, **extra: Any) -> Dict[str, Any]:
    """固定八栏：判据、通道给的三个数、HTTP 码、一句原始错误、打到哪儿去了，
    以及 `local`——True 表示**这个请求根本没出站**（本地闸拦下的，不是通道说的）。"""
    payload = {"code": code, "sent": None, "reason_code": "", "channel_id": None,
               "url": endpoint, "http": None, "error": None, "local": False}
    payload.update(extra)
    return payload


def guard(body: Dict[str, Any]) -> Optional[str]:
    """出站前的最后一道**本地断言**：不合规就一个字节都不发。

    通道对这两格很挑（`scene` 只认 group／private；`target` 必须是 JSON 数字正整数），
    实测把 target 当字符串发出去会被它 400。这道闸不替 actions 做判断（那边已经归一过），
    它保证的是：**本模块被单独调用时也不会把违规的 body 发出去**。
    """
    if body.get("scene") not in SCENES:
        return "scene 只认 %s（拿到 %r）" % ("／".join(SCENES), body.get("scene"))
    target = body.get("target")
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        return "target 必须是正整数，JSON 里也得是数字（拿到 %r）" % (target,)
    chunks = body.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return "chunks 必须是非空数组（拿到 %r）" % (chunks,)
    return None


def say_image(*, scene: Any, target: Any, path: Any, name: Any,
              url: Any = None, timeout: Any = None) -> Dict[str, Any]:
    """把「这张图 + 这一句话」交给通道。**这里不判成败**，只把通道的答案带回来。

    体形状冻结：`{"scene":…, "target":…, "chunks":[{"image":{"file":"file://…","summary":…}}]}`。
    """
    endpoint = base_url(url) + SAY_ROUTE
    body = {"scene": scene, "target": target,
            "chunks": [{"image": {"file": file_uri(path), "summary": str(name or "")}}]}
    problem = guard(body)
    if problem is not None:
        # 本地就不放行：不发请求，也不假装这是通道的回答（local=True）。
        return _outcome(SEND_FAILED, endpoint=endpoint, error="内部闸：" + problem, local=True)
    try:
        status, raw = _post_json(endpoint, body, timeout_seconds(timeout))
    except Unreachable as exc:
        return _outcome(CHANNEL_UNREACHABLE, endpoint=endpoint, error=str(exc))

    if not 200 <= status < 300:
        # HTTP 层就没成：body 里写什么都**不算通道的判断**，不拿来当 sent／reason_code。
        return _outcome(SEND_FAILED, endpoint=endpoint, http=status,
                        error="通道回了 HTTP %d，这不算一句回答" % status)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return _outcome(SEND_FAILED, endpoint=endpoint, http=status,
                        error="通道回的不是 JSON（%s）" % exc)
    if not isinstance(payload, dict):
        return _outcome(SEND_FAILED, endpoint=endpoint, http=status,
                        error="通道回的不是一个对象：%r" % type(payload).__name__)

    sent = payload.get("sent")
    if isinstance(sent, bool) or not isinstance(sent, int):
        return _outcome(SEND_FAILED, endpoint=endpoint, http=status,
                        error="回执里没有能读的 sent：%r" % (sent,))

    carried = {"sent": int(sent), "reason_code": "" if payload.get("reason_code") is None
               else str(payload.get("reason_code")),
               "channel_id": None if payload.get("id") is None else str(payload.get("id")),
               "http": status}
    if sent == 0:
        # 通道自己拦下了：它的 reason_code 原样带走，人不许替它编一个理由。
        return _outcome(CHANNEL_BLOCKED, endpoint=endpoint, **carried)
    if payload.get("ok") is True and sent > 0:
        return _outcome(OK, endpoint=endpoint, **carried)
    return _outcome(SEND_FAILED, endpoint=endpoint,
                    error="ok=%r、sent=%r，这不算发出去" % (payload.get("ok"), sent), **carried)
