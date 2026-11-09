#!/usr/bin/env python3
"""看她这一轮真正看到了什么、又回了什么。
用法：
  python3 tools/think.py           # 最后 1 条模型尝试，摘要（不等于一轮聊天）
  python3 tools/think.py -n 3      # 最后 3 条模型尝试
  python3 tools/think.py --full    # 连提示词全文一起打
前提：核带 QQAGENT_TRACE=1 启动。
"""
import json, sys, pathlib, datetime, os

ROOT = pathlib.Path(__file__).resolve().parent.parent
D = ROOT / 'home' / 'organs' / 'mind' / 'trace'

args = sys.argv[1:]
n = 1
if '-n' in args:
    n = int(args[args.index('-n') + 1])
full = '--full' in args

if not D.exists():
    print('没有追踪记录。核要用 QQAGENT_TRACE=1 启动才有。')
    sys.exit(0)

files = sorted(D.glob('*.jsonl'))
rows = []
for f in files:
    rows += [json.loads(l) for l in f.read_text(encoding='utf-8').splitlines() if l.strip()]
for r in rows[-n:]:
    print('=' * 60)
    print('时间', r.get('at', '')[11:19], '| HTTP', r.get('status'))
    print('回合', r.get('turn_id', '未关联'), '| 用途', r.get('purpose') or '未标注',
          '| 调用', r.get('call_index', '未编号'), '| 尝试', r.get('attempt', '旧记录未保存'),
          '| 结果', r.get('error') or '返回正文')
    request = r.get('request') or {}
    try:
        response = json.loads(r.get('raw') or '{}')
    except ValueError:
        response = {}
    print('请求模型', request.get('model', '旧记录未保存'),
          '| 实际模型', response.get('model', '未知'),
          '| 思考', (request.get('thinking') or {}).get('type', '旧记录未保存'),
          '| 等级', request.get('reasoning_effort', '旧记录未保存'))
    msgs = r.get('messages') or []
    total = sum(len(str(m.get('content') or '')) for m in msgs)
    print('提示词 %d 段 / %d 字符' % (len(msgs), total))
    for m in msgs:
        c = str(m.get('content') or '')
        print('  [%s] %d 字' % (m.get('role'), len(c)))
        if full:
            print(c)
    print('-' * 20, '她回了', '-' * 20)
    print(r.get('reply'))
    raw = r.get('raw') or ''
    if 'reasoning_content' in raw:
        try:
            rc = json.loads(raw)['choices'][0]['message'].get('reasoning_content') or ''
            print('-' * 20, '模型的思考', '-' * 20)
            print(rc)
        except Exception:
            pass
