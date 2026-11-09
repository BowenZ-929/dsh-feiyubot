"""外部学习的面板入口；开始/继续是显式动作，查看不触发网络或模型。"""
import json

from organs.eye import bilibili
from organs.home import knowledge, expressions
from organs.mind import learning, recent
from organs.brain import model
from organs.day import phone_browse

LEARNING_ROUTES = {"/learning/status": "GET", "/learning/read": "GET",
                   "/learning/web": "GET",
                   "/learning/search": "GET", "/learning/start": "POST",
                   "/learning/expressions": "GET", "/learning/expressions/set": "POST"}


def dispatch(route, params, body):
    try:
        if route == "/learning/web":
            from organs.mind import search
            return 200, {"enabled": search.enabled(), "jobs": [row for row in knowledge.jobs()
                         if row["kind"] == knowledge.KIND_WEB][:50]}
        if route == "/learning/expressions":
            browsing = {}
            for job in phone_browse.view(limit=None)['jobs']:
                for ident in {event['record_id'] for event in job.get('events', [])
                              if event.get('kind') == 'expressions' and event.get('record_id')}:
                    browsing.setdefault(ident, []).append({**{key: job.get(key, '') for key in
                        ('id', 'created_at', 'title', 'status', 'model_turn_id')},
                        'video_source': job.get('video_source', {})})
            usage = {}
            path = expressions.library_root() / "usage.jsonl"
            if path.exists():
                for line in path.read_text().splitlines():
                    event = json.loads(line)
                    if event["kind"] != "offered_to_model":
                        continue
                    for ident in event["card_ids"]:
                        count = usage.get(ident, (0, None, None))[0]
                        usage[ident] = (count + 1, event["at"], event.get('delivery_id'))
            sent, turns = {}, {}
            for path in (recent.path_of().parent / 'deliveries').glob('*.json'):
                receipt = json.loads(path.read_text(encoding='utf-8'))
                turns[path.stem] = receipt.get('turn_id')
                if not receipt.get('confirmed') or receipt.get('sleep_notice'):
                    continue
                for card in receipt.get('expression_references', []):
                    if card['phrase'] and card['phrase'] in receipt.get('text', ''):
                        found = sent.setdefault(card['id'], {'sent_match_count': 0,
                            'last_sent_at': None, 'last_sent_turn_id': None})
                        found['sent_match_count'] += 1
                        at = receipt['confirmed_at']
                        if found['last_sent_at'] is None or model.parse_ts(at) > model.parse_ts(found['last_sent_at']):
                            found.update(last_sent_at=at, last_sent_turn_id=receipt.get('turn_id'))
            rows = []
            for record in expressions.records():
                row = {k: record[k] for k in ("id", "created_at", "enabled", "source", "verification")}
                row["source_rejected"] = bool(record.get("source_rejected"))
                row['browse_sessions'] = browsing.get(record['id'], [])
                row["cards"] = [dict(card, enabled=card.get("enabled",record["enabled"]), offered_count=usage.get(card["id"], (0, None))[0],
                                     last_offered_at=usage.get(card["id"], (0, None))[1],
                                     last_offered_turn_id=turns.get(usage.get(card['id'], (0, None, None))[2]),
                                     **sent.get(card['id'], {'sent_match_count': 0, 'last_sent_at': None,
                                                            'last_sent_turn_id': None})) for card in record["cards"]]
                rows.append(row)
            return 200, {"records": rows}
        if route == "/learning/expressions/set":
            if not isinstance(body, dict) or set(body) not in ({"id", "enabled"}, {"id", "enabled", "card_id"}):
                raise ValueError("需要表达记录编号和采用开关")
            try:
                row = expressions.enable(body["id"], body["enabled"], card_id=body.get("card_id"))
            except FileNotFoundError:
                return 404, {"ok": False, "message": "找不到这组表达记录"}
            return 200, {"id": row["id"], "enabled": row["enabled"]}
        if route == "/learning/status":
            # 这一页的每一列（片段数、下一段、投递）都只对字幕有意义：网页卡混进来只会显示
            # 成一片空字段，还会把「继续字幕」的入口指向一份根本没有字幕的记录。
            # 先过滤再截 50 条——先截后滤会让网页卡把字幕的名额占掉。
            subtitle = [row for row in knowledge.jobs()
                        if row["kind"] == knowledge.KIND_SUBTITLE][:50]
            return 200, {"credential_ready": bool(bilibili.read_cookie()), "jobs": [
                {"id": row["id"], "status": learning.state(row)["status"], "created_at": row["created_at"],
                 "title": row.get("source", {}).get("meta", {}).get("title", row["reference"]),
                 "cards": len(row["cards"]), "next_segment": row["next_segment"],
                 "delivery": {"status": learning.state(row).get("delivery", {}).get("status", "not_requested")},
                 "total_segments": row.get("total_segments", 0)} for row in subtitle]}
        if route == "/learning/read":
            job = knowledge.read(params.get("id"))
            if job["kind"] == knowledge.KIND_WEB:
                raise ValueError("网页知识不是字幕任务，不能在这里打开")
            return 200, {k: v for k, v in learning.state(job).items() if k != "outputs"}
        if route == "/learning/search":
            q = params.get("q", "").strip()
            if not q or len(q) > 500:
                raise ValueError("搜索词需为1至500字")
            return 200, {"items": knowledge.search(q)}
        if not isinstance(body, dict):
            raise ValueError("需要视频链接或学习记录编号")
        ident = body.get("id")
        if ident:
            # 只认字幕：网页卡没有 bvid/page 那套字幕来源，接进「继续字幕」等于拿视频字段
            # 去解析一张网页卡，读不出来还会把进度写成 0。网页卡的去处不是这条流程。
            job = knowledge.read(ident)
            if job["kind"] == knowledge.KIND_WEB:
                raise ValueError("网页知识不是字幕任务，不能继续")
            if job.get("destination"):
                from organs.mind import qq_learning
                return 202, learning.start(ident=ident, on_done=qq_learning.deliver)
        return 202, learning.start(body.get("reference"), ident=ident)
    except ValueError as exc:
        return 400, {"ok": False, "message": str(exc)}
