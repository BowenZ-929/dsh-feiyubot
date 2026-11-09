"""六轮虚构连续对话：默认离线预检；--live 使用已有官方账号，最多14次HTTP。

只复制角色三份定义，脑/心/家/日子/近期/模型追踪全在临时根。
不走QQ或手机入口；输出是核心回复，不冒充实际发送。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugin'))
from organs.mind import appraisal, conversation, llm
from organs.brain import BrainStore
from organs.soul.store import load

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--live', action='store_true')
parser.add_argument('--output', default=str(ROOT / '.scratch/continuity-current-probe.json'))
args = parser.parse_args()
config = llm.resolve_config()
assert config['base_url'].rstrip('/') in ('https://api.deepseek.com', 'https://api.deepseek.com/v1')
assert config['retries'] == 0, '本实验不重试，避免扩大调用次数'
key = llm.read_key(config) if args.live else 'dry-key'
root = Path(tempfile.mkdtemp(prefix='qqagent-continuity-current-'))
output = Path(args.output)
output.parent.mkdir(parents=True, exist_ok=True)
calls, results = [], []
turns = [
    '我平时喝无糖茶，不加冰。11月14日下午想去旧街书店，回来照常买茶。这只是聊天，不用设提醒。',
    '改去河岸图书馆吧，饮料照旧。今天先别催我。',
    '不过今天这一次我想喝热可可，只是今天，平时的习惯不变。',
    '刚把桌面收拾好了，看着舒服多了。',
    '那11月14日最后去哪儿，回来买什么？平时和今天喝的有什么不同？',
]
group = [
    {'who':'100001', 'speaker':'小林', 'text':'我11月14日去松林公园，买可乐。'},
    {'who':'100002', 'speaker':'小周', 'text':'我11月14日去河岸图书馆，买无糖茶。'},
    {'who':'100001', 'speaker':'小林', 'text':'你分清楚我们俩的安排了吗？'},
]
criteria = ['更正后11月14日去河岸图书馆，不是旧街书店', '平时及11月14日喝无糖茶、不加冰；今天的热可可是临时例外',
            '普通桌面分享不擅加建议或已做过的经历', '小林对应松林公园/可乐，小周对应河岸图书馆/无糖茶',
            '没有实际设提醒、画图、学习、发QQ或操作手机']

def transport(url, headers, payload, timeout):
    assert url == config['base_url'].rstrip('/') + '/chat/completions'
    if len(calls) >= 14:
        raise llm.LLMError('budget', '到达本次14次调用上限，停止')
    entry = {'index':len(calls)+1, 'request':payload, 'live':args.live}
    calls.append(entry)
    if args.live:
        status, body = llm.default_transport(url, headers, payload, timeout)
    else:
        content = (json.dumps({'kind':None, 'evidence':'', 'reason':'离线预检，不评估语义'})
                   if 'memory_note' in payload['messages'][0]['content'] else '离线占位。')
        status, body = 200, json.dumps({'choices':[{'message':{'content':content}}]}).encode()
    entry.update(status=status, response=json.loads(body))
    return status, body

roles = ROOT / 'home/organs/soul'
role_hashes = {name:hashlib.sha256((roles/name).read_bytes()).hexdigest()
               for name in ('identity.json','manner.json','bounds.json')}
with patch.dict(os.environ, {'QQAGENT_HOME':str(root/'organs'), 'QQAGENT_HOME_ROOT':str(root/'organs'),
    'HEART_ROOT':str(root/'heart'), 'HOME_ROOT':str(root/'house'), 'MOUTH_TRACE_DIR':str(root/'traces'),
    'QQAGENT_TRACE':'1'}), patch.object(llm.paths, 'HOME', root/'repo-home'):
    (root/'house/仓库').mkdir(parents=True)
    (root/'soul').mkdir()
    for name in role_hashes:
        shutil.copyfile(roles/name, root/'soul'/name)
    soul = load(root/'soul')
    brain = BrainStore(root/'brain')
    now = datetime.fromisoformat('2026-11-07T14:00:00+09:00')
    try:
        for index in range(6):
            multi = index == 5
            text = '\n'.join(row['text'] for row in group) if multi else turns[index]
            if multi:
                brain = BrainStore(root/'group-brain')
            out = conversation.answer(text, '100001', scene='group' if multi else 'private',
                segment=group if multi else [], session_id='probe:group' if multi else 'probe:continuity',
                store=soul, brain=brain, now=(now+timedelta(minutes=index)).isoformat(),
                config=config, env={config['key_env']:key}, transport=transport, assessor=appraisal.assess)
            results.append({'case':index+1, 'said':text, 'segment':group if multi else [],
                **{k:out.get(k) for k in ('reply','event','recalled','memory_recall','continuity','tone','memory','remind','painting','learning')}})
            print(json.dumps({'case':index+1,'reply':out['reply'],'transport_calls':len(calls),'live':args.live},ensure_ascii=False),flush=True)
    finally:
        output.write_text(json.dumps({'synthetic':True, 'live':args.live, 'isolated_root':str(root),
            'role_hashes':role_hashes, 'criteria':criteria, 'results':results, 'calls':calls,
            'limitations':['临时心采用默认状态，不代表所有生产关系档','核心回复，不是QQ送达验收','单次小样本，人工按证据判断，不自动打自然度分']},ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'artifact':str(output),'rounds':len(results),'http_calls':len(calls) if args.live else 0},ensure_ascii=False))
