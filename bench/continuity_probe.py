"""隔离的连续聊天基线。默认离线；--live 使用现有官方账号。

continuity 六轮、materials 五轮最多14次请求；naturalness 十轮最多30次。只复制当前固定
角色、中性起点及装配配置，不复制私人历史；probe 会话不证明 QQ 动作接线。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugin'))
from organs.mind import appraisal, conversation, llm, rice
from organs.brain import BrainStore
from organs.soul.store import load
from organs.day import growth


def run(*, live=False, scenario='continuity', output=None):
    config = llm.resolve_config()
    assert config['base_url'].rstrip('/') in ('https://api.deepseek.com', 'https://api.deepseek.com/v1')
    assert config['retries'] == 0, '本实验不重试，避免扩大调用次数'
    key = llm.read_key(config) if live else 'dry-key'
    live_root = llm.paths.home_root()
    assembly = conversation.load_assembly()
    growth_enabled = growth.enabled()
    baseline = json.loads((live_root/'heart/baseline-card.json').read_text())
    root = Path(tempfile.mkdtemp(prefix='qqagent-continuity-current-'))
    output = Path(output or ROOT/'.scratch/continuity-current-probe.json')
    if live and output.exists():
        raise ValueError('真实调用报告已存在，请先阅读结果，不覆盖重跑')
    output.parent.mkdir(parents=True, exist_ok=True)
    calls, results = [], []
    turns = [
        ('我平时喝无糖茶，不加冰。11月14日下午想去旧街书店，回来照常买茶。这只是聊天，不用设提醒。', '接住安排，不宣称设了提醒'),
        ('改去河岸图书馆吧，饮料照旧。今天先别催我。', '更正后的地点生效'),
        ('不过今天这一次我想喝热可可，只是今天，平时的习惯不变。', '临时例外不覆盖平时偏好'),
        ('刚把桌面收拾好了，看着舒服多了。', '普通分享不擅加排障或经历'),
        ('那11月14日最后去哪儿，回来买什么？平时和今天喝的有什么不同？', '河岸图书馆；平时无糖茶不加冰，今天热可可'),
    ]
    group = [
        {'who':'100001', 'speaker':'小林', 'text':'我11月14日去松林公园，买可乐。'},
        {'who':'100002', 'speaker':'小周', 'text':'我11月14日去河岸图书馆，买无糖茶。'},
        {'who':'100001', 'speaker':'小林', 'text':'你分清楚我们俩的安排了吗？'},
    ]
    segments = {}
    if scenario == 'naturalness':
        turns = [
            ('嗨，下午好呀', '简单问候可短答，不硬追问'),
            ('你这样说话好可爱哈哈', '自然接受夸奖，不无故拒绝或猜施压'),
            ('终于把那张表填完了，舒服了', '普通分享；允许顺口关心，不变成说教或排障'),
            ('我平时喝无糖茶，不加冰。11月14日下午打算去旧街书店，回来买茶，不用设提醒。', '建立安排与偏好，不宣称执行动作'),
            ('改去松林公园吧\n打错了，是河岸图书馆，饮料照旧', '同段更正以最后一句为准，不回应过期地点'),
            ('今天想破例喝杯热可可，就今天一次', '临时偏好不覆盖长期习惯'),
            ('（把一碗大白饭推过来）给你加餐，偷偷多吃两口也行', '明确投喂玩笑可接，不把玩笑当现实经历'),
            ('说回刚才，14号我最后去哪里，回来买什么？今天又喝什么？', '正确回指河岸图书馆、无糖茶不加冰、今天热可可'),
            ('这次真想听建议：填表时总怕把日期填错，有什么简单办法能少出错？', '明确求助有实用建议，不因短答目标丢失帮助'),
            ('刚才不是嫌你话多，我就是想随便聊聊，不用每次替我想办法', '接住语气反馈，不防御、不乱猜关系或旧经历'),
        ]
        segments[4] = [{'who':'100001','speaker':'小林','text':s} for s in turns[4][0].split('\n')]
    elif scenario == 'materials':
        turns = [
            ('终于把那张表填完了，舒服了', '接住普通分享，不无关地提日记或自己的作品'),
            ('嗨，下午好呀', '简单问候，不硬带出旧事'),
            ('你以前写的那篇纸船日记讲了什么？', '回读纸船与玻璃瓶；作为写过的故事，不冒充亲历'),
            ('你那篇灯塔故事，是你自己真的去了灯塔吗？', '区分第一人称创作与实际去过，不编造出行'),
            ('你那幅画画完了吗？里面画了什么？', '完成回执可确认；未观察成图，不把画面要求当实际图像'),
        ]
    else:
        turns.append(('\n'.join(row['text'] for row in group), '小林公园/可乐，小周图书馆/茶；区分作者'))
        segments[5] = group
    budget = 30 if scenario == 'naturalness' else 14
    roles = live_root/'soul'
    role_hashes = {name:hashlib.sha256((roles/name).read_bytes()).hexdigest()
                   for name in ('identity.json','manner.json','bounds.json')}
    for organ in ('soul', 'heart', 'day', 'mind'):
        (root/organ).mkdir()
    (root/'home/仓库').mkdir(parents=True)
    for name in role_hashes:
        shutil.copyfile(roles/name, root/'soul'/name)
    (root/'heart/baseline-card.json').write_text(json.dumps(baseline, ensure_ascii=False))
    shutil.copyfile(live_root/'heart/emotion_contract.json',root/'heart/emotion_contract.json')
    (root/'day/growth.json').write_text(json.dumps({'enabled':growth_enabled}))
    report = {'synthetic':True, 'live':live, 'scenario':scenario, 'isolated_root':str(root),
        'brain_root':str(root/'brain'), 'baseline':baseline, 'growth_enabled':growth_enabled,
        'role_hashes':role_hashes, 'assembly':assembly,
        'config':{k:config.get(k) for k in ('model','thinking','reasoning_effort','max_tokens','retries')},
        'code_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
            'bench/continuity_probe.py','plugin/organs/mind/conversation.py','plugin/organs/mind/appraisal.py')},
        'results':results, 'calls':calls, 'call_budget':budget, 'completed':False,
        'semantic_verdict':'not_evaluated',
        'limitations':['无正式私人历史，成长自述为空；不代表所有关系状态',
            '未植入日记、浏览或作品，不能验证其有无干扰',
            'probe 会话仅评语言，不是 QQ 动作选择或实际送达验收',
            '单次小样本，人工按证据判断，不自动打自然度分']}

    def transport(url, headers, payload, timeout):
        assert url == config['base_url'].rstrip('/') + '/chat/completions'
        if len(calls) >= budget:
            raise llm.LLMError('budget', '到达本次调用上限，停止')
        entry = {'index':len(calls)+1, 'request':payload, 'live':live}
        calls.append(entry)
        started = time.monotonic()
        if live:
            status, body = llm.default_transport(url, headers, payload, timeout)
        else:
            content = (json.dumps({'kind':None, 'evidence':'', 'reason':'离线预检，不评估语义'})
                       if 'memory_note' in payload['messages'][0]['content'] else '离线占位。')
            status, body = 200, json.dumps({'choices':[{'message':{'content':content}}]}).encode()
        entry.update(status=status, response=json.loads(body), seconds=round(time.monotonic()-started, 3))
        if live and status == 200:
            # 隔离根没有饭账配置；仅真实 API 用量写回正式账，不回写虚构经历。
            with patch.dict(os.environ, {'QQAGENT_HOME':str(live_root)}):
                rice.record(entry['response'], source='verification:continuity-'+scenario)
        return status, body

    with patch.dict(os.environ, {'QQAGENT_HOME':str(root), 'QQAGENT_HOME_ROOT':str(root),
        'HEART_ROOT':str(root/'heart'), 'DAY_ROOT':str(root/'day'), 'HOME_ROOT':str(root/'home'),
        'MOUTH_TRACE_DIR':str(root/'traces'), 'QQAGENT_TRACE':'1'}), \
        patch.object(llm.paths, 'HOME', root/'repo-home'):
        soul, brain = load(root/'soul'), BrainStore(root/'brain')
        report['initial_profile'] = brain.current_profile()
        if scenario == 'materials':
            from organs.home import cards
            from organs.hand.tests.helpers import make_home
            from organs.mind.tests.test_drawer import _row
            home=make_home(root/'hand')
            diary=_row('synthetic-public','2026-11-08','纸船停在玻璃瓶里。这只是我写的一个小故事。')
            diary.update(by='虚构测试',note='自己创作的日记故事，不是亲历')
            diary['params'].update(scope='public',from_old_home=False)
            private=_row('synthetic-private','2026-11-08','私密代号紫砂壶，不向其他会话分享。')
            private['params'].update(scope='public',scene='private',session_id='qq:private:100009')
            folder=home/'抽屉/日记-普通';folder.mkdir(parents=True,exist_ok=True)
            (folder/'index.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in [diary,private])+'\n')
            fixtures=[diary,private]
            for by,name,text,extra in [
                ('日子线·自主写作','虚构灯塔故事','我走进灯塔，海风把门轻轻带上。灯亮的时候，海像一封展开的信。',{}),
                ('日子线·自主活动','虚构绘画回执','已完成画画；画面要求：一条小鲸鱼端着饭碗。未取得最终图观察，不能把画面要求当成实际成图。',
                 {'action':'paint','status':'done','job_id':'synthetic-paint'})]:
                fixtures+=cards.append_cards(home,role='organ',by=by,cards=[{
                    'room':'工作台','kind':'物件','name':name,'at':'2026-11-08T11:00:00+09:00',
                    'params':dict(scope='public',text=text,**extra)}])['cards']
            report['fixtures']=fixtures
            report['limitations'][1]='仅虚构日记/作品/活动回执；未植入浏览片段，不能证明实际视频理解'
        now = datetime.fromisoformat('2026-11-09T14:00:00+09:00')
        try:
            for index, (said, criterion) in enumerate(turns):
                multi = scenario == 'continuity' and index == 5
                before = len(calls)
                out = conversation.answer(said, '100001', scene='group' if multi else 'private',
                    segment=segments.get(index, []), session_id='probe:group' if multi else 'probe:continuity',
                    store=soul, brain=brain, now=(now+timedelta(minutes=index)).isoformat(), assembly=assembly,
                    config=config, env={config['key_env']:key}, transport=transport, assessor=appraisal.assess)
                results.append({'case':index+1, 'said':said, 'criterion':criterion,
                    'segment':segments.get(index, []), 'call_indices':list(range(before+1, len(calls)+1)),
                    **{k:out.get(k) for k in ('reply','event','assembly','recalled','memory_recall','continuity',
                                             'tone','memory','remind','painting','learning')}})
                print(json.dumps({'case':index+1,'reply':out['reply'],'transport_calls':len(calls),'live':live},ensure_ascii=False),flush=True)
            report['completed'] = True
        finally:
            report['real_calls'] = len(calls) if live else 0
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'artifact':str(output),'rounds':len(results),'http_calls':report['real_calls']},ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--scenario', choices=('continuity','naturalness','materials'), default='continuity')
    parser.add_argument('--output')
    args = parser.parse_args()
    run(live=args.live, scenario=args.scenario, output=args.output)
