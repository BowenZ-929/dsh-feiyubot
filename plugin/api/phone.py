"""手机面板；设备操作只来自显式 POST，读取状态不会自动点屏幕。"""
from adapters.adb import AdbError
from organs.hand import phone, ledger
from organs.day import phone_browse
from organs.mind import persona

PHONE_ROUTES = {"/phone/status": "GET", "/phone/connect": "POST",
                "/phone/screen": "POST", "/phone/action": "POST", "/phone/inspect": "POST",
                "/phone/browse": "GET", "/phone/browse/start": "POST", "/phone/browse/stop": "POST",
                "/phone/browse/settings": "POST"}


def dispatch(route, body, store=None, brain=None):
    try:
        if route == "/phone/browse":
            return 200, phone_browse.view()
        if route.startswith("/phone/browse/"):
            fields = {"enabled"} if route.endswith("settings") else set()
            if not isinstance(body, dict) or (not set(body) <= {'platform'} if route.endswith('start') else set(body) != fields):
                raise ValueError("浏览参数不匹配")
            if route.endswith("settings"):
                return 200, phone_browse.configure(body["enabled"])
            if route.endswith("stop"):
                return 200, phone_browse.stop()
            if store is None:
                raise ValueError("魂尚未载入，不能替她选择内容")
            return 202, phone_browse.start(persona=persona.render(store, brain), platform=body.get('platform','bilibili'))
        if route in ("/phone/connect", "/phone/action") and phone_browse.view()["active"]:
            raise ValueError("她正在使用手机，先点停止浏览再手动操作")
        if route == "/phone/status":
            return 200, {**phone.status(), "actions": [r for r in ledger.read_tail(100)
                                                    if str(r.get("action", "")).startswith("phone.")][-20:]}
        if route == "/phone/action":
            if not isinstance(body, dict) or "action" not in body or "actor" in body:
                raise ValueError("手机操作参数不匹配")
            return 200, phone.act(**body)
        fields = {"/phone/connect": {"serial"}, "/phone/screen": set(), "/phone/inspect": set()}[route]
        if not isinstance(body, dict) or set(body) != fields:
            raise ValueError("手机操作参数不匹配")
        if route == "/phone/connect":
            return 200, phone.connect(body["serial"])
        if route == "/phone/screen":
            return 200, phone.screen()
        return 200, phone.inspect()
    except ValueError as exc:
        return 400, {"ok": False, "message": str(exc)}
    except (AdbError, OSError, ledger.LedgerWriteFailed) as exc:
        return 503, {"ok": False, "message": str(exc)}
