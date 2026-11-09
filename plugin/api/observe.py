"""只读观察入口：展示已有证据，不生成经历、不推断调用归属。"""
from collections import deque
from datetime import date, datetime
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from organs import paths
from organs.mind import recent, events, conversation
from organs.home import knowledge
from organs.day import model as day_model
from organs.hand import channel as hand_channel, ledger as hand_ledger

OBSERVE_ROUTES = {"/observe/records": "GET", "/observe/activity": "GET", "/observe/rice": "GET",
                  "/observe/participation": "GET", "/observe/channel": "GET", "/observe/channel-runtime": "GET"}


def channel_runtime():
    """只读当前业务通道的启动快照；不拿磁盘配置冒充运行值。"""
    try:
        address = urlsplit(hand_channel.base_url())
        port = address.port or 80
        if (address.scheme != 'http' or address.hostname not in ('127.0.0.1', 'localhost', '::1')
                or address.username is not None or address.password is not None
                or address.path not in ('', '/') or address.query or address.fragment):
            raise ValueError
    except ValueError:
        return 500, {'ok': False, 'message': '运行参数只读取已配置的本机 HTTP 业务通道'}
    connection = HTTPConnection(address.hostname, port, timeout=2)
    try:
        connection.request('GET', '/runtime')
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError
        data = json.loads(response.read())
        if (not isinstance(data, dict) or data.get('ok') is not True
                or data.get('channel') != 'onebot-business' or data.get('mode') not in ('send', 'silent')
                or type(data.get('core_enabled')) is not bool or not isinstance(data.get('started_at'), str)
                or type(data.get('core_timeout_ms')) is not int
                or type(data.get('disk_matches')) not in (bool, type(None))
                or any(not isinstance(data.get(key), dict) for key in ('policy', 'gate_words')) and data['core_enabled']):
            raise ValueError
        return 200, {'ok': True, 'checked_at': day_model.now_iso(), **{key: data.get(key) for key in (
            'channel', 'started_at', 'mode', 'core_enabled', 'core_timeout_ms',
            'policy', 'gate_words', 'disk_matches', 'config_error')}}
    except (OSError, HTTPException, ValueError):
        return 502, {'ok': False, 'message': 'QQ 运行参数暂时读不到；业务通道可能离线或尚未更新'}
    finally:
        connection.close()


def channel():
    """读取现有 NoneBot 健康接口；连接状态不是登录或实际送达验收。"""
    try:
        port = int(os.environ.get('QQAGENT_NONEBOT_PORT', '6290'))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        return 500, {'ok': False, 'message': '本机 NoneBot 端口配置无效'}
    connection = HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request('GET', '/healthz')
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('HTTP ' + str(response.status))
        data = json.loads(response.read())
        if (not isinstance(data, dict) or data.get('channel') != 'nonebot'
                or not isinstance(data.get('bots'), list)
                or any(not isinstance(bot, dict) or not isinstance(bot.get('self_id'), str)
                       or not bot['self_id'].isdigit() or type(bot.get('running')) is not bool
                       or type(bot.get('business_connected')) is not bool for bot in data['bots'])):
            raise ValueError('返回格式不匹配')
        return 200, {'ok': True, 'channel': 'nonebot', 'checked_at': day_model.now_iso(),
                     'bots': [{key: bot[key] for key in ('self_id', 'running', 'business_connected')}
                              for bot in data['bots']]}
    except (OSError, HTTPException, ValueError) as error:
        return 502, {'ok': False, 'message': 'NoneBot 状态暂时读不到：' + type(error).__name__}
    finally:
        connection.close()


