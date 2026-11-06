"""脑的存储：今日、长期字面档、人名录、自述、提案。

已裁决（docs/plan.md §3）：第一档 = 原文 + 字面检索，向量只做重排、永不 gate；
遗忘不物理删，状态位与行同时落盘、且进查询条件（追加行带 status，检索侧过滤）。
仍待定不碰：热记忆是 20 行还是薄索引（挤掉时必须记录被挤掉的是什么）；
记忆滤镜要不要按情绪生成主观副本。

写入路径只有一条：ingest()（一条消息进 → 今日多一条 + 人名录认人更新）。
长期档只从今日换日降级而来；自述与提案各走自己的程序写入口。
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import edges as edges_mod
from . import people as people_mod
from . import proposals as proposals_mod
from . import recall as recall_mod
from . import retention as retention_mod
from .model import (ALPHA_DEFAULT, AUDIENCE_PARTIES, AUDIENCE_TYPES,
                    BETA_DEFAULT, PLACEHOLDER_WHOS, SCENE_GROUP,
                    SCENE_PRIVATE, SCENE_TYPES, SCENE_UNKNOWN, SIDE_OTHER,
                    SIDE_SELF, SIDE_TYPES, SOURCE_LIVED, SOURCE_SCHEDULED,
                    SOURCE_TYPES, STATUS_ACTIVE, STATUS_ARCHIVED,
                    STATUS_STALE, day_key, limits, now_iso, valid_who)

TODAY_FILE = "today.json"
LONGTERM_FILE = "longterm.jsonl"
PEOPLE_FILE = "people.json"
SELF_FILE = "selfnarration.jsonl"
PROPOSALS_FILE = "proposals.json"
PROPOSAL_LOG_FILE = "proposal_log.jsonl"
TIERS_FILE = "tiers.json"
RETENTION_FILE = "retention.json"
TOPICS_FILE = "topics.json"
HOT_FILE = "hot.json"
EDGES_FILE = "edges.json"
EVICTIONS_FILE = "evictions.jsonl"
TOPIC_CANDIDATES_FILE = "topic_candidates.json"
PEOPLE_CARD_HISTORY = "people_card_history.jsonl"
USAGE_FILE = "usage.jsonl"
NOTE_HISTORY = "note_history.jsonl"
CARD_FIELDS = {"称呼": "display", "雷点": "prefs", "备注": "notes", "照片说明": "photo_note",
               "生日": "birthday"}  # 生日是结构化字段：不占记忆名额、不被遗忘曲线淡掉

TIERS_DEFAULT = {"today_max": 60, "today_demote_count": 20, "hot_max": 20, "hot_lines_max": 3}
RETENTION_DEFAULT = {
    "decay_rate_normal": 0.35, "decay_rate_owner": 0.12,
    "owner_ids": ["主人", "1000001"], "recall_cap": 12,
    "recency_bonus_recent_days": 7, "recency_bonus_recent": 18,
    "recency_bonus_mentioned_days": 2, "recency_bonus_mentioned": 8,
    "threshold": 0.08, "idle_days": 30,
    "clarity_vivid_above": 1.0, "clarity_clear_above": 0.5,
    "clarity_fuzzy_above": 0.2, "fuzzy_chars": 42, "title_chars": 12,
}


def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, dict) else default


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    with open(str(path), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    path = Path(path)
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


class BrainReadError(Exception):
    """脑数据读不到/读坏：端点要如实报错，不许折叠成 0 条。"""


def _read_json_strict(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise BrainReadError("读不到 %s：%s" % (path.name, exc)) from exc
    except json.JSONDecodeError as exc:
        raise BrainReadError("%s 不是合法 JSON：%s" % (path.name, exc)) from exc
    if not isinstance(data, dict):
        raise BrainReadError("%s 必须是 JSON 对象" % path.name)
    return data


def _read_jsonl_strict(path: Path) -> List[Dict[str, Any]]:
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BrainReadError("读不到 %s：%s" % (path.name, exc)) from exc
    rows: List[Dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BrainReadError("%s 第 %d 行不是合法 JSON：%s"
                                 % (path.name, number, exc)) from exc
        if not isinstance(row, dict):
            raise BrainReadError("%s 第 %d 行不是对象" % (path.name, number))
        rows.append(row)
    return rows


def _usage_rows(path: Path) -> List[Dict[str, Any]]:
    return _read_jsonl(path)


def _as_list(value: Any) -> List[str]:
    """名片里的多行字段：字符串按行拆，列表照旧，空值給空列表。"""
    if isinstance(value, str):
        return [ln.strip() for ln in value.split(chr(10)) if ln.strip()]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return []

def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text[:40] + "…" if len(text) > 40 else text


def _new_id(prefix: str, taken: set) -> str:
    while True:
        candidate = prefix + uuid.uuid4().hex[:12]
        if candidate not in taken:
            return candidate


def _within_merge_window(last: Dict[str, Any], ts: str) -> bool:
    last_ts = last.get("last_ts") or last.get("ts")
    if not isinstance(last_ts, str):
        return False
    try:
        delta = datetime.fromisoformat(ts) - datetime.fromisoformat(last_ts)
        return 0 <= delta.total_seconds() <= limits.merge_window_minutes * 60
    except (TypeError, ValueError):
        return False


def _reject(message: str) -> Dict[str, Any]:
    return {"ok": False, "message": message}


def _normalize_row(row: Dict[str, Any], entry_id: str = "") -> Dict[str, Any]:
    """数据导入器/存量回填的统一口径：补齐 status、来源、α/β、evidence_key 缺省字段。"""
    row = dict(row)
    row["id"] = row.get("id") or entry_id
    row["status"] = row.get("status") or STATUS_ACTIVE
    source_type = row.get("source_type") or SOURCE_LIVED
    if source_type not in SOURCE_TYPES:
        raise BrainReadError("未知 source_type：%r（只许 %s）"
                             % (source_type, "/".join(SOURCE_TYPES)))
    row["source_type"] = source_type
    row["alpha"] = row.get("alpha", ALPHA_DEFAULT)
    row["beta"] = row.get("beta", BETA_DEFAULT)
    if "evidence_key" not in row:
        # 亲历的原话出处就是这一行自己；日程模拟没有真实出处，留空 → 无记录背书
        row["evidence_key"] = row["id"] if row.get("source_type") == SOURCE_LIVED else ""
    # 两栏：quote 是对方原话（逐字），note 是她的理解（可为空）。
    # 旧数据只有 text → 按"只有原话"处理：quote=text、note=""，不猜不补。
    if "quote" not in row:
        row["quote"] = row.get("text", "")
    row["text"] = row.get("quote", "")   # text 保留为 quote 的兼容别名
    if "note" not in row:
        row["note"] = ""
    row.setdefault("unclear", False)
    row.setdefault("side", SIDE_OTHER)
    if "scene" not in row:
        row["scene"] = SCENE_UNKNOWN   # 拿不到就留空标 unknown，不许猜
    if row.get("scene") not in SCENE_TYPES:
        row["scene"] = SCENE_UNKNOWN
    if "audience" not in row:
        row["audience"] = AUDIENCE_PARTIES   # 默认当事人；只有明确私密才标私密
    if row.get("audience") not in AUDIENCE_TYPES:
        row["audience"] = AUDIENCE_PARTIES
    if "beta_note" not in row:
        # β 只给 quote 满分；note 另记一档（beta 降一档）。空 note 不记 beta_note。
        row["beta_note"] = (int(row.get("beta", BETA_DEFAULT)) - 1) if row.get("note") else None
    return row


class BrainStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._init_files()
        self._today = _read_json(self._root / TODAY_FILE, {"day": "", "entries": [], "last_closed_day": ""})

    @property
    def root(self) -> Path:
        return self._root

    def _init_files(self) -> None:
        defaults: Dict[str, Dict[str, Any]] = {
            TODAY_FILE: {"day": "", "entries": [], "last_closed_day": ""},
            PEOPLE_FILE: {"people": {}},
            PROPOSALS_FILE: {"night_of": "", "proposals": []},
            TIERS_FILE: dict(TIERS_DEFAULT),
            RETENTION_FILE: dict(RETENTION_DEFAULT),
            TOPICS_FILE: {"医院": ["住院", "医院", "病"]},
            HOT_FILE: {"rows": []},
            EDGES_FILE: {"nodes": {}, "edges": []},
        }
        for name, default in defaults.items():
            path = self._root / name
            if not path.is_file():
                _atomic_write_json(path, default)
        path = self._root / TOPIC_CANDIDATES_FILE
        if not path.is_file():
            _atomic_write_json(path, {
                "candidates": [],
                "_note": "未接 NLP：回想到的未登录词（按整句暂存）进候选池，人在面板上收进 topics.json",
            })
        for name in (LONGTERM_FILE, SELF_FILE, PROPOSAL_LOG_FILE, EVICTIONS_FILE,
                     PEOPLE_CARD_HISTORY, USAGE_FILE, NOTE_HISTORY):
            path = self._root / name
            if not path.is_file():
                path.write_text("", encoding="utf-8")

    # —— 今日 ————————————————————————————————
    def _ensure_today(self, now_ts: str, commit: bool = True) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """读的时候比日期：已过换日点就把今天整条降级进长期档再清空。"""
        current = day_key(now_ts)
        stored = self._today.get("day") or ""
        if stored == current:
            return self._today, []
        if stored > current:
            # 乱序时间戳（自测 / 回放）：不回滚，也不重开一天
            return self._today, []
        archived: List[Dict[str, Any]] = []
        entries = self._today.get("entries") or []
        if entries:
            for entry in entries:
                archived.append(_normalize_row({
                    "id": entry.get("id", ""),
                    "ts": entry.get("last_ts") or entry.get("ts", ""),
                    "day": entry.get("day") or self._today.get("day", ""),
                    "who": entry.get("who", ""),
                    "text": entry.get("text", ""),
                    "quote": entry.get("quote", entry.get("text", "")),
                    "note": entry.get("note", ""),
                    "unclear": entry.get("unclear", False),
                    "beta_note": entry.get("beta_note"),
                    "side": entry.get("side", SIDE_OTHER),
                    "scene": entry.get("scene", SCENE_UNKNOWN),
                    "audience": entry.get("audience", AUDIENCE_PARTIES),
                    "count": entry.get("count", 1),
                    "source": "today",
                    "kind": "fact",
                    "status": entry.get("status", STATUS_ACTIVE),
                    "source_type": entry.get("source_type", SOURCE_LIVED),
                    "alpha": entry.get("alpha", ALPHA_DEFAULT),
                    "beta": entry.get("beta", BETA_DEFAULT),
                    "evidence_key": entry.get("evidence_key"),
                }, entry_id=entry.get("id", "")))
            self._append_longterm(archived)
        new_today = {"day": current, "entries": [], "last_closed_day": self._today.get("day", "")}
        if commit:
            _atomic_write_json(self._root / TODAY_FILE, new_today)
        self._today = new_today
        return self._today, archived

    def today_entries(self, now_ts: Optional[str] = None) -> List[Dict[str, Any]]:
        """今日在役条目：status 进查询条件；读时触发条数降级（不挂时钟）。"""
        now_ts = now_ts or now_iso()
        self._ensure_today(now_ts)
        self._demote_today(now_ts)
        return [entry for entry in (self._today.get("entries") or [])
                if entry.get("status", STATUS_ACTIVE) == STATUS_ACTIVE]

    def today_view(self, now_ts: Optional[str] = None) -> Dict[str, Any]:
        now_ts = now_ts or now_iso()
        self._ensure_today(now_ts)
        entries = [self._annotate_retention(dict(entry), now_ts)
                   for entry in self.today_entries(now_ts)]
        return {"day": self._today.get("day"), "entries": entries}

    def _annotate_retention(self, entry: Dict[str, Any], now_ts: str) -> Dict[str, Any]:
        """读时现算 retention 与 clarity，不写盘；与 _settle_retention 同一套参数。"""
        params = self._retention_params()
        value = retention_mod.retention(entry, params, now_ts)
        entry["retention"] = value
        entry["clarity"] = retention_mod.clarity(value, params)
        return entry

    def _recent_memories_for(self, who: str, now_ts: str,
                             limit: int = 5) -> List[Dict[str, Any]]:
        """某人的最近 3–5 条在役记忆，带 ts / retention / clarity，供人物页显示日期。"""
        rows: List[Dict[str, Any]] = []
        for entry in self.today_entries(now_ts):
            if entry.get("who") == who and entry.get("side", SIDE_OTHER) != SIDE_SELF:
                row = dict(entry)
                row["source"] = "today"
                rows.append(row)
        for entry in self.longterm_active_entries():
            if entry.get("who") == who and entry.get("side", SIDE_OTHER) != SIDE_SELF:
                row = dict(entry)
                row["source"] = "longterm"
                rows.append(row)
        rows.sort(key=lambda row: row.get("ts", ""), reverse=True)
        return [self._annotate_retention(row, now_ts) for row in rows[:limit]]

    def _taken_ids(self) -> set:
        taken = {entry.get("id", "") for entry in (self._today.get("entries") or [])}
        for row in _read_jsonl(self._root / LONGTERM_FILE):
            if row.get("id"):
                taken.add(row["id"])
        return taken

    # —— 四档参数（读数据文件，代码里不写 magic number） ————————————————
    def _tiers(self) -> Dict[str, Any]:
        return _read_json(self._root / TIERS_FILE, dict(TIERS_DEFAULT))

    def _retention_params(self) -> Dict[str, Any]:
        return _read_json(self._root / RETENTION_FILE, dict(RETENTION_DEFAULT))

    def _topics(self) -> Dict[str, Any]:
        return _read_json(self._root / TOPICS_FILE, {})

    # —— 热（薄索引） ————————————————————————————————
    def _hot(self) -> Dict[str, Any]:
        return _read_json(self._root / HOT_FILE, {"rows": []})

    def _write_hot(self, data: Dict[str, Any]) -> None:
        _atomic_write_json(self._root / HOT_FILE, data)

    def hot_rows(self) -> List[Dict[str, Any]]:
        return list(self._hot().get("rows", []))

    def _record_eviction(self, reason: str, now_ts: str, **fields: Any) -> None:
        record = {"reason": reason, "ts": now_ts}
        record.update(fields)
        _append_jsonl(self._root / EVICTIONS_FILE, record)

    def evictions(self) -> List[Dict[str, Any]]:
        return _read_jsonl(self._root / EVICTIONS_FILE)

    def _line_for(self, who: str, count: int, text: str) -> str:
        short = (text or "")[:12]
        return "%s · %d条 · %s…" % (who, count, short)

    def _build_hot_rows(self, entries: Sequence[Dict[str, Any]], now_ts: str) -> List[Dict[str, Any]]:
        """把一批今日条目压成 ≤ hot_lines_max 行薄索引：只记'有什么可查'，不含全文。"""
        tiers = self._tiers()
        by_who: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            by_who.setdefault(entry.get("who", ""), []).append(entry)
        groups = sorted(by_who.items(), key=lambda pair: len(pair[1]), reverse=True)
        rows: List[Dict[str, Any]] = []
        taken = {row.get("id", "") for row in self.hot_rows()}
        for who, group in groups[: tiers.get("hot_lines_max", 3)]:
            source_ids = [entry.get("id", "") for entry in group]
            rows.append({
                "id": _new_id("h-", taken),
                "line": self._line_for(who, len(group), group[0].get("text", "")),
                "source_ids": source_ids,
                "created_at": now_ts,
                "last_hit_at": None,
                "hit_count": 0,
            })
            taken.add(rows[-1]["id"])
        return rows

    def _demote_today(self, now_ts: str) -> Dict[str, Any]:
        """今日 > today_max：最旧 today_demote_count 条全文进长期档，压成 ≤N 行薄索引进热。
        只在写入/读取时触发，不挂时钟。"""
        tiers = self._tiers()
        entries = self._today.get("entries") or []
        if len(entries) <= tiers.get("today_max", 60):
            self._evict_hot_if_needed(now_ts)
            return {"ok": True, "demoted": 0}
        count = min(tiers.get("today_demote_count", 20), len(entries))
        oldest = entries[:count]
        longterm_rows = []
        for entry in oldest:
            row = _normalize_row({
                "id": entry.get("id", ""),
                "ts": entry.get("ts", ""),
                "day": entry.get("day", ""),
                "who": entry.get("who", ""),
                "text": entry.get("text", ""),
                "quote": entry.get("quote", entry.get("text", "")),
                "note": entry.get("note", ""),
                "unclear": entry.get("unclear", False),
                "beta_note": entry.get("beta_note"),
                "side": entry.get("side", SIDE_OTHER),
                "scene": entry.get("scene", SCENE_UNKNOWN),
                "audience": entry.get("audience", AUDIENCE_PARTIES),
                "count": entry.get("count", 1),
                "source": "today",
                "kind": "fact",
                "status": STATUS_ACTIVE,
                "source_type": entry.get("source_type", SOURCE_LIVED),
                "alpha": entry.get("alpha", ALPHA_DEFAULT),
                "beta": entry.get("beta", BETA_DEFAULT),
                "evidence_key": entry.get("evidence_key", ""),
                "demoted_to": "hot",
            }, entry_id=entry.get("id", ""))
            longterm_rows.append(row)
        self._append_longterm(longterm_rows)
        hot_rows = self._build_hot_rows(oldest, now_ts)
        if hot_rows:
            hot = self._hot()
            hot.setdefault("rows", []).extend(hot_rows)
            self._write_hot(hot)
        self._record_eviction("today_over_60", now_ts,
                              demoted_ids=[e.get("id", "") for e in oldest],
                              demoted_lines=[row["line"] for row in hot_rows],
                              demoted_count=len(oldest))
        self._today["entries"] = entries[count:]
        _atomic_write_json(self._root / TODAY_FILE, self._today)
        self._evict_hot_if_needed(now_ts)
        return {"ok": True, "demoted": len(oldest), "hot_rows": len(hot_rows)}

    def _evict_hot_if_needed(self, now_ts: str) -> Dict[str, Any]:
        """热满 hot_max：挤最久未命中那条进长期库（全文早已在长期档），并记录被挤掉的是什么。"""
        tiers = self._tiers()
        hot = self._hot()
        rows = hot.get("rows", [])
        if len(rows) <= tiers.get("hot_max", 20):
            return {"ok": True, "evicted": 0}
        def row_last_hit(row: Dict[str, Any]) -> str:
            times = [self._latest_usage(sid).get("last_recalled_at", "")
                     if self._latest_usage(sid) else ""
                     for sid in row.get("source_ids", [])]
            hits = [t for t in times if t]
            return min(hits) if hits else (row.get("created_at") or "")

        victim = min(rows, key=row_last_hit)
        hot["rows"] = [row for row in rows if row.get("id") != victim.get("id")]
        self._write_hot(hot)
        self._record_eviction("hot_full", now_ts,
                              evicted_hot_id=victim.get("id"),
                              evicted_line=victim.get("line"),
                              source_ids=victim.get("source_ids", []))
        return {"ok": True, "evicted": 1, "victim": victim}

    def hot_expand(self, hot_id: str, now_ts: Optional[str] = None) -> Dict[str, Any]:
        """命中薄索引后展开正文：全文去长期档按 source_ids 读，命中计数 +1。"""
        now_ts = now_ts or now_iso()
        hot = self._hot()
        for row in hot.get("rows", []):
            if row.get("id") == hot_id:
                full_texts = []
                for source_id in row.get("source_ids", []):
                    found = self._latest_longterm(source_id)
                    if found:
                        full_texts.append(found)
                    self._record_hit(source_id, now_ts)
                return {"ok": True, "line": row.get("line"),
                        "source_ids": row.get("source_ids", []),
                        "full_texts": full_texts}
        return {"ok": False, "message": "没有这行热索引：%s" % hot_id}

    def _latest_longterm(self, entry_id: str) -> Optional[Dict[str, Any]]:
        latest = None
        for row in _read_jsonl(self._root / LONGTERM_FILE):
            if row.get("id") == entry_id:
                latest = row
        return latest

    # —— 遗忘曲线（读时结算）与冷仓 ————————————————————————————————
    def _settle_retention(self, now_ts: str) -> List[Dict[str, Any]]:
        """谁读到谁算：retention 低于阈值且闲置超限 → 标 archived（落冷仓），原文仍留盘。

        只在真的有行要归档时才写盘；已归档的行不重复追加（并发下账本不脏）。
        """
        params = self._retention_params()
        threshold = params.get("threshold", 0.08)
        idle_days = params.get("idle_days", 30)
        to_archive: List[Dict[str, Any]] = []
        for entry in self.longterm_active_entries():
            value = retention_mod.retention(entry, params, now_ts)
            last = entry.get("last_recalled_at") or entry.get("last_accessed_at") or entry.get("ts")
            idle = retention_mod.days_between(now_ts, last)
            if value < threshold and (idle is None or idle >= idle_days):
                archived_row = dict(entry)
                archived_row.pop("recall_count", None)
                archived_row.pop("last_recalled_at", None)
                archived_row.pop("hit_count", None)
                archived_row["status"] = STATUS_ARCHIVED
                archived_row["retention"] = value
                archived_row["clarity"] = retention_mod.clarity(value, params)
                to_archive.append(archived_row)
        for archived_row in to_archive:
            latest = self._latest_longterm(archived_row.get("id", ""))
            if latest and latest.get("status") == STATUS_ARCHIVED:
                continue   # 已经归档过，不重复追加
            _append_jsonl(self._root / LONGTERM_FILE, archived_row)
        return to_archive

    def cold_entries(self) -> List[Dict[str, Any]]:
        rows = _read_jsonl(self._root / LONGTERM_FILE)
        latest: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row.get("id"):
                latest[row["id"]] = row
        return [row for row in latest.values() if row.get("status") == STATUS_ARCHIVED]

    def cold_lookup(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """冷仓可捞：按 id 读回原文（不看过期状态）。"""
        return self._latest_longterm(entry_id)

    def tiers_summary(self, now_ts: Optional[str] = None,
                      recent: int = 3) -> Dict[str, Any]:
        """四档各多少条 + 每档最近几条。数据读不到/读坏就报错，不许折叠成 0 条。"""
        now_ts = now_ts or now_iso()
        today = _read_json_strict(self._root / TODAY_FILE)
        hot = _read_json_strict(self._root / HOT_FILE)
        rows = _read_jsonl_strict(self._root / LONGTERM_FILE)
        latest: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row.get("id"):
                latest[row["id"]] = row
        active = [row for row in latest.values()
                  if row.get("status", STATUS_ACTIVE) == STATUS_ACTIVE]
        cold = [row for row in latest.values() if row.get("status") == STATUS_ARCHIVED]

        def recent_of(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            ordered = sorted(items, key=lambda row: row.get("ts", ""), reverse=True)
            return [self._annotate_retention(dict(row), now_ts)
                    for row in ordered[:recent]]

        today_entries = [e for e in today.get("entries", [])
                         if e.get("status", STATUS_ACTIVE) == STATUS_ACTIVE]
        return {
            "today": {"count": len(today_entries), "recent": recent_of(today_entries)},
            "hot": {"count": len(hot.get("rows", [])),
                    "recent": hot.get("rows", [])[-recent:]},
            "longterm": {"count": len(active), "recent": recent_of(active)},
            "cold": {"count": len(cold), "recent": recent_of(cold)},
        }

    def rebuild_index(self, now_ts: Optional[str] = None) -> Dict[str, Any]:
        """重建热索引（hot.json）：从长期档 demoted_to=hot 的原文重新分组；usage 账不动。"""
        now_ts = now_ts or now_iso()
        rows = _read_jsonl(self._root / LONGTERM_FILE)
        latest: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row.get("id"):
                latest[row["id"]] = row
        demoted = [row for row in latest.values()
                   if row.get("demoted_to") == "hot"
                   and row.get("status", STATUS_ACTIVE) == STATUS_ACTIVE]
        rebuilt = self._build_hot_rows(demoted, now_ts)
        self._write_hot({"rows": rebuilt})
        return {"ok": True, "hot_rows": len(rebuilt), "source_rows": len(demoted)}

    def cold_page(self, cursor: Optional[str] = None,
                  limit: int = 20, now_ts: Optional[str] = None) -> Dict[str, Any]:
        """冷仓分页：空就是空数组；读不到/读坏要报错，不许折叠成 0 条。

        每项带现算的 retention/clarity——遗忘曲线的意义是"看得见正在变淡"，
        冷仓是最后一档，这里也必须能看见它淡到了哪一格。
        """
        now_ts = now_ts or now_iso()
        rows = _read_jsonl_strict(self._root / LONGTERM_FILE)
        latest: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row.get("id"):
                latest[row["id"]] = row
        cold = [row for row in latest.values() if row.get("status") == STATUS_ARCHIVED]
        cold.sort(key=lambda row: row.get("ts", ""), reverse=True)
        if cursor:
            cold = [row for row in cold if row.get("ts", "") < cursor]
        page = [self._annotate_retention(dict(row), now_ts) for row in cold[:limit]]
        next_cursor = page[-1].get("ts") if len(page) == limit else None
        return {"items": page, "next_cursor": next_cursor}

    # —— 边（星图地基） ————————————————————————————————
    def _edges(self) -> Dict[str, Any]:
        return edges_mod.load_graph(self._root)

    def _write_edges(self, graph: Dict[str, Any]) -> None:
        edges_mod.save_graph(self._root, graph)

    def _add_edges_for(self, entry: Dict[str, Any]) -> None:
        topics = edges_mod.extract_topics(entry.get("text", ""), self._topics())
        if not topics:
            return
        graph = self._edges()
        edges_mod.add_event_edges(graph, entry.get("id", ""), entry.get("who", ""),
                                  topics, entry.get("ts", ""))
        self._write_edges(graph)

    def _invalidate_edges_for(self, entry_id: str) -> int:
        graph = self._edges()
        node_id = entry_id
        if ":" not in node_id and ("event:" + node_id) in graph.get("nodes", {}):
            node_id = "event:" + node_id
        changed = edges_mod.invalidate_edges(graph, node_id)
        if changed:
            self._write_edges(graph)
        return changed

    def neighbors(self, node_id: str) -> Dict[str, Any]:
        graph = self._edges()
        if ":" not in node_id and ("event:" + node_id) in graph.get("nodes", {}):
            node_id = "event:" + node_id
        return {"node": node_id, "neighbors": edges_mod.neighbors(graph, node_id)}

    def explain_recall(self, items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """给一次回想结果，说清这几条是怎么连上的（人—事、事—话题）。"""
        graph = self._edges()
        explained = []
        for item in items:
            explained.append({
                "id": item.get("id", ""),
                "text": item.get("full_text", item.get("text", "")),
                "neighbors": edges_mod.neighbors(graph, item.get("id", "")),
            })
        return {"items": explained}

    def edge_health(self) -> Dict[str, Any]:
        return edges_mod.edge_health(self._edges())

    # —— 话题候选池（词表能长） ————————————————————————————————
    def _topic_candidates(self) -> Dict[str, Any]:
        return _read_json(self._root / TOPIC_CANDIDATES_FILE,
                          {"candidates": [], "_note": "未接 NLP"})

    def _write_topic_candidates(self, data: Dict[str, Any]) -> None:
        _atomic_write_json(self._root / TOPIC_CANDIDATES_FILE, data)

    def topic_candidates(self) -> List[Dict[str, Any]]:
        return list(self._topic_candidates().get("candidates", []))

    def _add_topic_candidates(self, items: Sequence[Dict[str, Any]], now_ts: str) -> None:
        """回想到的长期/热条目里，词表没盖住的整句进候选池；人之后在面板收进词表。"""
        topics = self._topics()
        data = self._topic_candidates()
        candidates = data.setdefault("candidates", [])
        known_texts = {c.get("text", "") for c in candidates}
        changed = False
        for item in items:
            if item.get("source") not in ("longterm", "hot"):
                continue
            text = item.get("full_text", item.get("text", ""))
            if not text or text in known_texts:
                continue
            if edges_mod.extract_topics(text, topics):
                continue   # 词表已经盖住，不进候选
            candidates.append({"text": text, "from": item.get("id", ""), "ts": now_ts})
            known_texts.add(text)
            changed = True
        if changed:
            self._write_topic_candidates(data)

    def promote_topic_candidate(self, label: str, text: str) -> Dict[str, Any]:
        """人把候选收进词表：topics[label] 增加 text 关键词，并从候选池移除该句。"""
        label = (label or "").strip()
        text = (text or "").strip()
        if not label or not text:
            return _reject("label 与 text 不能为空")
        topics = self._topics()
        keywords = topics.setdefault(label, [])
        if text not in keywords:
            keywords.append(text)
        _atomic_write_json(self._root / TOPICS_FILE, topics)
        data = self._topic_candidates()
        data["candidates"] = [c for c in data.get("candidates", [])
                              if c.get("text") != text]
        self._write_topic_candidates(data)
        return {"ok": True, "label": label, "keyword": text,
                "topics": topics.get(label)}

    def ingest(self, who: str, text: str, mood: Optional[Any] = None,
               display: Optional[str] = None, ts: Optional[str] = None,
               source_type: str = SOURCE_LIVED, alpha: int = ALPHA_DEFAULT,
               beta: int = BETA_DEFAULT, note: Optional[str] = None,
               unclear: bool = False, side: str = SIDE_OTHER,
               scene: Optional[str] = None, audience: Optional[str] = None,
               is_bot: bool = False) -> Dict[str, Any]:
        """一条消息进：今日追加或合并一条 + 人名录认人更新。

        text 就是 quote（对方原话）；note 是她的理解，可为空——空 note 是正常状态。
        拿不准就 note="" + unclear=True，只存原话，不许硬编理解。
        side：他说（默认可召回）／她说（她自己的话，默认不召回，引用走 selfnarration）。
        """
        ts = ts or now_iso()
        who = (who or "").strip()
        text = (text or "").strip()
        if side == SIDE_SELF:
            who = who or "她说"
        elif not who:
            who = "人"   # 来源不明：记忆照记，who 记成占位符"人"，但人名录不建卡
        elif who not in PLACEHOLDER_WHOS and not valid_who(who):
            return _reject("昵称不是身份：%s——同一个人在多个群用不同昵称，"
                           "请传平台+数字号（如 aiocqhttp:3425635473）" % who)
        if not text:
            return _reject("text 不能为空")
        if source_type not in SOURCE_TYPES:
            # 不认识的来源**直接拒**：猜成亲历等于把一条没有出处的话变成证词（且没法回查）
            return _reject("source_type 不在合法取值里：%r；只收 %s"
                           % (source_type, " / ".join(SOURCE_TYPES)))
        self._today, archived = self._ensure_today(ts)
        entries = self._today.setdefault("entries", [])
        merged = False
        if entries:
            last = entries[-1]
            if last.get("who") == who and _within_merge_window(last, ts):
                last["text"] = text
                last["quote"] = text
                last["last_ts"] = ts
                last["count"] = int(last.get("count", 1)) + 1
                last["scene"] = scene if scene in SCENE_TYPES else SCENE_UNKNOWN
                last["audience"] = audience if audience in AUDIENCE_TYPES else AUDIENCE_PARTIES
                last["side"] = side if side in SIDE_TYPES else SIDE_OTHER
                if note is not None:
                    last["note"] = note
                    last["beta_note"] = (int(last.get("beta", BETA_DEFAULT)) - 1) if note else None
                if unclear:
                    last["unclear"] = True
                if mood is not None:
                    last["mood"] = mood
                merged = True
        if not merged:
            entry: Dict[str, Any] = {
                "id": _new_id("t-", self._taken_ids()),
                "day": self._today.get("day", day_key(ts)),
                "ts": ts,
                "who": who,
                "text": text,
                "quote": text,            # 原话逐字；note 是她的理解，可为空
                "count": 1,
                "status": STATUS_ACTIVE,   # 状态位和行同时落盘
                "source_type": source_type,
                "alpha": alpha,
                "beta": beta,
                "note": note or "",
                "unclear": bool(unclear),
                "beta_note": (int(beta) - 1) if note else None,
                "side": side if side in SIDE_TYPES else SIDE_OTHER,
                "scene": scene if scene in SCENE_TYPES else SCENE_UNKNOWN,
                "audience": audience if audience in AUDIENCE_TYPES else AUDIENCE_PARTIES,
                "evidence_key": "",   # 创建后再补：亲历指向自己这行原话，日程模拟留空
            }
            if mood is not None:
                entry["mood"] = mood
            entries.append(entry)
            entry["evidence_key"] = entry["id"] if entry["source_type"] == SOURCE_LIVED else ""
        _atomic_write_json(self._root / TODAY_FILE, self._today)
        if not merged:
            self._add_edges_for(entries[-1])
        self._demote_today(ts)
        if side == SIDE_SELF:
            person = {"skipped": True, "reason": "self-side"}
        else:
            person = self.interact(who, display=display, ts=ts, text=text,
                                   is_bot=is_bot)
        return {"ok": True, "merged": merged, "entry": dict(entries[-1]),
                "archived_count": len(archived), "person": person}

    # —— 长期字面档 ————————————————————————————
    def _append_longterm(self, records: Sequence[Dict[str, Any]]) -> None:
        for record in records:
            _append_jsonl(self._root / LONGTERM_FILE, record)

    def longterm_entries(self) -> List[Dict[str, Any]]:
        """长期档全部行（含 tombstone，审计用）。"""
        return _read_jsonl(self._root / LONGTERM_FILE)

    def usage_entries(self) -> List[Dict[str, Any]]:
        """命中账：hit_count / last_hit_at / recall_count 单列一本账，索引重建不丢。"""
        return _usage_rows(self._root / USAGE_FILE)

    def _append_usage(self, record: Dict[str, Any]) -> None:
        _append_jsonl(self._root / USAGE_FILE, record)

    def _latest_usage(self, entry_id: str) -> Optional[Dict[str, Any]]:
        latest = None
        for row in _usage_rows(self._root / USAGE_FILE):
            if row.get("id") == entry_id:
                latest = row
        return latest

    def _merge_usage(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(entry)
        usage = self._latest_usage(entry.get("id", ""))
        if usage:
            merged["recall_count"] = usage.get("recall_count", 0)
            merged["last_recalled_at"] = usage.get("last_recalled_at", "")
            merged["hit_count"] = usage.get("hit_count", 0)
        return merged

    def longterm_active_entries(self) -> List[Dict[str, Any]]:
        """长期档在役条目：同 id 只认最后一行，status 进查询；命中账从 usage 现合并。"""
        rows = _read_jsonl(self._root / LONGTERM_FILE)
        latest: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            row_id = row.get("id")
            if row_id:
                latest[row_id] = row
        return [self._merge_usage(row) for row in latest.values()
                if row.get("status", STATUS_ACTIVE) == STATUS_ACTIVE]

    def import_rows(self, rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """数据导入器：旧存量行（缺 status/来源/α/β/evidence_key）归一化后追加进长期档。"""
        imported = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not row.get("text") and "quote" not in row:
                continue
            entry_id = row.get("id") or _new_id("t-", self._taken_ids())
            normalized = _normalize_row(row, entry_id=entry_id)
            if not normalized.get("ts"):
                normalized["ts"] = now_iso()
            if "day" not in normalized or not normalized.get("day"):
                normalized["day"] = day_key(normalized["ts"])
            _append_jsonl(self._root / LONGTERM_FILE, normalized)
            self._add_edges_for(normalized)
            imported += 1
        return {"ok": True, "imported": imported}

    def backfill(self) -> Dict[str, Any]:
        """存量回填：给已落盘的今日条目与长期档行补齐 status/来源/α/β/evidence_key。幂等。"""
        today_changed = 0
        entries = self._today.get("entries") or []
        for index, entry in enumerate(entries):
            before = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            normalized = _normalize_row(entry, entry_id=entry.get("id", ""))
            after = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
            if before != after:
                entries[index] = normalized
                today_changed += 1
        if today_changed:
            _atomic_write_json(self._root / TODAY_FILE, self._today)
        rows = _read_jsonl(self._root / LONGTERM_FILE)
        changed = 0
        for index, row in enumerate(rows):
            before = json.dumps(row, ensure_ascii=False, sort_keys=True)
            normalized = _normalize_row(row, entry_id=row.get("id", ""))
            after = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
            if before != after:
                rows[index] = normalized
                changed += 1
        if changed:
            tmp = self._root / (LONGTERM_FILE + ".tmp")
            with open(str(tmp), "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            os.replace(str(tmp), str(self._root / LONGTERM_FILE))
        return {"ok": True, "today_normalized": today_changed,
                "longterm_normalized": changed}

    def _mark_today(self, entry_id: str, status: str, ts: str) -> Dict[str, Any]:
        today = self._today.get("entries") or []
        for entry in today:
            if entry.get("id") == entry_id:
                entry["status"] = status
                _atomic_write_json(self._root / TODAY_FILE, self._today)
                self._invalidate_edges_for(entry_id)
                return {"ok": True, "id": entry_id, "status": status,
                        "where": "today", "ts": ts}
        _append_jsonl(self._root / LONGTERM_FILE, {
            "id": entry_id, "status": status, "ts": ts,
            "who": "", "text": "", "count": 0, "source": "status", "day": "",
        })
        self._invalidate_edges_for(entry_id)
        return {"ok": True, "id": entry_id, "status": status,
                "where": "longterm", "ts": ts}

    def forget(self, entry_id: str, ts: Optional[str] = None) -> Dict[str, Any]:
        """遗忘 = 打 stale，不物理删：今日条目就地改 status；长期档追加 tombstone 行。"""
        entry_id = (entry_id or "").strip()
        if not entry_id:
            return _reject("entry_id 不能为空")
        return self._mark_today(entry_id, STATUS_STALE, ts or now_iso())

    def retire(self, entry_id: str, ts: Optional[str] = None) -> Dict[str, Any]:
        """退役 = stale：行还在盘上，检索侧按 status 过滤掉。可回填恢复。"""
        entry_id = (entry_id or "").strip()
        if not entry_id:
            return _reject("entry_id 不能为空")
        return self._mark_today(entry_id, STATUS_STALE, ts or now_iso())

    def _longterm_by_day(self, day: str) -> List[Dict[str, Any]]:
        """长期档里某一天降级进来的在役条目（run_night 用，幂等）。"""
        if not day:
            return []
        return [entry for entry in self.longterm_active_entries()
                if entry.get("day") == day]

    def _recent_longterm(self, now_ts: str, days: int = limits.signal_scan_days) -> List[Dict[str, Any]]:
        try:
            floor = datetime.fromisoformat(now_ts) - timedelta(days=days)
        except ValueError:
            return []
        recent: List[Dict[str, Any]] = []
        for entry in self.longterm_active_entries():
            ts = entry.get("ts")
            if not isinstance(ts, str):
                continue
            try:
                if datetime.fromisoformat(ts) >= floor:
                    recent.append(entry)
            except ValueError:
                continue
        return recent

    # —— 人名录 ————————————————————————————————
    def _self_names(self) -> set:
        """魂的自称表（display + synonyms），从魂的公开只读口取；读不到就空集 + warn。"""
        try:
            from organs import paths
            from organs.soul import load as load_soul

            try:
                root = paths.organ_dir("soul") / "data"
                store = load_soul(root)
            except Exception:  # noqa: BLE001 - 实盘没有就退回出厂默认
                root = paths.factory_dir("soul")
                store = load_soul(root)
            snapshot = store.snapshot()
            return {snapshot.identity.display} | set(snapshot.identity.synonyms)
        except Exception as exc:  # noqa: BLE001 - 读不到魂就只挡占位符，不拦其它人
            import warnings

            warnings.warn("脑·人名录：读魂的自称表失败（%s），只挡占位符" % exc, RuntimeWarning)
            return set()

    def _blocked_who(self, who: str) -> Optional[str]:
        """返回 None = 可建卡；否则返回不建卡的原因。"""
        if who in PLACEHOLDER_WHOS:
            return "placeholder"
        if who in self._self_names():
            return "self"
        return None

    def _people_data(self) -> Dict[str, Any]:
        return _read_json(self._root / PEOPLE_FILE, {"people": {}})

    def _write_people(self, data: Dict[str, Any]) -> None:
        _atomic_write_json(self._root / PEOPLE_FILE, data)

    def _person_view(self, person: Dict[str, Any], now_ts: str) -> Dict[str, Any]:
        view = dict(person)
        view["familiarity"] = people_mod.familiarity(
            person.get("interactions", {}), now_ts,
            limits.familiarity_lambda, limits.familiarity_days)
        view["total_chats"] = sum(int(n) for n in person.get("interactions", {}).values())
        return view

    def interact(self, who: str, display: Optional[str] = None,
                 ts: Optional[str] = None,
                 text: Optional[str] = None,
                 is_bot: bool = False) -> Dict[str, Any]:
        """通道消息认人：建档 / 更新别名与最近互动 / 累计次数与聊过的话题。

        只建真发过言的人；占位符与她的自称不建卡（_blocked_who 守卫）。
        """
        ts = ts or now_iso()
        who = who.strip()
        if not who:
            return {"ok": True, "skipped": True, "reason": "placeholder",
                    "who": who, "ts": ts}
        if is_bot:
            return {"ok": True, "skipped": True, "reason": "bot",
                    "who": who, "ts": ts}
        if who not in PLACEHOLDER_WHOS and not valid_who(who):
            return _reject("昵称不是身份：%s——同一个人在多个群用不同昵称，"
                           "请传平台+数字号（如 aiocqhttp:3425635473）" % who)
        blocked = self._blocked_who(who)
        if blocked:
            import warnings

            if blocked == "self":
                warnings.warn("脑·人名录：%s 是她的自称，不是通讯录里的人，不建卡" % who,
                              RuntimeWarning)
            return {"ok": True, "skipped": True, "reason": blocked,
                    "who": who, "ts": ts}
        data = self._people_data()
        people = data.setdefault("people", {})
        person = people.get(who)
        if person is None:
            person = {"id": who, "display": display or who, "aliases": [],
                      "first_seen": ts, "last_interaction": ts, "cold": True,
                      "shadow": False, "interactions": {}, "topics": [],
                      "prefs": [], "notes": []}
            people[who] = person
        else:
            # 群昵称 ≠ 真实姓名：display 用当前群昵称，见过的旧叫法进 aliases 累积。
            if display and display != person.get("display"):
                old_display = person.get("display", "")
                aliases = person.setdefault("aliases", [])
                if old_display and old_display != who and old_display not in aliases:
                    aliases.append(old_display)
                if display != who and display not in aliases:
                    aliases.append(display)
                person["display"] = display
            person["last_interaction"] = ts
            if person.get("shadow"):
                person["shadow"] = False
        day = day_key(ts)
        person["interactions"][day] = int(person["interactions"].get(day, 0)) + 1
        if text:
            topics = edges_mod.extract_topics(text or "", self._topics())
            bucket = person.setdefault("topics", [])
            for topic in topics:
                if topic not in bucket:
                    bucket.append(topic)
        total = sum(int(n) for n in person["interactions"].values())
        person["cold"] = total <= 1   # 只聊过一次 = cold：不主动找、不引用他的事
        self._write_people(data)
        return self._person_view(person, ts)

    def people_list(self, now_ts: Optional[str] = None) -> List[Dict[str, Any]]:
        now_ts = now_ts or now_iso()
        people = self._people_data().get("people", {})
        views = [self._person_view(person, now_ts) for person in people.values()]
        for view in views:
            view["memories"] = self._recent_memories_for(view.get("id", ""), now_ts, 5)
        views.sort(key=lambda item: (item["familiarity"], item.get("last_interaction", "")), reverse=True)
        return views

    def lookup(self, name: str, now_ts: Optional[str] = None) -> Optional[Dict[str, Any]]:
        now_ts = now_ts or now_iso()
        name = (name or "").strip()
        if not name:
            return None
        people = self._people_data().get("people", {})
        for person in people.values():
            if person.get("id") == name or name in person.get("aliases", []) \
                    or person.get("display") == name:
                return self._person_view(person, now_ts)
        return None

    def add_alias(self, who: str, alias: str) -> Dict[str, Any]:
        who = who.strip()
        alias = (alias or "").strip()
        if not who or not alias:
            return _reject("who 与 alias 不能为空")
        data = self._people_data()
        people = data.setdefault("people", {})
        person = people.get(who)
        if person is None:
            return _reject("人名录里没有 %s" % who)
        if alias not in person.setdefault("aliases", []) and alias != who \
                and alias != person.get("display"):
            person["aliases"].append(alias)
        self._write_people(data)
        return {"ok": True, "person": self._person_view(person, now_iso())}

    def mark_cold(self, who: str, cold: bool = True) -> Dict[str, Any]:
        who = who.strip()
        if not who:
            return _reject("who 不能为空")
        data = self._people_data()
        people = data.setdefault("people", {})
        person = people.get(who)
        if person is None:
            return _reject("人名录里没有 %s" % who)
        person["cold"] = bool(cold)
        self._write_people(data)
        return {"ok": True, "person": self._person_view(person, now_iso())}

    def merge_people(self, primary: str, alias: str) -> Dict[str, Any]:
        """换号改昵称走别名表：两条并一条，取更早认识时间，旧标识留作别名。"""
        primary = primary.strip()
        alias = alias.strip()
        if not primary or not alias:
            return _reject("primary 与 alias 不能为空")
        data = self._people_data()
        people = data.setdefault("people", {})
        keep = people.get(primary)
        drop = people.get(alias)
        if keep is None or drop is None:
            return _reject("两条都在人名录里才能合并")
        for name in [alias] + drop.get("aliases", []):
            if name and name not in keep.setdefault("aliases", []) and name != primary \
                    and name != keep.get("display"):
                keep["aliases"].append(name)
        if (drop.get("display") and drop["display"] != keep.get("display")
                and drop["display"] != alias
                and drop["display"] not in keep.setdefault("aliases", [])):
            keep["aliases"].append(drop["display"])
        keep["first_seen"] = min(keep.get("first_seen", ""), drop.get("first_seen", ""))
        keep["last_interaction"] = max(keep.get("last_interaction", ""),
                                       drop.get("last_interaction", ""))
        for day, count in drop.get("interactions", {}).items():
            keep["interactions"][day] = int(keep["interactions"].get(day, 0)) + int(count)
        keep["cold"] = bool(keep.get("cold") or drop.get("cold", False))
        keep["prefs"] = list(dict.fromkeys(keep.get("prefs", []) + drop.get("prefs", [])))
        keep["notes"] = list(dict.fromkeys(keep.get("notes", []) + drop.get("notes", [])))
        people.pop(alias, None)
        self._write_people(data)
        return {"ok": True, "person": self._person_view(keep, now_iso())}

    def clean_dirty_cards(self, backup: bool = True) -> Dict[str, Any]:
        """顺手清脏卡：备份 people.json 后删掉占位符与她自己名下的卡。只删这两类，不动真卡。"""
        path = self._root / PEOPLE_FILE
        if not path.is_file():
            return {"ok": True, "removed": [], "backup": None}
        if backup:
            import shutil

            backup_path = path.with_suffix(".json.bak")
            shutil.copyfile(path, backup_path)
        else:
            backup_path = None
        data = _read_json(path, {"people": {}})
        people = data.get("people", {})
        removable = set(PLACEHOLDER_WHOS) | self._self_names()
        removed = [key for key in list(people) if key in removable]
        for key in removed:
            people.pop(key, None)
        _atomic_write_json(path, data)
        return {"ok": True, "removed": removed,
                "backup": str(backup_path) if backup_path else None}

    # —— 名片（人可写字段；熟稔是读数，绝不收） ————————————————————————————————
    def _people_card(self, person_id: str) -> Optional[Dict[str, Any]]:
        person = self._people_data().get("people", {}).get(person_id)
        if person is None:
            return None
        return {"id": person.get("id", ""),
                "display": person.get("display", ""),
                "prefs": _as_list(person.get("prefs")),
                "notes": _as_list(person.get("notes")),
                "birthday": person.get("birthday", "") or "",
                "photo_note": person.get("photo_note", "")}

    def card_view(self, person_id: str) -> Optional[Dict[str, Any]]:
        card = self._people_card(person_id)
        if card is None:
            return None
        card["history"] = self.card_history(person_id)
        return card

    def card_history(self, person_id: str) -> List[Dict[str, Any]]:
        return [row for row in _read_jsonl(self._root / PEOPLE_CARD_HISTORY)
                if row.get("person_id") == person_id]

    def patch_card(self, person_id: str, actor: str,
                   fields: Dict[str, Any]) -> Dict[str, Any]:
        """人写名片：只收白名单字段（称呼/雷点/备注/照片说明）；不收熟稔；写进历史。"""
        person_id = (person_id or "").strip()
        actor = (actor or "").strip()
        if not person_id or not actor:
            return _reject("person_id 与 actor 不能为空")
        if not isinstance(fields, dict):
            return _reject("fields 必须是 JSON 对象")
        unknown = [key for key in fields if key not in CARD_FIELDS]
        if unknown:
            return _reject("只收白名单字段（称呼/雷点/备注/照片说明），收到：%s"
                           % "/".join(unknown))
        data = self._people_data()
        person = data.get("people", {}).get(person_id)
        if person is None:
            return _reject("人名录里没有 %s" % person_id)
        history_lines = []
        for label, key in CARD_FIELDS.items():
            if label not in fields:
                continue
            old_value = person.get(key, "")
            value = fields[label]
            if key in ("prefs", "notes") and isinstance(value, str):
                value = [ln.strip() for ln in value.split(chr(10)) if ln.strip()]
            person[key] = value
            line = {"ts": now_iso(), "actor": actor, "person_id": person_id,
                    "field": label, "old_summary": _short(old_value),
                    "new_summary": _short(fields[label])}
            _append_jsonl(self._root / PEOPLE_CARD_HISTORY, line)
            history_lines.append(line)
        self._write_people(data)
        return {"ok": True, "card": self._people_card(person_id),
                "history_lines": history_lines}

    def edit_note(self, entry_id: str, actor: str, note: str,
                  unclear: Optional[bool] = None) -> Dict[str, Any]:
        """人改 note：quote 逐字不动，note 留痕。空 note 是正常状态。"""
        entry_id = (entry_id or "").strip()
        actor = (actor or "").strip()
        if not entry_id or not actor:
            return _reject("entry_id 与 actor 不能为空")
        note = note or ""
        # 今日条目就地改；长期档追加同 id 新行（quote 逐字从最新行复制）。
        for entry in self._today.get("entries") or []:
            if entry.get("id") == entry_id:
                old_note = entry.get("note", "")
                entry["note"] = note
                entry["unclear"] = bool(unclear) if unclear is not None else entry.get("unclear", False)
                entry["beta_note"] = (int(entry.get("beta", BETA_DEFAULT)) - 1) if note else None
                _atomic_write_json(self._root / TODAY_FILE, self._today)
                _append_jsonl(self._root / NOTE_HISTORY, {
                    "ts": now_iso(), "actor": actor, "entry_id": entry_id,
                    "old_note": old_note, "new_note": note})
                return {"ok": True, "entry_id": entry_id, "where": "today",
                        "note": note, "quote": entry.get("quote", entry.get("text", ""))}
        latest = self._latest_longterm(entry_id)
        if latest is None:
            return _reject("没有这条记忆：%s" % entry_id)
        updated = dict(latest)
        old_note = updated.get("note", "")
        updated["note"] = note
        updated["unclear"] = bool(unclear) if unclear is not None else updated.get("unclear", False)
        updated["beta_note"] = (int(updated.get("beta", BETA_DEFAULT)) - 1) if note else None
        _append_jsonl(self._root / LONGTERM_FILE, updated)
        _append_jsonl(self._root / NOTE_HISTORY, {
            "ts": now_iso(), "actor": actor, "entry_id": entry_id,
            "old_note": old_note, "new_note": note})
        return {"ok": True, "entry_id": entry_id, "where": "longterm",
                "note": note, "quote": updated.get("quote", updated.get("text", ""))}

    # —— 回想 ————————————————————————————————
    def recall(self, query: str, who: Optional[str] = None,
               now_ts: Optional[str] = None,
               scene: Optional[str] = None) -> Dict[str, Any]:
        """回想：读时结算遗忘曲线 → 今日/热/长期三路字面检索 → 装包；命中回写想起次数。"""
        now_ts = now_ts or now_iso()
        self._settle_retention(now_ts)
        today = [entry for entry in self.today_entries(now_ts)
                 if entry.get("side", SIDE_OTHER) != SIDE_SELF]
        longterm = [entry for entry in self.longterm_active_entries()
                    if entry.get("side", SIDE_OTHER) != SIDE_SELF]
        hot_candidates: List[Dict[str, Any]] = []
        for row in self.hot_rows():
            if recall_mod.literal_score(query, row.get("line", "")) > 0:
                for source_id in row.get("source_ids", []):
                    found = self._latest_longterm(source_id)
                    if found and found.get("status") == STATUS_ACTIVE \
                            and found.get("side", SIDE_OTHER) != SIDE_SELF:
                        hot_candidates.append(found)
        result = recall_mod.recall(query, today, longterm,
                                   hot_entries=hot_candidates, who=who)
        self._write_back_hits(result.get("items", []), now_ts)
        self._add_topic_candidates(result.get("items", []), now_ts)
        # 出口判断用：群里不当众复述"私密"条目。只打 retellable 标记，不过滤召回。
        if scene == SCENE_GROUP:
            for item in result.get("items", []):
                if item.get("audience") == "私密":
                    item["retellable"] = False
                    item["retell_note"] = "私密：不当众复述"
        return result

    def _record_hit(self, entry_id: str, now_ts: str) -> None:
        usage = self._latest_usage(entry_id) or {}
        record = {
            "id": entry_id,
            "ts": now_ts,
            "recall_count": int(usage.get("recall_count", 0)) + 1,
            "last_recalled_at": now_ts,
            "hit_count": int(usage.get("hit_count", 0)) + 1,
        }
        self._append_usage(record)

    def _write_back_hits(self, items: Sequence[Dict[str, Any]], now_ts: str) -> None:
        """命中回写：只进 usage 账（hit_count/想起次数/最后命中时间），行本身不动。"""
        for item in items:
            source = item.get("source", "")
            if source in ("longterm", "hot"):
                self._record_hit(item.get("id", ""), now_ts)

    def vector_rerank(self, query: str,
                      candidates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """向量只做重排、永不 gate：接口转发，未接入时原样返回候选。"""
        return recall_mod.vector_rerank(query, candidates)

    # —— 自述 ————————————————————————————————
    def record_selfnarration(self, text: str, note: str = "") -> Dict[str, Any]:
        """只记录，不解释：她写的「我最近像变了个人」这类话，留版本，永不覆盖基线。"""
        text = (text or "").strip()
        if not text:
            return _reject("自述正文不能为空")
        versions = _read_jsonl(self._root / SELF_FILE)
        taken = {row.get("id", "") for row in versions}
        record = {"id": _new_id("sn-", taken), "version": len(versions) + 1,
                  "ts": now_iso(), "text": text, "note": note}
        _append_jsonl(self._root / SELF_FILE, record)
        return {"ok": True, "record": record}

    def selfnarration_latest(self) -> Optional[Dict[str, Any]]:
        versions = _read_jsonl(self._root / SELF_FILE)
        return versions[-1] if versions else None

    def selfnarration_versions(self) -> List[Dict[str, Any]]:
        return _read_jsonl(self._root / SELF_FILE)

    # —— 提案 ————————————————————————————————
    def proposals(self) -> Dict[str, Any]:
        return _read_json(self._root / PROPOSALS_FILE, {"night_of": "", "proposals": []})

    def mark_proposal(self, proposal_id: str, status: str, actor: str, ts: str,
                      reason: str = "") -> Dict[str, Any]:
        """人处理了一条提案：只在当前那份 proposals.json 里原地打终态。

        只许 accepted / rejected 两个终态；找不到 id 就如实说不写盘（不新建、不改形状）。
        其余字段逐字不动——提案正文由程序写，人只动「处理结果」这几个键。
        reason 形参是**拒绝的理由**，落盘键是 decision_reason：条目里原有的 reason 是
        「她为什么提」，那句话一个字都不许被人的处理结果盖掉。
        """
        if status not in ("accepted", "rejected"):
            raise ValueError("status 只许 accepted / rejected，实际 %r" % (status,))
        wanted = (proposal_id or "").strip()
        batch = self.proposals()
        for row in batch.get("proposals") or []:
            if isinstance(row, dict) and row.get("id") == wanted:
                row["status"] = status
                row["actor"] = actor
                row["ts"] = ts
                if status == "rejected":
                    row["decision_reason"] = reason
                _atomic_write_json(self._root / PROPOSALS_FILE, batch)
                return {"ok": True, "proposal_id": wanted, "status": status,
                        "proposal": dict(row)}
        return {"ok": False, "message": "没有这条提案：%s" % wanted}

    def run_night(self, now_ts: Optional[str] = None) -> Dict[str, Any]:
        """夜结算：换日降级 + 从刚结束的一天里提炼 0–2 条改基线提案。幂等。"""
        now_ts = now_ts or now_iso()
        current = day_key(now_ts)
        batch = self.proposals()
        self._today, archived = self._ensure_today(now_ts)
        if batch.get("night_of") == current:
            return {"ok": True, "night_of": current, "rolled_over": len(archived),
                    "proposals": batch["proposals"], "rerun": True}
        closed_day = self._today.get("last_closed_day", "")
        evidence_entries = archived if archived else self._longterm_by_day(closed_day)
        if evidence_entries:
            proposals = proposals_mod.compose_proposals(evidence_entries, now_ts=now_ts)
            note = ""
        else:
            proposals = []
            note = "没有可结算的一天，不硬凑提案"
        _atomic_write_json(self._root / PROPOSALS_FILE,
                           {"night_of": current, "proposals": proposals})
        _append_jsonl(self._root / PROPOSAL_LOG_FILE,
                      {"night_of": current, "ts": now_ts, "proposals": proposals})
        return {"ok": True, "night_of": current, "rolled_over": len(archived),
                "proposals": proposals, "note": note}

    def proposal_log(self) -> List[Dict[str, Any]]:
        return _read_jsonl(self._root / PROPOSAL_LOG_FILE)


def load(root: Path) -> BrainStore:
    return BrainStore(root)