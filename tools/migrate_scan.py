#!/usr/bin/env python3
"""旧家记忆的只读盘点：出"该迁哪些"的清单，不写她的实盘。

判据：**这条将来会不会被问起？** 会 → 候选。

四件事：
  1. 切：把"一天的叙事摘要"切成事件单元（按句切，最多两句一单元，<=200 字）
  2. 门槛：只收"有具体人/事"的——必须命中 家人／雷点／约定／冲突 之一，
     或（被夸 且 提到具体人）。工作学业与爱好只做加分，单独命中不收
     （原库工作学业 429 条全是日常水账，收进来只会把装包名额占光）
  3. 脱敏：旧记忆里全是群友 QQ 号与号↔昵称的对照（"齐神就是白、3275916838 那个"）。
     红线是"号任何方向都不念"，所以迁移时一律把 9-11 位数字替换成「某号」，记 redacted；
     昵称保留（昵称不是号）。
  4. 去重：三字组 Jaccard >= 0.7 视为重复，只留分高的。

产出：
  docs/organs/brain/migration-candidates.jsonl
  终端一张分布表

用法：python3 tools/migrate_scan.py [--min-score 3]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

OLD = Path('/Users/bowen/qqAgent/AstrBot/data/plugin_data/astrbot_plugin_livingmemory')
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'docs' / 'organs' / 'brain' / 'migration-candidates.jsonl'

CATS = {
    '家人': (3, ['妈', '爸', '奶奶', '外婆', '住院', '手术', '医院', '家里']),
    '雷点': (3, ['不许', '别跟', '别提', '讨厌', '不能提', '不要提', '敏感']),
    '约定': (3, ['说好', '答应', '约好', '下次', '记得', '到时候']),
    '冲突': (2, ['吵', '凶', '骂', '道歉', '对不起', '闹']),
    '被夸': (2, ['夸', '好听', '可爱', '厉害', '喜欢你', '谢谢']),
    '工作学业': (1, ['工作', '上班', '考试', '论文', '毕业', '导师', '面试', '加班']),
    '爱好': (1, ['游戏', '看番', '歌', '画', '书', '电影']),
}
STRONG = {'家人', '雷点', '约定', '冲突'}
NUM = re.compile(r'(?<!\d)\d{9,11}(?!\d)')
SPLIT = re.compile(r'[。！？\n]+')


def names() -> list[str]:
    con = sqlite3.connect('file:%s?mode=ro' % (OLD / 'conversations.db'), uri=True)
    rows = con.execute('select distinct sender_name from messages where sender_name is not null').fetchall()
    con.close()
    return [r[0] for r in rows if r[0] and len(r[0]) >= 2]


def grams(text: str, n: int = 3) -> set[str]:
    clean = re.sub(r'\s+', '', text)
    return {clean[i:i + n] for i in range(max(0, len(clean) - n + 1))}


def scan(min_score: int = 3) -> None:
    con = sqlite3.connect('file:%s?mode=ro' % (OLD / 'livingmemory.db'), uri=True)
    docs = con.execute('select id, text, created_at from documents order by id').fetchall()
    con.close()
    who = names()

    kept: list[dict] = []
    seen: list[set[str]] = []
    docs_with_ids = 0
    for doc_id, text, created in docs:
        text = text or ''
        if NUM.search(text):
            docs_with_ids += 1
        bits = [b.strip() for b in SPLIT.split(text) if len(b.strip()) >= 8]
        for i in range(0, len(bits), 2):
            unit = ''.join(bits[i:i + 2])[:200]
            cats = [c for c, (_, ws) in CATS.items() if any(w in unit for w in ws)]
            score = sum(CATS[c][0] for c in cats)
            mentions = [n for n in who if n in unit]
            if mentions:
                score += 1
            if score < min_score:
                continue
            if not (set(cats) & STRONG) and not ('被夸' in cats and mentions):
                continue
            redacted = bool(NUM.search(unit))
            clean = NUM.sub('某号', unit)
            g = grams(clean)
            if any(len(g & s) / max(1, len(g | s)) >= 0.7 for s in seen):
                continue
            seen.append(g)
            kept.append({
                'text': clean, 'score': score, 'cats': cats, 'mentions': mentions,
                'redacted': redacted,
                'source': {'doc_id': doc_id, 'created_at': created},
            })

    kept.sort(key=lambda c: (-c['score'], -len(c['text'])))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open('w', encoding='utf-8') as fh:
        for c in kept:
            fh.write(json.dumps(c, ensure_ascii=False) + '\n')

    cat = Counter(x for c in kept for x in c['cats'])
    print('原文 %d 篇（其中 %d 篇含 9-11 位数字）→ 候选 %d 条' % (len(docs), docs_with_ids, len(kept)))
    print('候选里被脱敏的:', sum(1 for c in kept if c['redacted']))
    print('类别分布:', dict(cat))
    print('长度: 均值 %.0f 字，最长 %d 字' % (
        sum(len(c['text']) for c in kept) / max(1, len(kept)), max((len(c['text']) for c in kept), default=0)))
    for c in kept[:3]:
        print('  [%d分 %s] %s…' % (c['score'], '/'.join(c['cats']), c['text'][:56]))
    print('清单写到', OUT.relative_to(ROOT))


if __name__ == '__main__':
    args = sys.argv[1:]
    thr = int(args[args.index('--min-score') + 1]) if '--min-score' in args else 3
    scan(thr)
