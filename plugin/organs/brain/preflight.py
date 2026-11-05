"""接库前只读 preflight：schema 版本、坏行、悬空边、字段缺失。只报不修。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from . import edges as edges_mod

JSON_FILES = ("today.json", "people.json", "proposals.json", "tiers.json",
              "retention.json", "topics.json", "hot.json", "edges.json",
              "topic_candidates.json")
JSONL_FILES = ("longterm.jsonl", "selfnarration.jsonl", "proposal_log.jsonl",
               "evictions.jsonl", "usage.jsonl", "people_card_history.jsonl")


def _issue(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "level": "error", "message": message}


def preflight(root: Path) -> Dict[str, Any]:
    root = Path(root)
    issues: List[Dict[str, str]] = []
    for name in JSON_FILES:
        path = root / name
        if not path.is_file():
            issues.append(_issue("FILE_MISSING", "%s 缺失" % name))
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            issues.append(_issue("FILE_UNREADABLE", "%s 读不到：%s" % (name, exc)))
            continue
        except json.JSONDecodeError as exc:
            issues.append(_issue("JSON_BAD", "%s 不是合法 JSON：%s" % (name, exc)))
            continue
        if name == "edges.json":
            if not isinstance(data, dict) or "edges" not in data or "nodes" not in data:
                issues.append(_issue("EDGES_SHAPE", "edges.json 缺 nodes/edges"))
    for name in JSONL_FILES:
        path = root / name
        if not path.is_file():
            issues.append(_issue("FILE_MISSING", "%s 缺失" % name))
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            issues.append(_issue("FILE_UNREADABLE", "%s 读不到：%s" % (name, exc)))
            continue
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                issues.append(_issue("JSONL_BAD", "%s 第 %d 行不是合法 JSON" % (name, number)))
                break
            if not isinstance(row, dict):
                issues.append(_issue("JSONL_BAD", "%s 第 %d 行不是对象" % (name, number)))
                break
    # 悬空边：活跃边的端点必须在 nodes 里
    edges_path = root / "edges.json"
    if edges_path.is_file():
        try:
            graph = json.loads(edges_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            graph = {}
        nodes = set((graph or {}).get("nodes", {}).keys())
        for edge in (graph or {}).get("edges", []):
            if edge.get("status") != "active":
                continue
            for endpoint in (edge.get("from"), edge.get("to")):
                if endpoint and endpoint not in nodes:
                    issues.append(_issue(
                        "EDGE_DANGLING",
                        "边 %s 的端点 %s 不在 nodes 里（悬空边）" % (edge.get("id"), endpoint)))
    # 字段缺失：长期档每行必须带 id/text/status/source_type
    longterm = root / "longterm.jsonl"
    if longterm.is_file():
        try:
            lines = longterm.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            for field in ("id", "text", "status", "source_type"):
                if field not in row:
                    issues.append(_issue(
                        "FIELD_MISSING",
                        "longterm.jsonl 第 %d 行缺字段 %s" % (number, field)))
    return {"ok": not issues, "issues": issues}
