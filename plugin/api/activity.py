"""已有执行记录的只读汇总；不另存事件、不把计划写成经历。"""
from datetime import date, datetime
import json
import os
from pathlib import Path
from urllib.parse import quote

from organs import paths
from organs.day import model, night, phone_browse, proactive, growth, solitude, life
from organs.home import art, expressions, knowledge, memes, store
from organs.heart import changes
from organs.mind import recent, stroke_painting, learning

SOURCES = {'phone': '手机浏览汇总', 'phone_event': '手机过程', 'phone_settings': '手机开关变化', 'painting': '绘画汇总', 'painting_event': '绘画过程', 'chat': '聊天底稿', 'delivery': 'QQ 通道',
           'night': '夜间整理', 'heart': '状态变化', 'eye': '看图', 'ear': '听语音', 'voice': '合成语音',
           'model': '模型调用', 'reminder': '提醒评估', 'proactive': '主动开口', 'learning': '字幕资料任务',
           'expression': '评论表达学习', 'autonomy': '自主活动', 'growth': '认识变化',
           'growth_settings': '自主活动设置', 'decision': 'QQ 发言判断', 'meme': '表情图库变化', 'soul': '初始设定变化',
           'model_settings': '模型设置变化', 'life': '生活背景设置与调整', 'art_assets': '画室素材变化',
           'people': '人物名片变化', 'memory_edit': '记忆理解修改', 'preference': '相处偏好变化'}
TEST_ORIGINS = {'acceptance', 'validation_run'}


