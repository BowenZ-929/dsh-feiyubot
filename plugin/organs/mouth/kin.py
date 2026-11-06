"""嘴的认亲判据：这一轮认不认这个号，由人名录定，不由回复里的字定。

**这一关治的是「跟着文本认亲」。** 魂那条红线（bounds.no-claim-kin，enforced_by mouth·语义复核）
说的是原则：不因自称认亲，对陌生来客可以客气，不能替他认领身份。**字面不在这里抄**——魂那边是
原则，这边是词表，两边各留一份必然漂移：熟人称呼表、记忆复述标记、中性替换词、稳妥话术、命中与
放行的人话、闸用的两个名字（clause_id / kind）全在同目录 data/kin.json 里，经 warehouse.load("kin")
只读借入；本文件不写死一个词、一句说法。缺键、坏形状当场抛 KinBroken，**不补默认值**：补一个默认
词表，等于代码里又多了一处真源。

**判据只有一条**：这一轮的 who 不够熟（陌生人，或刚建档、读数还没爬到那一档），而回复里出现了
**只有熟人才能触发的称呼或记忆复述**，就是认亲错。称呼类与记忆类各是一张表，全在数据里。

**「在册」不是「熟」，这是这一关的关键。** answer() 会把他这句话 ingest 建档，所以「人名录里查得到
他」这件事，说一句话就成立——拿它当「熟」，等于这一关对谁都放行。所以 known 收的是**一份读数**，
两段判：先看在不在册，再看档位/读数够不够得着数据里 known_by.min 那一档（档位阶梯只有
address_table.json 那一份，这里只引用它的整数，不另造一套判定）。

**三件事必须分得开，这是这一关的全部内容**：

- **够熟（在册 + 读数到档）→ 一律放行**：他爱怎么叫就怎么叫，不许误伤。
- **判不了（known=None）→ 不判**：这一轮没拿到人名录读数（who 为空、脑读不出来），返回 None，
  状态由调用方记成 not-checked。**判不了就不诬陷**——把「没查」当成「查过、是生人」，代价是把一句
  公道话拦下来。
- **不够熟（不在册，或在册但档位没到）→ 照实判**：命中给一条证据，没命中给 None。

known 只收 None 或一份读数（含 in_book 与数据里 known_by.field 说的那一格）；**布尔、数字、字符串
一律抛**（布尔守卫）：一个 True 说不出「够不够熟」，把它当放行就是这一关漏的那个洞；1 / "true"
更是替调用方猜读过没有。

**命中返回的不是布尔。** check 返回 KinHit（哪个词、命中的那段原文、那一整句、一句人话、两个闸用
的名字），一个都对不上才返回 None——布尔说不出「为什么拦」，日志、面板、账本也就没法复盘。

**改不了的处置是中性化，不是整条丢。** neutralize 只改那一处：称呼类命中把那个称呼换成数据里的
中性词（例如「你」），记忆类命中把那一整句换成数据里的实话，其余句子一个字不动；改完再补一句数据
里的稳妥话术（同一泡里已经有那句就不再补）。改完只剩空白才由闸按「没有正文可发」弃整条。

纯：不 import time / datetime / random，不取时钟、不联网、不写盘，不碰宿主；文件 IO 只经 warehouse。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .warehouse import load

STATUS_OK = "ok"                     # 真的判过了：这一轮拿到了人名录读数（True 与 False 都算）
STATUS_NOT_CHECKED = "not-checked"   # 没拿到人名录读数（known=None），这一关没跑；不是「不是认亲」

__all__ = ["KinBroken", "KinHit", "STATUS_OK", "STATUS_NOT_CHECKED",
           "check", "neutralize", "clear_reason", "neutralize_reason", "blank_reason",
           "leftover_reason"]


class KinBroken(ValueError):
    """认亲的判据表不合格：缺节、缺键、词表写成了一整串字、两张表撞名、中性词自己带着判据词，都算，坏一处就当场响。

    报错信息里带着那一项的名字：**不许拿一个空词表当「这一关没有判据」**，那等于静默放行，
    而且没人知道她为什么又开始跟着文本认亲（CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("认亲判据 %s%s（判据数据在 organs/mouth/data/kin.json，"
                         "由 warehouse.load(\"kin\") 读出）" % (where, detail))


