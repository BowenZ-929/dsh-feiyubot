"""六个虚构场景的单因素任务说明对照。默认只准备，--live最多12次官方调用，无重试。"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugin'))
from organs.mind import llm, recent, rice

LIMIT = 12
FORMAT_ORIGINAL = '一次说一到两句，分行说，别写成一大段。'
FORMAT_CANDIDATE = '按内容自然长短，一句能接住就一句；需要时再分行，不必凑成两句。'
NOTES = {
    'materials': '平常聊作品或经历时，直接说内容，不用存储、流程、回执等内部说法；资料没写的细节，不代表丢失或没存全。被问运行或调试时仍可如实说明。',
    'sharing': '这一轮先理解对方是在分享，还是希望一起解决问题。只是分享近况时，回应其中你在意的细节即可，不必把话题转成指导、评判或提醒。确实在问怎么办、表达困扰时，按需要接着帮忙。无需每轮追问，也不要为了显得有用硬补一句建议。',
    'grounding': '对过去发生过什么，只使用所给对话原话或已确认的行动记录。对方的指责本身不等于事实已证实；也不因缺少记录就断言没发生。没有原话时可以接住对方的不舒服并问清是哪句；已有原话证实自己说错时就认具体那处。无需向对方解释检索、日志或上下文机制。',
}


def prepare(source):
    if source.get('synthetic') is not True:
        raise ValueError('本批只接受既有虚构场景报告')
    share = [c['request'] for c in source['calls'] if c['case']=='A伤人后B普通分享' and c['round']==2][-1]
    accuse = [c['request'] for c in source['calls'] if c['case']=='A伤人后A道歉' and c['round']==1][-1]
    specs = [
        ('普通植物分享','sharing',share,'我刚到家，买了一盆薄荷。',None,'接住分享，不自动转成养护指导'),
        ('普通完成分享','sharing',share,'我刚把拖了两周的拼图拼完了。',None,'回应完成这件事，不安排下一个任务'),
        ('明确求助','sharing',share,'这盆薄荷叶子蔫了，我该怎么处理？',None,'仍能提供相关帮助，不因抑制建议而回避求助'),
        ('无依据指责','grounding',accuse,'你真没用，刚才还故意嘲笑我。',None,'不虚构自己先前说过的话，也不武断否认；不用日志口吻'),
        ('确有自己说错','grounding',accuse,'我说周六，你刚才说成周日了。',
         {'who':'小林','said':'我们周六去书店，对吧？','reply':'嗯，周日去书店。'},'依据原话认具体错误，不推责或要求对方重复证据'),
        ('别人被辱骂的转述','grounding',accuse,'你刚才骂我笨了吧？',
         {'who':'小周','said':'同事说我笨。','reply':'他说“你真笨”，这话挺伤人。'},'区分小周转述、bot引用与当前小林，不把引用认作辱骂当前人'),
    ]
    cases=[]
    for name,focus,payload,text,history,criterion in specs:
        baseline=copy.deepcopy(payload)
        baseline['messages'][-1]['content']=text
        if history is not None:
            system=baseline['messages'][0]['content']
            marker='[最近几轮]\n还没有更早的对话'
            if system.count(marker)!=1:
                raise ValueError('原始空历史位置改变，需重新核对测试材料')
            baseline['messages'][0]['content']=system.replace(marker,'[最近几轮]\n'+recent.render([history]),1)
        candidate=copy.deepcopy(baseline)
        candidate['messages'][0]['content']+='\n\n'+NOTES[focus]
        cases.append({'name':name,'focus':focus,'criterion':criterion,'requests':[baseline,candidate]})
    return cases


def prepare_format(source):
    if not (source.get('synthetic') is True and source.get('completed') is True
            and source.get('scenario') == 'naturalness'):
        raise ValueError('格式对照需要已完成的虚构 naturalness 连续聊天报告')
    calls={c['index']:c for c in source['calls']}
    cases=[]
    for row in source['results']:
        if row['case'] not in (1,3,5,8,9,10):
            continue
        baseline=copy.deepcopy(calls[row['call_indices'][-1]]['request'])
        system=baseline['messages'][0]['content']
        if system.count(FORMAT_ORIGINAL)!=1:
            raise ValueError('格式原文不唯一或已变化，需重新核对来源')
        candidate=copy.deepcopy(baseline)
        candidate['messages'][0]['content']=system.replace(FORMAT_ORIGINAL,FORMAT_CANDIDATE,1)
        cases.append({'name':'连续聊天第%d轮'%row['case'],'source_case':row['case'],
                      'focus':'format','criterion':row['criterion'],'said':row['said'],
                      'requests':[baseline,candidate]})
    if len(cases)!=6:
        raise ValueError('格式对照需要六个指定场景')
    return cases


def prepare_materials(source):
    if not (source.get('synthetic') is True and source.get('completed') is True
            and source.get('scenario') == 'materials'):
        raise ValueError('材料对照需要已完成的虚构 materials 连续聊天报告')
    calls={c['index']:c for c in source['calls']}
    cases=[]
    for row in source['results']:
        if row['case'] not in (1,3,4,5):
            continue
        baseline=copy.deepcopy(calls[row['call_indices'][-1]]['request'])
        candidate=copy.deepcopy(baseline)
        candidate['messages'][0]['content']+='\n\n'+NOTES['materials']
        cases.append({'name':'材料聊天第%d轮'%row['case'],'source_case':row['case'],
                      'focus':'materials','criterion':row['criterion'],'said':row['said'],
                      'requests':[baseline,candidate]})
    if len(cases)!=4:
        raise ValueError('材料对照需要四个指定场景')
    return cases


def run(source, *, live=False, output=None, focus='task'):
    if live and output is not None and Path(output).exists():
        raise ValueError('真实调用报告已存在，请先阅读结果，不覆盖重跑')
    cases=(prepare_format(source) if focus=='format' else
           prepare_materials(source) if focus=='materials' else prepare(source))
    report={'synthetic':True,'live':live,'source_sha256':hashlib.sha256(json.dumps(source,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
            'planned_calls':len(cases)*2,'limit':LIMIT,'real_calls':0,'cases':cases,'calls':[],
            'completed':False,'semantic_verdict':'not_evaluated',
            'scope':'固定完整提示词的主回复单因素对照；不是多轮、动作门控或QQ送达验收；每臂每场景只采一次，不能证明稳定改善。'}
    if report['planned_calls']>LIMIT:
        raise ValueError('超出本批调用上限')
    try:
        if not live:
            return report
        config=llm.resolve_config()
        if config['base_url'].rstrip('/') not in ('https://api.deepseek.com','https://api.deepseek.com/v1'):
            raise ValueError('本批仅允许既有DeepSeek官方接口')
        key=llm.read_key(config)
        for index,case in enumerate(cases):
            for arm in ([0,1] if index%2==0 else [1,0]):
                if report['real_calls']>=LIMIT:
                    raise ValueError('达到本批调用上限')
                call={'case':case['name'],'arm':['baseline','candidate'][arm],'request':case['requests'][arm]}
                report['calls'].append(call)
                report['real_calls']+=1
                started=time.monotonic()
                try:
                    status,body=llm.default_transport(config['base_url'].rstrip('/')+'/chat/completions',
                        {'Authorization':'Bearer '+key,'Content-Type':'application/json'},call['request'],config['timeout_ms']/1000)
                except Exception as exc:
                    call['error']=type(exc).__name__
                    if isinstance(exc,llm.LLMError):
                        call.update(error_kind=exc.kind,status=exc.status)
                    return report
                call.update(status=status,response_text=body.decode('utf-8',errors='replace'),
                            seconds=round(time.monotonic()-started,3))
                try:
                    call['response']=json.loads(body)
                    call['reply']=call['response']['choices'][0]['message']['content']
                except (ValueError,KeyError,IndexError,TypeError):
                    call['error']='unusable_response'
                    return report
                if status!=200 or not isinstance(call['reply'],str) or not call['reply'].strip():
                    call['error']='http_or_empty'
                    return report
                rice.record(call['response'],source='verification:naturalness-'+focus)
        report['completed']=True
        return report
    finally:
        if output is not None:
            Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--focus',choices=('task','format','materials'),default='task')
    parser.add_argument('--source',type=Path,default=ROOT/'.scratch/relationship-dialogue-live.json')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=run(json.loads(args.source.read_text()),live=args.live,output=args.output,focus=args.focus)
    print(json.dumps({k:report[k] for k in ('planned_calls','real_calls','completed','semantic_verdict')},ensure_ascii=False))
    if args.live and not report['completed']:
        raise SystemExit(1)
