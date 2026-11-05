"""模型客户端：给 messages，回一段字。只认 HTTP，不认魂/脑/心，也不认任何框架。

三件事写在这份文件的 docstring 里，因为它们是接口：

一、key 从 .env 读，不进代码、不进 git。查找顺序先 $QQAGENT_HOME/.env（没设这个
环境变量就跳过），再 <仓库>/home/.env（路径经 organs/paths.py 的 paths.HOME 换算）。
两处都没有、或写了但值是空的，就抛 LLMError(kind="no-key") 并把找过的绝对路径列出来——
绝不静默返回空串。.env 的解析自己写：一行一条 KEY=VALUE，跳过空行与 # 注释，
忽略 export 前缀，去掉成对引号，不执行任何 shell。**.env 优先，环境变量兜底**
（os.environ[key_env]，key_env 由数据文件给出）。

二、模型名、地址、超时、重试次数全在 data/llm.json 里，代码里不写死：
实盘 paths.organ_dir("mind")/llm.json（存在才读）→ 出厂 paths.factory_dir("mind")/llm.json。
数据文件没写的字段不补默认值——补了就是两处真源，直接按 config 报错。

三、失败一律 LLMError(kind, message)，str(exc) == message。kind 分得清才有不同的人话：
no-key / auth / timeout / rate_limit / empty / http / config。空的 200 响应算 empty，
不算成功；只有 timeout、rate_limit 与 HTTP >= 500 会重试，次数上限 = retries + 1。
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .. import paths

ENV_HOME = "QQAGENT_HOME"        # 实盘根；设了就先去它底下的 .env 找 key
ENV_FILE = ".env"                # key 只活在 .env 里
CONFIG_NAME = "llm.json"         # 模型设置的唯一真源
POST_PATH = "/chat/completions"  # OpenAI 兼容协议：POST {base_url}/chat/completions
BODY_CLIP = 200                  # 非 200 的响应体只留 200 字，够认人话，不塞满日志
RETRY_KINDS = ("timeout", "rate_limit")   # 除了 http >= 500，只有这两类值得再试

# transport：注入点。签名 (url, headers, payload, timeout) -> (status: int, body: bytes)；
# payload 是已经摊开的请求体 dict（默认实现负责 encode，测试直接读 payload["model"]）。
Transport = Callable[[str, Dict[str, str], Dict[str, Any], float], Tuple[int, bytes]]


class LLMError(Exception):
    """模型这边的失败：kind 给机器分岔，message 给人看。

    status 只对 kind="http" 有意义（拿不到状态码时是 0），重试判据靠它。
    """

    def __init__(self, kind: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status

    def __str__(self) -> str:
        return self.message


class LLMConfigError(LLMError):
    """配置读不出来（数据文件坏、缺字段、base_url 空）：kind 固定 config。"""

    def __init__(self, message: str) -> None:
        super().__init__("config", message)


def env_candidates(env: Optional[Mapping[str, str]] = None) -> List[Path]:
    """按查找顺序列出 .env 的候选绝对路径：$QQAGENT_HOME/.env 在前，<仓库>/home/.env 在后。

    env 给了 dict 就用它认 QQAGENT_HOME（测试换来源用），None 时读 os.environ。
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw = str(source.get(ENV_HOME, "") or "").strip()
    found: List[Path] = []
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():          # 相对路径一律按仓库根补全，便于直接抄去建文件
            candidate = paths.REPO_ROOT / candidate
        found.append(candidate / ENV_FILE)
    found.append(paths.HOME / ENV_FILE)
    return found


