"""语音合成状态与开关；密钥只在本机配置，不经网页读写。"""
import json
from organs import paths
from organs.mouth import voice

VOICE_ROUTES = {"/voice/status": "GET", "/voice/config": "POST"}


def dispatch(route, body):
    config = voice.load_config()
    ready = bool(voice.read_key())
    if route == "/voice/config":
        if not isinstance(body, dict) or set(body) != {"enabled"} or type(body["enabled"]) is not bool:
            return 400, {"ok": False, "message": "只接受 enabled 语音开关"}
        if body["enabled"] and not ready:
            return 409, {"ok": False, "message": "请先注册账号，并用本地配置工具填入密钥"}
        before = config["enabled"]
        config["enabled"] = body["enabled"]
        path = paths.organ_dir("mouth") / "voice.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        voice.record({"kind": "setting", "parameter": "enabled", "before": before,
                      "after": config["enabled"], "source": "panel"})
    return 200, {"ok": True, "config": config, "credential_ready": ready,
                 "attempts_today": voice.attempts_today()}
