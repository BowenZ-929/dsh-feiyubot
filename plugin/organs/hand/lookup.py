"""查：把一句公共 query 交给检索接口，只把结果规范化——**不落卡、不接线、不记账**。

为什么要独立一个文件：网络只许长在登记过的模块里（tests/test_source_rules.py 的
NETWORK_FILES 看住这一点）。本模块是「外面那一层」，与 draw.py 同性质：只拿回结构化结果，
不碰家、不碰账；落账与回执归 actions.lookup。

三条契约（改它要同步改用例）：

1. 端点＝base_url 去掉尾部斜杠 + 「/tools/search」。实盘 base_url 是
   https://api.deepseek.com/v1，所以这里**绝不自己再拼 /v1**——拼出来就是
   /v1/v1/tools/search，服务端只会回 404，而 404 在账上很像「对面挂了」，会把人引向错误的排查。
2. 请求体只发 query / count / need_content=false。**不发** ori_question、定位、完整上下文：
   检索服务不需要知道「谁在问」，多发一个字就是把私聊材料送出去（brief 第 3 节 门0）。
   门0 本身不在本模块实现——本模块只保证**没有**一个「顺手带上原话」的字段。
3. 地址、key、超时全部由调用方注入；本模块**不读环境变量、不读 .env、不读 llm.json**。
   手不许依赖脑：配置住在编排层，读不到就当没配（fail closed），一次请求都不发。

出错不抛给调用方，一律折叠成同一形状：

    {"state": "ok"|"empty"|"error", "results": [...], "calls": 0|1,
     "error": 类别|None, "status": HTTP 码|None}

**分类必须分得开**：鉴权失败／限流／服务错／超时／连不上／坏响应六种要能各自记账。一律写
"failed" 就等于把「没通路」和「没知识」糊成同一句，而那正是这次要修的病（brief 第 4 节）。

calls 记**实际发出的请求数**（0 或 1）：本地就拒掉的不许记成 1，否则「根本没发请求」和
「发了、失败了」在手账上分不开；计费次数另按服务口径（200 且结果非空）由账那边记，本模块
不替它算，也不拿本地异常去冒充服务端结算（brief 第 14.3 节）。

**不重试**（brief 第 11 节已定关闭自动重试）：超时的时间与钱都不该由这里替人做主再花一次。
"""
from __future__ import annotations

import hashlib
import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

#: 端点后缀。base_url 由调用方给（实盘已含 /v1），这里只补后缀。
ENDPOINT = "/tools/search"
#: 服务口径：query 1–128 个字符。超了本地就拒，不花一次往返去换一个 400。
MAX_QUERY_CHARS = 128
#: 默认结果数：首版按小预算取（brief 第 14.3 节）。
DEFAULT_COUNT = 5
#: 兜底超时。调用方应当注入自己的值；但没有它，没配的人会一直挂着等。
DEFAULT_TIMEOUT = 20.0

OK = "ok"
EMPTY = "empty"
ERROR = "error"
STATES = (OK, EMPTY, ERROR)

#: 对面说的五种 + 这边判的四种。分类是账上的判据，所以是常量而不是散在各处的字符串。
AUTH = "auth"
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"
BAD_RESPONSE = "bad_response"
NO_ENDPOINT = "no_endpoint"
NO_KEY = "no_key"
QUERY_BLANK = "query_blank"
QUERY_TOO_LONG = "query_too_long"
ERRORS: Tuple[str, ...] = (AUTH, RATE_LIMITED, SERVER_ERROR, TIMEOUT, UNREACHABLE,
                           BAD_RESPONSE, NO_ENDPOINT, NO_KEY, QUERY_BLANK, QUERY_TOO_LONG)

#: 规范化后每条结果的八格：面板与卡都可以少读，但**格子必须都在**（缺格与空值是两件事）。
RESULT_FIELDS: Tuple[str, ...] = ("title", "url", "snippet", "summary", "published_at",
                                  "site_name", "authority", "rerank_score")

#: 注入点：url, headers, payload, timeout -> (status, body)。测试一律用假的。
Transport = Callable[[str, Dict[str, str], Dict[str, Any], float], Tuple[int, bytes]]


def endpoint_of(base_url: Any) -> str:
    """→ 完整端点；没配就是空串（调用方据此一次请求都不发）。"""
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return base + ("" if base.endswith("/v1") else "/v1") + ENDPOINT


