"""夜编排：固定五步，顺序不改，一步失败记一行继续下一步。

五步（顺序固定，理由写在每一步头上）：
1. brain_compact：真调脑的公开动作 BrainStore.run_night(now_ts)（脑自己幂等）。
2. brain_decay：**空实现**——脑没有公开的衰减结算动作（_settle_retention 是私有的，
   结算发生在 recall 读的时候）。所以只报 stubbed + 理由，什么也不写。
3. home_diary：真写——素材只取今天的真东西（脑 today + 心的心情档 + 日子自己的账），
   交核（mind.complete）写一篇，落家·抽屉（kind=日记），脑里只留一行索引。
   一天一张：当天已有就跳过并返回既有那张，绝不写第二张。
4. home_dream：真写——素材与落点同日记，kind=梦，脑里索引的 source_type 是「梦」
   （梦不作证词，evidence_key 由脑留空）。同样一天一张、幂等。
   写之前先把抽屉索引备一份到日子线自己的根下（回滚点），备份失败 = 这一步失败。
5. heart_settle：真调心的公开只读动作 energy.read（读时结算并写回，精力是时间的函数），
   回报 value/tier。本模块自己不写心的任何文件。

第三步与第四步的参数全在 data/writing.json（字数、素材上限、落点、命名、温度档、提示词、
回滚点），代码里不写这些数。核那边失败一律 LLMError(kind, message)：这两步把 kind 与人话
原样写进 detail（"timeout: 模型没在时限内回话…"），**绝不把没写成记成成功、也不写半篇**
——正文要么整篇落进抽屉，要么这一行 status=failed，剩下的交给既有的退避与 never_ran。

每步单独 try/except：失败记 status=failed + 一行错误，**继续下一步**——一件坏事
不许让整夜停摆。跑完往 data/night_log.jsonl 追加**一行** JSON：
    {"ts": ..., "day": ..., "attempt": 0, "retried_steps": [四步名字],
     "written": 1, "steps": {四步各自 {status, detail}}, "failed": [...]}
written 是本次落盘的日志行数（恒为 1；其余各步的落盘由各器官自己负责）。

重试只重跑没成功的那几步（only=...），其余步骤用 carry 原样合并回去——
两次日志对照起来才看得出"这一次重试了什么"，也才看得出上一次的结果没被重写。
attempt 是这一次尝试的序号（0 起数：0 是首跑，1 是第一次重试）；retried_steps 是
这一次真的跑了的步骤。日志只有一条流：重试与 never-ran 都往同一个 night_log.jsonl 写，
不另开第二个审计文件（别把日志做成第二套记忆）。
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from organs.heart import energy, mood
from organs.mind import LLMError, complete, load_config

from . import model

STEPS = ("brain_compact", "brain_decay", "home_diary", "home_dream", "heart_settle")
LOG_FILE = "night_log.jsonl"
WRITING_FILE = "writing.json"
STATUS_OK = "ok"
STATUS_STUBBED = "stubbed"
STATUS_FAILED = "failed"
# 今天没有一句真素材可写：**既不是成功也不是失败**——没写就是没写，不许记成 ok
STATUS_SKIPPED = "skipped"
# 这一次没跑、结果原样沿用上一次的那一步（重试只跑失败步，别的步不许被重写）
STATUS_CARRIED = "carried"
# 夜日志里的第二种行：该跑却试满次数也没跑成（与"跑了一次"同一份日志，不另开文件）
EVENT_NEVER_RAN = "never_ran"
# 温度档名里"交给核的默认档"那一个：writing.json 写它就表示不传 temperature
TIER_CORE_DEFAULT = "default"

STUB_BRAIN_DECAY = "脑没有公开的衰减结算动作（_settle_retention 是私有，读时结算在 recall 里）"


def log_path(root: Any = None) -> Path:
    return model.day_root(root) / LOG_FILE


def writing_file() -> Path:
    """写作参数的位置：包内 data/writing.json（只读参数，不是实盘）。"""
    return Path(__file__).resolve().parent / "data" / WRITING_FILE


# ---- 五步 -----------------------------------------------------------------


def _brain_compact(at: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """真调脑的公开动作：换日降级 + 从刚结束的一天里提炼提案（脑自己幂等）。"""
    brain = ctx.get("brain")
    if brain is None:
        return {"status": STATUS_FAILED, "detail": "脑未载入（brain=None），夜结算没得调"}
    out = brain.run_night(at)
    proposals = out.get("proposals") or []
    detail = "night_of=%s 换日降级 %s 条，提案 %d 条" % (
        out.get("night_of", ""), out.get("rolled_over", 0), len(proposals))
    if out.get("rerun"):
        detail += "（重跑：同一天已经结过，幂等返回）"
    if out.get("note"):
        detail += "；%s" % out["note"]
    return {"status": STATUS_OK, "detail": detail, "night_of": out.get("night_of"),
            "proposals": len(proposals), "rerun": bool(out.get("rerun"))}


def _brain_decay(at: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """空实现：没有公开的衰减结算动作，就只报 stubbed，不假装结过。"""
    return {"status": STATUS_STUBBED, "detail": STUB_BRAIN_DECAY}


# ---- 第三、四步：写日记与梦 -------------------------------------------------


class WritingError(Exception):
    """这一步的输入或参数不对：message 原样进夜日志 detail（不吞、不改写成成功）。"""


def _need_text(where: str, data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WritingError("%s 缺 %s（或为空）：改 data/writing.json" % (where, key))
    return value


def _need_count(where: str, data: Dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WritingError("%s 的 %s 要正整数：改 data/writing.json" % (where, key))
    return int(value)


def load_writing(path: Any = None) -> Dict[str, Any]:
    """读写作参数并校验一遍：坏一处就 WritingError，**不补默认值**（补了就是第二处真源）。

    校验的是形状与量级（字数区间、素材上限、落点、命名模板、温度档名、提示词、回滚点），
    不校验措辞——提示词写什么由人改这份文件，代码不替人挑词。
    """
    target = Path(path) if path is not None else writing_file()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WritingError("读不了写作参数 %s：%s" % (target, exc)) from exc
    except ValueError as exc:
        raise WritingError("写作参数不是 JSON：%s：%s" % (target, exc)) from exc
    if not isinstance(raw, dict):
        raise WritingError("写作参数要一份对象：%s" % target)
    words = raw.get("word_range")
    if not isinstance(words, dict):
        raise WritingError("写作参数缺 word_range（字数区间）：%s" % target)
    low = _need_count("word_range", words, "min")
    high = _need_count("word_range", words, "max")
    if low > high:
        raise WritingError("word_range 的 min 比 max 大：%d > %d" % (low, high))
    blocks = {}
    for name in ("material", "drawer", "rollback", "entries"):
        block = raw.get(name)
        if not isinstance(block, dict):
            raise WritingError("写作参数缺 %s（或不是对象）：%s" % (name, target))
        blocks[name] = block
    skip = blocks["material"].get("skip_source_types") or []
    if not isinstance(skip, list) or any(not isinstance(item, str) for item in skip):
        raise WritingError("material.skip_source_types 要是字符串数组：%s" % target)
    entries: Dict[str, Dict[str, Any]] = {}
    for key, entry in blocks["entries"].items():
        where = "entries.%s" % key
        if not isinstance(entry, dict):
            raise WritingError("%s 不是对象：%s" % (where, target))
        clean = {}
        for name in ("step", "kind", "name_template", "temperature_tier", "brain_source_type",
                     "brain_who_template", "index_template", "system_prompt", "user_template"):
            clean[name] = _need_text(where, entry, name)
        prompt_range = entry.get("prompt_range")
        if not isinstance(prompt_range, dict):
            raise WritingError("%s 缺 prompt_range（提示词里报的字数）：%s" % (where, target))
        low_prompt = _need_count(where + ".prompt_range", prompt_range, "min")
        high_prompt = _need_count(where + ".prompt_range", prompt_range, "max")
        if not low_prompt <= high_prompt:
            raise WritingError("%s.prompt_range 的 min 比 max 大：%s" % (where, target))
        if low_prompt < low or high_prompt > high:
            raise WritingError("%s.prompt_range 要落在 word_range 里（%d–%d）：%s"
                               % (where, low, high, target))
        clean["prompt_range"] = {"min": low_prompt, "max": high_prompt}
        extra = entry.get("extra_params") or {}
        if not isinstance(extra, dict) or any(not isinstance(v, str) for v in extra.values()):
            raise WritingError("%s.extra_params 要是 {键: 模板字符串}：%s" % (where, target))
        clean["extra_params"] = dict(extra)
        if clean["step"] in entries:
            raise WritingError("两个条目抢同一个步骤 %r：%s" % (clean["step"], target))
        entries[clean["step"]] = clean
    drawer = blocks["drawer"]
    rollback = blocks["rollback"]
    material = blocks["material"]
    return {
        "word_range": {"min": low, "max": high},
        "material": {"limit": _need_count("material", material, "limit"),
                     "empty": _need_text("material", material, "empty"),
                     "skip_source_types": tuple(skip)},
        "drawer": {"room": _need_text("drawer", drawer, "room"),
                   "index_name": _need_text("drawer", drawer, "index_name"),
                   "role": _need_text("drawer", drawer, "role"),
                   "by": _need_text("drawer", drawer, "by"),
                   "list_limit": _need_count("drawer", drawer, "list_limit")},
        "rollback": {"dir": _need_text("rollback", rollback, "dir"),
                     "room_template": _need_text("rollback", rollback, "room_template"),
                     "shelf_template": _need_text("rollback", rollback, "shelf_template")},
        "entries": entries,
    }


def _home_root(ctx: Dict[str, Any]) -> Path:
    """家的根：给了就用给的；没给按家自己的三级解析（构造参数 > HOME_ROOT > 仓内实盘）。"""
    given = ctx.get("home_root")
    if given is not None:
        return Path(given)
    from organs.home import store as home_store
    return Path(home_store.home_root(None))


def _rollback_point(day: str, step: str, cfg: Dict[str, Any],
                    ctx: Dict[str, Any]) -> Dict[str, Any]:
    """写之前把抽屉索引复制到日子线自己的根下：这是回滚点，**备份没成就是这一步失败**。

    抽屉是「一间房 + 几个格子」，索引不止一份（房级一份 + 日记/梦/…各一份），所以盘上真有
    的每一份各备一份；还没建过索引的格子没有可备的东西，如实记成 0 份，不假装备过。
    """
    home = _home_root(ctx)
    drawer = home / cfg["drawer"]["room"]
    index_name = cfg["drawer"]["index_name"]
    target_dir = model.day_root(ctx.get("root")) / cfg["rollback"]["dir"]
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WritingError("回滚点没备成（%s）：%s；没备份不照写" % (target_dir, exc)) from exc
    # 备份名里带 step：日记与梦各备各的，后一步不许把前一步的回滚点盖掉
    sources = [(drawer / index_name,
                cfg["rollback"]["room_template"].format(day=day, step=step))]
    if drawer.is_dir():
        for child in sorted(piece for piece in drawer.iterdir() if piece.is_dir()):
            sources.append((child / index_name,
                            cfg["rollback"]["shelf_template"].format(
                                day=day, step=step, shelf=child.name)))
    saved: List[Dict[str, Any]] = []
    for source, name in sources:
        if not source.is_file():
            continue
        dest = target_dir / name
        try:
            shutil.copyfile(str(source), str(dest))
            os.chmod(str(dest), 0o600)
        except OSError as exc:
            raise WritingError("回滚点没备成（%s → %s）：%s；没备份不照写"
                               % (source, dest, exc)) from exc
        rows = len(source.read_text(encoding="utf-8").splitlines())
        saved.append({"from": str(source), "to": str(dest), "lines": rows})
    return {"dir": str(target_dir), "copied": saved}


def _today_memories(ctx: Dict[str, Any], day: str,
                    cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """脑 today 里**今天的**真条目：空的、非今天的、她自己的索引行都不进素材。"""
    brain = ctx.get("brain")
    if brain is None:
        raise WritingError("脑未载入（brain=None）：日记要往脑里留一行索引，本步不写半份")
    rows: List[Dict[str, Any]] = []
    for entry in brain.today_entries(ctx["now"]):
        text = str(entry.get("text") or "").strip()
        if not text or entry.get("source_type") in cfg["material"]["skip_source_types"]:
            continue
        try:
            if model.day_key(entry.get("ts") or ctx["now"]) != day:
                continue
        except ValueError:
            continue
        rows.append(entry)
    return rows[-cfg["material"]["limit"]:]


def _today_schedule(ctx: Dict[str, Any], day: str) -> List[str]:
    """日子自己的账：当天 schedule_log.jsonl 里"今天做了什么"（没有就是没有，不补）。"""
    from . import schedule as schedule_module
    path = schedule_module.log_path(ctx.get("root"))
    if not path.is_file():
        return []
    items: List[str] = []
    for text in path.read_text(encoding="utf-8").splitlines():
        if not text.strip():
            continue
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("day") != day:
            continue
        item = str(row.get("item") or "").strip()
        if not item:
            continue
        slot = str(row.get("slot") or "").strip()
        items.append(("%s：%s" % (slot, item)) if slot else item)
    return items


def _bullets(rows: List[str], empty: str) -> str:
    """素材原样列成几条；一条都没有就写那句"没有"，绝不拿别的东西填。"""
    return ("\n".join("- " + row for row in rows)) if rows else empty


def _existing_card(ctx: Dict[str, Any], cfg: Dict[str, Any], day: str,
                   kind: str) -> Optional[Dict[str, Any]]:
    """今天这张写没写过：写过就把既有那张原样返回（一天一张，绝不写第二张）。"""
    from organs.home import cards
    listing = cards.list_room(_home_root(ctx), room=cfg["drawer"]["room"],
                              limit=cfg["drawer"]["list_limit"])
    for card in listing.get("items") or []:
        if card.get("kind") != kind or card.get("consumed"):
            continue
        if str((card.get("params") or {}).get("date")) == day:
            return card
    return None


def _tier_temperature(tier: str, ctx: Dict[str, Any]) -> Optional[float]:
    """温度档名 → 数：默认档回 None（交给核的配置），其余档从核的配置里取，代码里不写数。"""
    if tier == TIER_CORE_DEFAULT:
        return None
    mind = ctx.get("mind") or {}
    config = mind.get("config")
    conf = config if isinstance(config, dict) else load_config()
    tiers = (conf or {}).get("temperature_tiers") or {}
    value = tiers.get(tier)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WritingError("温度档 %r 在核的配置里没有数字（temperature_tiers=%r）：改一份"
                           % (tier, tiers))
    return float(value)


def _ask_core(ctx: Dict[str, Any], messages: List[Dict[str, str]],
              temperature: Optional[float]) -> str:
    """把这一篇交给核：真调 organs.mind.complete。失败原样上抛 LLMError，由 run 记 kind。"""
    mind = ctx.get("mind") or {}
    return complete(messages, config=mind.get("config"), env=mind.get("env"),
                    transport=mind.get("transport"), temperature=temperature)


def _append_card(ctx: Dict[str, Any], cfg: Dict[str, Any], card: Dict[str, Any],
                 day: str) -> Dict[str, Any]:
    """落家·抽屉：门在 access 里，本模块不判一次写权（cards 按顺序把两道门调一遍）。"""
    from organs.home import cards
    try:
        outcome = cards.append_cards(_home_root(ctx), role=cfg["drawer"]["role"],
                                     by=cfg["drawer"]["by"], cards=[card])
    except cards.CardError as exc:
        if exc.code == "DATE_ALREADY_WRITTEN":
            existing = _existing_card(ctx, cfg, day, str(card.get("kind")))
            if existing is not None:
                return existing
        raise WritingError("家那边拒了这张卡（%s：%s）" % (exc.code, exc.message)) from exc
    return (outcome.get("cards") or [{}])[0]


def _index_in_brain(ctx: Dict[str, Any], entry: Dict[str, Any], fields: Dict[str, Any],
                    card: Dict[str, Any]) -> Dict[str, Any]:
    """脑里只留一行索引（指向家里那张卡）：正文只在抽屉一份，绝不抄进脑。"""
    from organs.brain import store as brain_store
    brain = ctx.get("brain")
    text = entry["index_template"].format(**fields)
    who = entry["brain_who_template"].format(**fields)
    result = brain.ingest(who=who, text=text, source_type=entry["brain_source_type"],
                          ts=ctx["now"], side=brain_store.SIDE_SELF)
    if not result.get("ok"):
        raise WritingError("脑拒了这行索引：%s（家里那张卡已经落了：%s）"
                           % (result.get("message"), card.get("card_id")))
    if result.get("merged"):
        raise WritingError("脑把这一行并进了上一条（who=%r 撞车）：索引没单独成行"
                           "（家里那张卡已经落了：%s）" % (who, card.get("card_id")))
    return {"entry_id": (result.get("entry") or {}).get("id"), "text": text}


def _write_entry(at: str, ctx: Dict[str, Any], step: str) -> Dict[str, Any]:
    """第三、四步共用的那一套：既有就跳过 → 素材 → 核 → 回滚点 → 抽屉 → 脑里一行索引。

    顺序是刻意的：先查今天写没写过（省掉一次核调用），再收集今天的真素材，再交核写整篇，
    **整篇拿到之后才动回滚点与抽屉**——核失败时抽屉一个字都没动过，不会留半篇。
    """
    cfg = load_writing(ctx.get("writing"))
    entry = cfg["entries"].get(step)
    if entry is None:
        raise WritingError("写作参数里没有这一步（%r）：只配了 %s"
                           % (step, "、".join(sorted(cfg["entries"]))))
    day = model.day_key(at)
    kind = entry["kind"]
    name = entry["name_template"].format(kind=kind, date=day)
    # 提示词里报的是**提示区间**（比验收区间收窄一点，别让模型贴着边写）；
    # 落卡时的硬判据仍是 word_range——两件事分开，收窄提示词不等于放松验收
    fields: Dict[str, Any] = {"date": day, "kind": kind, "room": cfg["drawer"]["room"],
                              "name": name, "card_id": "",
                              "min_chars": entry["prompt_range"]["min"],
                              "max_chars": entry["prompt_range"]["max"]}
    existing = _existing_card(ctx, cfg, day, kind)
    if existing is not None:
        return {"status": STATUS_OK, "card_id": existing.get("card_id"), "rerun": True,
                "chars": int(existing.get("chars") or 0),
                "detail": "幂等：%s %s 今天已有《%s》（%s），不写第二张"
                          % (kind, day, existing.get("name"), existing.get("card_id"))}

    memories = _today_memories(ctx, day, cfg)
    schedule_rows = _today_schedule(ctx, day)
    if not memories and not schedule_rows:
        return {"status": STATUS_SKIPPED,
                "detail": "今天一句真素材都没有（脑 today 0 条 + 日程账 0 行）：不写、不编"}

    read = mood.read(root=ctx.get("heart_root"), now=at)
    tier = str(read.get("tier") or "").strip()
    if not tier:
        raise WritingError("心的心情档读不出档位（mood.read 回了 %r）：素材缺心情这一项" % (read,))
    fields["mood"] = tier
    fields["memories"] = _bullets([str(row.get("text") or "").strip() for row in memories],
                                 cfg["material"]["empty"])
    fields["schedule"] = _bullets(schedule_rows, cfg["material"]["empty"])
    messages = [{"role": "system", "content": entry["system_prompt"].format(**fields)},
                {"role": "user", "content": entry["user_template"].format(**fields)}]
    temperature = _tier_temperature(entry["temperature_tier"], ctx)
    text = _ask_core(ctx, messages, temperature).strip()

    low, high = cfg["word_range"]["min"], cfg["word_range"]["max"]
    if not low <= len(text) <= high:
        raise WritingError("核回了 %d 字，%s 只收 %d–%d 字（data/writing.json）："
                           "这一篇不写，也不许截半篇" % (len(text), kind, low, high))

    backup = _rollback_point(day, step, cfg, ctx)

    # 正文栏就是这一篇：真源在家·抽屉，人打开面板读到的也得是它。
    # 给脑用的那一行索引只进脑（见 _index_in_brain），绝不拿来冒充正文。
    # params["text"] 留着是因为家那层对日记/梦**要求**它（cards._check_kind_fields 拿它算字数），
    # 它必须与正文逐字相同——两栏一旦不同，账上的字数与卡上的正文就对不上了。
    params: Dict[str, Any] = {"date": day, "text": text, "mood": tier,
                              "from_memories": [str(row.get("id")) for row in memories
                                                if row.get("id")]}
    for key, template in entry["extra_params"].items():
        params[key] = template.format(**fields)
    card = {"room": cfg["drawer"]["room"], "kind": kind, "name": name,
            "chars": len(text), "note": text, "at": at, "params": params}
    written = _append_card(ctx, cfg, card, day)
    if written.get("note") != text or int(written.get("chars") or 0) != len(text):
        raise WritingError("卡与正文对不上（卡上 %d 字、正文 %d 字）：这一篇不算写成"
                           % (int(written.get("chars") or 0), len(text)))
    fields["card_id"] = str(written.get("card_id") or "")
    fields["name"] = str(written.get("name") or name)
    index = _index_in_brain(ctx, entry, fields, written)
    return {"status": STATUS_OK, "card_id": written.get("card_id"), "chars": len(text),
            "name": written.get("name"), "entry_id": index["entry_id"],
            "index": index["text"], "backup": backup,
            "detail": "%s %s 写成《%s》（%d 字，%s 档），脑里一行索引 %s；回滚点 %d 份" % (
                kind, day, written.get("name"), len(text),
                entry["temperature_tier"], index["entry_id"],
                len(backup["copied"]))}


def _home_diary(at: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """第三步：真写一篇日记（参数在 data/writing.json 里 step=home_diary 那一条）。"""
    return _write_entry(at, ctx, "home_diary")


def _home_dream(at: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """第四步：真写一篇梦（参数在 data/writing.json 里 step=home_dream 那一条）。"""
    return _write_entry(at, ctx, "home_dream")


def _heart_settle(at: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """真调心的公开只读动作：read 读时结算并写回；本模块不碰心的任何文件。"""
    read = energy.read(root=ctx.get("heart_root"), now=at)
    detail = "value=%s tier=%s（结算 %s → %s，%s 小时%s）" % (
        read.get("value"), read.get("tier"), read.get("settled_from"), read.get("settled_to"),
        read.get("hours"), "，触到上限" if read.get("capped") else "")
    return {"status": STATUS_OK, "detail": detail,
            "value": read.get("value"), "tier": read.get("tier")}


_STEP_FUNCS = {
    "brain_compact": _brain_compact,
    "brain_decay": _brain_decay,
    "home_diary": _home_diary,
    "home_dream": _home_dream,
    "heart_settle": _heart_settle,
}


# ---- 跑一次夜 --------------------------------------------------------------


def _append_log(line: Dict[str, Any], root: Any = None) -> Path:
    """追加一行 JSON（一行一次，不重写旧行；夜日志是流水，不是账）。"""
    target = log_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 不排序键：五步的顺序是契约，落进日志也得看得出来（排一下就成了字典序）
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    os.chmod(str(target), 0o600)
    return target


def run(now_ts: Any = None, *, root: Any = None, brain: Any = None, heart_root: Any = None,
        home_root: Any = None, mind: Optional[Dict[str, Any]] = None,
        break_step: Optional[int] = None, only: Optional[List[str]] = None,
        carry: Optional[Dict[str, Dict[str, Any]]] = None,
        attempt: int = 0,
        backoff_minutes: Optional[List[int]] = None,
        max_retries: Optional[int] = None,
        writing: Any = None) -> Dict[str, Any]:
    """跑一次夜：五步固定顺序，每步单独兜错，末尾落一行日志。

    break_step=N 只给验收用：让第 N 步（1 起数）当场抛错，看后面几步还跑不跑。

    only= 只跑这几步（重试时只重跑没成功的那几步）；其余步骤原样取 carry 里上一次的
    结果合并回去，不跑、不重写。carry 里没有的那一步报 carried 并说明，不假装跑过。
    attempt 是这次尝试的序号（0 起数），与 only/carry 一起落进日志那一行。
    backoff_minutes / max_retries 给了就顺手算出这次失败后的下一次几点
    （next_retry_at = 此刻 + backoff[attempt] 分钟，末项之后沿用末项）：退避算术只有这一处，
    rhythm 只负责把它抄进计划行，别处不许再算一遍。

    home_root 是家的根（第三、四步往抽屉里写），mind 是核的注入点
    （{"config", "env", "transport"}，与 organs.mind.complete 的三个关键字同名）——
    两个都缺省 None 就各走各的默认：家走自己的三级解析，核读 data/llm.json 与 .env。
    writing 是写作参数文件的位置（缺省 data/writing.json），只给测试与验收注入。
    """
    at = model.iso(now_ts) if now_ts is not None else model.now_iso()
    day = model.day_key(at)
    wanted = list(STEPS) if only is None else [name for name in STEPS if name in set(only)]
    carried = carry or {}
    ctx: Dict[str, Any] = {"now": at, "root": root, "brain": brain, "heart_root": heart_root,
                           "home_root": home_root, "mind": mind, "writing": writing}
    steps: Dict[str, Dict[str, Any]] = {}
    for number, name in enumerate(STEPS, 1):
        if name not in wanted:
            previous = carried.get(name)
            if isinstance(previous, dict):
                steps[name] = dict(previous)
            else:
                steps[name] = {"status": STATUS_CARRIED,
                               "detail": "这一次没跑（重试只跑没成功的步骤），上一次也没有这一步的记录"}
            continue
        try:
            if break_step is not None and int(break_step) == number:
                raise RuntimeError("验收注入：故意让第 %d 步（%s）抛错" % (number, name))
            steps[name] = _STEP_FUNCS[name](at, ctx)
        except LLMError as exc:
            # 核那边失败：kind 与人话原样进 detail（"timeout: 模型没在时限内回话…"），
            # 既不记成成功，也不写半篇——剩下的交给既有退避与 never_ran
            steps[name] = {"status": STATUS_FAILED,
                           "detail": "%s: %s" % (exc.kind, exc.message),
                           "error_kind": exc.kind}
        except Exception as exc:  # noqa: BLE001 - 一步失败只记一行，继续下一步
            steps[name] = {"status": STATUS_FAILED,
                          "detail": "%s: %s" % (type(exc).__name__, exc)}
    failed: List[str] = [name for name in STEPS if steps[name]["status"] == STATUS_FAILED]
    next_retry_at = _next_retry(at, attempt, bool(failed), backoff_minutes, max_retries)
    line = {"ts": at, "day": day, "attempt": int(attempt), "retried_steps": wanted,
            "next_retry_at": next_retry_at, "written": 1, "steps": steps, "failed": failed}
    target = _append_log(line, root)
    return {"ok": not failed, "now": at, "day": day, "attempt": int(attempt),
            "retried_steps": wanted, "next_retry_at": next_retry_at, "steps": steps,
            "failed": failed, "written": 1, "log_path": str(target), "log_line": line}


def _next_retry(at: str, attempt: int, failed: bool,
                backoff_minutes: Optional[List[int]],
                max_retries: Optional[int]) -> Optional[str]:
    """这次失败后的下一次重试几点；不再重试就是 None（**不写一个不会发生的时刻**）。

    退避是累加的：第 attempt 次尝试失败后等 backoff[attempt] 分钟。成功 / 额度用完 /
    没给退避表 → None。
    """
    if not failed or max_retries is None or int(attempt) >= int(max_retries):
        return None
    window = [int(minute) for minute in (backoff_minutes or [])]
    if not window:
        return None
    minutes = window[min(int(attempt), len(window) - 1)]
    return model.iso(model.parse_ts(at) + timedelta(minutes=minutes))


def audit_never_ran(now_ts: Any = None, *, root: Any = None, day: str = "",
                    attempt: int = 0, failed_steps: Optional[List[str]] = None,
                    reason: str = "", next_retry_at: Optional[str] = None) -> Dict[str, Any]:
    """never-ran 的那一条审计：追加到同一个 night_log.jsonl（**不另开第二个审计文件**）。

    attempt 是最后一次尝试的序号（0 起数），reason 用人的话写清"哪一步、试了几次"——
    账上 pending 里那条的计划行也带同一份快照，两边对得上。
    """
    at = model.iso(now_ts) if now_ts is not None else model.now_iso()
    line = {"ts": at, "day": day or model.day_key(at), "event": EVENT_NEVER_RAN,
            "attempt": int(attempt), "next_retry_at": next_retry_at,
            "failed_steps": list(failed_steps or []), "reason": reason}
    target = _append_log(line, root)
    return {"ok": True, "log_path": str(target), "log_line": line}


def log_tail(limit: int = 20, *, root: Any = None) -> Dict[str, Any]:
    """夜日志尾部：lines 是解出来的对象，raw 是原样的那几行（贴回执用）。"""
    target = log_path(root)
    if not target.exists():
        return {"ok": True, "path": str(target), "count": 0, "bad": 0, "lines": [], "raw": []}
    rows = target.read_text(encoding="utf-8").splitlines()
    tail = rows[-int(limit):] if limit and int(limit) > 0 else rows
    lines: List[Any] = []
    bad = 0
    for text in tail:
        if not text.strip():
            continue
        try:
            lines.append(json.loads(text))
        except ValueError:
            bad += 1
    return {"ok": True, "path": str(target), "count": len(lines), "bad": bad,
            "lines": lines, "raw": tail}