@dataclass(frozen=True)
class KinHit:
    """一次「跟着文本认亲」的证据：哪个词、命中的那段原文、那一整句、一句人话、闸用的两个名字。

    字段（api 与闸按这个接，改字段等于改接口）：
      term      命中的那个词，取自数据里的两张表之一（日志按它分组最好用）
      matched   命中的那段原文（就是 term 在文本里的那一次出现），闸把它挂到 Hit.matched 上
      sentence  matched 所在的那一句，**不带句末标点**——中性化换整句时按它定位
      reason    一句人话，里面带着命中的那一段，账本与面板直接用这句
      clause_id 闸用的 clause_id，取自数据——闸不自己造名字，弃言账本里看到的就是数据里那个
      kind      闸用的 kind，取自数据，理由同上
    """

    term: str
    matched: str
    sentence: str
    reason: str
    clause_id: str
    kind: str


@dataclass(frozen=True)
class _Rules:
    """判据表拆成一堆可以直接用的值：数据里没有的一样都不补。"""

    kin_terms: Tuple[str, ...]
    memory_markers: Tuple[str, ...]
    known_field: str
    known_min: int
    term_word: str
    memory_sentence: str
    say_once: str
    join: str
    sentence_marks: str
    clause_id: str
    kind: str
    reason_hit: str
    reason_clear: str
    reason_neutralize: str
    reason_blank: str
    reason_leftover: str


def check(text: str, known: Any, table: Optional[Mapping[str, Any]] = None) -> Optional[KinHit]:
    """看这段字有没有跟着文本认亲：命中给一条证据，没命中给 None。

    table 是判据表（warehouse.load("kin") 读出来的那份），传 None 就现读仓库里的默认表。

    known 是这一轮的人名录读数：None（读不到，判不了）或一份键值表，两种形状各是一种结论，
    **必须分得开**。读数里两段，缺一段就是没读完：
      in_book  这个号在不在人名录里（真布尔）；
      relation 关系档的整数（生人 1 / 认识 2 / 熟人 3 / 挚友 4，阶梯在 address_table.json）；
    **在册还不够，读数要够得着数据里 known_by.min 那一档才放行**——answer() 会 ingest 建档，
    「查得到他」说一句话就成立，拿它当熟人等于这一关没有。不在册、或档位没到，都照实判。
    别的形状（True / False / 1 / "true" / 缺 in_book 的键值表）当场抛 KinBroken：一个布尔说不出
    「够不够熟」，猜一次就等于替调用方把这一关关掉。

    扫描按出现顺序，最先命中的那个词先报；同一起点上取长的那一条（长词先于它的前缀短词）。
    """
    rules = _rules(table)                 # 表先校验：坏表当场响，不许因为这一轮判不了就悄悄跳过
    reading = _reading(known)
    if reading is None or _familiar(reading, rules):
        return None                       # 判不了 / 够熟：两种都是「这一关不拦」，状态由调用方分
    body = _text_of(text)
    spots = _matches(body, rules)
    if not spots:
        return None
    return _hit(rules, body, spots[0])


def neutralize(text: str, hit: KinHit,
               table: Optional[Mapping[str, Any]] = None) -> Tuple[str, str]:
    """把命中的那一处换掉，返回（剩下的文本, 被换掉的那一段原文）；换不动就原样返回、changed 是空串。

    只改那一处：称呼类命中把那个称呼换成数据里的中性词，记忆类命中把那一整句换成数据里的实话，
    **别的句子一个字不动**。改完再补一句数据里的稳妥话术（同一泡里已经有那一句就不再补），
    因为「认错了亲」这件事本身要有一句实话收尾。changed 按原文带回被换掉的那一段，账本与面板
    靠它复盘「到底换掉了什么」。

    只认 KinHit：拿一次别的判据的命中进来，会换错地方，所以形状不对当场抛。
    """
    rules = _rules(table)
    if not isinstance(hit, KinHit):
        raise KinBroken("hit", "必须是 check 返回的那种 KinHit，读到的是 %s；拿别的命中进来会换错地方。"
                        % type(hit).__name__)
    body = _text_of(text)
    if hit.term in rules.memory_markers:
        return _swap_sentence(body, hit, rules)      # 记忆类：整句换成实话
    return _swap_term(body, hit, rules)              # 称呼类：只换那一处称呼


