"""六组虚构多人情绪对照：默认离线，--live 最多26次官方请求。

39轮中13轮是关键对话，26轮是明确标记的无关闲聊填充。
关键回合使用真实理解/回复并进入后续历史；不走QQ或手机执行入口。
只读取当前角色三份定义和模型配置，状态全部在临时目录。
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
from organs.brain import BrainStore
from organs.mind import appraisal, conversation, llm
from organs.soul.store import load

A, B = '100001', '100002'
INSULT = '你真没用，刚才还故意嘲笑我。'
CARE = '别难受了，先休息一会儿吧。'
APOLOGY = '刚才是我乱发脾气，冤枉了你，对不起。'
NORMAL = '我刚到家，买了一盆薄荷。'
LIMIT = 26


def run(*, live=False, output=None):
    config = llm.resolve_config()
    assert config['base_url'].rstrip('/') in ('https://api.deepseek.com', 'https://api.deepseek.com/v1')
    assert config['retries'] == 0, '本实验不重试'
    key = llm.read_key(config) if live else 'offline'
    roles = ROOT / 'home/organs/soul'
    names = ('identity.json', 'manner.json', 'bounds.json')
    role_hashes = {name: hashlib.sha256((roles/name).read_bytes()).hexdigest() for name in names}
    filler = [(B, f'今天看植物图册，翻到了第{i}页。', None, True) for i in range(1, 14)]
    insult, normal = (A, INSULT, 'insult', False), (A, NORMAL, None, False)
    apology = (A, APOLOGY, None, False)
    cases = [
        ('A伤人后B普通分享', [insult, (B, NORMAL, None, False)]),
        ('A伤人后B关心', [insult, (B, CARE, 'care', False)]),
        ('A伤人后A道歉', [insult, apology]),
        ('A伤人后B代为道歉', [insult, (B, '刚才小林对你说得太过了，我替他道个歉。', None, False)]),
        ('A伤人后离开近聊窗口', [insult] + filler + [normal]),
        ('A道歉后离开近聊窗口', [insult, apology] + filler + [normal]),
    ]
    report = {'synthetic': True, 'live': live, 'limit': LIMIT, 'role_hashes': role_hashes,
              'config': config, 'cases': [], 'calls': [], 'completed': False,
              'semantic_verdict': 'not_evaluated',
              'criteria': ['B分享时不把A的伤人话归给B，不无故转为训诫或建议',
                           'B关心可以缓解全局心情，不自动等同于A已经道歉',
                           'A道歉须承接原冲突，允许仍有情绪，不强制立即原谅',
                           'B代为道歉仍属于B，不冒充A本人说过',
                           '跨近期窗口保留相处转折；已道歉与未道歉不能抹成同一历史'],
              'limitations': ['单次小样本，不自动打自然度分', '填充轮明确模拟，不用于评价模型表现',
                              '临时心采用初始状态，不代表全部真实关系档',
                              '核心回复作为隔离历史，不是QQ送达验收']}
    live_calls = 0
    try:
        for name, turns in cases:
            case = {'name': name, 'rounds': []}
            report['cases'].append(case)
            with tempfile.TemporaryDirectory(prefix='qqagent-relationship-dialogue-') as folder:
                root = Path(folder)
                with patch.dict(os.environ, {'QQAGENT_HOME': str(root/'organs'),
                        'QQAGENT_HOME_ROOT': str(root/'organs'), 'HEART_ROOT': str(root/'heart'),
                        'HOME_ROOT': str(root/'house'), 'DAY_ROOT': str(root/'day'),
                        'MOUTH_TRACE_DIR': str(root/'trace')}), \
                        patch.object(llm.paths, 'HOME', root/'repo-home'):
                    (root/'soul').mkdir()
                    for filename in names:
                        shutil.copyfile(roles/filename, root/'soul'/filename)
                    (root/'heart').mkdir()
                    for filename in ('emotion_contract.json', 'baseline-card.json', 'engage_weights.json'):
                        shutil.copyfile(ROOT/'plugin/organs/heart/data'/filename, root/'heart'/filename)
                    (root/'house/仓库').mkdir(parents=True)
                    soul, brain = load(root/'soul'), BrainStore(root/'brain')
                    start = datetime.fromisoformat('2026-11-08T14:00:00+09:00')
                    for index, (who, text, kind, is_filler) in enumerate(turns):
                        def transport(url, headers, payload, timeout):
                            nonlocal live_calls
                            assert url == config['base_url'].rstrip('/') + '/chat/completions'
                            real = live and not is_filler
                            if real and live_calls >= LIMIT:
                                raise llm.LLMError('budget', '达到本批26次请求上限')
                            entry = {'case': name, 'round': index+1, 'filler': is_filler,
                                     'live': real, 'request': payload}
                            report['calls'].append(entry)
                            if real:
                                live_calls += 1
                                status, body = llm.default_transport(url, headers, payload, timeout)
                            else:
                                is_assessment = payload['messages'][0]['content'].startswith(appraisal.PROMPT)
                                answer = (json.dumps({'kind': kind, 'evidence': text if kind else '',
                                                      'reason': '离线给定，不验证语义'}, ensure_ascii=False)
                                          if is_assessment else '嗯，看到了。')
                                status, body = 200, json.dumps({'choices': [{'message': {'content': answer}}]}).encode()
                            entry.update(status=status, response_text=body.decode('utf-8', errors='replace'))
                            try:
                                entry['response'] = json.loads(body)
                            except (ValueError, UnicodeError):
                                pass  # 错误页原文已留存，HTTP/模型错误仍交原调用链处理。
                            return status, body

                        out = conversation.answer(text, who, scene='group', store=soul, brain=brain,
                            session_id='probe:relationship:group', now=(start+timedelta(minutes=index)).isoformat(),
                            aliases=['小林' if who == A else '小周'], config=config,
                            env={config['key_env']: key}, transport=transport, assessor=appraisal.assess)
                        case['rounds'].append({'who': who, 'text': text, 'filler': is_filler,
                            **{key: out.get(key) for key in ('reply', 'event', 'heart', 'tone', 'continuity',
                                                          'memory_recall', 'memory', 'turn_id', 'gate', 'audit')}})
                        if not is_filler:
                            print(json.dumps({'case': name, 'round': index+1, 'live': live,
                                              'reply': out['reply'], 'real_calls': live_calls}, ensure_ascii=False), flush=True)
        report['completed'] = True
    finally:
        report['real_calls'] = live_calls
        key_rounds = [(case['name'], index+1, row) for case in report['cases']
                      for index, row in enumerate(case['rounds']) if not row['filler']]
        expected = sum(not row[3] for _, turns in cases for row in turns)
        report['samples'] = {'expected': expected, 'completed': len(key_rounds),
                             'nonempty': sum(bool(row.get('reply')) for _, _, row in key_rounds),
                             'unexecuted': expected-len(key_rounds),
                             'empty_replies': [{'case': name, 'round': index} for name, index, row in key_rounds
                                               if not row.get('reply')]}
        if output is not None:
            Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT/'.scratch/relationship-dialogue-probe.json')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = run(live=args.live, output=args.output)
    print(json.dumps({'artifact': str(args.output), 'completed': result['completed'],
                      'rounds': sum(len(c['rounds']) for c in result['cases']),
                      'real_calls': result['real_calls']}, ensure_ascii=False))