def participation(query):
    zone = day_model.local_tz()
    selected = query.get('date') or datetime.now(zone).date().isoformat()
    try:
        if date.fromisoformat(selected).isoformat() != selected:
            raise ValueError
    except ValueError:
        return 400, {'ok': False, 'message': '日期需为 YYYY-MM-DD'}
    channel = paths.REPO_ROOT / 'channels/onebot'
    sample_every, config_error = None, None
    try:
        config = json.loads((channel / 'data/gate.json').read_text(encoding='utf-8'))
        if not isinstance(config, dict) or type(config.get('chitchatSampleMod')) is not int or config['chitchatSampleMod'] < 1:
            raise ValueError('群聊抽样配置无效')
        sample_every = config['chitchatSampleMod']
    except (OSError, UnicodeError, ValueError) as error:
        config_error = '当前抽样配置读取失败：' + str(error)
    path = channel / '.state/decisions.jsonl'
    counters = ('segments', 'sent', 'spontaneous', 'spontaneous_sent', 'directed', 'directed_sent',
                'other', 'other_sent', 'unconfirmed', 'proactive', 'proactive_sent', 'proactive_unconfirmed')
    groups, invalid = {}, 0
    exists = path.is_file()
    try:
        if exists:
            with path.open(encoding='utf-8') as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        m = row.get('machine') if isinstance(row, dict) else None
                        if not isinstance(m, dict):
                            raise ValueError
                        if m.get('scene') != 'group':
                            continue
                        target, source = m.get('target'), m.get('source')
                        batch, direct = m.get('batch'), m.get('direct')
                        if (not isinstance(target, str) or not target.startswith('group:') or not target[6:].isdigit()
                                or source not in ('inbound', 'proactive') or not isinstance(batch, dict)
                                or not isinstance(direct, dict) or m.get('decided') not in ('sent', 'silent')
                                or not isinstance(m.get('reason_code'), str)
                                or (direct.get('rule') is not None and not isinstance(direct['rule'], str))):
                            raise ValueError
                        stamp = day_model.parse_ts(batch.get('at')).astimezone(zone)
                        sent = m['decided'] == 'sent'
                        if sent != (m['reason_code'] in ('sent', 'replaced_too_long')):
                            raise ValueError
                    except ValueError:
                        invalid += 1
                        continue
                    if stamp.date().isoformat() != selected:
                        continue
                    group = groups.setdefault(target, {'target': target, **dict.fromkeys(counters, 0), 'silent_reasons': {}})
                    unconfirmed = m['reason_code'] == 'outbound_failed'
                    if source == 'proactive':
                        group['proactive'] += 1
                        group['proactive_sent'] += sent
                        group['proactive_unconfirmed'] += unconfirmed
                        continue
                    rule = direct.get('rule')
                    category = ('spontaneous' if rule == 'chitchat' else
                                'directed' if rule in ('at', 'quote', 'nickname', 'voice', 'continuation') else 'other')
                    group['segments'] += 1
                    group['sent'] += sent
                    group[category] += 1
                    group[category + '_sent'] += sent
                    group['unconfirmed'] += unconfirmed
                    if not sent:
                        reason = m['reason_code']
                        if reason == 'core_empty' and m.get('reason') in ('rest-asleep', 'rest-deep', 'rest-holding'):
                            reason = m['reason']
                        group['silent_reasons'][reason] = group['silent_reasons'].get(reason, 0) + 1
    except (OSError, UnicodeError) as error:
        return 500, {'ok': False, 'message': '发言账读取失败：' + str(error)}
    total = {key: sum(g[key] for g in groups.values()) for key in counters}
    total['silent_reasons'] = {}
    for group in groups.values():
        for reason, count in group['silent_reasons'].items():
            total['silent_reasons'][reason] = total['silent_reasons'].get(reason, 0) + count
    ordered = sorted(groups.values(), key=lambda g: (-g['segments'], g['target']))
    for g in [total, *ordered]:
        for field, numerator, denominator in (('spontaneous_pct', 'spontaneous_sent', 'segments'),
                                              ('directed_pct', 'directed_sent', 'directed'),
                                              ('total_pct', 'sent', 'segments')):
            g[field] = round(100 * g[numerator] / g[denominator], 1) if g[denominator] else None
    return 200, {'ok': True, 'date': selected, 'timezone': str(zone), 'exists': exists,
                 'sample_every': sample_every, 'config_error': config_error, 'invalid_lines': invalid,
                 'groups': ordered, 'total': total}


