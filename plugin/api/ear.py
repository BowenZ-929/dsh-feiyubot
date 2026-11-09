"""耳的本地识别状态与开关。"""
import json
from organs import paths
from organs.ear import hearing

EAR_ROUTES = {"/ear/status": "GET", "/ear/config": "POST"}


def dispatch(route, body):
    config = hearing.load_config()
    ready = ((paths.REPO_ROOT / config["python"]).is_file()
             and (paths.REPO_ROOT / config["model"] / "model.bin").is_file())
    if route == "/ear/config":
        if not isinstance(body, dict) or set(body) != {"enabled"} or type(body["enabled"]) is not bool:
            return 400, {"ok": False, "message": "只接受 enabled 听语音开关"}
        if body["enabled"] and not ready:
            return 409, {"ok": False, "message": "本地识别环境或模型尚未准备好"}
        before = config["enabled"]
        config["enabled"] = body["enabled"]
        path = paths.organ_dir("ear") / "asr.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        hearing.record({"kind": "setting", "parameter": "enabled", "before": before,
                        "after": config["enabled"], "source": "panel"})
    sound_ready = bool(config.get('sound_model') and (paths.REPO_ROOT / config['sound_model']).is_file())
    return 200, {"ok": True, "config": config, "model_ready": ready, "sound_model_ready": sound_ready}
