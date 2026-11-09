"""本地 CPU/int8 转写，参考 dsh-dafeiyu-life 的 faster-whisper 调用。

只读内存音频，PyAV 自带解码；最长读 61 秒，超长仅转前 30 秒。
逐段保留置信度，低置信文字不冒充听清，发送者不冒充说话者。
"""
import io


def transcribe(raw, model_path, language="zh", *, sound_model=None):
    import av
    import numpy as np
    from faster_whisper import WhisperModel

    chunks, samples = [], 0
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(io.BytesIO(raw)) as container:
        for frame in container.decode(audio=0):
            frame.pts = None
            for part in resampler.resample(frame):
                array = part.to_ndarray().reshape(-1)
                chunks.append(array)
                samples += len(array)
            if samples > 60 * 16000:
                break
        else:
            for part in resampler.resample(None):
                chunks.append(part.to_ndarray().reshape(-1))
    audio = np.concatenate(chunks).astype(np.float32) / 32768.0 if chunks else np.array([], dtype=np.float32)
    duration = len(audio) / 16000
    partial = duration > 60
    if partial:
        audio = audio[:30 * 16000]
    seconds = len(audio) / 16000
    rows = []
    if seconds >= 1:
        model = WhisperModel(str(model_path), device="cpu", compute_type="int8",
                             local_files_only=True, cpu_threads=4)
        segments, info = model.transcribe(audio, language=None if language == 'auto' else language, vad_filter=True,
                                       beam_size=5, condition_on_previous_text=False)
        language = info.language
        for part in segments:
            if not part.text.strip():
                continue
            rows.append({"start": round(min(seconds, max(0, part.start)), 3),
                         "end": round(min(seconds, max(0, part.end)), 3),
                         "text": part.text.strip(), "avg_logprob": part.avg_logprob,
                         "no_speech_prob": part.no_speech_prob,
                         "uncertain": part.avg_logprob < -1 or part.no_speech_prob > .6})
    data = result(rows, duration, seconds, partial, str(model_path), language)
    if sound_model:
        from . import sound_events
        data['sound_events'] = sound_events.classify(audio, sound_model)
    return data


def result(rows, duration, seconds, partial, model, language):
    clear = sum(not row["uncertain"] for row in rows)
    status = ("short" if seconds < 1 else "empty" if not rows else "unclear" if not clear
              else "partial" if partial or clear < len(rows) else "ok")
    return {"status": status,
            "transcript": "".join("[听不清]" if row["uncertain"] else row["text"] for row in rows),
            "segments": rows, "duration": round(duration, 3), "duration_is_lower_bound": partial,
            "processed_seconds": round(seconds, 3), "partial": partial, "model": model,
            "language": language, "source": "local_asr", "speaker_identity": "unknown",
            "original_author": "unknown", "recording_time": "not_provided", "attribution": "sender_only"}