# ---- 检索账 --------------------------------------------------------------
# 账只有一本：手账里 `action=lookup` 的那些折叠行。观察页不另存一份——存一份就等于给
# 「查了几次、花了没有」留两个答案，对账时第一步就得先问信谁。
# 被门0 挡下来的那一轮也在这本账上（brief §5 第 3 条：零外部请求 ≠ 零记录），否则永远分不清
# 「判了不查」和「根本没判」；但那一行只留原因与回合编号，被拒的那句话既不落账，也不在这里重拼。
LOOKUP_NOT_REQUESTED = "not_requested"
LOOKUP_FAILED = "requested_failed"
LOOKUP_UNSAVED = "looked_up_not_saved"
LOOKUP_LANDED = "landed"
LOOKUP_OUTCOME_LABELS = {LOOKUP_NOT_REQUESTED: "未请求", LOOKUP_FAILED: "已请求失败",
                         LOOKUP_UNSAVED: "已查到未落盘", LOOKUP_LANDED: "已落盘"}

#: 检索账准露的格子。**白名单**而不是黑名单：账行将来多一个字段（摘要、原文、定位），
#: 它默认不出现在观察页，而不是默认漏出去——隐私口径不能靠"记得把它删掉"。
#: 门与裁决用判据层（`mind/search.py` 的 `decide()`）那两个名字 `gate`/`action`，
#: 观察页与判据读同一套词；`door`/`verdict` 只作旧写法的兼容别名。
LOOKUP_FIELDS = ("gate", "action", "reason", "status", "code", "http", "results_count",
                 "elapsed_ms", "calls", "landed", "card_id", "turn_id", "billed_calls",
                 "billing_status", "currency", "cost", "price_url", "query", "query_sha256")
#: 门0 挡下的那一行：只留原因与回合编号，明细一格不给。
LOOKUP_PRIVACY = "privacy"
LOOKUP_DENY = ("deny", "block")


def _lookup_outcome(calls, status, landed):
    """四态只由账上的事实判；缺一格就回未知，不许拿默认值把它凑成某一态。"""
    if type(calls) is not int or calls < 0:
        return None
    if landed is True:
        return LOOKUP_LANDED
    if calls == 0:
        return LOOKUP_NOT_REQUESTED
    if status == "error":
        return LOOKUP_FAILED
    if status == "empty":
        return LOOKUP_FAILED
    if status == "ok":
        return LOOKUP_UNSAVED
    return None


def _lookup_record(row):
    """账行 → 观察页那几格。缺的留空（None），不按「大概是什么」补。"""
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    picked = {"gate": params.get("gate") or params.get("door"),
              "action": params.get("action") or params.get("verdict"),
              "reason": params.get("reason"), "status": params.get("state"),
              "code": row.get("code"), "http": params.get("http"),
              "results_count": params.get("results_count"), "elapsed_ms": row.get("elapsed_ms"),
              "calls": params.get("calls"), "landed": params.get("landed"),
              "card_id": params.get("card_id"),
              "turn_id": row.get("turn_id") or params.get("turn_id")}
    record = {key: picked.get(key, params.get(key)) for key in LOOKUP_FIELDS}
    if record["gate"] == LOOKUP_PRIVACY and record["action"] in LOOKUP_DENY:
        return {"gate": record["gate"], "action": record["action"], "reason": record["reason"],
                "turn_id": record["turn_id"], "redacted": True}
    outcome = _lookup_outcome(record["calls"], record["status"], record["landed"])
    record["outcome"] = outcome
    record["outcome_label"] = LOOKUP_OUTCOME_LABELS.get(outcome)
    return record


