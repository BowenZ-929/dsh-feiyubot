"""三条虚构长原文回读验收，默认离线；--live 最多三次现有官方API调用。"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'plugin'))
from organs.mind import llm, memory_read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT/'.scratch/memory-source-read-probe.json')
    args = parser.parse_args()
    config = dict(llm.resolve_config(), retries=0)
    assert config['base_url'].rstrip('/') == 'https://api.deepseek.com/v1'
    key = llm.read_key(config) if args.live else 'offline-key'
    calls, results = [], []
    correction = '11月14日的计划更正：改去河岸图书馆，不去旧街书店，回来仍买无糖茶、不加冰。'
    sentence = '我整理了桌面上的纸张，把空纸盒放回柜子，又把散落的铅笔收到笔袋里。'
    filler = sentence * 45
    for position in ('start', 'middle', 'end'):
        pieces = {'start': [correction, filler],
                  'middle': ['11月14日原本计划去旧街书店，回来买无糖茶、不加冰。', sentence*22, correction, sentence*23,
                             '提到“去旧街书店”只是转述旧方案，不是改回去了。'],
                  'end': ['11月14日原本计划去旧街书店，回来买无糖茶、不加冰。', filler, correction]}[position]
        source = ''.join(pieces)
        def transport(url, headers, payload, timeout):
            if len(calls) >= 3:
                raise AssertionError('本实验最多三次调用')
            assert url == 'https://api.deepseek.com/v1/chat/completions'
            call = {'request':payload, 'live':args.live}
            calls.append(call)
            if args.live:
                status, body = llm.default_transport(url, headers, payload, timeout)
            else:
                content = json.dumps({'quote':[correction], 'note':[]}, ensure_ascii=False)
                status, body = 200, json.dumps({'choices':[{'message':{'content':content}}]}).encode()
            call.update(status=status, response=json.loads(body))
            return status, body
        # 禁止离线读取生产.env；追踪也仅写临时目录。
        with tempfile.TemporaryDirectory(prefix='qqagent-memory-read-probe-') as tmp, \
             patch.dict(os.environ, {'QQAGENT_HOME':tmp, 'QQAGENT_HOME_ROOT':tmp, 'QQAGENT_TRACE':'0'}), \
             patch.object(llm.paths, 'HOME', Path(tmp)):
            result = memory_read.read({'full_text':source, 'note_full':'', 'who':'100001',
                'ts':'2026-11-07T10:00:00+09:00'}, '11月14日最后去哪儿，回来喝什么？', 600,
                complete=lambda messages: llm.complete(messages, config=config, env={config['key_env']:key},
                    transport=transport, purpose='虚构长记忆回读验收'))
        results.append({'position':position, 'source':source, 'result':result})
        print(json.dumps({'position':position, 'result':result}, ensure_ascii=False), flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'synthetic':True, 'live':args.live, 'results':results, 'calls':calls,
            'scope':'只验证回读摘录；不等同于真实聊天自然度或送达验收'},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'artifact':str(args.output), 'external_calls':len(calls) if args.live else 0}))


if __name__ == '__main__':
    main()
