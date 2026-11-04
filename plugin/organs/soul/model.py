"""魂的六件：数据结构、校验规则与字数预算。

正文只有六件：名字（含自称）、形象、三句自述、怕什么、待人姿态、红线条目，合计 ≤1000 字
（红线可占大头）。六件里形象与怕什么都是硬线：用户各指定 ≤60 字，超了拒载；其余四件的数值
（40／60／100／250）只是建议值：超了报 WARN、不拦——plan §2「数值一律是默认值，
不是验收线」。六轴卡不在这里——它是心的性格参数（docs/plan.md §2「魂是底色，心是
性格与情绪」，卡片已移交 organs/heart/）。

坏卡一律拒载：校验只报 Issue，绝不给缺失字段补默认值——补默认值等于
给同一块多加一个写者。六件之外的字段（来历、她写的自述、话术、语料）
一律按坏卡拒收：它们归家·抽屉、脑、嘴，不归魂。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

ERROR = "error"
WARN = "warn"

BLOCK_NAMES = ("identity", "manner", "bounds")
# 红线的执行方：这条线由谁拦住。缺了就是没人接线，报 warn 让人看得见。
# soul·字面 = 魂内字面/正则能判；mouth·语义复核 = 要嘴按语义复核；home·仓库词表 = 词表在家。
ENFORCERS = ("soul·字面", "mouth·语义复核", "home·仓库词表")

# 每块只许这些顶层键：多一个就是六件之外的东西混进来了
ALLOWED_KEYS = {
    "identity": ("self_names", "appearance", "self_statements", "fears"),
    "manner": ("stance",),
    "bounds": ("items",),
}

_SCRIPT_QUOTE = re.compile(r'"[^"]*"|“[^”]*”')
_SCRIPT_PHRASES = ("就说", "回答", "必须说", "要回")
# 引号里的短词是强调（别把"不熟"演成"不理人"）；够长或带标点才像一句台词
_SCRIPT_QUOTE_MIN = 10
_SCRIPT_PUNCT = "，。、；！？"
_MATCH_TYPES = ("literal", "regex", "external", "review")
# 这两种不靠魂内字面/正则判定：词表在别处、语义要模型复核，所以 match() 一律不命中
UNENFORCED_MATCH_TYPES = ("external", "review")
_ACTIONS = ("block",)

# 提案 target → 块内 path。只有一处：待人姿态。
# 红线永久冻结，不可提案；六轴卡已不是魂的卡，写 baseline.* 一律按未知 target 拒。
_PROPOSAL_MANNER = "manner.stance"


@dataclass(frozen=True)
class Limits:
    # 单件里 appearance 与 fears 是硬线（用户各指定 ≤60 字，超了拒载）；其余四个是建议值，
    # 超了只 WARN。body_total 是合计硬线。
    self_names: int = 40
    appearance: int = 60
    self_statements: int = 60
    fears: int = 60
    statement_count: int = 3
    manner: int = 100
    bounds: int = 250
    body_total: int = 1000
    proposal_evidence: int = 3
    proposal_monthly_limit: int = 2


limits = Limits()
PROPOSAL_MONTHLY_LIMIT = limits.proposal_monthly_limit


@dataclass(frozen=True)
class Issue:
    code: str
    level: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        """给要跨进程/跨语言边界（HTTP 层、CLI）一个 JSON 安全的形状。"""
        return {"code": self.code, "level": self.level, "message": self.message}


class SoulLoadError(Exception):
    """载入失败；issues 里至少有 error 级条目。"""

    def __init__(self, issues: Sequence[Issue]) -> None:
        self.issues = list(issues)
        super().__init__("；".join("%s %s" % (item.code, item.message) for item in self.issues))


@dataclass(frozen=True)
class Identity:
    display: str
    synonyms: Tuple[str, ...]
    # 只写她看起来是什么（发色、瞳色、体型、尾巴、看着多大）；画风、生成参数、模型、
    # 服装细节归家·仓库的素材卡，不进魂。
    appearance: str
    self_statements: Tuple[str, ...]
    # 她怕什么：一句，60 字硬线。心里很怕、几乎不露（六轴 inner 78／shown 15），
    # 所以这里写的是她怎么绕着怕走，不是把"我怕"说出口。
    fears: str


@dataclass(frozen=True)
class Manner:
    stance: str          # 一句姿态；对象矩阵归脑（熟人/生人=人名录副本、群档=场合判断）


@dataclass(frozen=True)
class BoundsItem:
    id: str
    rule: str
    match_type: str
    pattern: str
    action: str
    active: bool
    source: str = ""      # 只有 external 用：词表在谁那儿（例如 家·仓库/脏话词表）
    enforced_by: str = ""  # 谁负责拦住这条线（ENFORCERS 之一）；空 = 没人接线，报 warn


@dataclass(frozen=True)
class Bounds:
    items: Tuple[BoundsItem, ...]


def char_count(text: str) -> int:
    """字数 = 字符数，中文一个字算 1。"""
    return len(text)


def identity_name_chars(identity: Identity) -> int:
    return char_count(identity.display) + sum(char_count(item) for item in identity.synonyms)


def appearance_chars(identity: Identity) -> int:
    return char_count(identity.appearance)


def identity_statement_chars(identity: Identity) -> int:
    return sum(char_count(item) for item in identity.self_statements)


def fears_chars(identity: Identity) -> int:
    return char_count(identity.fears)


def manner_chars(manner: Manner) -> int:
    """待人姿态 = 那一句的字。矩阵已归脑，魂里不再有分档的字。"""
    return char_count(manner.stance)


def bounds_chars(bounds: Bounds) -> int:
    """红线条目正文按条算；撤下的线（active=false）仍占预算——它还在魂里，不许靠撤线省字数。"""
    return sum(char_count(item.rule) for item in bounds.items)


def body_parts(identity: Identity, manner: Manner, bounds: Bounds
               ) -> List[Tuple[str, int, int, bool]]:
    """六件的（名字, 字数, 上限, 是不是硬线）：check 摘要、CLI、面板都读这一份。

    形象与怕什么都是硬线（用户各指定 ≤60 字，超了报 error 拒载）；其余四个的第三个数字是
    建议值，不是验收线。硬线还有 body_total，那是合计。顺序即渲染顺序。
    """
    return [
        ("名字与自称", identity_name_chars(identity), limits.self_names, False),
        ("形象", appearance_chars(identity), limits.appearance, True),
        ("三句自述", identity_statement_chars(identity), limits.self_statements, False),
        ("怕什么", fears_chars(identity), limits.fears, True),
        ("待人姿态", manner_chars(manner), limits.manner, False),
        ("红线条目", bounds_chars(bounds), limits.bounds, False),
    ]


def body_chars(identity: Identity, manner: Manner, bounds: Bounds) -> int:
    return sum(chars for _, chars, _, _ in body_parts(identity, manner, bounds))


def proposal_target(target: Any) -> Optional[Tuple[str, str]]:
    """提案 target → (block, 块内 path)；不在白名单内返回 None。

    白名单只有 manner.stance 一项：红线永久冻结、不可提案；
    六轴卡归心，baseline.* 不是魂的 target，一律当未知 target 拒。
    """
    if target == _PROPOSAL_MANNER:
        return ("manner", "stance")
    return None


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bad(message: str) -> Issue:
    return Issue("SOUL_JSON_UNREADABLE", ERROR, message)


def _unknown_keys(block: str, data: Dict[str, Any], issues: List[Issue]) -> None:
    extra = [str(key) for key in data if key not in ALLOWED_KEYS[block]]
    if extra:
        issues.append(_bad("%s 只许有 %s；多出 %s（六件之外的不进魂：来历归家·抽屉，"
                           "她写的自述归脑，具体话术归嘴）"
                           % (block, "/".join(ALLOWED_KEYS[block]), "/".join(extra))))


def _shape_identity(data: Dict[str, Any], issues: List[Issue]) -> Optional[Identity]:
    ok = True
    names = data.get("self_names")
    if not isinstance(names, dict):
        issues.append(_bad("identity.self_names 必须是对象"))
        return None
    display = names.get("display")
    synonyms = names.get("synonyms")
    if not _is_text(display):
        issues.append(_bad("identity.self_names.display 必须是非空字符串"))
        ok = False
    if not (isinstance(synonyms, list) and all(_is_text(item) for item in synonyms)):
        issues.append(_bad("identity.self_names.synonyms 必须是非空字符串数组"))
        ok = False
    appearance = data.get("appearance")
    if not _is_text(appearance):
        issues.append(Issue("IDENTITY_APPEARANCE_MISSING", ERROR,
                            "identity.appearance 必须是非空字符串：只写她看起来是什么"
                            "（发色、瞳色、体型、有没有尾巴、看着多大）"))
        ok = False
    elif char_count(appearance) > limits.appearance:
        issues.append(Issue("IDENTITY_APPEARANCE_TOO_LONG", ERROR,
                            "identity.appearance %d 字 > %d 字硬线（超了拒载，不截断）："
                            "只写她看起来是什么"
                            % (char_count(appearance), limits.appearance)))
        ok = False
    fears = data.get("fears")
    if not _is_text(fears):
        issues.append(Issue("IDENTITY_FEARS_MISSING", ERROR,
                            "identity.fears 必须是非空字符串：一句她怕什么"
                            "（心里很怕、几乎不露，所以写她怎么绕着怕走）"))
        ok = False
    elif char_count(fears) > limits.fears:
        issues.append(Issue("IDENTITY_FEARS_TOO_LONG", ERROR,
                            "identity.fears %d 字 > %d 字硬线（超了拒载，不截断）：只写一句"
                            % (char_count(fears), limits.fears)))
        ok = False
    statements = data.get("self_statements")
    if not (isinstance(statements, list) and all(_is_text(item) for item in statements)):
        issues.append(_bad("identity.self_statements 必须是非空字符串数组"))
        ok = False
    elif len(statements) != limits.statement_count:
        issues.append(_bad("identity.self_statements 必须固定 %d 句（人写的），当前 %d 句"
                           % (limits.statement_count, len(statements))))
        ok = False
    if not ok:
        return None
    return Identity(display=display, synonyms=tuple(synonyms), appearance=appearance,
                    self_statements=tuple(statements), fears=fears)


def _shape_manner(data: Dict[str, Any], issues: List[Issue]) -> Optional[Manner]:
    stance = data.get("stance")
    if not _is_text(stance):
        issues.append(_bad("manner.stance 必须是非空字符串（一句姿态，不是禁令、不是分档矩阵）"))
        return None
    return Manner(stance=stance)


def _shape_bounds(data: Dict[str, Any], issues: List[Issue]) -> Optional[Bounds]:
    ok = True
    items = data.get("items")
    if not isinstance(items, list):
        issues.append(_bad("bounds.items 必须是数组"))
        return None
    parsed: List[BoundsItem] = []
    seen: List[str] = []
    duplicated: List[str] = []
    for index, item in enumerate(items):
        where = "bounds.items[%d]" % index
        if not isinstance(item, dict):
            issues.append(_bad(where + " 必须是对象"))
            ok = False
            continue
        item_id = item.get("id")
        if not _is_text(item_id):
            issues.append(_bad(where + ".id 必须是非空字符串"))
            ok = False
            continue
        if item_id in seen:
            duplicated.append(item_id)
        else:
            seen.append(item_id)
        item_ok = True
        rule = item.get("rule")
        if not _is_text(rule):
            issues.append(_bad(where + ".rule 必须是非空字符串"))
            item_ok = False
        match = item.get("match")
        match_type = pattern = source = None
        if isinstance(match, dict):
            match_type = match.get("type")
            pattern = match.get("pattern")
            source = match.get("source")
        if match_type not in _MATCH_TYPES:
            issues.append(_bad(where + ".match.type 只能 literal / regex / external / review"))
            item_ok = False
        if match_type == "external":
            if not _is_text(source):
                issues.append(_bad(where + ".match.source 必须是非空字符串：external 靠外挂词表判定"))
                item_ok = False
        elif match_type in ("literal", "regex"):
            if not _is_text(pattern):
                issues.append(_bad(where + ".match.pattern 必须是非空字符串"))
                item_ok = False
            elif match_type == "regex":
                try:
                    re.compile(pattern)
                except re.error as exc:
                    issues.append(_bad("%s.match.pattern 不是合法正则：%s" % (where, exc)))
                    item_ok = False
        enforced_by = item.get("enforced_by")
        if enforced_by is None:
            # 不拦：缺执行方的线照样生效（字面/复核规则不变），但没人接线会永久悬空，得看得见
            issues.append(Issue("BOUNDS_ENFORCER_MISSING", WARN,
                                "%s 没写 enforced_by：这条线没人接线，将来会永久悬空" % where))
            enforced_by = ""
        elif not _is_text(enforced_by) or enforced_by not in ENFORCERS:
            issues.append(_bad("%s.enforced_by 只能是 %s，实际 %r"
                               % (where, "/".join(ENFORCERS), enforced_by)))
            item_ok = False
        action = item.get("action")
        if action not in _ACTIONS:
            issues.append(_bad(where + ".action 只能是 block"))
            item_ok = False
        active = item.get("active", True)
        if not isinstance(active, bool):
            issues.append(_bad(where + ".active 必须是布尔"))
            item_ok = False
        if not item_ok:
            ok = False
            continue
        if match_type in UNENFORCED_MATCH_TYPES:
            pattern = ""     # 非字面条目没有魂内模式，免得把 match 里的别的东西当模式用
        else:
            source = ""
        parsed.append(BoundsItem(id=item_id, rule=rule, match_type=match_type,
                                 pattern=pattern, source=source or "",
                                 action=action, active=active,
                                 enforced_by=enforced_by or ""))
    if duplicated:
        issues.append(Issue("BOUNDS_ID_DUPLICATE", ERROR,
                            "红线 id 必须唯一，重复：" + "/".join(duplicated)))
        ok = False
    if not ok:
        return None
    return Bounds(items=tuple(parsed))


_SHAPERS = {
    "identity": _shape_identity,
    "manner": _shape_manner,
    "bounds": _shape_bounds,
}


def _is_script(quoted: str) -> bool:
    """引号里是强调词就放行，够长或带标点才当成一句具体台词。"""
    inner = quoted[1:-1]
    return len(inner) >= _SCRIPT_QUOTE_MIN or any(char in inner for char in _SCRIPT_PUNCT)


def _script_issues(manner: Manner, issues: List[Issue]) -> None:
    fields = [("manner.stance", manner.stance)]
    for where, text in fields:
        scripts = [span.group(0) for span in _SCRIPT_QUOTE.finditer(text) if _is_script(span.group(0))]
        if scripts:
            issues.append(Issue("MANNER_HAS_SCRIPT", ERROR,
                                "%s 里有引号包住的具体句子「%s」：话术归嘴，manner 只写待人倾向"
                                % (where, scripts[0])))
            continue
        for phrase in _SCRIPT_PHRASES:
            if phrase in text:
                issues.append(Issue("MANNER_HAS_SCRIPT", ERROR,
                                    "%s 里出现祈使话术「%s」：话术归嘴，manner 只写待人倾向"
                                    % (where, phrase)))
                break


def _budget_issues(block: str, parsed: Any, issues: List[Issue]) -> None:
    """单件建议值：超了报 WARN，不拦（plan §2 数值一律是默认值）。

    合计 1000 的硬闸要四块齐了才能算，由 store.read_blocks 补。
    """
    if block == "identity":
        parts = [("identity.self_names", identity_name_chars(parsed), limits.self_names),
                 ("identity.self_statements", identity_statement_chars(parsed), limits.self_statements)]
    elif block == "manner":
        parts = [("manner.stance（一句姿态）", manner_chars(parsed), limits.manner)]
    elif block == "bounds":
        parts = [("bounds.rule（各条）", bounds_chars(parsed), limits.bounds)]
    else:
        return
    for where, chars, limit in parts:
        if chars > limit:
            issues.append(Issue("SUGGESTED_BUDGET_EXCEEDED", WARN,
                                "%s %d 字 > 建议 %d 字（建议值不是验收线，不拦）"
                                % (where, chars, limit)))


def validate(block: str, data: Any) -> List[Issue]:
    """校验一块卡；返回全部 Issue，其中 error 级即坏卡。"""
    if not isinstance(data, dict):
        return [_bad("%s 必须是 JSON 对象" % block)]
    issues: List[Issue] = []
    _unknown_keys(block, data, issues)
    parsed = _SHAPERS[block](data, issues)
    if parsed is None:
        return issues
    if block == "manner":
        _script_issues(parsed, issues)
    _budget_issues(block, parsed, issues)
    return issues


def parse(block: str, data: Dict[str, Any]) -> Any:
    """把已校验通过的卡转成数据结构。"""
    return _SHAPERS[block](data, [])

