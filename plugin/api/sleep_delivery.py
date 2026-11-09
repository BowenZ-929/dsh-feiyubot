"""硬撑结束的私聊告别；无需新入站消息，不开启其他主动分享。"""
from contextlib import nullcontext
import json
from pathlib import Path
from uuid import uuid4

from organs.brain import model
from organs.day import model as day_model
from organs.hand import channel
from organs.heart import rest, store as heart_store
from organs.mind import delivery, recent
from . import chat


def run(*, root=None, heart_root=None, brain, store, guard=None):
    if brain is None or store is None:
        return []
    # 与主动发送一样，调试数据根不能借用正式通道；时间取实际时钟。
    if (day_model.day_root(root).resolve() != day_model.day_root().resolve()
            or (Path(heart_root) if heart_root is not None else heart_store.default_root()).resolve()
               != heart_store.default_root().resolve()):
        return []
    with guard if guard is not None else nullcontext():
        now = model.now_iso()
        resting = rest.advance(now=now)
        if resting['phase'] != 'deep' or model.parse_ts(resting['since']) > model.parse_ts(now):
            return []
        results = []
        for who in rest.take_notice_targets():
            token = uuid4().hex
            notice = chat._sleep_notice({'who':who}, brain, store, resting, token, token, '',
                                       'qq:private:'+who, mode='proactive')
            if notice is None:
                continue
            receipt = channel.say_text(scene='private', target=int(who), text=notice['reply'], reply_kind='sleep_notice')
            # 保存真实传输结果；没有确切回执就不记成已说过。
            path = recent.path_of().parent/'deliveries'/(token+'.json')
            record = json.loads(path.read_text(encoding='utf-8'))
            delivery.stage(token, dict(record, transport_receipt=receipt))
            sent = (receipt.get('code') == channel.OK and receipt.get('confirmed_text') == notice['reply'])
            saved = delivery.confirm(token, notice['reply'], brain) if sent else False
            results.append({'who':who,'delivery_id':token,'sent':sent,'memory_saved':saved,'receipt':receipt})
        return results
