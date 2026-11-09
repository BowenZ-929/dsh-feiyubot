"""本人明确的相处偏好；按人和会话保存来源及替换历史，不写全局性格。"""
import uuid
from .store import _append_jsonl, _read_jsonl_strict

FILE = 'interaction_preferences.jsonl'


def history(root, who):
    if not (root / FILE).exists():
        return []
    return [row for row in _read_jsonl_strict(root / FILE) if row['who'] == who]


def current(root, who, session_id):
    rows = [row for row in history(root, who) if row['session_id'] == session_id]
    replaced = {key for row in rows for key in row['replaces']}
    return [row for row in rows if row['text'] and row['id'] not in replaced]


def record(root, *, who, session_id, change, at, turn_id='', entry_ids=(), actor='conversation'):
    row = dict(change, id=uuid.uuid4().hex, who=who, session_id=session_id,
               at=at, turn_id=turn_id, entry_ids=list(entry_ids), actor=actor)
    _append_jsonl(root / FILE, row)
    return row