def clear_reason(table: Optional[Mapping[str, Any]] = None) -> str:
    """放行那一句人话（数据里的 reason.clear），原样填出来。

    为什么要单独一个入口：check 的返回类型是 Optional[KinHit]，装不下「没命中 + 为什么放行」这一种
    读数；把放行塞成一条假 KinHit，会把「没命中」与「判过没问题」挤进同一条通道，调用方再也分不开。
    闸把这一句带进 Verdict.reason，面板上便看得见这一关查过了。
    """
    return _rules(table).reason_clear


def neutralize_reason(changed: str, table: Optional[Mapping[str, Any]] = None) -> str:
    """中性化救回来那句人话（数据里的 reason.neutralize）：带上被换掉的那一段，账本靠它认出认亲被换过。"""
    return _fill(_rules(table).reason_neutralize, "reason.neutralize", changed=changed)


def blank_reason(changed: str, table: Optional[Mapping[str, Any]] = None) -> str:
    """换完只剩空白那句人话（数据里的 reason.blank）：没有正文可发，整条弃掉。"""
    return _fill(_rules(table).reason_blank, "reason.blank", changed=changed)


def leftover_reason(clause_id: str, matched: str,
                    table: Optional[Mapping[str, Any]] = None) -> str:
    """中性化之后的正文仍被别的判据拦下那句人话（数据里的 reason.leftover）：换过不等于放行。"""
    return _fill(_rules(table).reason_leftover, "reason.leftover",
                 clause_id=clause_id, matched=matched)


def _hit(rules: _Rules, body: str, spot: Tuple[int, str]) -> KinHit:
    """凑一次命中的证据：两个闸用的名字取自数据，reason 由数据里的模板填出来。"""
    start, needle = spot
    matched = body[start:start + len(needle)]
    return KinHit(term=needle, matched=matched, sentence=_sentence_at(body, start, rules),
                  reason=_fill(rules.reason_hit, "reason.hit", matched=matched),
                  clause_id=rules.clause_id, kind=rules.kind)


def _matches(body: str, rules: _Rules) -> List[Tuple[int, str]]:
    """这段字里所有命中词的出现位置，按出现顺序；同一起点上长的先报（长词先于它的前缀短词）。"""
    out: List[Tuple[int, str]] = []
    for needle in rules.kin_terms + rules.memory_markers:
        start = body.find(needle)
        while start >= 0:
            out.append((start, needle))
            start = body.find(needle, start + 1)
    out.sort(key=lambda one: (one[0], -len(one[1])))
    return out


def _swap_term(body: str, hit: KinHit, rules: _Rules) -> Tuple[str, str]:
    """称呼类命中：只把那一次出现换成中性词，别处不论出现几次都不动（换一处报一处，闸会接着判）。"""
    needle = hit.matched or hit.term
    spot = body.find(needle)
    if spot < 0:
        return body, ""
    rest = body[:spot] + rules.term_word + body[spot + len(needle):]
    return _with_say(rest, rules), needle


def _swap_sentence(body: str, hit: KinHit, rules: _Rules) -> Tuple[str, str]:
    """记忆类命中：那一整句换成数据里的实话，别的句子一个字不动（逗号不算句末）。"""
    needle = hit.matched or hit.term
    spot = body.find(needle)
    for start, end, mark_end in _spans(body, rules.sentence_marks):
        inside = (start <= spot < end) if spot >= 0 else (body[start:end].strip() == hit.sentence)
        if not inside:
            continue
        changed = body[start:end].strip()
        tail = body[mark_end:]
        head = body[:start] + rules.memory_sentence
        rest = head + rules.join + tail if tail.strip() else head
        return _with_say(rest, rules), changed
    return body, ""


