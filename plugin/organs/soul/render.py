"""魂的注入渲染：只有六件正文；数值不进文本（性格档位词归心，不在魂里）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

from . import model

if TYPE_CHECKING:
    from .store import Snapshot

def self_name(identity: model.Identity) -> str:
    if identity.synonyms:
        return "%s（%s）" % (identity.display, "、".join(identity.synonyms))
    return identity.display


def appearance_line(identity: model.Identity) -> str:
    """她看起来是什么：发色、瞳色、体型、尾巴、看着多大。

    画风、生成参数、模型、服装细节不在这里——那些归家·仓库的素材卡。
    """
    return identity.appearance


def stance_line(manner: model.Manner, audience: str = "") -> str:
    """一句姿态，不分档：熟人/生人归人名录、群档归场合判断，都不在魂里。

    audience 只为旧调用（CLI --audience、/soul/render?audience=）留着，一律不读。
    """
    return manner.stance


def self_statements(identity: model.Identity) -> str:
    """三句自述逐句分隔；本句已自带句末标点就不再加。"""
    parts = []
    for text in identity.self_statements:
        if not text:
            continue
        parts.append(text if text[-1] in "。！？" else text + "。")
    return "".join(parts)


def fears_line(identity: model.Identity) -> str:
    """她怕什么：一句。心里很怕、几乎不露，所以写的是她怎么绕着怕走，不是台词。"""
    return identity.fears


def redlines(bounds: model.Bounds) -> str:
    """只出 rule 正文：条目 id 是给 match_bounds 回报用的，不该进她的自我描述。"""
    return "；".join(item.rule for item in bounds.items if item.active)


def sections(snapshot: "Snapshot", audience: str = "") -> List[Tuple[str, str]]:
    """六件的（标题, 正文）：魂的注入就是这六行，没有第七段。

    第二行是她的样子，第三行是她的性格底色，第四行是她怕什么，都不是「她是谁」的档案：
    名字在上面一行，世界怎么看她（爱称、外号、熟稔档）都在脑·人名录，不在这里。
    """
    return [
        ("【自称】", self_name(snapshot.identity)),
        ("【形象】", appearance_line(snapshot.identity)),
        ("【性格底色】", self_statements(snapshot.identity)),
        ("【怕什么】", fears_line(snapshot.identity)),
        ("【待人姿态】", stance_line(snapshot.manner, audience)),
        ("【红线】", redlines(snapshot.bounds)),
    ]


def budget_used(snapshot: "Snapshot") -> int:
    """正文预算 = 六件的字，按源文本算（不数标题）。"""
    return model.body_chars(snapshot.identity, snapshot.manner, snapshot.bounds)


def render(snapshot: "Snapshot", audience: str = "") -> str:
    return "\n".join("%s%s" % (head, body) for head, body in sections(snapshot, audience))

