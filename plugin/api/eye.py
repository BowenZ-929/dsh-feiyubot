"""眼的状态与开关。凭据不经页面返回或编辑。"""
import json
from organs import paths
from organs.eye import vision

EYE_ROUTES = {"/eye/status": "GET", "/eye/config": "POST"}


def dispatch(route, body):
    config = vision.load_config()
    if route == "/eye/config":
        if not isinstance(body, dict) or set(body) != {"enabled"} or type(body["enabled"]) is not bool:
            return 400, {"ok": False, "message": "只接受 enabled 看图开关"}
        before = config["enabled"]
        config["enabled"] = body["enabled"]
        path = paths.organ_dir("eye") / "vision.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        vision.record({"kind": "setting", "parameter": "enabled", "before": before,
                       "after": config["enabled"], "source": "panel"})
    return 200, {"ok": True, "config": config, "credential_ready": bool(vision.read_key())}
