"""心的实际写入变化；不重算状态，不补造旧历史，不进入聊天记忆。"""
from datetime import date, datetime
import json
from pathlib import Path

from organs.day import model as day_model
from . import store

GROUPS = {'mood': '心情与基线', 'energy': '精力', 'baseline': '性格基线', 'offset': '六轴偏移', 'sleep': '睡眠', 'proactive': '主动交流反馈'}


def record(root, *, at, group, reason, before, after, source=None, from_at=None, origin=None):
    changed = {key: {'before': before.get(key), 'after': value}
               for key, value in after.items() if before.get(key) != value}
    if changed:
        base = Path(root) if root is not None else store.default_root()
        store.append_jsonl(base / 'changes.jsonl', {'at': at, 'from_at': from_at, 'group': group,
                           'reason': reason, 'source': source, 'changes': changed,
                           **({'origin': origin} if origin else {})})


def read_history():
    """心页与经历时间线共用原账校验；不截断历史，也不写回文件。"""
    path = store.default_root() / 'changes.jsonl'
    rows, invalid = [], 0
    if path.exists():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                at = day_model.parse_ts(row['at']).astimezone(day_model.local_tz())
                if (row['group'] not in GROUPS or not isinstance(row['changes'], dict)
                        or not isinstance(row.get('reason'), str)
                        or not all(isinstance(c, dict) and 'before' in c and 'after' in c for c in row['changes'].values())):
                    raise ValueError('未知记录')
            except (ValueError, KeyError, TypeError):
                invalid += 1
                continue
            rows.append(dict(row, at=at.isoformat(), source_line=number))
    return {'rows': rows, 'invalid_records': invalid, 'available': path.exists()}


def view(params):
    selected = params.get('date') or datetime.now(day_model.local_tz()).date().isoformat()
    group = params.get('group', 'all')
    limit, offset = int(params.get('limit', '20')), int(params.get('offset', '0'))
    if date.fromisoformat(selected).isoformat() != selected or group not in ('all', *GROUPS) or not 1 <= limit <= 100 or offset < 0:
        raise ValueError('请检查日期、参数类别和页码')
    history = read_history()
    rows = [row for row in history['rows'] if row['at'][:10] == selected and group in ('all', row['group'])]
    rows.sort(key=lambda row: (row['at'], row['source_line']), reverse=True)
    return {'ok': True, 'readonly': True, 'date': selected, 'group': group, 'groups': GROUPS, 'rows': rows[offset:offset+limit],
            'total': len(rows), 'offset': offset, 'limit': limit, 'has_older': len(rows)>offset+limit,
            'invalid_records': history['invalid_records'], 'available': history['available'], 'timezone': str(day_model.local_tz())}
