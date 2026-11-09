"""实际休息阶段。时钟和关系判断由入口提供，读状态不会补造睡眠。"""
from datetime import timedelta
from pathlib import Path
from threading import RLock

from . import changes, model, store

HOLD_MINUTES = 15
PHASES = {'awake': '醒着', 'asleep': '睡着', 'holding': '被叫醒后硬撑', 'deep': '彻底睡着'}
_LOCK = RLock()  # 日子线程与聊天入口都会推进同一份状态。


def state(root=None):
    base = Path(root) if root is not None else store.default_root()
    data = store.read_json(base / 'rest.json', {})
    if not data:
        return {'phase': 'untracked'}
    if data.get('phase') not in PHASES:
        raise ValueError('休息阶段不正确')
    return data


def is_resting(root=None):
    phase = state(root)['phase']
    return None if phase == 'untracked' else phase != 'awake'


def advance(root=None, now=None, *, can_wake=False, notice_who=None):
    from . import energy, sleep
    at = model.parse_ts(now or model.now_iso()).astimezone(model._local_tz())
    stamp = model.iso(at)
    base = Path(root) if root is not None else store.default_root()
    with _LOCK:
        while True:
            previous = state(base)
            phase = previous['phase']
            data = dict(previous)
            terms = sleep.overview(base, stamp)
            window = terms['effective_window']
            in_window = window[0] <= at.hour < window[1] if window[0] < window[1] else at.hour >= window[0] or at.hour < window[1]
            anchor = at.replace(hour=window[0], minute=0, second=0, microsecond=0)
            if anchor > at:
                anchor -= timedelta(days=1)
            transition = at
            if phase == 'untracked':
                data = {'phase': 'awake', 'since': stamp, 'wake_at': None, 'hold_until': None, 'awake_minutes': 0.0}
            if phase in ('awake', 'untracked') and in_window and (phase == 'untracked' or anchor >= model.parse_ts(data['since'])):
                end = at.replace(hour=window[1], minute=0, second=0, microsecond=0)
                if end <= at:
                    end += timedelta(days=1)
                data.update(phase='asleep', wake_at=model.iso(end), hold_until=None, awake_minutes=0.0, notice_targets=[])
            elif phase in ('asleep', 'holding', 'deep'):
                wake = model.parse_ts(data['wake_at'])
                hold = model.parse_ts(data['hold_until']) if phase == 'holding' else None
                if hold is not None and hold < wake and at >= hold:
                    transition = hold
                    data.update(phase='deep', hold_until=None)
                elif at >= wake:
                    transition = wake
                    data.update(phase='awake', hold_until=None, wake_at=None, notice_targets=[])
                elif phase == 'holding':
                    current = energy.read(base, stamp, sleep_window=window, wake_cap=terms['wake_cap'])
                    if current['value'] <= 0:
                        data.update(phase='deep', hold_until=None)
                elif phase == 'asleep' and can_wake:
                    data.update(phase='holding', hold_until=model.iso(at + timedelta(minutes=HOLD_MINUTES)))
            if data['phase'] == 'holding' and can_wake and notice_who:
                targets = data.get('notice_targets', [])
                if notice_who not in targets:
                    data['notice_targets'] = targets + [notice_who]
            if data['phase'] == previous['phase']:
                if data != previous:
                    store.write_json(base/'rest.json', data)
                return data
            # 已保存的截止点在重启后仍生效；没有记录过的离线时段不补造入睡。
            changed_at = model.iso(transition)
            energy.read(base, changed_at, sleep_window=window, wake_cap=terms['wake_cap'], force=True)
            if previous['phase'] == 'holding':
                data['awake_minutes'] += max(0, (transition-model.parse_ts(previous['since'])).total_seconds()/60)
            if data['phase'] == 'asleep':
                sleep.record('asleep', base, changed_at)
            elif data['phase'] == 'awake' and previous['phase'] in ('asleep', 'holding', 'deep'):
                sleep.record('awake', base, changed_at, awake_minutes=data['awake_minutes'])
            data['since'] = changed_at
            store.write_json(base/'rest.json', data)
            if data['phase'] in ('asleep', 'deep'):
                from . import pending
                pending.settle('rest', root=base, now=changed_at)
            changes.record(base, at=changed_at, group='sleep', reason=PHASES[data['phase']], source='rest',
                           before={'实际阶段': PHASES.get(previous['phase'], '尚未开始记录')},
                           after={'实际阶段': PHASES[data['phase']]})


def take_notice_targets(root=None, *, who=None):
    """定时告别每人只尝试一次；无回执时不循环重发，下条入站私聊仍可提醒。"""
    base = Path(root) if root is not None else store.default_root()
    with _LOCK:
        data = state(base)
        targets = data.get('notice_targets', []) if data['phase'] == 'deep' else []
        if who is not None:
            targets = [target for target in targets if target == who]
        if targets:
            store.write_json(base/'rest.json', dict(data,
                notice_targets=[target for target in data['notice_targets'] if target not in targets]))
        return targets


def notice_sent(who, wake_at, root=None, *, mark=False):
    """按实际发送回执去重；下一次睡眠的 wake_at 不同，会重新提示。"""
    base = Path(root) if root is not None else store.default_root()
    path = base / 'sleep_notices.json'
    with _LOCK:
        data = store.read_json(path, {})
        sent = data.get(who) == wake_at
        if mark:
            data[who] = wake_at
            store.write_json(path, data)
        return sent
