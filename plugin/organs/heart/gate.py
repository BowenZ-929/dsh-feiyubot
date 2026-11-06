"""心的出站闸：只回答「发不发」，不改她怎么想，也不生成一句话。

立场（人定）：
- **本模块不重判任何门**。三态由 engage.decide 出（硬门：夜间窗口 / 被骂当天 /
  mood 与 energy 双低；软门六条），这里只把「态」翻译成「发不发」：
  SILENT → 不发（账上写清「她这轮不想说」+ 理由码）、LESS → 发但更短且不带反问、
  SPEAK → 正常发。判据与硬门一个字都不动——要改门请去 engage.py / model.py。
- **本模块不生成任何措辞**。话在嘴（mouth），这里连一个句子都不拼；它只给
  limits（max_chars / no_question），怎么说由嘴决定。
- **顺序钉死：先人级（engage 三门）→ 后群级（群滑窗）**，任一层不过就不发。
  层级写在 level 字段里（"engage" / "group"）；理由码里群级自带 group- 前缀
  （group-quota），人级用 engage 自己的码（night / today-volume / ...），
  一眼看得出是哪一层拦的。人级先判也意味着：夜里她不想说的时候，去数群配额
  没有意义——所以人级拦下时，群窗只读不占。
- **冷却单位对齐**：engage 是人级（对这个人的整体节奏），于是她在群 A 聊炸之后
  换到群 B 还能继续刷屏。这里补一条**群级软配额**：group_quota_per_hour 条 /
  群 / group_window_min 分钟滑窗。**私聊不占、不写**（group=None 或空串 →
  不查群窗、不落群账）：私聊是两个人说话，没有「刷屏」这件事。

数据文件（都在 store.default_root() 下，落盘、人可改、改完即生效、不用重启）：
- gate_config.json：{"group_quota_per_hour": 6, "group_window_min": 60,
  "less_max_chars": 48}。读不到 / 读不懂 → 内置默认 + WARN，**永不抛**；
  第一次 gate 落一份（人得有个文件可改）。
- gate_log.jsonl：一行一判（**发也写、不发也写**）。与 engage_log.jsonl 分开：
  那一本是「她怎么想」（判决），这一本是「门怎么判」（出站）。同一件事的两面，
  混成一本就再也说不清分歧是判决歪了还是门歪了。
- group_window.json：{"<群>": [iso, iso, ...]}，只留窗口内的时刻；私聊永远不出现
  在这里（一个键都不给）。

两根尺子各归各的（不借 mood 的 3h/7d，也不借 offset 的 6h/4h）：
- 人级的尺在 engage（它只用当刻快照，一条半衰都不持有）；
- 群级的尺就是本文件的 group_window_min 滑窗：**读时现剪**，滑走即消失，
  没有需要回滚的累积状态。
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from organs.heart import engage, model, store

CONFIG_FILE = "gate_config.json"
LOG_FILE = "gate_log.jsonl"
WINDOW_FILE = "group_window.json"

#: 两道层级的名字：写进返回体与账行的 level 字段（"没拦"时是 None）。
LEVEL_ENGAGE = "engage"
LEVEL_GROUP = "group"

#: 群级软配额的唯一理由码（自带 group- 前缀，与 engage 的码不会混）。
REASON_GROUP_QUOTA = "group-quota"

#: 出厂默认（**默认值，不是验收线**；人改 gate_config.json 即生效）。
CONFIG_DEFAULTS: Dict[str, Any] = {
    "group_quota_per_hour": 6,
    "group_window_min": 60,
    "less_max_chars": 48,
}

#: LESS 是「发但收着发」：字更少、且不把话头递回去（反问最容易勾出下一轮）。
LESS_LIMITS_NO_QUESTION = True


# ---- 落盘路径与小工具 ----------------------------------------------------


def _base(root: Any) -> Path:
    """数据根：给了就用给的（测试一律临时目录），没给就是 store 的实盘根。"""
    return Path(root) if root is not None else store.default_root()


def _config_path(root: Any) -> Path:
    return _base(root) / CONFIG_FILE


def _log_path(root: Any) -> Path:
    return _base(root) / LOG_FILE


def _window_path(root: Any) -> Path:
    return _base(root) / WINDOW_FILE


def _at(now: Optional[str], inp: Any) -> str:
    """这一判的「现在」：now 参数 > inp.now > 此刻。

    先认 now 参数再认 inp.now：判决与账行必须是同一个时刻，否则「她怎么想」与
    「门怎么判」各记一个时间，事后对不上。
    """
    if isinstance(now, str) and now:
        return now
    stamp = getattr(inp, "now", None)
    if isinstance(stamp, str) and stamp:
        return stamp
    return model.now_iso()


def _group(group: Any) -> Optional[str]:
    """群号归一：空串 / None / 非字符串 → None（= 私聊：不查群窗、不占额度）。"""
    if not isinstance(group, str):
        return None
    text = group.strip()
    return text or None


def _int(value: Any, default: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _num(value: Any, default: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _plain(number: float) -> Any:
    """整数值落盘写成 60（不是 60.0）：config 是给人改的，别逗人。"""
    return int(number) if float(number).is_integer() else float(number)


# ---- 配置：读不到就默认 + WARN，永不抛 -----------------------------------


def _normalise(raw: Dict[str, Any]) -> Dict[str, Any]:
    """生效配置：只认这三个键，并归一到安全范围。

    - group_quota_per_hour 至少 1：0 不是「关掉配额」，是「出口彻底关死」——
      她永远发不出去，那不可能是人的本意（要静音请改 engage 的门）；
    - group_window_min 必须 > 0：0 分钟的窗口等于没有账；
    - less_max_chars 至少 1：0 字不是「更短」，是「不说」。
    """
    cfg = dict(CONFIG_DEFAULTS)
    for key in CONFIG_DEFAULTS:
        if key in raw:
            cfg[key] = raw[key]
    cfg["group_quota_per_hour"] = max(
        1, _int(cfg.get("group_quota_per_hour"), CONFIG_DEFAULTS["group_quota_per_hour"]))
    window = _num(cfg.get("group_window_min"), CONFIG_DEFAULTS["group_window_min"])
    cfg["group_window_min"] = _plain(max(1.0, window))
    cfg["less_max_chars"] = max(
        1, _int(cfg.get("less_max_chars"), CONFIG_DEFAULTS["less_max_chars"]))
    return cfg


def _read_config(root: Any) -> Dict[str, Any]:
    """读配置：缺文件 / 坏 JSON / 不是对象 → 内置默认 + 一行 WARN，永不抛。"""
    path = _config_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        warnings.warn(
            "心·gate：%s 还没有，先按内置默认 %r 走（第一次 gate 会落一份，人可改）"
            % (path, CONFIG_DEFAULTS), RuntimeWarning, stacklevel=2)
        return dict(CONFIG_DEFAULTS)
    except OSError as exc:
        warnings.warn(
            "心·gate：%s 读不到（%s：%s），按内置默认 %r 走"
            % (path, type(exc).__name__, exc, CONFIG_DEFAULTS),
            RuntimeWarning, stacklevel=2)
        return dict(CONFIG_DEFAULTS)
    try:
        raw = json.loads(text)
    except ValueError as exc:
        warnings.warn(
            "心·gate：%s 读不懂（%s：%s），按内置默认 %r 走"
            % (path, type(exc).__name__, exc, CONFIG_DEFAULTS),
            RuntimeWarning, stacklevel=2)
        return dict(CONFIG_DEFAULTS)
    if not isinstance(raw, dict):
        warnings.warn(
            "心·gate：%s 不是 JSON 对象（%s），按内置默认 %r 走"
            % (path, type(raw).__name__, CONFIG_DEFAULTS),
            RuntimeWarning, stacklevel=2)
        return dict(CONFIG_DEFAULTS)
    return _normalise(raw)


def _ensure_config(root: Any) -> None:
    """第一次 gate 落一份默认配置：人得有个文件可改（读不懂的那份不覆盖，等人修）。

    peek / overview 一个字节都不写，所以只有门自己（gate）会走到这里。
    """
    path = _config_path(root)
    if path.exists():
        return
    try:
        store.write_json(path, dict(CONFIG_DEFAULTS))
    except Exception as exc:  # 配置落盘失败不该让这一轮变成异常
        warnings.warn(
            "心·gate：默认配置落盘失败（%s：%s），这一轮仍按内置默认判"
            % (type(exc).__name__, exc), RuntimeWarning, stacklevel=2)


# ---- 群滑窗账 ------------------------------------------------------------


def _read_window(root: Any) -> Dict[str, List[str]]:
    """读群窗账：只认 {群: [iso, ...]}，坏行 / 坏值一律跳过，不炸调用方。"""
    data = store.read_json(_window_path(root), {})
    window: Dict[str, List[str]] = {}
    for group, stamps in data.items():
        if not isinstance(group, str) or not group or not isinstance(stamps, list):
            continue
        window[group] = [stamp for stamp in stamps if isinstance(stamp, str) and stamp]
    return window


def _prune(stamps: List[str], at: str, window_min: float) -> List[str]:
    """只留窗口内的时刻：滑走的那几条读时现剪，不落任何"已过期"标记。

    读不懂的时间戳一律跳过并 WARN（一条坏时刻不该让整本账作废），但**不当作
    窗口内的条目**：宁可少算一格，也不凭空多拦她一次。
    """
    rows: List[str] = []
    for stamp in stamps:
        try:
            age = model.hours_between(stamp, at) * 60.0
        except ValueError as exc:
            warnings.warn(
                "心·gate：群窗账里的时刻读不懂（%r：%s），这一条不算"
                % (stamp, exc), RuntimeWarning, stacklevel=3)
            continue
        if age <= window_min:
            rows.append(stamp)
    return rows


def _window_usage(root: Any, at: str, cfg: Dict[str, Any]) -> Dict[str, List[str]]:
    """每个群此刻窗口内的时刻表（已按窗口剪过，**不落盘**）。"""
    window_min = float(cfg["group_window_min"])
    return dict((group, _prune(stamps, at, window_min))
                for group, stamps in _read_window(root).items())


def _limits(state: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """态 → 出口的说话限制：不说没有限制可谈；少说更短且不带反问；说就不限。

    **只在 send=True 时有意义**；被群级拦下时它仍然是「本可以怎么发」的读数——
    门只改发不发，不改她本来想怎么说；要不要看它，由拿 send 的那一方决定。
    """
    if state == model.STATE_LESS:
        return {"max_chars": int(cfg["less_max_chars"]),
                "no_question": LESS_LIMITS_NO_QUESTION}
    return {}


# ---- 判决：先人级，后群级 ------------------------------------------------


def _judge(inp: engage.EngageInput, group: Optional[str], root: Any,
           now: Optional[str]) -> Tuple[Dict[str, Any], str, Optional[str],
                                        Dict[str, List[str]]]:
    """一次出站判断（不写账）：返回 (返回体, at, 占到的群或 None, 剪过的群窗)。

    人级（engage.decide）先判：SILENT 直接不发，LESS 给限制，SPEAK 正常。
    人级放行**且**有群号时才查群窗：窗口内已满 → 群级拦下；没满 → 占一格。
    私聊（group=None）不查不占，quota 报 used=0（limit 照给，人要知道数值在）。
    """
    cfg = _read_config(root)
    at = _at(now, inp)
    verdict = engage.decide(inp)
    state = verdict["state"]
    reason = verdict["reason"]
    hits = list(verdict["hits"])

    send = True
    level: Optional[str] = None
    if state == model.STATE_SILENT:
        # 她这轮不想说：账上留「态 + 理由码」，下游据此闭嘴。
        send = False
        level = LEVEL_ENGAGE

    window = _window_usage(root, at, cfg)
    limit = int(cfg["group_quota_per_hour"])
    quota: Dict[str, int] = {"used": 0, "limit": limit, "left": limit}
    occupied: Optional[str] = None

    if group is not None:
        rows = list(window.get(group, []))
        used = len(rows)
        if not send:
            # 人级已经拦下了：群窗只读（不占、不写），免得「不想说」也吃掉一格额度。
            quota = {"used": used, "limit": limit, "left": max(0, limit - used)}
        elif used >= limit:
            send = False
            level = LEVEL_GROUP
            reason = REASON_GROUP_QUOTA
            quota = {"used": used, "limit": limit, "left": 0}
        else:
            rows.append(at)
            window[group] = rows
            occupied = group
            used += 1
            quota = {"used": used, "limit": limit, "left": max(0, limit - used)}

    payload = {
        "send": send,
        "state": state,
        "reason": reason,
        "hits": hits,
        "level": level,
        "limits": _limits(state, cfg),
        "quota": quota,
        "window_min": cfg["group_window_min"],
    }
    return payload, at, occupied, window


def _warn_blocked(level: Optional[str], state: str, reason: Any, group: Optional[str],
                  quota: Dict[str, int], window_min: Any) -> None:
    """不发就响一声（一行，写清哪一层、什么理由）——沉默的拦截最难查。"""
    if level == LEVEL_GROUP:
        warnings.warn(
            "心·gate：这一轮不发——群级拦的（群 %s 在 %s 分钟窗口里已经 %d 条，"
            "上限 %d 条，理由 %s）"
            % (group, window_min, quota["used"], quota["limit"], reason),
            RuntimeWarning, stacklevel=3)
        return
    warnings.warn(
        "心·gate：这一轮不发——人级拦的（她这轮不想说：态「%s」，理由 %s）"
        % (state, reason), RuntimeWarning, stacklevel=3)


def gate(inp: engage.EngageInput, group: Optional[str] = None, root: Any = None,
         now: Optional[str] = None) -> Dict[str, Any]:
    """出站闸：这一次到底发不发。**每次都落一行 gate_log，发也写、不发也写。**

    顺序：engage 的三态（人级）→ 群滑窗（群级），任一不过就不发。
    返回 {"send", "state", "reason", "hits", "level", "limits", "quota", "window_min"}。
    """
    group_name = _group(group)
    _ensure_config(root)
    payload, at, occupied, window = _judge(inp, group_name, root, now)
    if occupied is not None:
        try:
            store.write_json(_window_path(root), window)
        except Exception as exc:  # 账写失败只 WARN：这一轮已经判完了，别把结论炸掉
            warnings.warn(
                "心·gate：群滑窗账写失败（%s：%s），这一格没占上——下一次还会被当成没发过"
                % (type(exc).__name__, exc), RuntimeWarning, stacklevel=2)
    row = {
        "at": at,
        "who": getattr(inp, "who", None),
        "group": group_name,
        "send": payload["send"],
        "level": payload["level"],
        "state": payload["state"],
        "reason": payload["reason"],
        "hits": list(payload["hits"]),
        "quota_used": payload["quota"]["used"],
    }
    try:
        store.append_jsonl(_log_path(root), row)
    except Exception as exc:  # 日志是旁证，不是判决的前提
        warnings.warn(
            "心·gate：出站账写失败（%s：%s），这一判不发响但结论不受影响"
            % (type(exc).__name__, exc), RuntimeWarning, stacklevel=2)
    if not payload["send"]:
        _warn_blocked(payload["level"], payload["state"], payload["reason"],
                      group_name, payload["quota"], payload["window_min"])
    return payload


def peek(inp: engage.EngageInput, group: Optional[str] = None, root: Any = None,
         now: Optional[str] = None) -> Dict[str, Any]:
    """**同样的判断，但一个字节都不写、一格额度都不占**（只读端点就靠它）。

    「如果现在真发，门会怎么判」——这是同一件事，只是不落账：账是发生了才写的，
    查询不是发生。所以 gate_log.jsonl 与 group_window.json 在它前后逐字节相同。

    quota.used 报的是**这一判真发之后**的用量（与 gate 的返回逐字相同，好让调用方
    拿同一个数对账），但它只是算出来的读数：那一格并没有真占，下一次 gate 还是从这里
    的数起算。
    """
    payload, _at_value, _occupied, _window = _judge(inp, _group(group), root, now)
    return payload


def overview(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """只读总览：config + 每个群此刻窗口内的用量（不落盘、不剪文件）。"""
    cfg = _read_config(root)
    at = _at(now, None)
    window = _window_usage(root, at, cfg)
    limit = int(cfg["group_quota_per_hour"])
    groups = dict(
        (group, {"used": len(rows), "limit": limit, "left": max(0, limit - len(rows))})
        for group, rows in window.items())
    return {"at": at, "config": cfg, "window_min": cfg["group_window_min"],
            "limit": limit, "groups": groups}
