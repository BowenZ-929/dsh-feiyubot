"""scrcpy只采手机播放音频；转写复用耳的本地工具，不上传音频。"""
import json
import os
import re
from pathlib import Path
import signal
import subprocess

from organs.hand import phone
from . import hearing

ROOT = Path(__file__).resolve().parents[3]


def start(path, seconds):
    cfg = phone.config()
    executable = cfg.get('scrcpy_path')
    if not executable:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([executable, '--serial='+cfg['serial'], '--no-video', '--no-control',
                             '--no-window', '--no-playback', '--audio-source=playback', '--audio-dup',
                             '--require-audio', '--time-limit='+str(seconds), '--record='+str(path)],
                            env=dict(os.environ, ADB=cfg['adb_path']), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def finish(process, path):
    try:
        _, error = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        stop(process)
        return {'status': 'failed', 'reason': '录音未按时结束'}
    if process.returncode or not path.is_file() or not path.stat().st_size:
        return {'status': 'failed', 'reason': '未取得播放音频', 'detail': error[-800:]}
    cfg = hearing.load_config()
    if not cfg['enabled']:
        return {'status': 'unavailable', 'reason': '本地语音识别已关闭'}
    try:
        result = subprocess.run([str(ROOT / cfg['python']), str(ROOT / 'tools/transcribe.py'), str(path),
                                 '--model', str(ROOT / cfg['model']), '--language', 'auto',
                                 *(['--sound-model', str(ROOT / cfg['sound_model'])] if cfg.get('sound_model') else [])],
                                capture_output=True, text=True, timeout=cfg['timeout_seconds'])
        if result.returncode:
            return {'status': 'failed', 'reason': '本地转写未成功'}
        heard = json.loads(result.stdout)
        if not hearing.validate(heard):
            raise ValueError('转写结果格式不正确')
        # 真实片段复现，dsh-dafeiyu-life/prompts/duty.md 也记录了仅字幕署名的结果。
        # 只说明没有可用对白，不能据此断言原视频没有声音。
        if re.fullmatch(r'字幕\s*by\s*\S{1,24}', heard.get('transcript', '').strip(), re.I):
            heard.update(status='unclear', raw_transcript=heard['transcript'], transcript='',
                         reason='仅识别到字幕署名，未取得可用对白')
        return dict(heard, recording_source='phone_playback', audio_file=str(path))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'status': 'failed', 'reason': '本地转写不可用或超时'}
