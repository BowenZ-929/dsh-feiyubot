"""按需朗读最终回复。参考 Breeze 官方 TTS 接口和本仓 mouth-voice 契约。

只负责合成，不发送 QQ、不改人设、不写亲历。失败保留文字，不重试扣费。
"""
from datetime import datetime
import io
import json
import os
import re
import time
import uuid
from urllib import error, request
import wave

from organs import paths


def load_config():
    live = paths.organ_dir("mouth") / "voice.json"
    return json.loads((live if live.is_file() else paths.factory_dir("mouth") / "voice.json").read_text())


def read_key():
    path = paths.organ_dir("mouth") / "breeze_api_key"
    return os.environ.get("BREEZE_API_KEY", "").strip() or (path.read_text().strip() if path.is_file() else "")


def record(row):
    path = paths.organ_dir("mouth") / "voice.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(at=datetime.now().astimezone().isoformat(), **row), ensure_ascii=False) + "\n")


def attempts_today():
    path = paths.organ_dir("mouth") / "voice.jsonl"
    if not path.is_file():
        return 0
    today = datetime.now().astimezone().date().isoformat()
    with path.open(encoding="utf-8") as handle:
        rows = (json.loads(line) for line in handle if line.strip())
        return sum(row.get("kind") == "attempt" and row["at"].startswith(today) for row in rows)


def synthesize(text, config, key):
    """一个同步请求；只接受完整 PCM WAV。超时/异步回包不自动重新生成。"""
    req = request.Request(
        "https://api.breeze.blue/v1/text-to-speech/" + config["voice_id"] + "?output_format=wav_24000&delivery=sync",
        data=json.dumps({"text": text, "model_id": config["model"], "language_code": "zh"}).encode(),
        # 服务入口拒绝 urllib 默认 UA；明确标识本客户端后只读鉴权已实测通过。
        headers={"Content-Type": "application/json", "xi-api-key": key,
                 "User-Agent": "NewQQAgent/1.0"}, method="POST")
    with request.urlopen(req, timeout=config["timeout_seconds"]) as response:
        if response.status != 200:
            raise ValueError("synthesis_not_complete")
        raw = response.read(5_000_001)
    if len(raw) > 5_000_000:
        raise ValueError("audio_too_large")
    with wave.open(io.BytesIO(raw), "rb") as audio:
        frames = audio.getnframes()
        size = frames * audio.getnchannels() * audio.getsampwidth()
        if frames == 0 or audio.getframerate() != 24000 or len(audio.readframes(frames)) != size:
            raise ValueError("incomplete_audio")
        duration = frames / audio.getframerate()
    return raw, duration


def generate(text, *, delivery_id=""):
    """测试工具与 QQ 共用额度；请求前记一次尝试，失败/中断同样占次数。"""
    config = load_config()
    key = read_key()
    if not key:
        return {"status": "unavailable", "reason": "missing_key"}
    if not text.strip() or len(text) > config["max_chars"] or re.search(r"https?://|www\.|`|\[|\]", text):
        return {"status": "skipped", "reason": "text_unsuitable"}
    if attempts_today() >= config["daily_attempts"]:
        return {"status": "skipped", "reason": "daily_limit"}
    token = uuid.uuid4().hex
    meta = {"id": token, "delivery_id": delivery_id, "voice_id": config["voice_id"], "characters": len(text)}
    record(dict(meta, kind="attempt"))
    start = time.monotonic()
    try:
        raw, duration = synthesize(text, config, key)
        output = paths.organ_dir("mouth") / "audio" / (token + ".wav")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
    except (OSError, ValueError, EOFError, wave.Error, error.URLError) as exc:
        # 不记录服务端回包或异常原文，避免 URL/请求头中的凭据落日志。
        result = {"status": "failed", "reason": "HTTP_%s" % exc.code if isinstance(exc, error.HTTPError) else type(exc).__name__}
    else:
        result = {"status": "generated", "file": output.resolve().as_uri(), "duration": round(duration, 3)}
    record(dict(meta, kind="result", elapsed_seconds=round(time.monotonic() - start, 2), **result))
    return result


def apply(payload, requested, delivery_id):
    """所有回复闸之后调用；音频独占一泡，文字仅作为发送回执的内容。"""
    if not requested or payload.get("send") is not True or not payload.get("chunks"):
        return
    if not load_config()["enabled"]:
        payload["voice"] = {"status": "unavailable", "reason": "disabled"}
        return
    chunks = payload["chunks"]
    if any(chunk.get("image") or chunk.get("audio") for chunk in chunks):
        payload["voice"] = {"status": "skipped", "reason": "mixed_media"}
        return
    # 不删词、不截字；用最终泡的原文合成，确保回执与待朗读文字一致。
    text = "\n".join(chunk["text"] for chunk in chunks)
    result = generate(text, delivery_id=delivery_id)
    payload["voice"] = result
    if result["status"] == "generated":
        payload["chunks"] = [{"text": text, "audio": {"file": result["file"]}, "delay_ms": 0}]
