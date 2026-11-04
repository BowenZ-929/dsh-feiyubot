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

from . import people as people_mod
from . import proposals as proposals_mod
from . import recall as recall_mod
from .model import (ALPHA_DEFAULT, BETA_DEFAULT, SOURCE_LIVED,
                    SOURCE_SCHEDULED, STATUS_ACTIVE, STATUS_STALE,
                    day_key, limits, now_iso)

TODAY_FILE = "today.json"
LONGTERM_FILE = "longterm.jsonl"
PEOPLE_FILE = "people.json"
SELF_FILE = "selfnarration.jsonl"
PROPOSALS_FILE = "proposals.json"
PROPOSAL_LOG_FILE = "proposal_log.jsonl"


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
    row["source_type"] = source_type if source_type in (SOURCE_LIVED, SOURCE_SCHEDULED) else SOURCE_LIVED
    row["alpha"] = row.get("alpha", ALPHA_DEFAULT)
    row["beta"] = row.get("beta", BETA_DEFAULT)
    if "evidence_key" not in row:
        # 亲历的原话出处就是这一行自己；日程模拟没有真实出处，留空 → 无记录背书
        row["evidence_key"] = row["id"] if row.get("source_type") == SOURCE_LIVED else ""
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
        for name in (TODAY_FILE, PEOPLE_FILE, PROPOSALS_FILE):
            path = self._root / name
            if path.is_file():
                continue
            default: Dict[str, Any] = {"day": "", "entries": [], "last_closed_day": ""} if name == TODAY_FILE else \
                ({"people": {}} if name == PEOPLE_FILE else {"night_of": "", "proposals": []})
            _atomic_write_json(path, default)
        for name in (LONGTERM_FILE, SELF_FILE, PROPOSAL_LOG_FILE):
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
        """今日在役条目：status 进查询条件，forgotten 的不给。"""
        now_ts = now_ts or now_iso()
        self._ensure_today(now_ts)
        return [entry for entry in (self._today.get("entries") or [])
                if entry.get("status", STATUS_ACTIVE) == STATUS_ACTIVE]

    def today_view(self, now_ts: Optional[str] = None) -> Dict[str, Any]:
        now_ts = now_ts or now_iso()
        self._ensure_today(now_ts)
        return {"day": self._today.get("day"), "entries": self.today_entries(now_ts)}

    def _taken_ids(self) -> set:
        taken = {entry.get("id", "") for entry in (self._today.get("entries") or [])}
        for row in _read_jsonl(self._root / LONGTERM_FILE):
            if row.get("id"):
                taken.add(row["id"])
        return taken

    def ingest(self, who: str, text: str, mood: Optional[Any] = None,
               display: Optional[str] = None, ts: Optional[str] = None,
               source_type: str = SOURCE_LIVED, alpha: int = ALPHA_DEFAULT,
               beta: int = BETA_DEFAULT) -> Dict[str, Any]:
        """一条消息进：今日追加或合并一条 + 人名录认人更新。

        source_type 只许 亲历 / 日程模拟；日程模拟只写她自己、不给证据出处。
        """
        ts = ts or now_iso()
        who = (who or "").strip()
        text = (text or "").strip()
        if not who or not text:
            return _reject("who 与 text 不能为空")
        self._today, archived = self._ensure_today(ts)
        entries = self._today.setdefault("entries", [])
        merged = False
        if entries:
            last = entries[-1]
            if last.get("who") == who and _within_merge_window(last, ts):
                last["text"] = text
                last["last_ts"] = ts
                last["count"] = int(last.get("count", 1)) + 1
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
                "count": 1,
                "status": STATUS_ACTIVE,   # 状态位和行同时落盘
                "source_type": source_type if source_type in (SOURCE_LIVED, SOURCE_SCHEDULED) else SOURCE_LIVED,
                "alpha": alpha,
                "beta": beta,
                "evidence_key": "",   # 创建后再补：亲历指向自己这行原话，日程模拟留空
            }
            if mood is not None:
                entry["mood"] = mood
            entries.append(entry)
            entry["evidence_key"] = entry["id"] if entry["source_type"] == SOURCE_LIVED else ""
        _atomic_write_json(self._root / TODAY_FILE, self._today)
        person = self.interact(who, display=display, ts=ts)
        return {"ok": True, "merged": merged, "entry": dict(entries[-1]),
                "archived_count": len(archived), "person": person}

    # —— 长期字面档 ————————————————————————————
    def _append_longterm(self, records: Sequence[Dict[str, Any]]) -> None:
        for record in records:
            _append_jsonl(self._root / LONGTERM_FILE, record)

    def longterm_entries(self) -> List[Dict[str, Any]]:
        """长期档全部行（含 tombstone，审计用）。"""
        return _read_jsonl(self._root / LONGTERM_FILE)

    def longterm_active_entries(self) -> List[Dict[str, Any]]:
        """长期档在役条目：同 id 只认最后一行（tombstone 覆盖旧行），status 进查询条件。"""
        rows = _read_jsonl(self._root / LONGTERM_FILE)
        latest: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            row_id = row.get("id")
            if row_id:
                latest[row_id] = row
        return [row for row in latest.values()
                if row.get("status", STATUS_ACTIVE) == STATUS_ACTIVE]

    def import_rows(self, rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """数据导入器：旧存量行（缺 status/来源/α/β/evidence_key）归一化后追加进长期档。"""
        imported = 0
        for row in rows:
            if not isinstance(row, dict) or not row.get("text"):
                continue
            entry_id = row.get("id") or _new_id("t-", self._taken_ids())
            normalized = _normalize_row(row, entry_id=entry_id)
            if not normalized.get("ts"):
                normalized["ts"] = now_iso()
            if "day" not in normalized or not normalized.get("day"):
                normalized["day"] = day_key(normalized["ts"])
            _append_jsonl(self._root / LONGTERM_FILE, normalized)
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
                return {"ok": True, "id": entry_id, "status": status,
                        "where": "today", "ts": ts}
        _append_jsonl(self._root / LONGTERM_FILE, {
            "id": entry_id, "status": status, "ts": ts,
            "who": "", "text": "", "count": 0, "source": "status", "day": "",
        })
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
    def _people_data(self) -> Dict[str, Any]:
        return _read_json(self._root / PEOPLE_FILE, {"people": {}})

    def _write_people(self, data: Dict[str, Any]) -> None:
        _atomic_write_json(self._root / PEOPLE_FILE, data)

    def _person_view(self, person: Dict[str, Any], now_ts: str) -> Dict[str, Any]:
        view = dict(person)
        view["familiarity"] = people_mod.familiarity(
            person.get("interactions", {}), now_ts,
            limits.familiarity_lambda, limits.familiarity_days)
        return view

    def interact(self, who: str, display: Optional[str] = None,
                 ts: Optional[str] = None) -> Dict[str, Any]:
        ts = ts or now_iso()
        who = who.strip()
        if not who:
            return _reject("who 不能为空")
        data = self._people_data()
        people = data.setdefault("people", {})
        person = people.get(who)
        if person is None:
            person = {"id": who, "display": display or who, "aliases": [],
                      "first_seen": ts, "last_interaction": ts, "cold": False,
                      "shadow": False, "interactions": {}, "prefs": [], "notes": []}
            people[who] = person
        else:
            if display:
                person["display"] = display
            person["last_interaction"] = ts
            if person.get("shadow"):
                person["shadow"] = False
        day = day_key(ts)
        person["interactions"][day] = int(person["interactions"].get(day, 0)) + 1
        self._write_people(data)
        return self._person_view(person, ts)

    def people_list(self, now_ts: Optional[str] = None) -> List[Dict[str, Any]]:
        now_ts = now_ts or now_iso()
        people = self._people_data().get("people", {})
        views = [self._person_view(person, now_ts) for person in people.values()]
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

    # —— 回想 ————————————————————————————————
    def recall(self, query: str, who: Optional[str] = None,
               now_ts: Optional[str] = None) -> Dict[str, Any]:
        """回想：只用 status 在役的条目；装包（分桶配额 + 预算 + 每项 cap）在 recall_mod 里。"""
        now_ts = now_ts or now_iso()
        return recall_mod.recall(query, self.today_entries(now_ts),
                                 self.longterm_active_entries(), who=who)

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