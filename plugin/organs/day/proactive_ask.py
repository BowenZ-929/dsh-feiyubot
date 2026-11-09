"""交给核的那一层：拼提示词 → 调核 → 解析回执。**只管这三件**。

判据（先写在这里，别处不许再判一次）：

1. **提示词与解析全参数化进 data/proactive.json**：模板、temperature、最多收几段、
   回执里那两个键叫什么、要不要先剥 ```json 壳，全在那份文件里。本文件只留
   一份兜底（DEFAULTS），好让人把配置删坏了也不至于没有提示词可发。
2. **不改核**：只调 organs.mind.complete（它自己的 config/env/transport 三个口子
   原样转进去），核的失败按 LLMError.kind 如实带回——本文件不吞异常、不改 kind、
   不重试（主动开口不做退避重试：下一 tick 可以由动念重新产生，受每人每日上限约束）。
3. **回执只认一段 JSON**：{"send": bool, "chunks": [str, ...]}。解析不出就回
   error_kind="parse"，绝不把一段裸文本当成她要说的话——那是"猜她想说什么"，不是读回执。
4. **本文件不落盘、不发送、不记账**：谁收到这份回执，谁负责 consume / 落账 / 进待说队列
   （见 organs/day/proactive.py 的 run_due）。所以这里没有 root，也没有"顺便写一行"。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Mapping, Optional

from organs.mind import LLMError, complete as mind_complete

#: 回执里的默认键名（真源在 data/proactive.json 的 ask.send_key / ask.chunks_key）。
SEND_KEY = "send"
CHUNKS_KEY = "chunks"

#: 兜底：配置文件读不到时也要有一份能发出去的提示词。
DEFAULTS: Dict[str, Any] = {
    "temperature": 0.7,
    "max_chunks": 6,
    "chunk_join": "\n",
    "send_key": SEND_KEY,
    "chunks_key": CHUNKS_KEY,
    "strip_fences": True,
    "system": "你是{who}身边那个人。你想主动找{who}说句话。语气档：{tone}。说人话，一到两段。",
    "user": "你想开口的由头（真事，不许编）：{because}\n{rule}不想说就 send=false；"
            "要说就给 1-{max_chunks} 段短句。\n只回一个 JSON：{\"send\": true, \"chunks\": []}",
}


class ParseError(ValueError):
    """回执不是那段 JSON：解析失败要单独一类，不能与"核打挂了"混成一个桶。"""


# ---- 提示词 ---------------------------------------------------------------


def ask_config(config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """ask 那一块的生效值：文件里写了的覆盖兜底，缺项按兜底补。

    config 传的是整份 proactive.json（或只它的 ask 一块）：两块都认，省得调用方
    为了这一个参数去拆字典。
    """
    raw: Any = config
    if isinstance(raw, Mapping) and isinstance(raw.get("ask"), Mapping):
        raw = raw["ask"]
    out = dict(DEFAULTS)
    if isinstance(raw, Mapping):
        out.update(dict(raw))
    return out


def _key(cfg: Mapping[str, Any], name: str, fallback: str) -> str:
    """回执里那两个键的名字（send_key / chunks_key），坏值退回兜底。"""
    value = cfg.get(name)
    return str(value) if isinstance(value, str) and value.strip() else fallback


def _render(template: Any, fields: Mapping[str, Any]) -> str:
    """占位符逐个替换（不认 str.format：提示词里本来就带 JSON 的花括号）。"""
    text = str(template if template is not None else "")
    for key, value in fields.items():
        text = text.replace("{%s}" % key, str(value))
    return text


def build_messages(ctx: Mapping[str, Any],
                   ask_cfg: Optional[Mapping[str, Any]] = None) -> List[Dict[str, str]]:
    """一次开口要送给核的两段：system（她是谁、什么语气）+ user（由头 + 回执格式）。"""
    cfg = ask_config(ask_cfg)
    fields = {
        "who": str(ctx.get("who") or ""),
        # tone 这一轮先留空（语气档归嘴/soul，日子线还没有那个读数），占位符照样替换
        "tone": str(ctx.get("tone") or ""),
        "because": str(ctx.get("because") or ""),
        # rule：只对 pending_task 那条由头有内容（"只能用给定的标题与时间"），其余理由码是空串。
        # 空串在模板里就是一段空白：别的由头不该读到一句任务卡的规矩。
        "rule": str(ctx.get("rule") or ""),
        "max_chunks": int(cfg.get("max_chunks") or 0) or DEFAULTS["max_chunks"],
        "send_key": _key(cfg, "send_key", SEND_KEY),
        "chunks_key": _key(cfg, "chunks_key", CHUNKS_KEY),
    }
    system = _render(cfg.get("system"), fields)
    if ctx.get("persona"):
        system = str(ctx["persona"]) + "\n\n" + system
    user = _render(cfg.get("user"), fields)
    context = {key: ctx[key] for key in ("now", "reason_code", "state", "source") if ctx.get(key)}
    if context:
        user = ("[本轮资料：状态用于把握语气，不照读参数；来源原话属于原作者，不是你的亲历]\n"
                + json.dumps(context, ensure_ascii=False) + "\n\n" + user)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---- 解析 -----------------------------------------------------------------


def _strip_fences(text: str) -> str:
    """剥掉 ```json ... ``` 这层壳；没壳就原样返回。"""
    body = text.strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()
    if lines:
        lines = lines[1:]
    while lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _first_object(text: str) -> str:
    """从第一个 "{" 起按花括号配平截一段出来（回执前后多说了两句也还能读）。"""
    start = text.find("{")
    if start < 0:
        raise ParseError("回执里没有 JSON 对象：%r" % (text[:120],))
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ParseError("回执里的 JSON 没有闭合：%r" % (text[start:start + 120],))


def parse(reply: Any, ask_cfg: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """回执 → {"send", "chunks", "text"}；不是那段 JSON 就抛 ParseError（不猜）。

    send 必须是真布尔（"false" 这种字符串不算：那是模型没说清，不是她不想说）；
    chunks 的空白段落直接丢掉——空产出是"她这轮没话"，由调用方记 empty，不在这里编一句。
    """
    cfg = ask_config(ask_cfg)
    text = "" if reply is None else str(reply)
    body = _strip_fences(text) if cfg.get("strip_fences", True) else text
    payload = json.loads(_first_object(body))
    if not isinstance(payload, dict):
        raise ParseError("回执的 JSON 不是一个对象：%r" % (payload,))
    send_key = _key(cfg, "send_key", SEND_KEY)
    chunks_key = _key(cfg, "chunks_key", CHUNKS_KEY)
    send = payload.get(send_key)
    if not isinstance(send, bool):
        raise ParseError("回执缺 %s 或它不是 true/false：%r" % (send_key, payload))
    raw = payload.get(chunks_key)
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ParseError("回执的 %s 要是列表：%r" % (chunks_key, raw))
    chunks = [str(item).strip() for item in raw]
    chunks = [item for item in chunks if item]
    try:
        limit = int(cfg.get("max_chunks") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        chunks = chunks[:limit]
    join = str(cfg.get("chunk_join") if cfg.get("chunk_join") is not None else DEFAULTS["chunk_join"])
    return {"send": send, "chunks": chunks, "text": join.join(chunks).strip()}


# ---- 对外 -----------------------------------------------------------------


def ask(ctx: Mapping[str, Any], *, complete: Optional[Callable[..., str]] = None,
        config: Optional[Mapping[str, Any]] = None, env: Optional[Mapping[str, str]] = None,
        transport: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """问一次核：「现在你想不想说，想说什么」。

    ctx：{who, because, tone, rule}（tone / rule 这一轮允许空；rule 只有 pending_task 那条由头
    才会填，内容是"只能用卡上的标题与时间"）；可带persona/now/state/source/reason_code，
    均由编排传入；root不进提示词，本文件不读盘。
    complete / config / env / transport：一律可注入（测试一个真网络请求都不发；
    生产时全不传，核自己去读 data/llm.json 与 .env）。
    返回：
        成功 {"ok": True, "send": bool, "chunks": [...], "text": str, "raw": 回执原文}
        核挂了 {"ok": False, "error_kind": <LLMError.kind>, "error": ..., "raw": ""}
        解析不出 {"ok": False, "error_kind": "parse", "error": ..., "raw": 回执原文}
    """
    ask_cfg = ask_config(config)
    messages = build_messages(ctx, ask_cfg)
    call = complete if complete is not None else mind_complete
    try:
        reply = call(messages, config=None, env=env, transport=transport,
                     temperature=ask_cfg.get("temperature"))
    except LLMError as exc:
        return {"ok": False, "error_kind": str(exc.kind), "error": str(exc), "raw": ""}
    except Exception as exc:  # noqa: BLE001 - 核之外的错也要变成能分岔的一行，不许炸掉这一轮
        return {"ok": False, "error_kind": "exception",
                "error": "%s: %s" % (type(exc).__name__, exc), "raw": ""}
    raw = "" if reply is None else str(reply)
    try:
        parsed = parse(raw, ask_cfg)
    except ParseError as exc:
        return {"ok": False, "error_kind": "parse", "error": str(exc), "raw": raw}
    except ValueError as exc:  # json 的 JSONDecodeError 也是 ValueError
        return {"ok": False, "error_kind": "parse", "error": str(exc), "raw": raw}
    return {"ok": True, "send": bool(parsed["send"]), "chunks": list(parsed["chunks"]),
            "text": parsed["text"], "raw": raw}
