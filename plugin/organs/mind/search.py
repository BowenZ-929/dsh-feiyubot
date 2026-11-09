"""检索判据层：六道门在这里裁决「这一轮要不要查、查什么」。只做判断——
不联网、不落盘、不认聊天上下文，也不替谁说话。

六条边界，都是「为什么」：

一、**判据住在她的仓库里，不在代码里**。词表在 `家·仓库/判据/search_rules.json`，
   人写人改，本模块只以读方式打开。代码里一个默认词表都不留：留一份就是第二处真源，
   而这份真源一旦分叉，分叉的后果是把私聊送出去。

二、**缺文件＝检索关闭**（fail-closed）。没有那份文件就是「人还没开这个口子」，
   不是「用默认值悄悄跑起来」。坏文件（非 JSON／结构不对）同样关闭，只是 reason
   说得出坏在哪一步，且**不抛到聊天里**——聊天里抛异常等于让一次配置失误变成一次事故。

三、**先硬否决，再谈能不能查**（门 0→5 顺序固定）。门 0 最硬：命中就不查，
   **不许脱敏改写后再查**——改写本身就是把私人材料送到出去的路径上。
   号码、身份证与邮箱的形状长在代码里（`_PRIVATE_SHAPES`）：什么算手机号与判据
   怎么写无关，且这条挡住的是最不该被一次误编辑关掉的东西。它是 `bounds.no-user-info-leak`
   的**出口副本**——魂那条管的是话说出去之后，门 0 管的是**送出去的 query**，送出去
   就收不回来，所以执行前得有一份能在这一步挡的副本；照 `mouth/data/claim.json`
   「魂定判据、执行器官留自己的出口副本」的先例，副本可以存在，但必须与魂那条同语义
   （手机 11 位带前后界、身份证 15／17+X；邮箱比魂多一条，方向只多不少），
   `tests/test_search_policy.py` 的对拍用例看住「只许更严、不许更松」。
   真名／住址／私聊片段这些**词**不在这里：它们是判据文件里的名单（人改、程序只读）。

四、**状态只驻进程内存**：复用窗口、失败抑制、每轮计数都是「这一次进程记得」，
   不落任何新文件。落文件就等于给程序开了一条写判据目录的通道，§12 的三条护栏就白写了。

五、**reason 与 refuse_say 都不许含被拒原文**。门 0 的账只记「哪道门、裁决、为什么」，
   不记被拒的那句——落进账的原文比不落盘还难收回。

六、**`gate` 是决策落点门，不是唯一理由**：门 5 放行的裁决也报 `throttle`，
   同一个 `privacy` 可能来自号码形状、也可能来自词表命中。要看「为什么」读
   `reason`；要看「在哪道门落下的」读 `gate`。两者混着读，账就会开始骗人。

判据文件的结构（人写；缺任一键＝结构不对＝关闭，不补默认值）：

    {
      "revision": 1,                  // 人改一次进一位；可追溯性靠它，不靠 git
      "updated": "2026-11-09",        // 人改的日期
      "enabled": true,                // 总开关：false 就是「这一阵别查」
      "ask_markers": [...],           // 门1：明确要答案的说法（帮我查／到底／几号）
      "challenge_markers": [...],     // 门1 例外：指认她答错了（「你这搜索不行」）
      "private_markers": [...],       // 门0：真名／住址／私聊片段（号码形状在代码里）
      "hidden_markers": [...],        // 门3：局内实时、此刻位置、别人此刻的想法
      "volatile_markers": [...],      // 门4：会变的量（赛程／价格／版本／在售／排行榜）
      "doubt_markers": [...],         // 门4：不确定信号（确定吗／核实），门4 的放行口
      "per_turn_limit": 1,            // 门5：每轮上限
      "reuse_window_seconds": 300,    // 门5：复用窗口（5 分钟是**最长**窗口，不是可靠性证明）
      "fail_suppress_seconds": 120,   // 门5：失败抑制窗（限会话＋查询＋时间窗，不永久拉黑）
      "query_max_chars": 128          // 门5：query 长度上限
    }

门 4 只有「会变」与「不确定」两张名单、没有「不会变」那张：不需要列举的东西就别列举，
否定的判据（没认出来就是不变）比列举的判据稳。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Optional, Tuple

from ..home import residents
from ..home import store as home_store

#: 判据目录与文件名：位置是 §12 已经拍定的一条，不从这里派生第二份。
RULES_ROOM = "判据"
RULES_NAME = "search_rules.json"

#: `bounds.no-user-info-leak` 的出口副本，见模块 docstring 第三、六条。与魂那条同语义：
#: 手机 11 位带前后界、身份证 15 位与 17 位+X（一条不落）、邮箱多一条。
#: **不拿「五位数字」这种宽度兜**——价格 12999、番号 12345、`128726 次观看` 都会被误伤，
#: 后果是她连价格都不查了，而账上只剩一个 privacy，看不出是被形状挡的还是被人挡的。
_PRIVATE_SHAPES = (
    re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])"),
    re.compile(r"(?<![0-9])[0-9]{15}(?![0-9])"),
    re.compile(r"(?<![0-9])[0-9]{17}[0-9Xx](?![0-9])"),
    re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"),
)

#: query 首尾要剥掉的东西：语气、标点、空白。剥的是「怎么说」，不是「问什么」。
_TRIM = " \t\r\n？?。·.！!～~，,、：:；;\"'“”‘’（）()"

#: 进程内状态。三个桶：查到过（可复用）、失败过（抑制）、这一轮查过几次（封顶）。
_STATE: Dict[str, Dict[Any, Any]] = {"ok": {}, "fail": {}, "turns": {}}

#: 轮次计数只用来封顶，不记历史：给个上限，别让长跑的进程慢慢吃内存。
_TURNS_MAX = 512


def _is_flag(value: Any) -> bool:
    return isinstance(value, bool)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_window(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_words(value: Any) -> bool:
    # 空名单是合法的（「这一道门我先不开」），名单里出现空串不是合法的（那会命中一切）。
    return isinstance(value, (list, tuple)) and all(_is_text(item) for item in value)


_SHAPE = (
    ("revision", _is_count),
    ("updated", _is_text),
    ("enabled", _is_flag),
    ("ask_markers", _is_words),
    ("challenge_markers", _is_words),
    ("private_markers", _is_words),
    ("hidden_markers", _is_words),
    ("volatile_markers", _is_words),
    ("doubt_markers", _is_words),
    ("per_turn_limit", _is_count),
    ("reuse_window_seconds", _is_window),
    ("fail_suppress_seconds", _is_window),
    ("query_max_chars", _is_count),
)


def rules_path(root=None):
    """判据文件在哪：一律经 `store.home_root()` 推，本模块不认第二条路。

    家的根解析不出来时返回 None 而不是抛：解析不出根＝没有判据＝检索关闭，
    这与「文件缺失」是同一件事，不该让调用方分两种情形处理。
    """
    try:
        return home_store.home_root(root) / residents.WAREHOUSE_ROOM / RULES_ROOM / RULES_NAME
    except home_store.StoreError:
        return None


def _shape_error(data: Any) -> str:
    """结构不对就说哪一步不对（给人改文件用）；空串＝结构没问题。"""
    if not isinstance(data, dict):
        return "顶层不是对象"
    for key, check in _SHAPE:
        if key not in data:
            return "缺 %s" % key
        if not check(data[key]):
            return "%s 形状不对" % key
    return ""


def _read(root=None) -> Tuple[Optional[Dict[str, Any]], str]:
    """读一次判据：→ (判据 或 None, 读不成的原因)。**只读打开，绝不写、绝不建目录。**"""
    path = rules_path(root)
    if path is None:
        return None, "家的根解析不出来"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "判据文件还没落"
    except OSError:
        return None, "判据文件读不出来"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "判据文件不是 JSON"
    trouble = _shape_error(data)
    if trouble:
        return None, "判据结构不对（%s）" % trouble
    return data, ""


def load(root=None):
    """读判据配置：不存在／坏／结构不对 → None（一律当关闭，不抛）。"""
    rules, _ = _read(root)
    return rules


def enabled(root=None) -> bool:
    """这一阵开不开检索：文件在、能读、结构对、开关是 true，四条齐了才算开。"""
    rules = load(root)
    return bool(rules and rules["enabled"])


def _now() -> float:
    """单调用单调钟：墙上时钟回拨不该让复用窗口忽然变长或变短。"""
    return time.monotonic()


def _flat(text: Any) -> str:
    return " ".join(str(text or "").split())


def _hit(hay: str, words) -> bool:
    # hay 已经小写；名单也按小写比——人写「QQ号」而这里只认「qq号」，
    # 就是一条永远不生效的判据，而且它不报错，只在需要挡的时候悄悄放行。
    return any(word.lower() in hay for word in words)


def _private(hay: str, words) -> bool:
    return _hit(hay, words) or any(shape.search(hay) for shape in _PRIVATE_SHAPES)


def _query(text: Any, rules) -> str:
    """从本轮原话里提炼 query：剥掉开头的求助说法与首尾语气，再按上限截断。

    只碰本轮这一句：历史聊天、人设、记忆正文一个都不进来——门 0 再密也挡不住
    「把私聊改写进去」这条路，唯一的挡法是这些材料根本不出现在这个函数手里。
    """
    said = _flat(text)
    for marker in rules["ask_markers"]:
        if said.startswith(marker):
            said = said[len(marker):].lstrip(_TRIM)
            break
    said = said.strip(_TRIM)
    limit = rules["query_max_chars"]
    return said[:limit]


def _result(action: str, gate: str, query: Optional[str], reason: str,
            refuse_say: Optional[str] = None) -> Dict[str, Any]:
    return {"action": action, "gate": gate, "query": query, "reason": reason,
            "refuse_say": refuse_say}


def _prune(now: float, rules) -> None:
    keep = max(rules["reuse_window_seconds"], rules["fail_suppress_seconds"])
    for bucket in ("ok", "fail"):
        for key, at in list(_STATE[bucket].items()):
            if now - at > keep:
                del _STATE[bucket][key]


def note_outcome(*, session_id, query, ok) -> None:
    """把上一次的结果告诉门 5：查到了（窗口内可复用）还是失败（抑制窗内不重试）。

    必须有这一步：没有结果，「刚查过、去读那张卡」与「刚失败、别再来一次」
    在内存里长得一模一样，可它们对调用方是相反的指令。
    """
    if not query:
        return
    key = (session_id, query)
    if ok:
        _STATE["ok"][key] = _now()
        _STATE["fail"].pop(key, None)
    else:
        _STATE["fail"][key] = _now()
        _STATE["ok"].pop(key, None)


def reset_state() -> None:
    """清空进程内状态：进程重启与测试隔离用；这是内存，不是判据。"""
    for bucket in _STATE.values():
        bucket.clear()


def decide(*, text, who, scene, session_id, memory_hit=False, root=None, rules=None,
           turn_id=None, question=None, freshness=None) -> Dict[str, Any]:
    """六道门，顺序固定，先硬否决。**只判，不做**：返回值是这一轮的裁决，不是动作。

    `who` / `scene` 现在没有一道门读它们——契约里留着位子，是因为「谁在问、
    什么场合」迟早要进判据；现在没有依据就先不假造判据。`turn_id` 缺省时门 5
    只能靠复用窗口兜住重复请求（同一 query 不会发两次），封顶得等调用方报出轮次。

    行动作是门 5 的通过，所以 `action="search"` 的 gate 报 `"throttle"`：
    裁决总落在让它落地的那道门上，账里才不会出现「通过了哪道门」这种没有的字。
    """
    if rules is None:
        rules, why = _read(root)
        if rules is None:
            return _result("off", "off", None, "检索关闭：%s" % why)
    else:
        trouble = _shape_error(rules)
        if trouble:
            return _result("off", "off", None, "检索关闭：判据结构不对（%s）" % trouble)
    if not rules["enabled"]:
        return _result("off", "off", None, "检索关闭：开关关着")

    hay = _flat(question if question is not None else text).lower()
    challenged = _hit(_flat(text).lower(), rules["challenge_markers"])

    # 门0 隐私：硬否决，后面几道门没有投票权。
    if _private(hay, rules["private_markers"]):
        return _result("deny", "privacy", None, "问题里带着私人材料，不往外发",
                       "这个我不往外问。")

    # 门1 求助：她要的是答案才查；纯感叹吐槽自问自答，她有拒绝权。被指认答错是例外，
    # 但例外也只到这道门为止——门0 已经过去了，它管不着。
    if question is None and not (_hit(hay, rules["ask_markers"]) or challenged):
        return _result("deny", "ask", None, "不像是在要答案，不查")

    # 门2 记忆：私人事实记忆永远优先，网上查到也不推翻它，所以连查都不必。
    if memory_hit:
        return _result("deny", "memory", None, "这轮召回到了记忆，先按记忆答")

    # 门3 可查性：局内实时、此刻位置、别人此刻的想法——查不到，也不许编。
    # 这句话与「不知道」是两回事：通路问题不是知识问题。
    if _hit(hay, rules["hidden_markers"]):
        return _result("deny", "verifiable", None, "局内实时／此刻的位置与心思，看不到",
                       "这个我这儿看不到。")

    # 门4 新鲜度：会变的量必查；不会变的量靠权重答，但一旦她自己也拿不准就仍要查。
    if not (question is not None or freshness == "volatile" or challenged
            or _hit(hay, rules["volatile_markers"]) or _hit(hay, rules["doubt_markers"])):
        return _result("deny", "freshness", None, "没认出是会变的量，按权重答")

    # 门5 节流：失败优先于复用（一次失败之后不该再被算成「刚查过」）。
    query = _flat(question).strip(_TRIM) if question is not None else _query(text, rules)
    if len(query) > min(128, rules["query_max_chars"]):
        return _result("deny", "query", None, "公共问题过长，需要缩小范围", "需要先缩小查询范围。")
    if not query:
        return _result("deny", "ask", None, "问得太空，提不出可查的词")

    now = _now()
    _prune(now, rules)
    key = (session_id, query)

    failed_at = _STATE["fail"].get(key)
    if failed_at is not None and now - failed_at <= rules["fail_suppress_seconds"]:
        return _result("deny", "throttle", None, "同一问题刚失败过，抑制窗内不重试")

    reused_at = _STATE["ok"].get(key)
    if not challenged and reused_at is not None and now - reused_at <= min(300, rules["reuse_window_seconds"]):
        return _result("reuse", "throttle", query, "刚查过，去读已经落下的那张卡")

    if turn_id is not None:
        slot = (session_id, turn_id)
        if _STATE["turns"].get(slot, 0) >= min(1, rules["per_turn_limit"]):
            return _result("deny", "throttle", None, "这一轮已经查过一次了")
        _STATE["turns"][slot] = _STATE["turns"].get(slot, 0) + 1
        while len(_STATE["turns"]) > _TURNS_MAX:
            _STATE["turns"].pop(next(iter(_STATE["turns"])))

    return _result("search", "throttle", query, "该查，且没被前面几道门挡下")
