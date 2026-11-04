"""边：星图的地基。写记忆时顺手记 人—事 / 事—话题 / 人—话题 三类边，每条带时间与权重。

记忆被退役/落冷仓时边不许悬空：要么一起消失，要么标失效——这里选标失效。
结构边守卫：事—话题（结构边）在活跃边里占比低于 5% 就报错（参考实现里边类型会悄悄塌）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

EDGE_TYPES = ("person-event", "event-topic", "person-topic")
STRUCTURAL_EDGE = "event-topic"
STRUCTURAL_MIN_RATIO = 0.05


def load_graph(root: Path) -> Dict[str, Any]:
    path = Path(root) / "edges.json"
    if not path.is_file():
        return {"nodes": {}, "edges": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"nodes": {}, "edges": []}
    if not isinstance(data, dict):
        return {"nodes": {}, "edges": []}
    data.setdefault("nodes", {})
    data.setdefault("edges", [])
    return data


def save_graph(root: Path, graph: Dict[str, Any]) -> None:
    path = Path(root) / "edges.json"
    text = json.dumps(graph, ensure_ascii=False, indent=2) + "\n"
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def extract_topics(text: str, topics: Dict[str, List[str]]) -> List[str]:
    text = text or ""
    found = []
    for label, keywords in topics.items():
        if any(keyword in text for keyword in keywords):
            found.append(label)
    return found


def _node_key(kind: str, label: str) -> str:
    return "%s:%s" % (kind, label)


def _edge_id(edge_type: str, from_node: str, to_node: str, ts: str) -> str:
    return "%s:%s:%s:%s" % (edge_type, from_node, to_node, ts)


def add_event_edges(graph: Dict[str, Any], event_id: str, who: str,
                    topics: Sequence[str], ts: str, weight: float = 1.0) -> Dict[str, Any]:
    """写一条记忆时顺手记三类边。event 节点 = 记忆 id。"""
    nodes = graph.setdefault("nodes", {})
    edges = graph.setdefault("edges", [])

    def ensure_node(kind: str, label: str) -> str:
        key = _node_key(kind, label)
        nodes.setdefault(key, {"id": key, "kind": kind, "label": label})
        return key

    person_node = ensure_node("person", who)
    event_node = ensure_node("event", event_id)

    added = []
    # 人—事
    eid = _edge_id("person-event", person_node, event_node, ts)
    if not any(e.get("id") == eid for e in edges):
        edges.append({"id": eid, "type": "person-event", "from": person_node,
                      "to": event_node, "ts": ts, "weight": weight, "status": "active"})
        added.append(eid)

    for topic in topics:
        topic_node = ensure_node("topic", topic)
        # 事—话题
        eid = _edge_id("event-topic", event_node, topic_node, ts)
        if not any(e.get("id") == eid for e in edges):
            edges.append({"id": eid, "type": "event-topic", "from": event_node,
                          "to": topic_node, "ts": ts, "weight": weight, "status": "active"})
            added.append(eid)
        # 人—话题
        eid = _edge_id("person-topic", person_node, topic_node, ts)
        if not any(e.get("id") == eid for e in edges):
            edges.append({"id": eid, "type": "person-topic", "from": person_node,
                          "to": topic_node, "ts": ts, "weight": weight, "status": "active"})
            added.append(eid)
    return {"added": added}


def neighbors(graph: Dict[str, Any], node_id: str,
              active_only: bool = True) -> List[Dict[str, Any]]:
    out = []
    for edge in graph.get("edges", []):
        if active_only and edge.get("status") != "active":
            continue
        if edge.get("from") == node_id or edge.get("to") == node_id:
            other = edge.get("to") if edge.get("from") == node_id else edge.get("from")
            node = graph.get("nodes", {}).get(other, {})
            out.append({"node": other, "label": node.get("label", other),
                        "kind": node.get("kind", ""), "edge_type": edge.get("type"),
                        "ts": edge.get("ts"), "weight": edge.get("weight"),
                        "status": edge.get("status")})
    out.sort(key=lambda item: (item.get("ts", ""), item.get("node", "")))
    return out


def invalidate_edges(graph: Dict[str, Any], node_id: str) -> int:
    changed = 0
    for edge in graph.get("edges", []):
        if edge.get("status") != "active":
            continue
        if edge.get("from") == node_id or edge.get("to") == node_id:
            edge["status"] = "invalid"
            changed += 1
    return changed


def edge_health(graph: Dict[str, Any]) -> Dict[str, Any]:
    active = [e for e in graph.get("edges", []) if e.get("status") == "active"]
    by_type: Dict[str, int] = {}
    for edge in active:
        edge_type = edge.get("type", "other")
        by_type[edge_type] = by_type.get(edge_type, 0) + 1
    total = len(active)
    structural = by_type.get(STRUCTURAL_EDGE, 0)
    ratio = (structural / total) if total else 1.0
    return {"total": total, "by_type": by_type,
            "structural_edge": STRUCTURAL_EDGE, "structural_ratio": round(ratio, 4),
            "ok": total == 0 or ratio >= STRUCTURAL_MIN_RATIO}
