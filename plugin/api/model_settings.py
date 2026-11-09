"""主模型的公共调用参数；密钥和服务地址仍由本地配置管理。"""
import json
import math
from datetime import datetime

from organs import paths
from organs.home.store import atomic_write_text
from organs.mind import llm

MODEL_ROUTES = {'/model/status':'GET', '/model/config':'POST'}
FIELDS = ('model', 'thinking', 'reasoning_effort', 'max_tokens', 'timeout_ms', 'retries', 'temperature')
# DeepSeek Chat Completions 官方文档，2026-11-09 核对。
DEEPSEEK_MAX_TOKENS = 393216


def dispatch(route, body):
    try:
        live = paths.organ_dir('mind') / llm.CONFIG_NAME
        path = live if live.is_file() else paths.factory_dir('mind') / llm.CONFIG_NAME
        raw = json.loads(path.read_text(encoding='utf-8'))
        config = llm.resolve_config(raw)
        if route == '/model/config':
            if not isinstance(body, dict) or not body or set(body) - set(FIELDS):
                raise ValueError('只接受页面列出的模型调用参数')
            candidate = dict(raw)
            for name, value in body.items():
                if value is None and name in ('thinking', 'reasoning_effort'):
                    candidate.pop(name, None)
                else:
                    candidate[name] = value
            checked = llm.resolve_config(candidate)
            if checked['provider'] == 'deepseek' and checked['max_tokens'] > DEEPSEEK_MAX_TOKENS:
                raise ValueError('DeepSeek 输出 token 上限不能超过 ' + str(DEEPSEEK_MAX_TOKENS))
            if not math.isfinite(checked['temperature']) or not 0 <= checked['temperature'] <= 2:
                raise ValueError('温度需要在 0 到 2 之间')
            changed = {name:{'before':config.get(name), 'after':checked.get(name)}
                       for name in body if config.get(name) != checked.get(name)}
            if changed:
                for name in body:
                    if name in checked:
                        candidate[name] = checked[name]
                atomic_write_text(live, json.dumps(candidate, ensure_ascii=False, indent=2)+'\n')
                with (live.parent/'config_history.jsonl').open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps({'at':datetime.now().astimezone().isoformat(),
                        'source':'panel', 'changes':changed}, ensure_ascii=False)+'\n')
            config = checked
        return 200, {'ok':True, 'config':{name:config.get(name) for name in FIELDS},
                     'limits':{'max_tokens':DEEPSEEK_MAX_TOKENS if config['provider'] == 'deepseek' else None},
                     'temperature_tiers':config.get('temperature_tiers'),
                     'source':'live' if live.is_file() else 'factory'}
    except (ValueError, llm.LLMConfigError) as exc:
        return 400, {'ok':False, 'message':str(exc)}
