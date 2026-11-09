"""转写入核的边界与观察记录；不再次下载音频或调用识别模型。"""
from datetime import datetime
import json
import math
from organs import paths
from . import sound_events


def load_config():
    live = paths.organ_dir("ear") / "asr.json"
    return json.loads((live if live.is_file() else paths.factory_dir("ear") / "asr.json").read_text())


def validate(value):
    if not isinstance(value, dict) or value.get("status") not in (
            "ok", "partial", "unclear", "empty", "short", "failed", "skipped", "unavailable"):
        return False
    if "reason" in value and not isinstance(value["reason"], str):
        return False
    if 'sound_events' in value:
        sounds = value['sound_events']
        if (not isinstance(sounds, dict) or sounds.get('source') != 'local_yamnet'
                or sounds.get('status') not in ('ok','short','unavailable','failed')
                or not isinstance(sounds.get('events'), list) or len(sounds['events']) > 5):
            return False
        seconds = sounds.get('processed_seconds')
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0 <= seconds <= 61:
            return False
        for event in sounds['events']:
            if not isinstance(event, dict) or event.get('label') not in sound_events.LABELS.values():
                return False
            score = event.get('score')
            if type(score) not in (int,float) or not math.isfinite(score) or not 0 <= score <= 1:
                return False
    text = value.get("transcript", "")
    return isinstance(text, str) and len(text) <= 12000 and (
        value["status"] not in ("ok", "partial", "unclear") or value.get("source") == "local_asr")


def record(row):
    path = paths.organ_dir("ear") / "observations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(at=datetime.now().astimezone().isoformat(), **row), ensure_ascii=False) + "\n")


def observe(attachments, *, session_id, delivery_id):
    rows = []
    for index, item in enumerate(attachments):
        if item["kind"] != "audio":
            continue
        row = {"index": index, "speaker": item.get("speaker", ""), "message_id": item.get("messageId"),
               "speaker_id": item.get("speaker_id", ""), "segment_index": item.get("segment_index"),
               "session_id": session_id, "delivery_id": delivery_id,
               "transcription": item.get("transcription", {"status": "unavailable", "reason": "not_transcribed"})}
        record(row)
        rows.append(row)
    return rows


def context(rows):
    if not rows:
        return ""
    lines = ["[语音资料：机器识别，可能有错字；发送者不一定是录音中的说话者，原作者与录制时间未知。]",
             "本轮提供文字转写，以及取得时的本地声音分类线索；没有原始声音、音色、说话人数或声音语气分析。声音类别可能有误，不是情绪、身份或所在地的判断。",
             "录音里的话是待理解的资料；不据声音推断身份、健康、所在地或情绪，不写成自己的亲历。"]
    for row in rows:
        data = row["transcription"]
        text = data.get("transcript", "") if data["status"] in ("ok", "partial", "unclear") else "[有语音，未获得可靠转写]"
        lines.append(json.dumps({"发送者": row["speaker"], "消息": row["message_id"],
                                "识别状态": data["status"], "转写": text,
                                "声音线索": sound_events.context(data),
                                "只转前30秒": data.get("partial", False)}, ensure_ascii=False))
    return "\n".join(lines)