def find_env_file(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """第一个真正存在的 .env；一个都没有就是 None（不造文件、不猜路径）。"""
    for candidate in env_candidates(env):
        if candidate.is_file():
            return candidate
    return None


def parse_env(text: str) -> Dict[str, str]:
    """十几行的手写解析：空行/# 注释跳过，export 前缀忽略，KEY=VALUE，成对引号去掉。

    不做变量展开、不执行任何 shell——.env 只是「一张键值表」，不是脚本。
    """
    pairs: Dict[str, str] = {}
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs[name] = value
    return pairs


def read_key(config: Mapping[str, Any], env: Optional[Mapping[str, str]] = None) -> str:
    """把 key 拿出来：.env（先 $QQAGENT_HOME/.env，再 <仓库>/home/.env）优先，环境变量兜底。

    .env 里有这个键但值是空的，按「没配」处理——空 key 送出去只会换回一个 401。
    拿不到就抛 no-key，人话里带上 key 的名字和所有找过的绝对路径。
    """
    key_env = str(config.get("key_env") or "").strip()
    if not key_env:
        raise LLMConfigError("模型配置缺 key_env（key 的环境变量名）：改 data/llm.json")
    env_file = find_env_file(env)
    if env_file is not None:
        try:
            pairs = parse_env(env_file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise LLMError("no-key", "读不了 %s：%s" % (env_file, exc)) from exc
        value = pairs.get(key_env, "").strip()
        if value:
            return value
    source: Mapping[str, str] = os.environ if env is None else env
    value = str(source.get(key_env, "") or "").strip()
    if value:
        return value
    places = " 或 ".join(str(path) for path in env_candidates(env))
    raise LLMError("no-key", "还没配模型 key，去 %s 写 %s=<你的 key>" % (places, key_env))


def load_config(path: Optional[Any] = None) -> Dict[str, Any]:
    """读一份模型配置：实盘覆盖（存在才读）→ 出厂数据；path 给定时只读那一份。

    两份都没有是 config 错，不是「用默认值悄悄跑起来」。
    """
    if path is not None:
        return _read_config(Path(path))
    live = paths.organ_dir("mind") / CONFIG_NAME
    if live.is_file():
        return _read_config(live)
    factory = paths.factory_dir("mind") / CONFIG_NAME
    if factory.is_file():
        return _read_config(factory)
    raise LLMConfigError("找不到模型配置：%s 与 %s 都不存在" % (live, factory))


def complete(
    messages: List[Dict[str, Any]],
    *,
    config: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    transport: Optional[Transport] = None,
) -> str:
    """把 messages 送出去，把她的那句话拿回来（OpenAI 兼容协议，urllib 投递）。

    messages：[{"role": ..., "content": ...}]，空列表或全空白就是 config 错，不发请求。
    config：None 时按 load_config() 读数据文件；给了就当作校验对象直接用（测试换配置）。
    env：None 时读 os.environ；给 dict 就只认这份（测试换 key 来源，不碰本机环境）。
    transport：None 时走 urllib；给函数就完全走它（测试一律注入，绝不发真网络请求）。

    返回值是 choices[0].message.content 去首尾空白后的文本；空内容抛 empty，绝不返回 ""。
    重试只对 timeout / rate_limit / http >= 500，总尝试次数 = retries + 1。
    """
    conf = load_config() if config is None else _check_config(config, "传入的 config")
    prepared = _check_messages(messages)
    key = read_key(conf, env)
    send: Transport = default_transport if transport is None else transport
    url = conf["base_url"].rstrip("/") + POST_PATH
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    payload = {
        "model": conf["model"],
        "messages": prepared,
        "temperature": conf["temperature"],
        "max_tokens": conf["max_tokens"],
    }
    timeout = conf["timeout_ms"] / 1000.0
    error: Optional[LLMError] = None
    for _ in range(conf["retries"] + 1):
        try:
            status, body = send(url, headers, payload, timeout)
        except LLMError as exc:
            error = exc
        except (TimeoutError, socket.timeout, OSError) as exc:
            error = LLMError("timeout", "模型没在时限内回话：%s" % exc)
        except Exception as exc:  # 注入的 transport 抛别的，也要变成能分岔的 LLMError
            error = LLMError("http", "请求模型时出错：%s" % exc)
        else:
            try:
                return _unwrap(status, body)
            except LLMError as exc:
                error = exc
        if not _retryable(error):
            raise error
    raise error


def default_transport(url: str, headers: Dict[str, str], payload: Dict[str, Any],
                      timeout: float) -> Tuple[int, bytes]:
    """默认投递：urllib.request。返回 (状态码, 响应体字节)；4xx/5xx 也算「拿到了应答」。

    只有连不上/超时才在这里抛 LLMError：超时值得再试，握手失败（http, status=0）不值得。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:   # 非 2xx：HTTPError 也是应答，别当网络错
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        return int(exc.code), raw
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason):
            raise LLMError("timeout", "模型没在时限内回话：%s（地址 %s）" % (reason, url)) from exc
        raise LLMError("http", "连不上模型服务：%s（地址 %s）" % (reason, url)) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise LLMError("timeout", "模型没在时限内回话：%s（地址 %s）" % (exc, url)) from exc
    except OSError as exc:
        raise LLMError("http", "连不上模型服务：%s（地址 %s）" % (exc, url)) from exc


def _read_config(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LLMConfigError("读不了模型配置 %s：%s" % (path, exc)) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LLMConfigError("模型配置不是 JSON：%s：%s" % (path, exc)) from exc
    return _check_config(data, str(path))


def _check_config(data: Any, where: str) -> Dict[str, Any]:
    """校验并归一化：缺字段、类型不对、空值一律 config——数据里没有的数不许在代码里补。"""
    if not isinstance(data, dict):
        raise LLMConfigError("模型配置不是一份对象：%s" % where)
    conf: Dict[str, Any] = {}
    for name in ("provider", "model", "base_url", "key_env"):
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            raise LLMConfigError("模型配置缺字段 %s（或为空）：%s" % (name, where))
        conf[name] = value.strip()
    for name in ("timeout_ms", "max_tokens"):
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise LLMConfigError("模型配置字段 %s 要正整数：%s" % (name, where))
        conf[name] = value
    retries = data.get("retries")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise LLMConfigError("模型配置字段 retries 要非负整数：%s" % where)
    conf["retries"] = retries
    temperature = data.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise LLMConfigError("模型配置字段 temperature 要数字：%s" % where)
    conf["temperature"] = float(temperature)
    return conf


def _check_messages(messages: Any) -> List[Dict[str, str]]:
    """messages 的形状就那一种；空列表或全空白直接 config 错，不浪费一次请求。"""
    if not isinstance(messages, (list, tuple)) or not messages:
        raise LLMConfigError('messages 要一份非空列表：[{"role": ..., "content": ...}]')
    prepared: List[Dict[str, str]] = []
    for item in messages:
        if not isinstance(item, dict):
            raise LLMConfigError('messages 每一项都要是对象：{"role": ..., "content": ...}')
        role = item.get("role")
        content = item.get("content")
        if not isinstance(role, str) or not role.strip():
            raise LLMConfigError("messages 里有一项缺 role")
        if not isinstance(content, str):
            raise LLMConfigError("messages 里有一项 content 不是字符串")
        prepared.append({"role": role.strip(), "content": content})
    if not any(item["content"].strip() for item in prepared):
        raise LLMConfigError("messages 全空：没有一句话可以送出去")
    return prepared


def _unwrap(status: int, body: bytes) -> str:
    """把一次应答翻成文本或 LLMError：200 里也必须真的有字，空内容不是成功。"""
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    if int(status) != 200:
        raise _status_error(int(status), text)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LLMError("http", "模型回了 HTTP 200 但不是 JSON（%s）：%s"
                       % (exc, _clip(text)), 200) from exc
    content = None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str) or not content.strip():
        raise LLMError("empty", "模型回了空内容（这一句没说出口）", 200)
    return content.strip()


def _status_error(status: int, text: str) -> LLMError:
    """非 200 的分岔：401/403 是 key 的事，429 是节奏的事，别的都是服务端的事。"""
    if status in (401, 403):
        return LLMError("auth", "模型 key 不对或没权限（HTTP %d）" % status, status)
    if status == 429:
        return LLMError("rate_limit", "模型限流了，等下再试（HTTP 429）", status)
    return LLMError("http", "模型返回 HTTP %d：%s" % (status, _clip(text)), status)


def _clip(text: str) -> str:
    """响应体只留前 200 字：够认出「你余额不足」，不把整页 HTML 塞进日志。"""
    return str(text).strip()[:BODY_CLIP]


def _retryable(error: Optional[LLMError]) -> bool:
    """只重试 timeout / rate_limit / HTTP >= 500；auth、no-key、empty、config 一次就报。"""
    if error is None:
        return False
    if error.kind in RETRY_KINDS:
        return True
    return error.kind == "http" and error.status >= 500
