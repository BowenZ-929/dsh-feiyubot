"""离线检查更正资料是否真正进入回复上下文；不评判模型答案，不调用模型。

运行：python3 bench/memory_context_probe.py
所有资料均虚构，每个场景使用临时脑；真实原文不会被读取或重放。
"""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugin'))
from organs.brain import BrainStore
from organs.mind import conversation, memory_read


def run(*, with_source_reader=False):
    old = '11月14日去旧街书店，回来买无糖茶，不加冰。'
    correction = '计划改了，那天改去河岸图书馆，饮料照旧。'
    # 人工给定正确理解，隔离检索能力；不声称解释器一定会生成此理解。
    note = ('承接理解（可错，非原话）：11月14日改去河岸图书馆，回来买无糖茶、不加冰。'
            '\n本人本轮依据：' + correction + '\n本人前文依据：' + old)
    long_quote = old + '这是我今天整理东西时想到的一些无关细节。' * 65 + correction
    scenarios = [
        ('short_correction', [(old, ''), (correction, note)], [0, 1], '11月14日去哪儿、喝什么'),
        ('temporary_exception', [('平时喝无糖茶，不加冰。', ''),
                                 ('今天这一次喝热可可，平时习惯不变。', '')],
         [0, 1], '我平时和今天分别喝什么'),
        ('long_paragraph_correction', [(long_quote, '')], [0], '11月14日去哪儿'),
        ('crowded_unresolved', [(f'11月14日去旧街书店，安排细节{i}。', '') for i in range(7)]
         + [(correction, '')], [7], '11月14日去旧街书店'),
        ('crowded_with_understanding', [(f'11月14日去旧街书店，安排细节{i}。', '') for i in range(7)]
         + [(correction, note)], [7], '11月14日去旧街书店'),
        ('mixed_facts', [(old, ''), ('只是地点改成河岸图书馆，喝的和不加冰都照旧。', note)],
         [0, 1], '11月14日去哪儿、喝什么'),
    ]
    outcomes = []
    cap = conversation.load_assembly()['sections']['memory']['cap']
    with tempfile.TemporaryDirectory(prefix='qqagent-memory-context-') as tmp, \
         patch('socket.socket.connect', side_effect=AssertionError('本检查禁止网络连接')):
        for name, entries, required, query in scenarios:
            for cross_day in (False, True):
                brain = BrainStore(Path(tmp) / (name + str(cross_day)))
                start = datetime.fromisoformat('2026-11-07T10:00:00+09:00')
                saved = []
                for index, (quote, understanding) in enumerate(entries):
                    result = brain.ingest('100001', quote, note=understanding,
                        ts=(start + timedelta(minutes=index)).isoformat())
                    assert result['ok'], result
                    saved.append(result['entry'])
                now = (start + timedelta(days=int(cross_day), hours=1)).isoformat()
                original_recall = brain.recall
                reader_calls = []
                def reader(item, question, budget):
                    def complete(messages):
                        reader_calls.append(messages)
                        # 人工给定摘录，只检查现有回读/装配接线，不评判模型选段能力。
                        return json.dumps({'quote': [old, correction], 'note': []}, ensure_ascii=False)
                    return memory_read.read(item, question, budget, complete=complete)
                with patch.object(brain, 'recall', side_effect=lambda **kw: original_recall(now_ts=now, **kw)):
                    rows, prompt, audit = conversation._recall(brain, query, '100001', 'private', cap=cap,
                                                              reader=reader if with_source_reader else None)
                by_id = {row['id']: row for row in rows}
                checks = []
                for index in required:
                    entry = saved[index]
                    row = by_id.get(entry['id'])
                    checks.append({'input_index': index, 'id': entry['id'],
                        'selected': row is not None, 'in_prompt': bool(row and row['in_prompt']),
                        'stage': 'included' if row and row['in_prompt'] else 'prompt_budget' if row else 'retrieval',
                        'required_text_present': all(part in prompt for part in
                            ([old, correction] if name == 'long_paragraph_correction' else [entry['quote']])),
                        'quote_present': entry['quote'] in prompt})
                # 今天层和跨日归档都应保留原文；不能把截掉原文当成修复。
                stored = {row['id']: row for row in brain.today_entries(now) + brain.longterm_active_entries()}
                assert all(stored[row['id']]['quote'] == row['quote'] for row in saved)
                assert len(prompt) <= cap
                assert len(reader_calls) <= 1
                outcomes.append({'case': name, 'cross_day': cross_day, 'query': query,
                    'inputs': [{'quote': q, 'understanding': n} for q, n in entries],
                    'checks': checks, 'all_required_in_prompt': all(row['in_prompt'] and row['required_text_present'] for row in checks),
                    'simulated_reader_calls': len(reader_calls),
                    'prompt': prompt, 'audit': audit, 'originals_preserved': True})
    return {'synthetic': True, 'model_calls': 0, 'memory_cap': cap,
        'source_reader': 'real_excerpt_validation_with_simulated_selection' if with_source_reader else 'disabled_in_this_probe',
        'scope': '实际脑检索和主回复记忆装配；不含近期对话，不评判模型理解或回复质量',
        'understanding_source': '人工正确理解，仅用于与缺少理解时的检索行为对照',
        'cases': outcomes,
        'all_required_in_prompt': sum(row['all_required_in_prompt'] for row in outcomes),
        'total': len(outcomes)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-source-reader', action='store_true', help='接入真实长文回读校验，选段回复为人工给定；仍禁止网络')
    parser.add_argument('--output', type=Path, default=ROOT / '.scratch/memory-context-probe.json')
    args = parser.parse_args()
    report = run(with_source_reader=args.with_source_reader)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for row in report['cases']:
        print(json.dumps({key: row[key] for key in ('case', 'cross_day', 'checks')}, ensure_ascii=False))
    print(json.dumps({'artifact': str(args.output), 'included': report['all_required_in_prompt'],
                      'total': report['total'], 'model_calls': 0}, ensure_ascii=False))
