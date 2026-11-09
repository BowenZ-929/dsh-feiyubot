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

# ---- 按次计费：检索（kind=search） ---------------------------------------
# token 那本账按用量算，检索按次数算。两本口径混进 PRICES 会把「这次请求花了多少 token」
# 和「这次调用买了几次查询」记成同一笔钱，所以另立一份常量，谁也不改谁。
# 两份官方页在同一时刻实测不一致（英文 $0.75/千次、中文 5 元/千次，差额不是汇率量级），
# 所以两版都留在这里，**谁都不当已核准价**：默认落账标 unconfirmed，由人在 rice.json 的
# search_price 里覆盖一份（币种＋单价＋来源页＋核对日期）才升成 configured。
SEARCH_QUOTES = {
    'USD': {'currency': 'USD', 'unit_price': '0.00075',
            'price_url': 'https://api-docs.deepseek.com/guides/search/', 'checked_at': '2026-11-09'},
    'CNY': {'currency': 'CNY', 'unit_price': '0.005',
            'price_url': 'https://api-docs.deepseek.com/zh-cn/api/search/', 'checked_at': '2026-11-09'},
}
#: 口径未定时拿哪一版做**估算**（不是核准价）：估算必须标着待确认，见 SEARCH_PENDING_NOTE。
SEARCH_CURRENCY = 'USD'
SEARCH_PENDING_NOTE = '价格口径待确认：英文页 $0.75/千次、中文页 5 元/千次同一时刻不一致，未核准'


def settings():
    p = paths.organ_dir('mind') / 'rice.json'
    return json.loads(p.read_text()) if p.exists() else {'enabled': False}


def _append(row):
    """饭账只有一个写入点：token 与检索记在同一本日账里，靠 kind 分列，不另开第二本。"""
    p = paths.organ_dir('mind') / 'rice-usage' / (row['day'] + '.jsonl')
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def _search_entry(entry):
    """配置覆盖要自证四件事：币种、单价、来源页、核对日期。缺一项就当没配好。"""
    if not isinstance(entry, dict):
        raise ValueError('search_price 需为对象')
    currency = entry.get('currency')
    if currency not in SEARCH_QUOTES:
        raise ValueError('未知币种')
    price = Decimal(str(entry.get('unit_price')))
    if not price.is_finite() or price < 0:
        raise ValueError('单价无效')
    for key in ('price_url', 'checked_at'):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise ValueError('缺' + key)
    return {'currency': currency, 'unit_price': str(price),
            'price_url': entry['price_url'], 'checked_at': entry['checked_at']}


def search_price(config=None):
    """→ (单价口径, 口径状态)。状态三值：configured / configured_invalid / unconfirmed。

    坏配置不抛也不静默：降级成 official published 那一版并标 configured_invalid——账要落下来，
    配置错了不能把这次调用从账上抹掉，也不能让它悄悄冒充已核准价。
    """
    entry = (settings() if config is None else config).get('search_price')
    if entry is not None:
        try:
            return _search_entry(entry), 'configured'
        except (ValueError, TypeError, InvalidOperation):
            return dict(SEARCH_QUOTES[SEARCH_CURRENCY]), 'configured_invalid'
    return dict(SEARCH_QUOTES[SEARCH_CURRENCY]), 'unconfirmed'


def record_search(detail, now=None, source='live'):
    """记一次检索：调用次数与计费次数**分列**，计费判据只有一条——HTTP 200 且结果非空。

    detail 是手 lookup 回执里已有的那几格（state/calls/http/results_count）。这里不重新判
    「到底查过没有」：请求已经发出去了，账不能因为形状缺格就把它抹掉；反过来，空结果、
    失败、超时、本地就拒（calls=0）都不计费，也不拿本地异常冒充服务端结算。
    """
    cfg = settings()
    if not cfg.get('enabled'):
        return
    calls = detail.get('calls') if isinstance(detail, dict) else None
    if type(calls) is not int or calls < 0:
        return
    results = detail.get('results_count')
    at = now or datetime.now().astimezone()
    # 一轮最多发一个请求（brief §1/§5），所以计费次数不会超过调用次数。
    billed = 1 if calls and detail.get('http') == 200 and type(results) is int and results > 0 else 0
    unknown = bool(calls and (detail.get('http') is None
                             or (detail.get('http') == 200 and detail.get('state') == 'error')))
    price, status = search_price(cfg)
    bj = at.astimezone(BEIJING)
    row = {'kind': 'search', 'at': at.isoformat(), 'day': bj.date().isoformat(),
             'source': source, 'calls': calls, 'billed_calls': billed,
             'billing_status': 'unknown' if unknown else ('estimated' if billed else 'not_billed'),
             'currency': price['currency'], 'unit_price': price['unit_price'],
             'price_url': price['price_url'], 'checked_at': price['checked_at'],
             'price_status': status, 'quotes': SEARCH_QUOTES,
             'cost': str(Decimal(price['unit_price']) * billed),
             'note': None if status == 'configured' else SEARCH_PENDING_NOTE}
    _append(row)
    return row


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
    _append(row)


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
    cfg = settings()
    root = paths.organ_dir('mind')
    ledger = root / 'rice-usage' / (selected + '.jsonl')
    daily = dict.fromkeys(('calls', 'prompt_tokens', 'completion_tokens', 'reasoning_tokens', 'total_tokens', 'unpriced_calls', 'invalid_lines'), 0)
    # 检索与 token 分列：一本日账里两种口径，混着加会把「买了几个查询」算进「吃了多少 token」。
    search = {'calls': 0, 'billed_calls': 0, 'pending_billed': 0, 'unknown_calls': 0, 'costs': {}, 'invalid_lines': 0}
    lower = upper = Decimal(0)
    seen = set()
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                daily['invalid_lines'] += 1
                continue
            if row.get('kind') == 'search':
                try:
                    calls, billed, cost = row['calls'], row['billed_calls'], Decimal(row['cost'])
                    if (type(calls) is not int or type(billed) is not int or calls < 0 or billed < 0
                            or billed > calls or not cost.is_finite() or not isinstance(row['currency'], str)):
                        raise ValueError
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    search['invalid_lines'] += 1
                    continue
                search['calls'] += calls
                search['billed_calls'] += billed
                if row.get('billing_status') == 'unknown':
                    search['unknown_calls'] += calls
                search['costs'][row['currency']] = str(Decimal(search['costs'].get(row['currency'], '0')) + cost)
                if row.get('price_status') != 'configured':
                    search['pending_billed'] += billed
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
    price, price_status = search_price(cfg)
    return {'ok': True, 'enabled': bool(cfg.get('enabled')), 'date': selected, 'timezone': 'Asia/Shanghai',
            'daily': daily, 'search': search, 'balance': balance, 'pricing_url': PRICING_URL,
            'search_price': {'status': price_status, 'currency': price['currency'],
                             'unit_price': price['unit_price'], 'price_url': price['price_url'],
                             'checked_at': price['checked_at'], 'quotes': SEARCH_QUOTES,
                             'note': None if price_status == 'configured' else SEARCH_PENDING_NOTE},
            'scope': '本机已记录的 DeepSeek 调用；不含其他客户端、看图、语音等服务。饭钱为人民币估算，工作日峰时因节假日未核定显示区间；'
                     '检索按次另计，调用次数与计费次数分列，单价口径未核定时按 published 那一版估算。余额以官方快照为准。'}


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
