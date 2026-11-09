"""虚构多人事件的离线接线对照；语义结果人工给定，不评价模型理解或回复。"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugin'))
from organs.heart import changes, mood
from organs.mind import events


def run():
    scenarios = [
        ('群内直接关心', [('A', 'care', 'group', '别累着，休息一下')]),
        ('群内关心他人', [('A', None, 'group', '小林，别累着，休息一下')]),
        ('私聊先伤人再关心', [('A', 'insult', 'private', '你真没用'), ('A', 'care', 'private', '别难受了，休息一下')]),
        ('私聊另一人关心', [('A', 'insult', 'private', '你真没用'), ('B', 'care', 'private', '别难受了，休息一下')]),
        ('群内另一人关心', [('A', 'insult', 'group', '你真没用'), ('B', 'care', 'group', '别难受了，休息一下')]),
        ('私聊先夸再伤人', [('A', 'praise', 'private', '你真厉害'), ('A', 'insult', 'private', '你真没用')]),
    ]
    cases = []
    for name, inputs in scenarios:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            heart = base / 'heart'; heart.mkdir()
            shutil.copy(ROOT / 'plugin/organs/heart/data/emotion_contract.json', heart)
            with patch.dict('os.environ', {'QQAGENT_HOME': str(base), 'HEART_ROOT': str(heart)}), \
                    patch('socket.socket.connect', side_effect=AssertionError('离线对照禁止网络')):
                rows = []
                for index, (who, kind, scene, text) in enumerate(inputs):
                    at = f'2026-11-07T12:{index * 2:02d}:00+09:00'
                    before = mood.read(root=heart, now=at)['now']
                    interpreted = {'kind': kind, 'who': who, 'evidence': text if kind else '',
                                   'note': '人工给定的对bot事件' if kind else '关心其他人，不针对bot',
                                   'status': 'ok', 'source': 'semantic'}
                    recorded = events.record(text, scene=scene, who=who, at=at,
                                             interpretation=interpreted, turn_id=str(index) * 32)
                    rows.append({'who': who, 'scene': scene, 'text': text, 'semantic_kind': kind,
                                 'recorded_kind': recorded['kind'], 'applied': recorded['applied'],
                                 'reason': recorded['reason'], 'mood_before': before,
                                 'mood_after': mood.read(root=heart, now=at)['now']})
                cases.append({'case': name, 'events': rows, 'changes': changes.read_history()['rows']})
    return {'synthetic': True, 'model_calls': 0, 'qq_messages': 0,
            'scope': '人工语义结果→事件场景开关→心情契约→变化账；非真实回复效果', 'cases': cases}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.scratch/emotion-scope-probe.json')
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for case in report['cases']:
        print(json.dumps({'case': case['case'], 'events': case['events']}, ensure_ascii=False))
