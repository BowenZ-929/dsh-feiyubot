"""嘴的称呼：人名录的熟稔读数 → 四档关系 → 从「称呼表」取一条叫法。

只产三样（docs/modules/mouth.md mouth-address 的原话）：**对谁 + 一个称呼 + 关系档**。
关系档多带一个整数（relation 1..4），因为语气档要拿它当输入；除此之外不多产一个字。

四档档界、每档称呼词、频次说明、彩蛋改口、违禁自称、回退口径全在
organs/mouth/data/address_table.json 里（经 warehouse.load 读入），本文件不写死任何一个数：
改档界＝改数据，不改代码。

热加载：不缓存任何人名录读数，每次调用都把传进来的 person 现算一遍（familiarity 由脑现算，
读的时候才算，见 organs/brain/people.py）。本文件连「表」都不留模块级副本——
resolve 拿到的 table 就是调用方这一回合读来的那一份。

纯：不 import time / datetime / random；不碰宿主；文件 IO 只经 warehouse。

**本人要求换个叫法，当场就换**（2026-11-04 总览裁决）：退到中性叫法等于没听人话。
person 里的 rename_request（非空字符串、去首尾空白）就是这一轮要用的称呼，不再退到中性。
但换过去的那一个仍要过「违禁自称」这道红线：它落在 self_guard.words 里、又不在魂
identity.self_names 授权名单里时，**照样退到中性叫法**——不许靠改名绕过自称红线——
并在 note 里说清是被哪一条拦下的。

会话级的临时覆盖（SessionAddressBook）是同一件事的第二步：记下来的叫法可以盖过人名录的档位，
但**只活这个会话、进程重启即丢**；它的真值不在这本薄账里，而在 rename_memory 产出的那条亲历
记忆——投给脑，下回合由人名录带回来（本文件不写盘、不要求脑加任何人名录字段）。

**「他要人叫他什么」由数据认，不由代码猜**：requested_name 只读 data/address_table.json 的
rename_markers（named 带捕获组＝新叫法，denied 不带＝只否掉旧叫法），认不出就是 None——它不查
人名录，也不拿昵称认人（认人只认 id）。谁把抽到的叫法记进会话账、谁把它投给脑，是出口那一侧的事：
本文件只提供 requested_name / SessionAddressBook / rename_memory 三件工具，一次都不自己接线。

红线（不许抛错中断出口）：挑出来的叫法若落在魂 identity.self_names 授权名单之外又被当自称用
（「违禁自称」），一律退到中性叫法，把被挡下的那个词记在 banned_self 里，并在 note 里说一句实话；
不抛错、不写回脑、不写回魂。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .model import PolicyMiss, need, section
from .warehouse import load

__all__ = ["Addressee", "SessionAddressBook", "load_table", "rename_memory", "requested_name",
           "resolve", "tier_of"]

RENAME_KEY = "rename_markers"    # 数据里「要求改称呼」那一节
NAMED_KEY = "named"              # 那一节里「带捕获组：抓到的就是新叫法」的正则
DENIED_KEY = "denied"            # 那一节里「不带捕获组：只否掉旧叫法」的正则


@dataclass(frozen=True)
class Addressee:
    """一次称呼的产出：对谁 + 一个称呼 + 关系档，外加两句实话。

    字段（第四批 api 照这个接，改字段等于改接口）：
      who          人名录里的 id；人名录取不到这个人时是空串
      display      人名录里的显示名；没有就是空串
      tier         关系档的名字（"生人"/"认识"/"熟人"/"挚友"），档界取自数据
      relation     关系档的整数 1..4，与 tier 一一对应，给语气档当输入
      address      这一次要用的称呼；退到中性叫法时就是数据里的回退词
      frequency    频次说明（例如「不是每句都带」），来自数据
      banned_self  被红线挡下的那个违禁自称；没挡就是空串
      note         一句实话：为什么退到中性叫法，或者彩蛋改口了没，或者这一轮当场换了称呼
      renamed      这一轮的称呼是不是本人当场要的那个（会话覆盖与人名录档位都算 false）
      source       这个称呼从哪儿来：「人名录」档位挑的 / 会话里的临时覆盖 / 这一轮本人要求的
                   改名 / 退到了回退词——四个词都取自 table.source，代码里不写死
    """

    who: str
    display: str
    tier: str
    relation: int
    address: str
    frequency: str
    banned_self: str = ""
    note: str = ""
    renamed: bool = False
    source: str = ""


class SessionAddressBook:
    """会话级的临时称呼覆盖：纯内存的一本薄账，没有第二处副本。

    三句话就是它的全部语义，别读多了：
    - **只活这个会话**：换个会话就没了；它不是名录、不是设置，也不是"主人改过的叫法"；
    - **进程重启即丢**：不落盘、不留缓存，重启后的新实例 get() 一律是空串；
    - **真值靠投递给脑的那条记忆**：要长久的叫法得靠 rename_memory 那条亲历记忆进脑，
      下回合由人名录带回来——不靠这本薄账。

    谁来记：调用方（出口那一侧）。resolve 只读不写——它连 clear 都不调。
    """

    def __init__(self) -> None:
        self._book: Dict[str, str] = {}

    def remember(self, who: str, address: str) -> None:
        """记下「这个人这几轮就叫这个」；who 或 address 是空白就不记。

        空叫法等于是「退到中性」，那件事归 resolve 管（它按数据里的回退词走），
        不该由这本账替她决定——所以这里宁可什么都不记，也不存一个空串当覆盖。
        """
        key = _text(who).strip()
        value = _text(address).strip()
        if not key or not value:
            return
        self._book[key] = value

    def get(self, who: str) -> str:
        """这个人在本会话里有没有被覆盖过：有就给那个叫法，没有就是空串（不是 None）。"""
        return self._book.get(_text(who).strip(), "")

    def forget(self, who: str) -> bool:
        """忘掉这个人的覆盖：真忘了返回 True，本来就没有返回 False（不是一个空的判断）。"""
        key = _text(who).strip()
        if key in self._book:
            del self._book[key]
            return True
        return False

    def clear(self) -> None:
        """把整本账清空（换会话、或主人说「都按名录来」时用）。"""
        self._book.clear()

    def snapshot(self) -> Dict[str, str]:
        """这一刻账上有哪些覆盖（一份副本，改它不影响账本）；重启后它就是空的。"""
        return dict(self._book)


def load_table(root: Optional[Any] = None) -> dict:
    """现读一份称呼表（warehouse.load 本身不缓存）：改数据，下一回合就生效。"""
    return load("address_table", root)


def tier_of(familiarity: float, table: dict) -> str:
    """熟稔读数落在哪一档：档界（tiers[].min_familiarity）取自 table，代码不写死。

    读数取不到（None、非数、负数）按 0.0 算——没读数就是没记录，不是「上一次的档位」。
    """
    tiers = _tiers(table)
    reading = _reading(familiarity)
    chosen = tiers[0]
    for entry in tiers:
        if reading >= _min_of(entry):
            chosen = entry
    return str(need(chosen, "tier"))


def resolve(person: Optional[dict], table: dict,
            allowed_self: Optional[Sequence[str]] = None,
            session: Optional[Any] = None,
            who: Optional[str] = None) -> Addressee:
    """把一个「人名录读数」落成一次称呼。

    person 是脑人名录的一份读数（id / display / interactions / familiarity / rename_request …，
    见 BrainStore._person_view）：person 为 None（人名录取不到人）时档位是数据里最低那一档，
    称呼取 table.fallback（默认「你」）——这是回退，不是「沿用上一次」。

    这个称呼从哪儿来，优先级写死为三条：**会话里的临时覆盖 > 这一轮的 rename_request > 人名录档位**。
    - session 是一本 SessionAddressBook（或任何有 get(who) 的东西），取到就用它，source=会话覆盖；
    - 没有覆盖而 person 里有 rename_request（非空、去首尾空白），**当场就换**，source=本轮改名，
      renamed=True——退到中性等于没听人话，所以这里不退；
    - 两者都没有，才按名录档位挑，source=人名录。

    换过来的那个词（不管来自覆盖还是改名）**照样要过 self_guard**：落在违禁自称词表里、又不在
    allowed_self 授权名单里，一律退到回退词、renamed=False，并在 note 里说清是哪条拦下的——
    改名不是绕过自称红线的后门。

    allowed_self 是魂 identity.self_names 的授权名单（可选）：给了才判得出「违禁自称」。
    不给就等于没人核对名单，表里凡是自称用字一律按未授权处理（宁可退到中性叫法）。

    纯：**本函数不写 session、不写盘**——落 book 是调用方的事（记住哪一声，是出口那边的决定）。
    """
    fallback = str(need(section(table, "fallback"), "address"))
    frequency = str(need(section(table, "frequency"), "value"))
    guard = section(table, "self_guard")
    labels = section(table, "source")
    holder = person if isinstance(person, Mapping) else None
    # 会话账的键：名录里查得到人就用名录的 id，查不到就用调用方给的那个 id。
    # 只从 person 里取的话，名录一失手（冷/影子/新人/脑那边 lookup 抖动）会话账就形同不存在，
    # 于是「他明明让我叫他老李」下一轮又退回「你」——那是真坏，不是回显问题。
    key = (_text(holder.get("id")) if holder else "") or _text(who)
    who = key
    display = _text(holder.get("display")) if holder else ""
    reading = _reading(holder.get("familiarity")) if holder else 0.0
    tier_name = tier_of(reading, table)
    entry = _entry(table, tier_name)
    relation = int(need(entry, "relation"))
    candidate, note = _candidate(holder, entry, table, tier_name, who, display)
    source = str(need(labels, "directory"))
    renamed = False
    override = _session_override(session, key)
    rename = _rename_request(holder)
    if override:
        candidate = override
        note = str(need(guard, "session_note"))
        source = str(need(labels, "session"))
    elif rename:
        candidate = rename
        note = str(need(guard, "rename_applied"))
        source = str(need(labels, "rename"))
        renamed = True
    banned = ""
    if candidate and _forbidden_self(candidate, table, allowed_self):
        banned = candidate
        candidate = ""
        note = str(need(guard, "rename_note" if (override or renamed) else "note"))
        renamed = False
    if not candidate:
        candidate = fallback
        source = str(need(labels, "fallback"))
    return Addressee(who=who, display=display, tier=tier_name, relation=relation,
                     address=candidate, frequency=frequency,
                     banned_self=banned, note=note, renamed=renamed, source=source)


def rename_memory(who: str, address: str, ts: Optional[str] = None,
                  table: Optional[dict] = None) -> Dict[str, Any]:
    """「他要求过叫他某个叫法」→ 一条脑能直接吃的亲历记忆草稿。

    产出的形状就是 BrainStore.ingest(**draft) 的入参：who / text / source_type / ts，
    四个键全是 ingest 本来就认的参数——**不要求脑为这件事加任何人名录字段**，一个字也不加。
    措辞在 data（self_guard.rename_memory_text），source_type 也在 data
    （self_guard.rename_memory_source_type = 亲历：这是他对她说过的一句原话，不是日程模拟）。

    ts 由调用方给（本文件不 import time / datetime，取不到时钟也不该取）；不给就是 None，
    由脑那边按它自己的时钟落。本函数不写盘——投递是调用方的事。
    """
    book = table if isinstance(table, Mapping) else load("address_table")
    guard = section(book, "self_guard")
    template = str(need(guard, "rename_memory_text"))
    return {"who": _text(who).strip(),
            "text": template.format(address=_text(address).strip()),
            "source_type": str(need(guard, "rename_memory_source_type")),
            "ts": ts}


def requested_name(text: Any, table: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """从对方这句话里抽出「他这一轮要的那个叫法」；抽不到就是 None（**不许编**）。

    判据一个字都不在本文件：data/address_table.json 的 rename_markers 一节里有两张正则表——
    named 每条带**一个捕获组**，抓到的就是他这一轮要的称呼（「以后叫我老李」→ 老李）；
    denied 不带捕获组，它只是否掉旧叫法、并不给出新叫法（「别再叫我老王」）。两表的先后即优先级，
    而被 denied 那一截盖住的那一处 named 不算数：同一句里后面另有一处 named 才算
    （「别再叫我老王，叫我老李」→ 老李）。

    抽不到就回 None，三种情形一样对待：这话不像要求改称呼、只否掉旧叫法却没说新的、
    正则抓回来的那一截是空的。**不许**拿人名录里的名字、档位里的称呼词或上一次的叫法顶上——
    认不出他说的是哪个叫法，就照旧按档位叫（这是「没听清」，不是「听清了换一个」）。

    认人也不归它管：**只认 id**（谁在说话由人名录的 person.id / 请求里的 who 决定），
    这里只回答「他要人叫他什么」，不回答「他是谁」——昵称不是身份，同一句里出现三个名字
    也不代表来了三个人（那句话写在数据的 rename_markers.id_note 里）。

    table 不给就现读数据；缺节、缺键、词表里混进非字符串、正则编译不过，一律抛 PolicyMiss
    （缺键不补默认值，坏正则不许静默当成「没命中」）。
    """
    book = table if isinstance(table, Mapping) else load("address_table")
    cfg = section(book, RENAME_KEY)
    named = _regexes(cfg, NAMED_KEY, groups=True)
    denied = _regexes(cfg, DENIED_KEY, groups=False)
    body = _text(text).strip()
    if not body:
        return None
    covered = [found.span() for pattern in denied for found in _finditer(pattern, body)]
    for pattern in named:
        for found in _finditer(pattern, body):
            if any(start <= found.start() < end for start, end in covered):
                continue                      # 这一处被「别叫我…」否掉了，不当成他要的新叫法
            name = _text(found.group(1)).strip() if found.groups() else ""
            if name:
                return name
    return None


def _regexes(cfg: Mapping[str, Any], name: str, groups: bool) -> List[str]:
    """取一张正则表：必须是清单、每项非空字符串；named 那表还要求正好一个捕获组。

    缺键、非清单、混进非字符串、该带捕获组的没带（或带了不止一个），都抛 PolicyMiss 并指名。
    空清单照收：清空 named 就是「一句话都不认成要求改称呼」，那是人改数据的选择，不是坏件。
    """
    raw = need(cfg, name)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PolicyMiss("键 %r（读到的不是一份清单）" % (name,))
    out: List[str] = []
    for item in raw:
        pattern = _text(item).strip()
        if not pattern:
            raise PolicyMiss("表 %r 里有一项不是非空字符串（读到的 %r）" % (name, item))
        if groups:
            found = _compile(pattern)
            if found.groups != 1:
                raise PolicyMiss("表 %r 里的 %r 该带正好一个捕获组（现在是 %d 个）——抓不到就不知道"
                                 "他要哪个叫法" % (name, pattern, found.groups))
        out.append(pattern)
    return out


def _compile(pattern: str) -> "re.Pattern[str]":
    """编译一条正则；编译不过抛 PolicyMiss 并指名——坏的正则不许静默当成「没命中」。"""
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise PolicyMiss("正则 %r 编译不过：%s" % (pattern, exc)) from None


def _finditer(pattern: str, body: str) -> List[Any]:
    """一条正则在这段话里的全部命中（按位置先后）；编译不过照 _compile 的规矩抛。"""
    return list(_compile(pattern).finditer(body))


def _tiers(table: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """称呼表的四档，按档界从低到高排好；缺档、缺界就抛 PolicyMiss（数据坏了要人修）。"""
    rows = table.get("tiers") if isinstance(table, Mapping) else None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        raise PolicyMiss("节 'tiers'（称呼表的四档）")
    entries: List[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise PolicyMiss("节 'tiers' 里的一项（读到的不是键值表）")
        for key in ("tier", "relation", "min_familiarity"):
            need(row, key)
        entries.append(row)
    return sorted(entries, key=_min_of)


def _min_of(entry: Mapping[str, Any]) -> float:
    """这一档的档界取成 float；档界本身读不出数值也是数据坏了。"""
    value = need(entry, "min_familiarity")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyMiss("档界 min_familiarity（%r 不是数值）" % (value,))
    return float(value)


def _entry(table: Mapping[str, Any], tier_name: str) -> Mapping[str, Any]:
    """按档名取那一档；表里没有这一档就是数据坏了。"""
    for row in _tiers(table):
        if str(need(row, "tier")) == tier_name:
            return row
    raise PolicyMiss("档 %r（称呼表里没有这一档）" % (tier_name,))


def _candidate(holder: Optional[Mapping[str, Any]], entry: Mapping[str, Any],
               table: Mapping[str, Any], tier_name: str,
               who: str, display: str) -> Any:
    """这一次的候选称呼，以及一句关于它的实话。

    优先用人名（这一档允许叫人名、且人名录里有人名）；否则从这一档的称呼词里挑一条。
    然后看彩蛋改口：进了那一档、且累计互动够数，就换成彩蛋里的那个词。
    """
    note = ""
    candidate = ""
    if display and bool(entry.get("use_display", False)):
        candidate = display
    words = _words(entry.get("addresses"))
    if not candidate and words:
        candidate = words[_pick(who or display or tier_name, len(words))]
    egg = table.get("easter_egg")
    if isinstance(egg, Mapping) and _text(egg.get("tier")) == tier_name:
        if _total_interactions(holder) >= int(need(egg, "min_interactions")):
            candidate = str(need(egg, "address"))
            note = _text(egg.get("note"))
    return candidate, note


def _words(raw: Any) -> List[str]:
    """一档里的称呼词：只认非空的字符串，别的当没有（不许把 None 拼进回复）。"""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [str(item).strip() for item in raw if _text(item).strip()]


def _pick(seed: str, count: int) -> int:
    """稳定地挑一条：同一个人的同一档每次挑到同一个词，不掷骰子（本文件不 import random）。"""
    if count <= 1:
        return 0
    return sum(ord(char) for char in seed) % count


def _session_override(session: Optional[Any], who: str) -> str:
    """会话里的临时覆盖：只认「有 get(who) 的东西」，取到的非空字符串才算覆盖。

    只读一眼——resolve 不许往 session 里写（记住哪一声是调用方的决定）。
    """
    if session is None or not who:
        return ""
    getter = getattr(session, "get", None)
    if not callable(getter):
        return ""
    return _text(getter(who)).strip()


def _rename_request(holder: Optional[Mapping[str, Any]]) -> str:
    """person 里这一轮本人要求的叫法：非空字符串、去首尾空白；别的类型一律当没要求。"""
    if holder is None:
        return ""
    return _text(holder.get("rename_request")).strip()


def _forbidden_self(word: str, table: Mapping[str, Any],
                    allowed_self: Optional[Sequence[str]]) -> bool:
    """这个称呼词是不是「违禁自称」：它在自称用字表里，却不在魂的授权名单里。"""
    guard = section(table, "self_guard")
    words = _words(guard.get("words"))
    if word not in words:
        return False
    if allowed_self is None:
        return True
    return word not in {_text(item).strip() for item in allowed_self}


def _total_interactions(person: Optional[Mapping[str, Any]]) -> int:
    """累计互动次数：interactions 是 {"YYYY-MM-DD": 次数}，数不出来的项直接跳过。"""
    history = person.get("interactions") if isinstance(person, Mapping) else None
    if not isinstance(history, Mapping):
        return 0
    total = 0
    for value in history.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total += int(value)
    return total


def _reading(value: Any) -> float:
    """熟稔读数取成 float；读不到按 0.0（没读数＝没记录，不猜）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _text(value: Any) -> str:
    """只管字符串；别的类型一律当空串，绝不把 None 带进出口。"""
    return value if isinstance(value, str) else ""
