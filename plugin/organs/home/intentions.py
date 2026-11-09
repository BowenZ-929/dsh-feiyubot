"""自己的持续打算，复用工作台现状卡；模型没有通用写卡权限。"""
from uuid import uuid4
from . import cards, store

BY = '日子线·个人打算'


def _owned(root):
    rows, _ = cards._read_room_rows(store.home_root(root), '工作台')
    return [c for c in cards.fold(rows) if c.get('by') == BY and c.get('kind') == '物件'
            and c.get('params', {}).get('self') is True
            and c['params'].get('source') == 'autonomous_growth']


def listing(root=None):
    return [dict(card_id=c['card_id'], name=c['params']['title'], note=c['note'], updated_at=c.get('at'),
                 **{k: c['params'][k] for k in ('next_step', 'status', 'sources')}) for c in _owned(root)]


def apply(command, *, root=None, sources=()):
    if not isinstance(command, dict) or not set(command) <= {'card_id', 'name', 'note', 'next_step', 'status'}:
        raise ValueError('个人打算参数不匹配')
    for key, value in command.items():
        if not isinstance(value, str) or len(value) > (80 if key == 'name' else 600):
            raise ValueError('打算字段需要短文本')
    own = _owned(root)
    cid = command.get('card_id')
    target = next((c for c in own if c['card_id'] == cid), None)
    if cid and target is None:
        raise ValueError('只能更新程序确认属于她自己的打算')
    status = command.get('status', target['params']['status'] if target else 'active')
    if status not in ('active', 'paused', 'abandoned'):
        raise ValueError('完成必须由实际产出回执确认，不能自报 done')
    if status == 'active' and (not target or target['params']['status'] != 'active'):
        if sum(c['params']['status'] == 'active' for c in own) >= 3:
            raise ValueError('已有三项进行中的打算，可以先暂停或放弃一项')
    if target:
        card = dict(target, params=dict(target['params']))
        if 'name' in command:
            raise ValueError('更新打算保留原名字，补充内容写在 note 或 next_step')
        card['note'] = command.get('note', card['note'])
        card['params'].update(status=command.get('status', card['params']['status']),
                              next_step=command.get('next_step', card['params']['next_step']))
        card.pop('at', None)
    else:
        name = command.get('name', '').strip()
        if not name:
            raise ValueError('新打算需要具体名字')
        card = {'room': '工作台', 'kind': '物件', 'name': name + '-' + uuid4().hex[:10],
                'note': command.get('note', ''), 'params': {'self': True, 'owner': '她自己',
                'source': 'autonomous_growth', 'scope': 'public', 'title': name, 'current': True,
                'next_step': command.get('next_step', ''), 'status': status, 'sources': list(sources)}}
    written = cards.append_cards(root, role='organ', by=BY, cards=[card])['cards'][0]
    return next(c for c in listing(root) if c['card_id'] == written['card_id'])


def finish(card_id, evidence_id, *, root=None):
    """仅执行器调用，依据工作台的实际产出；模型不能提交伪回执。"""
    own = next((c for c in _owned(root) if c['card_id'] == card_id), None)
    rows, _ = cards._read_room_rows(store.home_root(root), '工作台')
    proof = next((r for r in rows if r['card_id'] == evidence_id
                  and r.get('params', {}).get('intention_id') == card_id
                  and (r.get('by') == '日子线·自主写作' or
                       (r.get('by') == '日子线·自主活动' and r['params'].get('action') == 'paint'
                        and r['params'].get('status') == 'done'))), None)
    if own is None or proof is None:
        raise ValueError('没有属于此打算的实际产出回执')
    if own['params']['status'] != 'active':
        return next(c for c in listing(root) if c['card_id'] == card_id)
    own = dict(own, params=dict(own['params'], status='done', sources=own['params']['sources'] + [evidence_id]))
    own.pop('at', None)
    cards.append_cards(root, role='organ', by=BY, cards=[own])
    return next(c for c in listing(root) if c['card_id'] == card_id)
