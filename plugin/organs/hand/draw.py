"""画：把一句描述交给外服，拿回一张能落盘的图。**这里只拿字节，不落盘。**

外服：`POST $HAND_DRAW_URL`，头 `authorization: Bearer $HAND_DRAW_KEY`（没配 key 就不带这个头），
体 `{"prompt": 描述, "no_watermark": true}`，回 `{"ok": true, "images": [{"url"|"path", "marked"}]}`。

选图顺序是一条判据，不是偏好：**先取 marked == false 的第一张**；一张没有才取
marked == true 的第一张——只有带标记的**不算失败**，账上如实记 `marked: true`。
两者都没有 → `DRAW_FAILED`。没配地址／连不上／超时／回的东西认不出 → `DRAW_UNAVAILABLE`
（人话「这个我画不出来」）。空图也按没画出来算：半成品与占位图不进盘。

`url` 要下载，`path` 是本地已有的文件（经 store 读字节）；**落盘不在这里**——
落盘只经 `store.land_draw`，登记进仓库归 actions。
地址与超时同样在调用时读：`HAND_DRAW_URL` / `HAND_DRAW_KEY` / `HAND_DRAW_TIMEOUT`（秒，默认 60）。
本模块与 channel.py 是仅有的两个能上网的文件，由 tests/test_source_rules.py 看住。
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import store

DRAW_ENV = "HAND_DRAW_URL"
KEY_ENV = "HAND_DRAW_KEY"
TIMEOUT_ENV = "HAND_DRAW_TIMEOUT"
#: 生图比发消息慢得多：默认给一分钟，仍读得动环境变量就按环境变量。
DEFAULT_TIMEOUT = 60.0

OK = "OK"
DRAW_UNAVAILABLE = "DRAW_UNAVAILABLE"
DRAW_FAILED = "DRAW_FAILED"
CODES = (OK, DRAW_UNAVAILABLE, DRAW_FAILED)

#: 后缀从 Content-Type 猜；猜不出再退到 URL／文件名的后缀，最后退 .png。
EXT_BY_TYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
               "image/webp": ".webp", "image/gif": ".gif"}
EXT_BY_SUFFIX = {".png": ".png", ".jpg": ".jpg", ".jpeg": ".jpg", ".webp": ".webp",
                 ".gif": ".gif"}


class Unreachable(Exception):
    """没有回答：连不上、超时。与「画不出来」不是一件事，但都归 DRAW_UNAVAILABLE。"""


def base_url(value: Any = None) -> str:
    """外服地址：显式给 > 环境变量。**空串就是没配**——这时一次网络都不发。"""
    raw = value
    if raw is None:
        raw = os.environ.get(DRAW_ENV)
    return str(raw or "").strip()


def api_key(value: Any = None) -> str:
    """key：显式给 > 环境变量。没配就空串，调用方据此**不带** authorization 头。"""
    raw = value
    if raw is None:
        raw = os.environ.get(KEY_ENV)
    return str(raw or "").strip()


def timeout_seconds(value: Any = None) -> float:
    raw = value
    if raw is None:
        raw = os.environ.get(TIMEOUT_ENV)
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return number if number > 0 else DEFAULT_TIMEOUT


def _headers(key: str) -> Dict[str, str]:
    headers = {"content-type": "application/json; charset=utf-8", "accept": "application/json"}
    if key:
        headers["authorization"] = "Bearer " + key
    return headers


def _outcome(code: str, *, origin: str, url: str = "", error: Any = None) -> Dict[str, Any]:
    """固定六栏：判据、origin（原始原因码）、字节、后缀、标记、来源。"""
    return {"code": code, "origin": origin, "data": None, "ext": store.DEFAULT_IMAGE_EXT,
            "marked": None, "source": None, "url": url, "http": None,
            "error": None if error is None else str(error)}


def _request_json(endpoint: str, body: Dict[str, Any], headers: Dict[str, str],
                  timeout: float) -> Tuple[int, bytes]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(endpoint, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 0) or 0), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code or 0), (exc.read() or b"")
    except urllib.error.URLError as exc:
        raise Unreachable(str(exc.reason)) from exc
    except (socket.timeout, TimeoutError, OSError) as exc:
        raise Unreachable(str(exc)) from exc


def download(url: str, timeout: float) -> Tuple[bytes, str]:
    """把一张图从 url 抓下来 → (字节, content-type)。抓不到就抛 Unreachable。"""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), str(response.headers.get("content-type") or "")
    except urllib.error.HTTPError as exc:
        raise Unreachable("HTTP %s" % exc.code) from exc
    except urllib.error.URLError as exc:
        raise Unreachable(str(exc.reason)) from exc
    except (socket.timeout, TimeoutError, OSError) as exc:
        raise Unreachable(str(exc)) from exc


def _candidates(images: Any) -> List[Dict[str, Any]]:
    """能下载或能读的那几张：`marked` **原样保留**（缺失就是缺失，不许补成 False）。"""
    if not isinstance(images, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in images:
        if not isinstance(item, dict):
            continue
        url, path = item.get("url"), item.get("path")
        has_url = isinstance(url, str) and bool(url.strip())
        has_path = isinstance(path, str) and bool(path.strip())
        if not (has_url or has_path):
            continue
        out.append(item)
    return out


def pick(images: Any) -> Optional[Dict[str, Any]]:
    """先取 `marked == false` 的第一张；没有才取 `marked == true` 的第一张。"""
    usable = _candidates(images)
    for wanted in (False, True):
        for item in usable:
            if item.get("marked") is wanted:
                return item
    return None


def ext_of(item: Dict[str, Any], source: str, content_type: str = "") -> str:
    """后缀：响应自己给的 > URL／文件名的后缀 > Content-Type > .png。"""
    hinted = item.get("ext")
    if isinstance(hinted, str) and hinted.strip():
        text = hinted.strip().lower()
        text = text if text.startswith(".") else "." + text
        if text in EXT_BY_SUFFIX:
            return EXT_BY_SUFFIX[text]
    suffix = Path(str(source).split("?")[0]).suffix.lower()
    if suffix in EXT_BY_SUFFIX:
        return EXT_BY_SUFFIX[suffix]
    plain = str(content_type).split(";")[0].strip().lower()
    return EXT_BY_TYPE.get(plain, store.DEFAULT_IMAGE_EXT)


def painting(*, description: Any, url: Any = None, key: Any = None,
             timeout: Any = None) -> Dict[str, Any]:
    """去外服要一张图 → `{"code", "origin", "data", "ext", "marked", "source", ...}`。

    `marked` 是**从那一条响应里原样读来的**布尔：记进账的那一格不许由这里推断。
    """
    endpoint = base_url(url)
    if not endpoint:
        return _outcome(DRAW_UNAVAILABLE, origin="HAND_DRAW_URL_UNSET",
                        error="没有配 HAND_DRAW_URL，这个我画不出来")
    budget = timeout_seconds(timeout)
    body = {"prompt": "" if description is None else str(description), "no_watermark": True}
    try:
        status, raw = _request_json(endpoint, body, _headers(api_key(key)), budget)
    except Unreachable as exc:
        return _outcome(DRAW_UNAVAILABLE, origin="UNREACHABLE", url=endpoint, error=str(exc))

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return _outcome(DRAW_UNAVAILABLE, origin="BAD_RESPONSE", url=endpoint,
                        error="回的不是 JSON（%s）" % exc)
    if not isinstance(payload, dict):
        return _outcome(DRAW_UNAVAILABLE, origin="BAD_RESPONSE", url=endpoint,
                        error="回的不是一个对象")
    if payload.get("ok") is not True:
        return _outcome(DRAW_UNAVAILABLE, origin="NOT_OK", url=endpoint,
                        error="ok=%r" % (payload.get("ok"),))

    chosen = pick(payload.get("images"))
    if chosen is None:
        # images 一张都没有，或者每一张都既没有 url 也没有 path／marked 不是布尔。
        return _outcome(DRAW_FAILED, origin="NO_USABLE_IMAGE", url=endpoint,
                        error="回来的 images 里没有一张能用的")

    raw_url = chosen.get("url")
    remote = isinstance(raw_url, str) and bool(raw_url.strip())
    sample = raw_url if remote else str(chosen.get("path") or "")
    data: Optional[bytes] = None
    content_type = ""
    if remote:
        try:
            data, content_type = download(str(raw_url).strip(), budget)
        except Unreachable as exc:
            return _outcome(DRAW_UNAVAILABLE, origin="DOWNLOAD_UNREACHABLE", url=endpoint,
                            error=str(exc))
        source = "url"
    else:
        try:
            data = store.read_blob(chosen.get("path"))
        except OSError as exc:
            return _outcome(DRAW_FAILED, origin="LOCAL_MISSING", url=endpoint,
                            error="它说图在 %s，可那里没有（%s）" % (chosen.get("path"), exc))
        source = "path"

    if not data:
        return _outcome(DRAW_FAILED, origin="EMPTY_IMAGE", url=endpoint,
                        error="回来的图是空的")
    return {"code": OK, "origin": "OK", "data": bytes(data),
            "ext": ext_of(chosen, sample, content_type), "marked": chosen.get("marked"),
            "source": source, "url": endpoint, "http": status, "error": None}
