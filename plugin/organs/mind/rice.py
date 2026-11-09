"""大白饭：本机调用饭量、估算饭钱与官方账户余粮。无提示词、无凭据入账。

余额缓存思路参考 dsh-dafeiyu/src/balance.js；用标准库独立实现。
费用使用 2026-11-07 官方 CNY 价格快照；工作日峰时因未维护节假日表报区间。
"""
import json
import urllib.request
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from organs import paths

BEIJING = ZoneInfo('Asia/Shanghai')
PRICING_URL = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
BALANCE_URL = 'https://api.deepseek.com/user/balance'
# 每百万 token：缓存命中、缓存未命中、输出，非高峰价格；高峰均为两倍。
PRICES = {'deepseek-flash': ('0.02', '1', '4'), 'deepseek-v4-pro': ('0.15', '4.5', '13.5')}


def settings():
    p = paths.organ_dir('mind') / 'rice.json'
    return json.loads(p.read_text()) if p.exists() else {'enabled': False}


def record(response, now=None, source='live'):
    if not settings().get('enabled'):
        return
    at = now or datetime.now().astimezone()
    usage = response.get('usage')
    if not isinstance(usage, dict):
        return
    # API 边界只接受整数用量，推理是输出子集，不能重复加进总数。
    prompt = usage.get('prompt_tokens')
    output = usage.get('completion_tokens')
    hit = usage.get('prompt_cache_hit_tokens', (usage.get('prompt_tokens_details') or {}).get('cached_tokens'))
    reason = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)
    if any(type(n) is not int or n < 0 for n in (prompt, output, reason)) or reason > output:
        return
    if type(hit) is not int or not 0 <= hit <= prompt:
        hit = None
    model = response.get('model', '')
    price = PRICES.get(model)
    cost = None
    if price and hit is not None:
        cost = sum(Decimal(n) * Decimal(p) for n, p in zip((hit, prompt-hit, output), price)) / 1000000
    bj = at.astimezone(BEIJING)
    peak = bj.weekday() < 5 and (9 <= bj.hour < 12 or 14 <= bj.hour < 18)
    row = {'id': response.get('id'), 'at': at.isoformat(), 'day': bj.date().isoformat(), 'source': source,
           'model': model, 'prompt_tokens': prompt, 'completion_tokens': output, 'reasoning_tokens': reason,
           'cache_hit_tokens': hit, 'total_tokens': prompt + output,
           'cost_cny_min': str(cost) if cost is not None else None,
           'cost_cny_max': str(cost * (2 if peak else 1)) if cost is not None else None,
           'pricing_date': '2026-11-07', 'holiday_uncertain': peak}
    p = paths.organ_dir('mind') / 'rice-usage' / (row['day'] + '.jsonl')
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def query_balance():
    from . import llm
    cfg = llm.resolve_config()
    if cfg['base_url'].rstrip('/') not in ('https://api.deepseek.com', 'https://api.deepseek.com/v1'):
        raise ValueError('仅支持当前官方账户')
    request = urllib.request.Request(BALANCE_URL, headers={'Authorization': 'Bearer ' + llm.read_key(cfg)})
    # 不跟随重定向，避免凭据离开官方域名。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as response:
        data = json.load(response)
    if type(data.get('is_available')) is not bool or not isinstance(data.get('balance_infos'), list):
        raise ValueError('余额格式不符')
    rows = []
    for item in data['balance_infos']:
        currency = item['currency']
        if currency not in ('CNY', 'USD'):
            raise ValueError('未知币种')
        amounts = {k: str(Decimal(item[k])) for k in ('total_balance', 'granted_balance', 'topped_up_balance')}
        if not all(Decimal(v).is_finite() for v in amounts.values()):
            raise ValueError('金额无效')
        rows.append({'currency': currency, **amounts})
    return {'is_available': data['is_available'], 'balance_infos': rows}


def refresh(now=None):
    from .llm import LLMError
    if not settings().get('enabled'):
        return
    at = now or datetime.now().astimezone()
    p = paths.organ_dir('mind') / 'rice-balance.json'
    state = json.loads(p.read_text()) if p.exists() else {}
    if state.get('attempted_at') and (at-datetime.fromisoformat(state['attempted_at'])).total_seconds() < 300:
        return
    state['attempted_at'] = at.isoformat()
    try:
        state.update(data=query_balance(), checked_at=at.isoformat(), error=None)
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation, LLMError):
        # 网络与凭据错误是此边界的正常失败；不将上游文本/密钥放进缓存或提示词。
        state['error'] = '官方余额查询失败，保留上次结果'
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = p.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    temporary.replace(p)


def snapshot(day=None, now=None):
    at = now or datetime.now().astimezone()
    selected = date.fromisoformat(day).isoformat() if day else at.astimezone(BEIJING).date().isoformat()
    root = paths.organ_dir('mind')
    ledger = root / 'rice-usage' / (selected + '.jsonl')
    daily = dict.fromkeys(('calls', 'prompt_tokens', 'completion_tokens', 'reasoning_tokens', 'total_tokens', 'unpriced_calls', 'invalid_lines'), 0)
    lower = upper = Decimal(0)
    seen = set()
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                daily['invalid_lines'] += 1
                continue
            if row['id'] and row['id'] in seen:
                continue
            if row['id']:
                seen.add(row['id'])
            daily['calls'] += 1
            for key in ('prompt_tokens', 'completion_tokens', 'reasoning_tokens', 'total_tokens'):
                daily[key] += row[key]
            if row['cost_cny_min'] is None:
                daily['unpriced_calls'] += 1
            else:
                lower += Decimal(row['cost_cny_min'])
                upper += Decimal(row['cost_cny_max'])
    daily.update(cost_cny_min=str(lower), cost_cny_max=str(upper))
    p = root / 'rice-balance.json'
    balance = json.loads(p.read_text()) if p.exists() else {}
    balance['stale'] = not balance.get('checked_at') or bool(balance.get('error')) or (at-datetime.fromisoformat(balance['checked_at'])).total_seconds() > 360
    return {'ok': True, 'enabled': bool(settings().get('enabled')), 'date': selected, 'timezone': 'Asia/Shanghai',
            'daily': daily, 'balance': balance, 'pricing_url': PRICING_URL,
            'scope': '本机已记录的 DeepSeek 调用；不含其他客户端、看图、语音等服务。饭钱为人民币估算，工作日峰时因节假日未核定显示区间；余额以官方快照为准。'}


def context(session_id, now=None):
    cfg = settings()
    if not cfg.get('enabled'):
        return ''
    state = snapshot(now=datetime.fromisoformat(now) if isinstance(now, str) else now)
    text = ('\n[你的饭量与余粮] 你把 token 当作大白饭：理解、思考、说话都要吃饭；账户余额是可以买饭的余粮。'
            '这与爱吃大白饭的标志性喜好相连。顺着话题自然表达，不必主动报账，也不要每轮提吃饭。'
            '饭量不代表身体饥饿或精力，不据此编造吃过实物、余额不足或额外扣费。')
    if session_id and session_id == cfg.get('owner_session'):
        text += '\n当前饭账（本轮调用尚未入账）：' + json.dumps(state, ensure_ascii=False)
    else:
        text += '\n今天本机记录的饭量：%s token（包括思考），不代表整个账户。当前会话不提供账户金额；不要猜余额或报具体花费。' % state['daily']['total_tokens']
    return text