def _with_say(body: str, rules: _Rules) -> str:
    """补一句稳妥话术：同一泡里已经有一模一样的一句就不再补；换完什么都不剩就照实返回空串。

    为什么补：中性化只解决了「叫错人」，还得有一句实话把这一轮交代过去——数据里那句稳妥话术正是
    为此而设。空着不补才是哑；补第二遍是同义反复，所以先查有没有。
    """
    rest = body.strip()
    if not rest or rules.say_once in rest:
        return rest
    sep = "" if rest[-1] in rules.sentence_marks else rules.join
    return rest + sep + rules.say_once


def _sentence_at(body: str, at: int, rules: _Rules) -> str:
    """命中的那个位置所在的那一句，**不带句末标点**——换整句时按它定位。"""
    for start, end, _mark_end in _spans(body, rules.sentence_marks):
        if start <= at < end:
            return body[start:end].strip()
    return body.strip()


def _spans(body: str, marks: str) -> List[Tuple[int, int, int]]:
    """按句末标点与换行分句：每句给（起, 句末, 标点之后）三个位置，空句也留着（位置要连续才换得准）。"""
    import re
    chars = "".join(dict.fromkeys(marks))
    pattern = re.compile("[%s]" % "".join(re.escape(ch) for ch in chars))
    out: List[Tuple[int, int, int]] = []
    start = 0
    for found in pattern.finditer(body):
        out.append((start, found.start(), found.end()))
        start = found.end()
    out.append((start, len(body), len(body)))
    return out


def _rules(table: Optional[Mapping[str, Any]] = None) -> _Rules:
    """把判据表读成一份只读规则；table 为 None 就去仓库现读默认表。

    缺节缺键一律抛 KinBroken——数据里没有的词不在代码里补默认值（补了就是第二处真源）。
    四条硬要求：两张表都非空、两张表不许撞名（同一个词算哪一边要看查表顺序，那是运气不是判据）、
    中性词与那两句实话都不许自己带着判据词（否则中性化之后的正文还命中，闸会一直换下去，
    最后整条被吞）、分句标点非空。
    """
    book = table if table is not None else load("kin")
    if not isinstance(book, Mapping):
        raise KinBroken("数据表", "不是一份键值表，读到的 %s。" % type(book).__name__)
    hit = _mapping(book, "hit")
    neutral = _mapping(book, "neutral")
    known_by = _mapping(book, "known_by")
    reason = _mapping(book, "reason")
    rules = _Rules(kin_terms=_words(book, "kin_terms"),
                   memory_markers=_words(book, "memory_markers"),
                   known_field=_word(known_by, "field"),
                   known_min=_count(known_by, "min"),
                   term_word=_word(neutral, "term_word"),
                   memory_sentence=_word(neutral, "memory_sentence"),
                   say_once=_word(neutral, "say_once"),
                   join=_word(neutral, "join"),
                   sentence_marks=_chars(book, "sentence_marks"),
                   clause_id=_word(hit, "clause_id"),
                   kind=_word(hit, "kind"),
                   reason_hit=_word(reason, "hit"),
                   reason_clear=_word(reason, "clear"),
                   reason_neutralize=_word(reason, "neutralize"),
                   reason_blank=_word(reason, "blank"),
                   reason_leftover=_word(reason, "leftover"))
    for one in rules.kin_terms:
        if one in rules.memory_markers:
            raise KinBroken("数据", "里称呼类与记忆类都收了 %r：同一个词换称呼还是换整句要看查表顺序，"
                                    "那是运气不是判据。" % one)
    needles = rules.kin_terms + rules.memory_markers
    for one, spot in ((rules.term_word, "neutral.term_word"),
                      (rules.memory_sentence, "neutral.memory_sentence"),
                      (rules.say_once, "neutral.say_once")):
        for needle in needles:
            if needle in one:
                raise KinBroken("数据", "里 %s（%r）自己带着判据词 %r：中性化之后的正文还命中，"
                                        "闸会一直换下去，最后把整条吞掉。" % (spot, one, needle))
    return rules