def lookup_account(query):
    """检索账：只读手账、只认 `action=lookup`、按日期与回合筛选；一行都不改盘。"""
    zone = day_model.local_tz()
    try:
        limit, offset = int(query.get("limit", "20")), int(query.get("offset", "0"))
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError
        selected = query.get("date", "")
        if selected and date.fromisoformat(selected).isoformat() != selected:
            raise ValueError
        turn_id = query.get("turn_id", "")
        if turn_id and (len(turn_id) > 160 or any(not (c.isalnum() or c in "-_:.") for c in turn_id)):
            raise ValueError
        if query.get("session_id"):
            raise ValueError
    except ValueError:
        return 400, {"ok": False, "message": "检索账只收日期（YYYY-MM-DD）、回合编号（不含空白，最多160字）、"
                                            "条数 1–100 与偏移量；会话编号不属于这本账"}
    path = hand_ledger.store.ledger_path()
    try:
        # 折叠归 ledger 一处；坏行数只能从字节层数。宁可多读一遍，也不在观察页重写一遍折叠规矩。
        rows = hand_ledger.read_all()
        _, invalid = hand_ledger.store.read_lines(path)
    except OSError as error:
        return 500, {"ok": False, "message": "检索账读取失败：" + str(error)}
    collected, undated, available_dates = [], 0, set()
    for row in rows:
        if row.get("action") != "lookup":
            continue
        record = _lookup_record(row)
        try:
            record_date = day_model.parse_ts(row.get("at")).astimezone(zone).date().isoformat()
            available_dates.add(record_date)
        except ValueError:
            record_date = ""
            undated += 1
        if selected and record_date != selected:
            continue
        if turn_id and record.get("turn_id") != turn_id:
            continue
        collected.append({"ledger_id": row.get("ledger_id"), "at": row.get("at"), "record": record})
    total = len(collected)
    page = list(reversed(collected))[offset:offset + limit]
    return 200, {"ok": True, "source": "lookup", "date": selected,
                 "dates": sorted(available_dates, reverse=True), "timezone": str(zone),
                 "undated_records": undated, "turn_id": turn_id, "exists": path.is_file(),
                 "total": total, "invalid_lines": invalid, "session_id": "", "sessions": [],
                 "offset": offset, "limit": limit, "has_older": total > offset + limit,
                 "rows": page}


