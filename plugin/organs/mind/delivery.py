"""QQ 已确认发言：生成稿不进自述记忆，收到通道回执后记实际文本。"""
from __future__ import annotations

import json
from organs.brain import model
from organs.hand import ledger
from . import recent


def stage(delivery_id, record, root=None, *, reuse_turn=False):
    path = recent.path_of(root).parent / "deliveries" / (delivery_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if reuse_turn:
        # 回答期间睡熟：只替换待发送内容的用途，沿用原轮的时间/消息编号。
        record = dict(json.loads(path.read_text(encoding='utf-8')), sleep_notice=record['sleep_notice'])
    path.write_text(json.dumps(dict(record, confirmed=False), ensure_ascii=False), encoding="utf-8")


def attach_image(delivery_id, *, image, receipt, meme, root=None):
    path = recent.path_of(root).parent / 'deliveries' / (delivery_id + '.json')
    record = json.loads(path.read_text(encoding='utf-8'))
    record.update(image=image, image_receipt=receipt, meme=meme)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')


def confirm(delivery_id, text, brain, root=None, images=(), stop_reason=None, heart_root=None):
    path = recent.path_of(root).parent / "deliveries" / (delivery_id + ".json")
    if not path.is_file():
        return False
    record = json.loads(path.read_text(encoding="utf-8"))
    if any(image != record.get("image") for image in images):
        return False
    if record["confirmed"]:
        return True
    receipt = record.get("image_receipt")
    if receipt and record.get("image") in images:
        ledger.append(ledger.make_entry(
            ledger_id=receipt["ledger_id"], who=receipt["who"], action="send_image",
            params=dict(receipt["params"], sent=1, delivery_id=delivery_id),
            state="done"), root=receipt["root"])
    if text:
        result = brain.ingest(who=record["speaker"], text=text, ts=model.now_iso(),
                              side=model.SIDE_SELF, scene=record["scene"], turn_id=record.get("turn_id", ""),
                              **({'session_id': record['session_id']} if record.get('session_id') else {}))
        if not result.get("ok"):
            return False
    notice = record.get('sleep_notice')
    if notice and text == notice['text']:
        from organs.heart import rest
        rest.notice_sent(notice['who'], notice['wake_at'], root=heart_root, mark=True)
    record['sent_images'] = list(images)
    recent.confirm_reply(record, text, root=root)
    record.update(confirmed=True, confirmed_at=model.now_iso(), text=text, sent_images=list(images))
    if text and record.get('proactive_context'):
        from organs.heart import proactive
        record['feedback'] = proactive.sent(root=heart_root, now=model.now_iso(), context=record['proactive_context'])
    if stop_reason is not None:
        record['stop_reason'] = stop_reason
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return True