def _reading(known: Any) -> Optional[Mapping[str, Any]]:
    """把这一轮的人名录读数收成 None 或一份键值表；别的形状当场抛（布尔守卫，形状守卫）。

    只收两种形状：None（读不到人名录，判不了）与含 in_book 真布尔的键值表（在册读数）。
    为什么连 True / False 都不收：**一个布尔说不出「够不够熟」**。True 会被当成「放行」，可
    answer() 会把他这句话 ingest 建档——「在册」说一句话就成立，拿它当熟等于把这一关关掉，
    而那正是要堵的那个洞。1 与 "true" 连「脑说过什么」都没说清，更不许猜（CONTRIBUTING.md §1.1
    第三条：失败必须响）。
    """
    if known is None:
        return None
    if not isinstance(known, Mapping):
        raise KinBroken("known", "只收 None 或一份人名录读数（含 in_book 与档位那一格），读到的是 %r"
                                 "（%s）：一个布尔说不出「够不够熟」。"
                        % (known, type(known).__name__))
    flag = known.get("in_book")
    if not isinstance(flag, bool):
        raise KinBroken("known", "读数里的 in_book 必须是真的布尔（不是 1 / \"true\" / 缺着），读到的是"
                                 " %r（%s）。" % (flag, type(flag).__name__))
    return known


def _familiar(reading: Mapping[str, Any], rules: _Rules) -> bool:
    """够不够熟：在册 **且** 读数到档（known_by.min）。两段缺一段都不算熟。

    档位那一格按数据里 known_by.field 取（现在取 relation：生人 1 / 认识 2 / 熟人 3 / 挚友 4，
    阶梯本身只有 address_table.json 那一份）。在册但取不到那一格、或它不是整数，当场抛——
    「在册、读数读歪了」不许悄悄当成「不熟」也不许当成「熟」，两种猜法都会让账本说谎。
    """
    if not reading.get("in_book"):
        return False                     # 不在册：陌生号，照实判
    value = reading.get(rules.known_field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise KinBroken("known", "在册的读数里缺少整数 %r（读到 %r）：档位读不到就判不了够不够熟，"
                                 "不许猜。" % (rules.known_field, value))
    return value >= rules.known_min


def _text_of(text: Any) -> str:
    """要判的那段输出：不是字符串就当空串，判据只认字。"""
    return text if isinstance(text, str) else ("" if text is None else str(text))


def _need(data: Mapping[str, Any], key: str) -> Any:
    """取一项；缺了就抛，不补默认值。"""
    try:
        return data[key]
    except (KeyError, TypeError):
        raise KinBroken("数据", "缺少 %r 这一项。" % key) from None


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """取一节，必须是键值表。"""
    value = _need(data, key)
    if not isinstance(value, Mapping):
        raise KinBroken("数据", "的 %r 不是一份键值表，读到的 %s。" % (key, type(value).__name__))
    return value


def _word(data: Mapping[str, Any], key: str) -> str:
    """取一句非空字符串；空着也算缺。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise KinBroken("数据", "的 %r 必须是一句非空字符串。" % key)
    return value


def _count(data: Mapping[str, Any], key: str) -> int:
    """取一个整数阈值（不许是布尔：True 会被当成 1，1 当成一档——那不是判据，是巧合）。"""
    value = _need(data, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise KinBroken("数据", "的 %r 必须是一个整数，读到的是 %r（%s）。"
                        % (key, value, type(value).__name__))
    return value


def _chars(data: Mapping[str, Any], key: str) -> str:
    """取一串非空字符（分句用的句末标点与换行），按字符逐个用。"""
    value = _need(data, key)
    if not isinstance(value, str) or not value.strip():
        raise KinBroken("数据", "的 %r 必须是一串非空字符（分句就是按这里的字符切）。" % key)
    return value


def _words(data: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    """取一张词表：非空清单、每项非空字符串；不接受「一整串字」冒充清单。"""
    value = _need(data, key)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise KinBroken("数据", "的 %r 必须是非空清单（每项一个非空字符串），不是一整串字。" % key)
    out: List[str] = []
    for one in value:
        if not isinstance(one, str) or not one.strip():
            raise KinBroken("数据", "的 %r 里有不是字符串、或空着的项。" % key)
        out.append(one)
    return tuple(out)


def _fill(template: str, spot: str, **fields: Any) -> str:
    """把模板填成人话；模板里有数据没填的占位符就抛，别把 {matched} 这种半成品发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise KinBroken(str(spot), " 这句填不出来：%s" % exc) from None