def dispatch(query):
    source = query.get("source", "model")
    if source not in ("model", "chat", "delivery", "decision", "eye", "voice", "ear",
                      "learning", "emotion", "tone", "lookup"):
        return 400, {"ok": False, "message": "未知的记录来源"}
    if source == "lookup":
        return lookup_account(query)
    try:
        limit = int(query.get("limit", "20"))
        offset = int(query.get("offset", "0"))
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError
        turn_id = query.get("turn_id", "")
        if turn_id and (source not in ("model", "chat", "emotion", "tone") or len(turn_id) != 32 or any(c not in "0123456789abcdef" for c in turn_id)):
            raise ValueError
        session_id = query.get("session_id", "")
        if session_id and (source not in ("model", "chat", "emotion", "tone") or len(session_id) > 200
                           or any(ord(c) < 32 for c in session_id)):
            raise ValueError
        selected = query.get("date", "")
        if selected and date.fromisoformat(selected).isoformat() != selected:
            raise ValueError
    except ValueError:
        return 400, {"ok": False, "message": "日期需为 YYYY-MM-DD，条数为 1–100，偏移量不能为负；回合编号需为32位小写十六进制，会话编号最多200字且不含控制字符；两者用于模型、对话、情绪和语气记录"}
    state = paths.REPO_ROOT / "channels" / "onebot" / ".state"
    dates = []
    if source == "model":
        # 与 llm._trace_dir 的实际写入路径一致，关闭追踪仍能读历史记录。
        trace = Path(os.environ.get("QQAGENT_HOME_ROOT") or paths.home_root()) / "mind" / "trace"
        dates = sorted((p.stem for p in trace.glob("????-??-??.jsonl")), reverse=True)
        selected = selected or (dates[0] if dates else date.today().isoformat())
        path = trace / (selected + ".jsonl")
    elif source == "learning":
        path = knowledge.library_root() / "usage.jsonl"
    else:
        path = {"chat": recent.path_of(), "delivery": state / "ledger.jsonl",
                "decision": state / "decisions.jsonl",
                "eye": paths.organ_dir("eye") / "observations.jsonl",
                "voice": paths.organ_dir("mouth") / "voice.jsonl",
                "ear": paths.organ_dir("ear") / "observations.jsonl",
                "emotion": events.log_path(), "tone": conversation.tone_log_path()}[source]
    model_turn = source == "model" and bool(turn_id)
    files = sorted(trace.glob("????-??-??.jsonl")) if model_turn else [path]
    rows = [] if model_turn else deque(maxlen=offset + limit)
    total = errors = undated = 0
    available_dates = set()
    sessions = set()
    zone = day_model.local_tz()
    exists = any(p.is_file() for p in files)
    for path in files:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError
                    except ValueError:
                        errors += 1
                        continue
                    if row.get('accounting_only') is True:
                        continue
                    record_session = row.get('session_id')
                    if source == 'emotion' and not record_session and isinstance(row.get('interpretation'), dict):
                        record_session = row['interpretation'].get('session_id')
                    if isinstance(record_session, str) and record_session:
                        sessions.add(record_session)
                    if session_id and record_session != session_id:
                        continue
                    if source != "model":
                        try:
                            stamp = row.get("ts" if source in ("chat", "delivery", "decision") else "at")
                            record_date = day_model.parse_ts(stamp).astimezone(zone).date().isoformat()
                            available_dates.add(record_date)
                        except ValueError:
                            record_date = ""
                            undated += 1
                        if selected and not turn_id and record_date != selected:
                            continue
                    if turn_id and row.get("turn_id") != turn_id:
                        continue
                    if model_turn and any(type(row.get(k)) is not int or row[k] < 1 for k in ('call_index', 'attempt')):
                        errors += 1
                        continue
                    total += 1
                    rows.append({"line": number, "record": row, **({"date": path.stem} if source == "model" else {})})
        except OSError as error:
            return 500, {"ok": False, "message": "记录读取失败：" + str(error)}
    ordered = sorted(rows, key=lambda item: (item["record"]["call_index"], item["record"]["attempt"])) if model_turn else list(reversed(rows))
    page = ordered[offset:offset + limit]
    if source == 'chat':
        receipt_dir = recent.path_of().parent / 'deliveries'
        for item in page:
            turn = item['record'].get('turn_id')
            item['delivery'] = {'state': 'unrecorded'}
            if not isinstance(turn, str) or len(turn) != 32 or any(c not in '0123456789abcdef' for c in turn):
                continue
            try:
                receipt = json.loads((receipt_dir / (turn + '.json')).read_text(encoding='utf-8'))
                if (not isinstance(receipt, dict) or receipt.get('turn_id') != turn
                        or type(receipt.get('confirmed')) is not bool
                        or (receipt['confirmed'] and not isinstance(receipt.get('text'), str))):
                    raise ValueError('回执字段不完整或不属于这一轮')
                item['delivery'] = ({'state': 'confirmed', 'text': receipt['text']} if receipt['confirmed']
                                    else {'state': 'pending'})
                if receipt['confirmed'] and receipt.get('stop_reason') == 'superseded':
                    item['delivery']['stop_reason'] = 'superseded'
            except FileNotFoundError:
                pass  # 老记录或未走QQ回执的轮次，没有可关联的收据。
            except (OSError, UnicodeError, ValueError):
                item['delivery'] = {'state': 'unavailable'}
    return 200, {"ok": True, "source": source, "date": selected,
                 "dates": dates if source == "model" else sorted(available_dates, reverse=True),
                 "timezone": str(zone), "undated_records": undated,
                 "turn_id": turn_id, "exists": exists, "total": total, "invalid_lines": errors,
                 "session_id": session_id, "sessions": sorted(sessions),
                 "offset": offset, "limit": limit, "has_older": total > offset + limit,
                 "rows": page}