def dispatch(query):
    try:
        selected = query.get('date') or datetime.now(model.local_tz()).date().isoformat()
        if date.fromisoformat(selected).isoformat() != selected:
            raise ValueError
        source = query.get('source', 'all')
        limit, offset = int(query.get('limit', '20')), int(query.get('offset', '0'))
        include_tests = query.get('include_tests', '0')
        if source not in ('all', *SOURCES) or not 1 <= limit <= 100 or offset < 0 or include_tests not in ('0', '1'):
            raise ValueError
    except ValueError:
        return 400, {'ok': False, 'message': '请检查日期、记录来源和分页参数'}
    rows, errors, available = [], {}, {}
    execution_jobs = {}
    invalid = hidden = 0
    sources = SOURCES if source == 'all' else [source]
    for kind in sources:
        try:
            if kind in ('phone_settings', 'growth_settings'):
                settings = phone_browse.config() if kind == 'phone_settings' else growth.config()
                records = [dict(change, source_line=index+1, origin='manual')
                           for index, change in enumerate(settings.get('changes', []))]
                available[kind] = 'changes' in settings
            elif kind == 'life':
                records = [dict(change, id='settings:'+str(index), event='settings', source_line=index+1)
                           for index, change in enumerate(life.config().get('changes', []))]
                plans = sorted((model.day_root()/'life-days').glob('*.json'))
                available[kind] = (model.day_root()/'life.json').is_file() or bool(plans)
                for path in plans:
                    saved = json.loads(path.read_text(encoding='utf-8'))
                    records.extend(dict(change, id=path.stem+':'+str(index), day=path.stem,
                                        event='adjust', source_line=index+1)
                                   for index, change in enumerate(saved.get('changes', [])))
            elif kind == 'art_assets':
                records = [dict(change, source_line=index+1)
                           for index, change in enumerate(art.library().get('changes', []))]
                available[kind] = (art.library_root()/'assets.json').is_file()
            elif kind == 'meme':
                records = [dict(change, source_line=index+1, origin='manual')
                           for index, change in enumerate(memes.catalog().get('changes', []))]
                available[kind] = (memes.library_root() / 'index.json').is_file()
            elif kind in ('phone', 'phone_event'):
                records = phone_browse.view(limit=None)['jobs']
                available[kind] = (model.day_root() / 'phone-sessions').is_dir()
                if kind == 'phone_event':
                    records = [dict(event, id=job['id']+':'+str(index), session_id=job['id'],
                                    origin=job.get('origin', 'unknown'), source_line=index+1,
                                    **({'model_turn_id':job['model_turn_id']} if job.get('model_turn_id') else {}))
                               for job in records for index, event in enumerate(job.get('events', []))]
            elif kind in ('painting', 'painting_event'):
                records = [stroke_painting.state(job) for job in art.painting_jobs()]
                available[kind] = bool(records)
                if kind == 'painting_event':
                    records = [dict(event, id=job['id']+':'+str(index), job_id=job['id'],
                                    origin=job.get('origin', 'unknown'), source_line=index+1,
                                    **({'model_turn_id':job['model_turn_id']} if job.get('model_turn_id') else {}))
                               for job in records for index, event in enumerate(job.get('events', []))]
            elif kind == 'learning':
                records = [learning.state(job) for job in knowledge.jobs()]
                available[kind] = knowledge.library_root().is_dir()
            elif kind == 'heart':
                history = changes.read_history()
                records = history['rows']
                available[kind] = history['available']
                invalid += history['invalid_records']
            else:
                if kind == 'model':
                    # 与观察页读同一份追踪；关闭写入开关不影响读取已有记录。
                    trace = Path(os.environ.get('QQAGENT_HOME_ROOT') or paths.home_root()) / 'mind' / 'trace'
                    files = sorted(trace.glob('????-??-??.jsonl'))
                    available[kind] = bool(files)
                else:
                    if kind == 'autonomy':
                        path = model.day_root() / solitude.LOG_FILE
                    elif kind == 'growth':
                        path = paths.organ_dir('brain') / 'selfnarration.jsonl'
                    elif kind == 'soul':
                        path = paths.organ_dir('soul') / 'history.jsonl'
                    elif kind == 'model_settings':
                        path = paths.organ_dir('mind') / 'config_history.jsonl'
                    elif kind == 'preference':
                        path = paths.organ_dir('brain') / 'interaction_preferences.jsonl'
                    elif kind in ('people', 'memory_edit'):
                        path = paths.organ_dir('brain') / ('people_card_history.jsonl' if kind == 'people' else 'note_history.jsonl')
                    elif kind in ('proactive', 'reminder'):
                        path = proactive.log_path() if kind == 'proactive' else proactive.task_log_path()
                    elif kind == 'expression':
                        path = expressions.library_root() / 'usage.jsonl'
                    elif kind in ('eye', 'ear', 'voice'):
                        path = paths.organ_dir('mouth' if kind == 'voice' else kind) / ('voice.jsonl' if kind == 'voice' else 'observations.jsonl')
                    elif kind == 'decision':
                        path = paths.REPO_ROOT / 'channels/onebot/.state/decisions.jsonl'
                    else:
                        path = (night.log_path() if kind == 'night' else recent.path_of() if kind == 'chat' else
                                paths.REPO_ROOT / 'channels/onebot/.state/ledger.jsonl')
                    available[kind] = path.is_file()
                    files = [path] if available[kind] else []
                records = []
                for path in files:
                    with path.open(encoding='utf-8') as handle:
                        for number, line in enumerate(handle, 1):
                            if not line.strip():
                                continue
                            try:
                                record = json.loads(line)
                                if not isinstance(record, dict):
                                    raise ValueError
                            except ValueError:
                                invalid += 1
                                continue
                            if record.get('accounting_only') is True:
                                continue  # 重养时保留的额度/去重记录不是新经历。
                            records.append(dict(record, source_line=number, source_date=path.stem))
                if kind == 'autonomy':
                    records = [r for r in records if r.get('event') in
                               ('autonomy_attempt', 'autonomy_result', 'autonomy_finished')]
                elif kind == 'growth':
                    previous, versions = {}, []
                    for record in records:
                        profile = record.get('profile')
                        if not isinstance(profile, dict):
                            continue
                        record['changes'] = {key: {'before': previous.get(key), 'after': profile.get(key)}
                                             for key in sorted(previous.keys() | profile.keys())
                                             if previous.get(key) != profile.get(key)}
                        versions.append(record)
                        previous = profile
                    records = versions
                if kind == 'expression':
                    available[kind] = expressions.library_root().is_dir()
                    for saved in expressions.records():
                        common = {'record_id':saved['id'], 'context':saved['source']['context'],
                                  'origin':saved.get('origin', 'unknown'),
                                  'source_rejected_now':bool(saved.get('source_rejected'))}
                        cards = [{key:card[key] for key in ('id', 'phrase', 'review') if key in card}
                                 for card in saved['cards']]
                        records.append(dict(common, id=saved['id']+':created', at=saved['created_at'],
                                            event='created', cards=[{'id':c['id'],'phrase':c['phrase']} for c in cards]))
                        if saved.get('reviewed_at'):
                            records.append(dict(common, id=saved['id']+':reviewed', at=saved['reviewed_at'],
                                                event='reviewed', cards=cards))
                        for index, change in enumerate(saved.get('selection_history', [])):
                            records.append(dict(common, id=saved['id']+':selection:'+str(index),
                                                at=change['at'], event='selection', changes=change['changes']))
            preference_history = {(r['who'], r['session_id'], r['id']): r for r in records} if kind == 'preference' else {}
            for record in records:
                stamp = record.get('created_at') if kind in ('phone', 'painting') else record.get('at') if kind in ('heart', 'eye', 'ear', 'voice', 'model') else record.get('ts')
                if kind == 'learning':
                    stamp = record.get('updated_at') or record.get('created_at')
                elif kind in ('proactive', 'reminder', 'expression', 'phone_event', 'phone_settings', 'painting_event', 'autonomy', 'growth_settings', 'meme', 'model_settings', 'life', 'art_assets', 'preference'):
                    stamp = record.get('at')
                    if kind == 'autonomy' and record.get('event') == 'autonomy_result':
                        stamp = record.get('recorded_at') or stamp
                try:
                    at = model.parse_ts(stamp).astimezone(model.local_tz())
                except ValueError:
                    invalid += 1
                    continue
                if at.date().isoformat() != selected:
                    continue
                origin = record.get('origin') or 'unknown'
                # 心的origin是聊天出处对象，其余来源的origin是运行/验收标签。
                if kind == 'heart' and isinstance(origin, dict):
                    origin = 'unknown'
                is_test = origin in TEST_ORIGINS or kind == 'soul' and record.get('kind') == 'probe'
                if is_test and include_tests == '0':
                    hidden += 1
                    continue
                row = {'id': kind + ':' + str(record.get('id') or record.get('source_line')),
                       'at': at.isoformat(), 'source': kind, 'origin': origin, 'is_test': is_test}
                if kind in ('phone','phone_event','painting','painting_event') and record.get('model_turn_id'):
                    row['model_turn_id'] = record['model_turn_id']
                if kind in ('phone_settings', 'growth_settings'):
                    row.update(status='setting', title='修改手机自主浏览开关' if kind == 'phone_settings' else '修改自主活动设置',
                               detail='', link='phone' if kind == 'phone_settings' else 'day#growth-card',
                               changes=record.get('changes', {'enabled': {'before':record['before'], 'after':record['after']}}),
                               source_line=record['source_line'])
                elif kind == 'preference':
                    previous = [preference_history.get((record['who'], record['session_id'], key))
                                for key in record['replaces']]
                    status = 'withdrawn' if not record['text'] else 'updated' if record['replaces'] else 'learned'
                    row.update(status=status, title={'learned':'记住相处偏好', 'updated':'调整相处偏好', 'withdrawn':'撤回相处偏好'}[status],
                               actor=record['actor'], person_id=record['who'], session_id=record['session_id'],
                               detail='只用于此人与此会话的相处参考，不修改全局性格。', evidence=record['evidence'],
                               changes={'interaction_preference':{'before':[p['text'] for p in previous if p is not None],
                                                                 'after':[record['text']] if record['text'] else []}},
                               missing_previous=[key for key, old in zip(record['replaces'], previous) if old is None],
                               memory_links=[{'id':key, 'link':'galaxy?memory='+quote('event:'+key, safe='')}
                                             for key in record.get('entry_ids', [])],
                               link='about?who='+quote(record['who'], safe='')+'#preferences-card')
                    turn = record.get('turn_id')
                    if isinstance(turn, str) and len(turn) == 32 and all(c in '0123456789abcdef' for c in turn):
                        row['model_turn_id'] = turn
                    row['origin'] = {'panel':'manual', 'conversation':'conversation'}.get(record['actor'], 'unknown')
                elif kind == 'people':
                    row.update(status='edited', title='修改人物名片', detail=record['field'],
                               actor=record.get('actor'), person_id=record['person_id'],
                               changes=record.get('changes'), summary_only='changes' not in record,
                               before_summary=record.get('old_summary'), after_summary=record.get('new_summary'), link='about')
                elif kind == 'memory_edit':
                    row.update(status='edited', title='修改记忆理解', detail='原话保留；这里记录对原话的理解修改。',
                               actor=record.get('actor'), entry_id=record['entry_id'],
                               changes=record.get('changes', {'note':{'before':record.get('old_note'), 'after':record.get('new_note')}}),
                               link='galaxy?memory='+quote('event:'+record['entry_id'], safe=''))
                elif kind == 'life':
                    setting = record['event'] == 'settings'
                    row.update(status='setting' if setting else 'edited',
                               title='修改生活背景开关' if setting else '调整生活背景安排',
                               detail='生活背景安排的修改，不代表实际经历。', link='day#life-card',
                               source_line=record['source_line'], day=record.get('day'),
                               revision=record.get('revision'), actor=record.get('by'),
                               changes={'enabled':{'before':record['before'],'after':record['after']}} if setting else
                                 {key:{'before':record['before'].get(key),'after':record['after'].get(key)}
                                  for key in ('start','end','activity','location','outfit','detail')
                                  if record['before'].get(key) != record['after'].get(key)})
                elif kind == 'art_assets':
                    before, after = dict(record.get('before') or {}), dict(record['after'])
                    for version in (before, after):
                        if 'images' in version:
                            version['images'] = [{key:image[key] for key in ('sha256','mime','width','height') if key in image}
                                                 for image in version['images']]
                    row.update(status='created' if record.get('before') is None else 'edited',
                               title='添加画室素材' if record.get('before') is None else '修改画室素材',
                               detail=after.get('name',''), asset_id=after['id'], revision=after['revision'],
                               link='studio', source_line=record['source_line'],
                               changes={key:{'before':before.get(key),'after':after.get(key)}
                                        for key in ('name','category','description','images')
                                        if before.get(key) != after.get(key)})
                elif kind == 'model_settings':
                    row.update(status='setting', title='修改主模型调用参数', detail='',
                               changes=record['changes'], trigger_source=record.get('source'),
                               source_line=record['source_line'], link='settings#model-card')
                elif kind == 'meme':
                    phase = record['kind']
                    if phase not in ('settings', 'edit', 'import'):
                        invalid += 1
                        continue
                    before, after = record.get('before', {}), record.get('after', {})
                    row.update(status=phase, title={'settings':'修改聊天选图开关', 'edit':'修改表情资料',
                               'import':'导入表情图库'}[phase], link='memes', source_line=record['source_line'],
                               detail=after.get('name', ''),
                               changes={key: {'before':before.get(key), 'after':after.get(key)}
                                        for key in ('chat_enabled', 'description', 'visible_text', 'keywords', 'enabled', 'reviewed')
                                        if before.get(key) != after.get(key)})
                    if phase == 'import':
                        row['imported'] = record['imported']
                    elif phase == 'edit':
                        row['meme_id'] = after['id']
                elif kind == 'soul':
                    phase = record.get('kind') or 'recorded'
                    field = '.'.join(str(record[k]) for k in ('block', 'path') if record.get(k))
                    row.update(status=phase, title={'edit':'修改初始设定', 'manual':'人工调整初始设定',
                               'migrate':'迁移初始设定', 'proposal':'采纳设定提案', 'probe':'设定验收测试',
                               'rollback':'回滚初始设定'}.get(phase, '初始设定历史记录'),
                               detail=record.get('reason', ''), actor=record.get('actor'), field=field,
                               before_summary=record.get('old_summary'), after_summary=record.get('new_summary'),
                               revision=record.get('revision'), target_revision=record.get('target_revision'),
                               source_line=record['source_line'], link='./#history-list')
                elif kind == 'growth':
                    row.update(status='changed', title='第 ' + str(record['version']) + ' 版自我认识',
                               detail=record.get('note', ''), version=record['version'],
                               changes=record['changes'], sources=record.get('sources', []), link='./#growth-card',
                               source_line=record['source_line'])
                elif kind == 'autonomy':
                    event, action = record['event'], record.get('action')
                    if event == 'autonomy_result':
                        row.update(started_at=record.get('at'), recorded_at=record.get('recorded_at'))
                    status = 'started' if event == 'autonomy_attempt' else record['status']
                    if action == 'idle' and status == 'done':
                        status = 'idle'
                    row.update(status=status, title={'idle':'这次闲着', 'plan':'处理自己的打算',
                        'write':'写自己的作品', 'read':'读自己的作品',
                        'paint':{'strokes':'逐笔绘画','image':'本地模型绘画'}.get(record.get('mode', 'strokes'), '绘画'),
                        'browse':'浏览片段'}.get(action, '选择自己的活动'),
                        detail=record.get('error_detail') or record.get('reason') or record.get('error', ''), event=event,
                        model_turn_id=record.get('model_turn_id'), intention_id=record.get('intention_id'),
                        receipt=record.get('receipt'), reaction=record.get('reaction'),
                        reaction_error=record.get('reaction_error'), action=action, link='day#growth-card',
                        source_line=record['source_line'])
                    receipt = record.get('receipt') or {}
                    job_id = receipt.get('job_id') or receipt.get('id')
                    if action in ('paint', 'browse') and job_id:
                        if action not in execution_jobs:
                            jobs = ([stroke_painting.state(job) for job in art.painting_jobs()]
                                    if action == 'paint' else phone_browse.view(limit=None)['jobs'])
                            execution_jobs[action] = {job['id']: job for job in jobs}
                        job = execution_jobs[action].get(job_id)
                        execution = {'job_id': job_id, 'status': job['status'] if job else 'missing'}
                        if job:
                            if job.get('model_turn_id'):
                                execution['model_turn_id'] = job['model_turn_id']
                            if action == 'paint':
                                execution['mode'] = job.get('request', {}).get('mode', 'strokes')
                        row['execution'] = execution
                elif kind == 'phone':
                    row.update(status=record['status'], title=record.get('title') or '浏览机会',
                               detail=record.get('error_detail') or record.get('note', ''), finished_at=record.get('finished_at'), link='phone')
                elif kind == 'phone_event':
                    event = record.get('kind', 'unknown')
                    row.update(status='recorded', event_kind=event, session_id=record['session_id'],
                               source_line=record['source_line'], link='phone',
                               title={'intention':'决定是否拿起手机', 'selection':'选择推荐视频',
                                      'target':'重新确认视频位置', 'target_changed':'视频位置已变化',
                                      'action':'手机操作记录', 'audio':'播放声音采集与转写',
                                      'frame':'采样一帧画面', 'observation':'片段画面理解',
                                      'expressions':'保存评论表达候选', 'reflection':'片段感想处理',
                                      'expression_review':'评论表达复核'}.get(event, event),
                               detail=record.get('reason') or record.get('scope', ''))
                    for key in ('action', 'index', 'cards', 'record_id', 'captured_at'):
                        if key in record:
                            row[key] = record[key]
                    if 'status' in record:
                        row['event_status'] = record['status']
                    if event == 'selection':
                        row['selected_title'] = next((o['title'] for o in record.get('options', [])
                                                      if o['node'] == record.get('node')), None)
                    elif event == 'target':
                        row['selected_title'] = record.get('selected', {}).get('title')
                elif kind == 'painting':
                    row.update(status=record['status'], title=record['request']['prompt'],
                               detail=record.get('error_detail') or record.get('error', ''), finished_at=record.get('finished_at'),
                               delivery_status=record.get('delivery', {}).get('status', 'not_recorded'), link='studio')
                elif kind == 'painting_event':
                    event = record.get('kind', 'unknown')
                    row.update(status=record.get('status', 'unknown'), event_kind=event, job_id=record['job_id'],
                               source_line=record['source_line'], link='studio',
                               title={'planning':'规划笔触', 'planned':'笔触规划已生成', 'draft':'初稿已渲染',
                                      'observation':'观察初稿画面', 'review':'检查初稿并决定是否修改',
                                      'revision':'改稿已渲染', 'finished':'绘画任务结束', 'delivery':'发送绘画结果'}.get(event,event),
                               detail=record.get('reason', ''))
                    for key in ('action', 'stroke_count', 'delivery_kind'):
                        if key in record:
                            row[key] = record[key]
                elif kind == 'learning':
                    row.update(status=record['status'], title=record.get('source', {}).get('meta', {}).get('title') or '字幕资料任务',
                               detail=record.get('note', ''), created_at=record.get('created_at'),
                               completed_segments=record.get('next_segment', 0), total_segments=record.get('total_segments'),
                               card_count=len(record.get('cards', [])), rejected_count=len(record.get('rejected', [])),
                               delivery_status=record.get('delivery', {}).get('status', 'not_recorded'), link='learning')
                elif kind == 'expression':
                    phase = record.get('event') or record.get('kind') or 'unknown'
                    row.update(status=phase, title={'created':'提炼评论表达候选', 'reviewed':'模型逐卡复核',
                               'selection':'人工调整采用', 'offered_to_model':'交给回复模型参考'}.get(phase,'表达学习记录'),
                               detail=record.get('context', ''), cards=record.get('cards', []),
                               changes=record.get('changes', []), card_ids=record.get('card_ids', []),
                               record_id=record.get('record_id'), source_rejected_now=record.get('source_rejected_now', False),
                               session_id=record.get('session_id'), delivery_id=record.get('delivery_id'),
                               link='expressions', source_line=record.get('source_line', 0))
                elif kind == 'reminder':
                    row.update(status=record.get('action', 'unknown'), title=record.get('name') or '提醒评估',
                               detail=record.get('reason', ''), due=record.get('due'), card_id=record.get('card_id'),
                               evaluation_count=record.get('count', 1), reminded_count=record.get('reminded_count'),
                               link='day', source_line=record['source_line'])
                elif kind == 'proactive':
                    # 旧版 due 阶段曾把日志入队记成 sent；只有 ack 阶段有通道确认依据。
                    action = record.get('action', 'unknown')
                    row.update(status='legacy_sent' if action == 'sent' and record.get('stage') != 'ack' else action,
                               title='任务提醒' if record.get('reason_code') == 'pending_task' else '主动开口',
                               detail=record.get('because', ''), stage=record.get('stage'), who=record.get('who'),
                               reason_code=record.get('reason_code'), engage=record.get('engage', {}),
                               after_generation=record.get('after_generation') is True,
                               correction_reason=record.get('reason'), correction_of=record.get('correction_of'),
                               error=record.get('error') or record.get('error_kind'), plan_id=record.get('plan_id'),
                               delivery_id=record.get('delivery_id'), link='day', source_line=record['source_line'])
                    row.update(turn_id=record.get('turn_id'), confirmed_text=record.get('confirmed_text'),
                               receipt=record.get('receipt'))
                    if record.get('stage') == 'config':
                        row['changes'] = record.get('changes', {})
                elif kind == 'chat':
                    row.update(status='recorded', title=(record.get('who') or '对方') + '的消息',
                               detail=record.get('said', ''), link='observe', source_line=record['source_line'])
                elif kind == 'night':
                    row.update(status=record.get('event') or 'recorded', title='夜间整理',
                               detail=record.get('reason', ''), day=record.get('day', ''),
                               attempt=record.get('attempt'), steps=record.get('steps', {}),
                               retried_steps=record.get('retried_steps'),
                               failed_steps=record.get('failed_steps', record.get('failed', [])),
                               next_retry_at=record.get('next_retry_at'),
                               link='day', source_line=record['source_line'])
                    if record.get('event') in ('growth_attempt', 'growth_result'):
                        result = record.get('result', {})
                        status = 'started' if record['event'] == 'growth_attempt' else result.get('status', 'unknown')
                        if status == 'ok':
                            status = 'changed' if result.get('changed') else 'unchanged'
                        row.update(status=status, title='整理自己的认识',
                                   model_turn_id=record.get('model_turn_id'),
                                   detail=result.get('record', {}).get('note') or result.get('error', ''),
                                   version=result.get('record', {}).get('version'))
                elif kind == 'heart':
                    row.update(status='recorded', title=record['reason'], detail='',
                               group=record['group'], group_label=changes.GROUPS[record['group']],
                               changes=record['changes'], trigger_source=record.get('source'),
                               from_at=record.get('from_at'), link='heart', source_line=record['source_line'])
                    if isinstance(record.get('origin'), dict):
                        row['emotion_origin'] = record['origin']
                elif kind == 'model':
                    # 一次调用可有多次尝试；文件日期和行号唯一，不用call_id吞掉重试。
                    turn = record.get('turn_id') or ''
                    linked = isinstance(turn, str) and len(turn) == 32 and all(c in '0123456789abcdef' for c in turn)
                    status = record.get('status')
                    failed = bool(record.get('error')) or (type(status) is int and status >= 400)
                    request = record.get('request') or {}
                    try:
                        response = json.loads(record.get('raw') or '{}')
                    except (ValueError, TypeError):
                        response = {}
                    row.update(id='model:' + record['source_date'] + ':' + str(record['source_line']),
                               source_date=record['source_date'], source_line=record['source_line'],
                               title=record.get('purpose') or '用途未记录',
                               status='failed' if failed else 'completed' if status == 200 and record.get('reply') else 'unknown',
                               detail=record.get('error') or '', http_status=status,
                               duration_ms=record.get('duration_ms'), attempt=record.get('attempt'),
                               call_index=record.get('call_index'), turn_id=turn if linked else '',
                               requested_model=request.get('model') if isinstance(request, dict) else None,
                               actual_model=response.get('model') if isinstance(response, dict) else None,
                               link='observe?source=model&' + ('turn_id=' + turn if linked else 'date=' + record['source_date']))
                elif kind in ('eye', 'ear', 'voice'):
                    # 同一语音任务的 attempt/result 是两条记录，不能用任务编号充当行编号。
                    row.update(id=kind + ':' + str(record['source_line']), source_line=record['source_line'],
                               link='observe?source=' + kind, title=record.get('purpose') or SOURCES[kind])
                    if record.get('kind') == 'setting':
                        row.update(status='setting', detail='', changes={record['parameter']: {
                            'before':record['before'], 'after':record['after']}}, trigger_source=record.get('source'))
                    else:
                        result = record.get('transcription', {}) if kind == 'ear' else record
                        row.update(status=result.get('status') or record.get('kind') or 'unknown',
                                   detail=result.get('reason', ''))
                        if kind == 'eye':
                            row.update(observation=record.get('observation', {}) if row['status'] == 'ok' else {},
                                       cached=record.get('cached', False))
                        elif kind == 'ear':
                            row['transcript'] = result.get('transcript', '') if row['status'] in ('ok', 'partial', 'unclear') else ''
                            if result.get('sound_events'):
                                row['sound_events'] = result['sound_events']
                        else:
                            row.update(duration=record.get('duration'), characters=record.get('characters'))
                elif kind == 'decision':
                    machine = record.get('machine')
                    if not isinstance(machine, dict) or (machine.get('batch') is not None and not isinstance(machine['batch'], dict)):
                        invalid += 1
                        continue
                    row.update(status='recorded', title=record.get('human') or '发言判断',
                               detail=machine.get('why') or '', decision=machine.get('decided'),
                               reason_code=machine.get('reason_code'), reason=machine.get('reason'), target=machine.get('target'),
                               batch=machine.get('batch'), source_line=record['source_line'],
                               link='observe?source=decision&date=' + selected)
                    if machine.get('decided') == 'silent' and machine.get('reason_code') == 'core_empty':
                        row['title'] = {'rest-deep':'彻底睡着，未回复', 'rest-asleep':'正在睡觉，未回复',
                                        'rest-holding':'硬撑期间未参与这段'}.get(machine.get('reason') or '', '本轮没有可发送的回复')
                else:
                    row.update(status=record.get('kind', 'unknown'), title=record.get('target') or 'QQ 通道事件',
                               detail=record.get('text') or record.get('note', ''), mode=record.get('mode', 'unknown'),
                               link='observe', source_line=record['source_line'])
                if kind in ('chat', 'delivery', 'eye', 'ear', 'voice'):
                    turn = record.get('turn_id')
                    exact_turn = (kind == 'chat' and isinstance(turn, str) and len(turn) == 32
                                  and all(c in '0123456789abcdef' for c in turn))
                    row['link'] = 'observe?source=' + kind + ('&turn_id=' + turn if exact_turn else '&date=' + selected)
                rows.append(row)
        except (OSError, ValueError, KeyError, TypeError, store.StoreError) as exc:
            # 各来源独立读盘，错误明确展示，不能把读失败当成没有事件。
            errors[kind] = '读取失败：' + type(exc).__name__
    rows.sort(key=lambda row: (row['at'], row['source'], row.get('source_line', 0), row['id']), reverse=True)
    return 200, {'ok': True, 'date': selected, 'source': source, 'sources': SOURCES,
                 'timezone': str(model.local_tz()), 'total': len(rows), 'offset': offset, 'limit': limit,
                 'rows': rows[offset:offset + limit], 'has_older': len(rows) > offset + limit,
                 'hidden_tests': hidden, 'invalid_records': invalid, 'errors': errors, 'available': available}
