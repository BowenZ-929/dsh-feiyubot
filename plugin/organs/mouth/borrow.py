"""嘴·借阅读数：这一轮真用了家里借来的哪几份词条、各用掉几条。

嘴这一侧只负责**算读数**，一个字节都不落盘：本文件全是纯函数——不读盘、不联网、不取时钟
（时刻由调用方给），输入进去、事件与读数行出来就完。落盘在家：登记借阅条是
organs/home/residents.py 的 register_borrow，写读数是它的 record_readout。**分界线就在这儿**：
嘴说「这一轮用到几条」，家说「这条读数落在哪张借阅条上、写进盘长什么样」。

名单与条数由 api/chat.py 的 _books() 交进来，本文件不自己列目录、不自己开门：它最清楚这一轮
到底读了哪几份、每份真用掉几条。**报一份没读过的，读数就是编的**——所以名单以调用方给的
books 为准（值为 None 的当作没读，不进名单）。称呼表 address_table.json 是装配那一步
（organs/mind/conversation.py）读的，不经过这一层，于是也不在这里记账。

两句话钉死口径，别在别处再解释一遍：

1. **learned 取不到就写 0**。0 是一次真实读数（这一轮一条都没用上），**不是缺失**；家那边照 0
   落账、read_learned / borrow_ledger 照 0 读成「已接入」。缺失是「这一行根本不写」——把
   「写了 0」与「没写」混成一样，账上就再也分不清「她这一轮没用上」和「没人量过」。
2. **一份词条算不算「用到」看调用方那张 counts**：注入了几条就用几条；没注入的那份按「整份读过
   就整份算上」兜底（顶层是列表数列表、是键值表数键），形状数不出来就是 0——同样是真实读数。

口径与措辞都在数据里（organs/mouth/data/borrow.json）：每份数据文件的中文名、那句说法、状态词
全从那儿取；本文件只放取值与算法的形状，一句写死的门槛都没有。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Union

STATUS_OK = "ok"
STATUS_FAILED = "failed"

#: borrow_id_of 既认映射（{文件名: borrow_id}）也认取值函数（source -> borrow_id）。
Lookup = Union[Mapping[str, Any], Callable[[str], Any], None]


def entries(table: Any) -> int:
    """一份数据里有多少条：顶层是列表就数列表；顶层是一份键值表时，**先认表里那条清单**
    （第一个列表值——clauses.json 的 clauses、address_table.json 的 tiers），没有清单才数键
    （policy.json 那种按段读的）。别的形状数不出来就算 0。

    为什么清单优先：这三份数据顶层都是一份键值表，可「借了多少条」问的是清单的条数，不是顶层
    有几个键——clauses.json 顶层两个键、里面八条条款，说借了 2 条是错的。0 在这里是「数不出来」
    的如实答案，不是一个默认值：调用方要区别「一条都没有」与「不是一张表」，去看 table 本身。
    """
    if isinstance(table, (list, tuple)):
        return len(table)
    if isinstance(table, Mapping):
        for value in table.values():
            if isinstance(value, (list, tuple)):
                return len(value)
        return len(table)
    return 0


def usage(books: Mapping[str, Any], counts: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    """这一轮真正读/用到的文件名单，以及每份「本轮用到几条」。

    books: {数据文件名: 这一轮读到的表}。值为 None（或没给这一格）的当作**没读**，不进名单——
      名单只放真读过的那几份，顺序沿用调用方给的插入序，本文件不重排。
    counts: 可选的 {数据文件名: 本轮真用掉的条数}，调用方最清楚这个数（比如条款按「命中的那几条」
      算）。没注入的那份用 entries(table) 兜底：整份读过就整份算上。
      注入的值不是非负 int（bool 也不算）时按 0 记——**0 是一次真实读数，不是缺失**。

    返回 {"files": [{source, entries, used}], "sources": [文件名…], "count": 份数}：
    entries 是这份文件总共有几条（登记借阅条时当 total 用），used 是这一轮用掉几条（就是 learned）。
    """
    book = dict(books or {})
    fixed = dict(counts or {})
    files: List[Dict[str, Any]] = []
    for source, table in book.items():
        if table is None:
            continue
        size = entries(table)
        used = fixed.get(source, size)
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            used = 0
        files.append({"source": str(source), "entries": size, "used": used})
    return {"files": files, "sources": [one["source"] for one in files], "count": len(files)}


def ids_of(borrows: Optional[Iterable[Mapping[str, Any]]]) -> Dict[str, str]:
    """把家那本借阅条账（borrow_ledger 的 borrows）翻成 {数据文件名: borrow_id}。

    同一 source 只认**第一条**：一条一借的账本来就该一 source 一条，多出来的那几条不在这里选，
    「为什么会有第二条」是家那边要报的事，不在嘴这一侧悄悄合并掉。缺 source 或缺 borrow_id 的
    行直接跳过——它们认不出是哪一份，凑一个 id 上去就是编的。
    """
    known: Dict[str, str] = {}
    for row in borrows or []:
        if not isinstance(row, Mapping):
            continue
        source = row.get("source")
        borrow_id = row.get("borrow_id")
        if not isinstance(source, str) or not isinstance(borrow_id, str):
            continue
        if not source or not borrow_id:
            continue
        known.setdefault(source, borrow_id)
    return known


def _borrow_id_of(source: str, borrow_id_of: Lookup) -> Optional[str]:
    if borrow_id_of is None:
        return None
    if callable(borrow_id_of):
        found = borrow_id_of(source)
    else:
        found = borrow_id_of.get(source)
    return found if isinstance(found, str) and found else None


def readout(event: Mapping[str, Any], borrow_id_of: Lookup = None) -> Dict[str, Any]:
    """把一次 usage 事件翻成给家的读数行：[{source, borrow_id, learned}]（每份一行）。

    learned = 该份文件这一轮真正用到的条目数，取自事件里的 used。**取不到就写 0**：事件里没有
    这一格、或者那一格不是非负 int（bool 也不算），一律 0。0 是一次真实读数——这一轮一条都没
    用上，家那边就应该读到 0，而不是「未接入」。**缺失是这一行根本不写**，不是写个别的数顶上。

    borrow_id_of 认映射或取值函数：取不到某份的 borrow_id 时这一行的 borrow_id 写 None——
    「家里还没有这张借阅条」，调用方得先登记一条再来写读数（登记是家的事，不在嘴这一侧）。
    没给 borrow_id_of 就是全 None，那一份份行还是照出行：读数是读数，id 是 id，缺哪个说哪个。
    """
    rows: List[Dict[str, Any]] = []
    missing: List[str] = []
    for item in (event or {}).get("files") or []:
        if not isinstance(item, Mapping):
            continue
        source = str(item.get("source") or "")
        if not source:
            continue
        used = item.get("used")
        learned = used if isinstance(used, int) and not isinstance(used, bool) and used >= 0 else 0
        borrow_id = _borrow_id_of(source, borrow_id_of)
        if borrow_id is None:
            missing.append(source)
        rows.append({"source": source, "borrow_id": borrow_id, "learned": learned})
    return {"rows": rows, "missing": missing, "count": len(rows)}


def label(table: Mapping[str, Any], source: str) -> str:
    """一份数据文件给人看的名字（取自 data/borrow.json 的 sources）；没登记过就用文件名本身。"""
    names = (table or {}).get("sources")
    if isinstance(names, Mapping):
        found = names.get(source)
        if isinstance(found, str) and found:
            return found
    return str(source)


def note_of(table: Mapping[str, Any], key: str, **fields: Any) -> str:
    """从 data/borrow.json 的 note 里取一句说法并填字段；缺这一句就当场抛，不编一句顶上。

    措辞只有数据这一份真源：改口径＝改数据，代码里不写死半句。
    """
    notes = (table or {}).get("note")
    template = notes.get(key) if isinstance(notes, Mapping) else None
    if not isinstance(template, str) or not template:
        raise ValueError("borrow.json 缺 note.%s 这一句说法" % key)
    return template.format(**fields) if fields else template