def fingerprint(query: Any) -> Dict[str, Any]:
    """账上只留指纹与长度：query 原文一旦进账，门0 的隐私口径就被账本身漏出去了。"""
    text = "" if query is None else str(query)
    return {"query_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "query_chars": len(text)}


def _shape(*, state: str, results: Any = (), calls: int = 0, error: Optional[str] = None,
           status: Any = None) -> Dict[str, Any]:
    """固定五格。status 单列：账上要能指着这一笔说清是哪一档 HTTP。"""
    return {"state": state, "results": list(results), "calls": int(calls), "error": error,
            "status": None if status is None else int(status)}


def _seconds(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return number if number > 0 else DEFAULT_TIMEOUT


def count_of(value: Any) -> int:
    """→ 实际发出去的结果数；认不出／非正数就退回默认值（不把 0 悄悄变成「查全部」）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_COUNT
    return min(number, 50) if number > 0 else DEFAULT_COUNT


def _headers(key: str) -> Dict[str, str]:
    return {"content-type": "application/json; charset=utf-8", "accept": "application/json",
            "authorization": "Bearer " + key}


def _post(url: str, headers: Dict[str, str], payload: Dict[str, Any],
          timeout: float) -> Tuple[int, bytes]:
    """默认 transport：标准库发**一次** POST。

    连不上、超时都让它抛出来，由 search 分类——这里吞掉就再也分不清是哪种没通了。
    4xx／5xx 反而**不算异常**：那是对方的回答，状态码就是判据。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 0) or 0), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code or 0), (exc.read() or b"")


def _status_error(status: int) -> str:
    """HTTP 状态 → 类别。401／402／403 归一类：对调用方来说都是「这个 key 现在不能用」。"""
    if status in (401, 402, 403):
        return AUTH
    if status == 429:
        return RATE_LIMITED
    if 500 <= status <= 599:
        return SERVER_ERROR
    if status == 0:
        return UNREACHABLE
    # 其余（含 404：多半是把路径拼错了）都算坏响应——不是「对面没通」，是「回的东西不对」。
    return BAD_RESPONSE


def _value(raw: Any) -> Any:
    """一格原样留着：数字还是数字，缺了就是 None。**不补默认值**——补了就是编。"""
    if raw is None or isinstance(raw, (str, int, float, bool)):
        return raw
    return None


def _results(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """→ 规范化后的结果表；结构不对返回 None（由 search 判成坏响应，不静默当空）。"""
    if not isinstance(payload, dict):
        return None
    items = payload.get("results")
    if not isinstance(items, list):
        return None
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        if not all(isinstance(item.get(name, ""), str) for name in ("title", "url", "snippet", "summary")):
            return None
        row = {name: _value(item.get(name)) for name in RESULT_FIELDS}
        row["published_at"] = _value(item.get("date", item.get("published_at")))
        out.append(row)
    return out


def search(query: Any, *, base_url: Any = None, key: Any = None, timeout: Any = None,
           count: Any = DEFAULT_COUNT,
           transport: Optional[Transport] = None) -> Dict[str, Any]:
    """查一次 → 规范化结果。**不重试、不抛异常**；扣不扣费也不在这里算。"""
    text = "" if query is None else str(query)
    if not text.strip():
        return _shape(state=ERROR, error=QUERY_BLANK)
    if len(text) > MAX_QUERY_CHARS:
        return _shape(state=ERROR, error=QUERY_TOO_LONG)

    url = endpoint_of(base_url)
    if not url:
        return _shape(state=ERROR, error=NO_ENDPOINT)
    secret = str(key or "").strip()
    if not secret:
        # 没 key 发出去只会换一个 401：既费一次往返，也把「没配」伪装成「鉴权失败」。
        return _shape(state=ERROR, error=NO_KEY)

    payload = {"query": text, "count": count_of(count), "need_content": False}
    post = transport or _post
    budget = _seconds(timeout)
    try:
        status, body = post(url, _headers(secret), payload, budget)
    except (TimeoutError, socket.timeout):
        return _shape(state=ERROR, calls=1, error=TIMEOUT)
    except urllib.error.URLError as exc:
        # urllib 把超时包在 reason 里：得看一眼原因，不能一律算「连不上」。
        kind = TIMEOUT if isinstance(exc.reason, (TimeoutError, socket.timeout)) else UNREACHABLE
        return _shape(state=ERROR, calls=1, error=kind)
    except Exception:  # 注入的 transport 塌了也是「没有回答」，不外抛给调用方
        return _shape(state=ERROR, calls=1, error=UNREACHABLE)

    code = int(status or 0)
    if code == 200:
        try:
            payload_in = json.loads(bytes(body or b"").decode("utf-8"))
        except (TypeError, ValueError, UnicodeDecodeError):
            return _shape(state=ERROR, calls=1, error=BAD_RESPONSE, status=code)
        items = _results(payload_in)
        if items is None:
            return _shape(state=ERROR, calls=1, error=BAD_RESPONSE, status=code)
        # 200 + 空数组是 **empty**：一次请求发出去了、也确实什么都没查到；
        # 它既不是成功（没东西给人），也不是失败（通路是好的）——三态必须分得开。
        return _shape(state=OK if items else EMPTY, results=items, calls=1, status=code)
    return _shape(state=ERROR, calls=1, error=_status_error(code), status=code)
