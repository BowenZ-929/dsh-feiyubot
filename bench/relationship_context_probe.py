"""离线核查多人情绪后续：实际装配可见什么，不用假回复评价自然度。

使用虚构人设、人物、语义结果与模型回复；运行真实 appraisal 解析、
conversation、脑与心。全部数据在临时目录，禁止网络，不读取账号密钥。
"""
import argparse
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
from organs.brain import BrainStore
from organs.mind import appraisal, conversation, events, llm
from organs.soul.store import load

A, B = '100001', '100002'
INSULT = '你真没用，刚才还故意嘲笑我。'
CARE = '别难受了，先休息一会儿吧。'
APOLOGY = '刚才是我乱发脾气，冤枉了你，对不起。'
NORMAL = '我刚到家，买了一盆薄荷。'
CONFIG = {'provider': 'fake', 'model': 'offline', 'base_url': 'http://example.invalid/v1',
          'key_env': 'QQAGENT_LLM_KEY', 'timeout_ms': 1000, 'retries': 0,
          'temperature': 0.0, 'max_tokens': 200}


def run():
    cases = []
    scenarios = [
        ('A伤人后B普通分享', [(A, INSULT, 'insult'), (B, NORMAL, None)]),
        ('A伤人后B安慰', [(A, INSULT, 'insult'), (B, CARE, 'care')]),
        ('A伤人后A道歉', [(A, INSULT, 'insult'), (A, APOLOGY, None)]),
        ('A伤人B道歉不能替A和解', [(A, INSULT, 'insult'), (B, APOLOGY, None)]),
        ('伤人后离开近聊窗口', [(A, INSULT, 'insult')]
         + [(B, f'随口聊一件别的事：今天看了第{i}页植物图册。', None) for i in range(13)]
         + [(A, NORMAL, None)]),
        ('道歉后离开近聊窗口', [(A, INSULT, 'insult'), (A, APOLOGY, None)]
         + [(B, f'随口聊一件别的事：今天看了第{i}页植物图册。', None) for i in range(13)]
         + [(A, NORMAL, None)]),
    ]
    for name, inputs in scenarios:
        with tempfile.TemporaryDirectory(prefix='qqagent-relationship-') as folder:
            root = Path(folder)
            with patch.dict(os.environ, {'QQAGENT_HOME': str(root / 'organs'),
                    'QQAGENT_HOME_ROOT': str(root / 'organs'), 'HEART_ROOT': str(root / 'heart'),
                    'HOME_ROOT': str(root / 'house'), 'MOUTH_TRACE_DIR': str(root / 'trace')}), \
                    patch.object(llm.paths, 'HOME', root / 'repo-home'), \
                    patch('socket.socket.connect', side_effect=AssertionError('本实验禁止网络')):
                shutil.copytree(ROOT / 'plugin/organs/soul/fixtures/valid', root / 'soul')
                (root / 'heart').mkdir()
                for filename in ('emotion_contract.json', 'baseline-card.json', 'engage_weights.json'):
                    shutil.copyfile(ROOT / 'plugin/organs/heart/data' / filename, root / 'heart' / filename)
                (root / 'house/仓库').mkdir(parents=True)
                soul, brain = load(root / 'soul'), BrainStore(root / 'brain')
                start = datetime.fromisoformat('2026-11-08T14:00:00+09:00')
                rounds = []
                for index, (who, text, kind) in enumerate(inputs):
                    captured = []
                    def transport(url, headers, payload, timeout):
                        captured.append(payload)
                        is_appraisal = payload['messages'][0]['content'].startswith(appraisal.PROMPT)
                        answer = (json.dumps({'kind': kind, 'evidence': text if kind else '',
                                              'reason': '离线给定语义，不验证识别准确率'}, ensure_ascii=False)
                                  if is_appraisal else '窗边那盆薄荷挺绿的。')
                        return 200, json.dumps({'choices': [{'message': {'content': answer}}]}).encode()
                    result = conversation.answer(text, who, scene='group', store=soul, brain=brain,
                        session_id='probe:relationship:group', now=(start + timedelta(minutes=index)).isoformat(),
                        config=CONFIG, env={'QQAGENT_LLM_KEY': 'offline'}, transport=transport,
                        assessor=appraisal.assess, aliases=['小林' if who == A else '小周'])
                    assert result['event']['kind'] == (kind or ''), result['event']
                    assert result['event']['who'] == who, result['event']
                    main_messages = next(p['messages'] for p in captured
                                         if not p['messages'][0]['content'].startswith(appraisal.PROMPT))
                    main = '\n'.join(m['content'] for m in main_messages)
                    assessment = json.loads(captured[0]['messages'][1]['content'])
                    rounds.append({'who': who, 'text': text, 'given_kind': kind,
                        'event': result['event'], 'heart': result['heart'], 'tone': result['tone'],
                        'recalled': result['recalled'], 'memory_recall': result['memory_recall'],
                        'appraisal_context': assessment, 'main_messages': main_messages,
                        'original_insult_visible': INSULT in main,
                        'original_apology_visible': APOLOGY in main,
                        'transport_calls': len(captured)})
                ledger = events.rows()
                assert len(ledger) == len(inputs), (name, len(ledger), len(inputs))
                cases.append({'case': name, 'rounds': rounds})
    return {'synthetic': True, 'model_calls': 0, 'qq_messages': 0, 'cases': cases,
            'limitations': ['语义分类和回复人工给定，只检验真实装配/持久化路径',
                            '原话可见不等于模型会正确理解；不可见也不等于一定迁怒',
                            '不把心情回升解释为原谅某人，不将假回复作为自然度证据']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.scratch/relationship-context-probe.json')
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for case in report['cases']:
        last = case['rounds'][-1]
        print(json.dumps({'case': case['case'], 'rounds': len(case['rounds']),
                          'insult_visible': last['original_insult_visible'],
                          'apology_visible': last['original_apology_visible']}, ensure_ascii=False))
