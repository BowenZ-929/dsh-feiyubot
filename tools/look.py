#!/usr/bin/env python3
"""看板：她现在在想什么、为什么回/不回、做了什么。一条命令全摊开。
用法：
  python3 tools/look.py            # 打一屏
  python3 tools/look.py --follow   # 每 5 秒刷新（Ctrl+C 退出）
"""
import json, os, subprocess, sys, time, pathlib, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
CH = ROOT / 'channels' / 'onebot' / '.state'
HOME = ROOT / 'home' / 'organs'

def tail_jsonl(p, n):
    try:
        rows = [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
        return rows[-n:]
    except Exception:
        return []

def sh(cmd):
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5).stdout.strip()
        return out
    except Exception:
        return ''

def lamp():
    core = sh("curl -s -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:8787/healthz")
    chan = sh("pgrep -f 'server.ts' | wc -l").strip()
    qq = sh("pgrep -f MacOS/QQ | wc -l").strip()
    mode = ''
    try:
        for l in (CH / 'logs' / 'channel.log').read_text(encoding='utf-8', errors='ignore').splitlines()[::-1]:
            if ' mode=' in l:
                mode = l.split('mode=')[1][:12]
                break
    except Exception:
        pass
    return core, chan, qq, mode

def heart_now():
    try:
        m = json.loads((HOME / 'heart' / 'mood.json').read_text(encoding='utf-8'))
        return m.get('mood'), m.get('energy'), m.get('updated_at', '')[:19]
    except Exception:
        return None, None, ''

def section(title):
    print('\n' + '─' * 12 + ' ' + title + ' ' + '─' * 12)

def once():
    core, chan, qq, mode = lamp()
    print('她现在的状态  ', time.strftime('%H:%M:%S'))
    print('  核 %-4s 通道 %-2s 个  QQ %-2s 个  模式 %s' % (core, chan, qq, mode or '?'))
    mood, energy, at = heart_now()
    if mood is not None:
        print('  心情 %-6s 精力 %-6s（%s）' % (mood, energy, at))

    section('她最后几次「要不要回」')
    for r in tail_jsonl(CH / 'decisions.jsonl', 8):
        m = r.get('machine') or {}
        b = m.get('batch') or {}
        d = m.get('direct') or {}
        g = m.get('gate') or {}
        a = (g or {}).get('audit') or {}
        print('  %s %-7s %-2s条 %-16s %s%s' % (
            r.get('ts', '')[11:19], r.get('scene'), b.get('msgs'),
            (r.get('human') or '')[:24],
            ('[%s]' % d.get('rule')) if d.get('rule') else '',
            (' 闸命中%s' % a.get('lcs_len')) if a.get('hit') else ''))

    section('她说过的话')
    sent = [r for r in tail_jsonl(CH / 'ledger.jsonl', 40) if r.get('kind') in ('sent', 'blocked')]
    for r in sent[-8:]:
        tag = '发出' if r.get('kind') == 'sent' else '被挡'
        t = (r.get('text') or '').replace(chr(10), ' ⏎ ')[:56]
        print('  %s %s %s' % (r.get('ts', '')[11:19], tag, t))

    section('她一个人时做了什么')
    rows = tail_jsonl(HOME / 'day' / 'proactive_idle_log.jsonl', 5)
    if not rows:
        print('  （还没有独处记录）')
    for r in rows:
        print('  %s %-10s %s' % (r.get('at', '')[11:19], r.get('action'), (r.get('why') or r.get('reason_code') or '')[:30]))

    section('她动过什么念')
    rows = tail_jsonl(HOME / 'day' / 'proactive_log.jsonl', 5)
    if not rows:
        print('  （还没有动念）')
    for r in rows:
        print('  %s %-8s %s' % (r.get('at', '')[11:19], r.get('action'), (r.get('because') or '')[:36]))

    section('她用手做了什么')
    rows = tail_jsonl(HOME / 'hand' / 'ledger.jsonl', 30)
    seen = []
    for r in rows:
        if r.get('action') and r.get('state'):
            seen.append(r)
    for r in seen[-6:]:
        print('  %s %-12s %-6s %s' % (r.get('at', '')[11:19], r.get('action'), r.get('state'), (r.get('code') or '')[:16]))

if __name__ == '__main__':
    if '--follow' in sys.argv:
        try:
            while True:
                os.system('clear' if os.name != 'nt' else 'cls')
                once()
                time.sleep(5)
        except KeyboardInterrupt:
            pass
    else:
        once()
