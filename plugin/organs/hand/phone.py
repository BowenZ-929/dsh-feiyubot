"""手的已绑定手机：连接、观察与固定动作，操作沿用手账。"""
import base64
import re
import struct
import sys
import xml.etree.ElementTree as ET

from adapters import adb
from . import ledger, store

COMMANDS = {
    "home": ["shell", "input", "keyevent", "KEYCODE_HOME"],
    "back": ["shell", "input", "keyevent", "KEYCODE_BACK"],
    "wake": ["shell", "input", "keyevent", "KEYCODE_WAKEUP"],
    "pause": ["shell", "input", "keyevent", "KEYCODE_MEDIA_PAUSE"],
    "play": ["shell", "input", "keyevent", "KEYCODE_MEDIA_PLAY"],
    "bilibili": ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "bilibili://home"],
    "douyin": ["shell", "am", "start", "-n", "com.ss.android.ugc.aweme/.splash.SplashActivity"],
}


def config():
    return store.read_json(store.hand_root() / "phone.json") or {"serial": "", "adb_path": "adb"}


def status():
    conf = config()
    result = {"ok": True, "serial": conf["serial"], "connected": False, "state": "unconfigured"}
    if not conf["serial"]:
        return result
    output = adb.run(["devices", "-l"], executable=conf["adb_path"]).decode("utf-8", errors="replace")
    devices = [line.split() for line in output.splitlines()[1:] if line.strip()]
    match = next((row for row in devices if row[0] == conf["serial"]), None)
    result["state"] = match[1] if match and len(match) > 1 else "disconnected"
    if result["state"] == "device":
        result["connected"] = True
        for name, prop in (("model", "ro.product.model"), ("android", "ro.build.version.release")):
            result[name] = adb.run(["-s", conf["serial"], "shell", "getprop", prop],
                                   executable=conf["adb_path"]).decode().strip()
    return result


def connect(serial):
    # USB 序列号或无线调试地址；拒绝选项、空白和命令字符。
    if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", serial):
        raise ValueError("请填手机的调试地址（IP:端口）或 USB 序列号")
    if ":" in serial:
        host, port = serial.rsplit(":", 1)
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError("无线调试地址需要有效的 IP/主机名和端口")
    conf = config()
    entry = ledger.append(ledger.make_entry(who="operator", action="phone.connect", params={"serial": serial}))
    try:
        if ":" in serial:
            adb.run(["connect", serial], executable=conf["adb_path"])
        store.save_phone_config({**conf, "serial": serial})
        result = status()
        ledger.append({**entry, "state": "done" if result["connected"] else "failed",
                       "code": "CONNECTED" if result["connected"] else result["state"]})
        return result
    except adb.AdbError:
        ledger.append({**entry, "state": "failed", "code": "ADB_UNCONFIRMED"})
        raise


def screen():
    conf = config()
    if not conf["serial"]:
        raise ValueError("先连接一部手机")
    png = adb.run(["-s", conf["serial"], "exec-out", "screencap", "-p"], executable=conf["adb_path"], timeout=15)
    if len(png) < 24 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise adb.AdbError("没有取得有效的手机截图")
    width, height = struct.unpack(">II", png[16:24])
    return {"ok": True, "serial": conf["serial"], "at": store.now_iso(), "width": width, "height": height,
            "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}


def inspect():
    conf = config()
    if not conf["serial"]:
        raise ValueError("先连接一部手机")
    xml = adb.hierarchy(conf["serial"], python=conf.get("ui_python"))
    try:
        tree = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise adb.AdbError("手机没有返回有效的界面文字") from exc
    parents = {child: parent for parent in tree.iter() for child in parent}
    nodes = []
    video_bounds = None
    for node in tree.iter("node"):
        a = node.attrib
        if (not a.get("package") or a["package"] == "com.android.systemui"
                or a.get("visible-to-user") == "false" or a.get("password") == "true"):
            continue
        bounds = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", a.get("bounds", ""))
        if not bounds:
            continue
        box = [int(x) for x in bounds.groups()]
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        if a.get("resource-id") == "tv.danmaku.bili:id/video_area_float_layer_container":
            video_bounds = box
        if a.get('resource-id') == 'com.ss.android.ugc.aweme:id/viewpager' and a.get('content-desc') == '视频':
            video_bounds = box
        if not (a.get("text") or a.get("content-desc")):
            continue
        ancestor = node
        in_scrollable = False
        in_comment_list = False
        while ancestor is not None:
            # B站评论区外层还有可横滑的ViewPager；固定输入栏不属于里面的评论列表。
            if ancestor.get("resource-id") == "tv.danmaku.bili:id/comment_container":
                in_comment_list = a["package"] == "tv.danmaku.bili" and in_scrollable
                break
            if ancestor.get('resource-id') == 'com.ss.android.ugc.aweme:id/x9g':
                in_comment_list = a['package'] == 'com.ss.android.ugc.aweme' and in_scrollable
                break
            in_scrollable |= ancestor.get("scrollable") == "true"
            ancestor = parents.get(ancestor)
        nodes.append({"text": a.get("text", ""), "description": a.get("content-desc", ""),
                      "resource_id": a.get("resource-id", ""), "package": a["package"],
                      "bounds": box, "clickable": a.get("clickable") == "true",
                      "class": a.get("class", ""), "in_scrollable": in_scrollable,
                      "in_comment_list": in_comment_list})
    return {"ok": True, "serial": conf["serial"], "at": store.now_iso(), "nodes": nodes, "video_bounds": video_bounds,
            "packages": sorted({node["package"] for node in nodes})}


def locked():
    """读系统锁屏状态；缺少该字段不能当作已解锁。"""
    conf = config()
    raw = adb.run(["-s", conf["serial"], "shell", "dumpsys", "window"], executable=conf["adb_path"]).decode()
    match = re.search(r"\bisKeyguardShowing=(true|false)\b", raw)
    if not match:
        raise adb.AdbError("未取得系统锁屏状态")
    return match[1] == "true"


def clipboard(text=None):
    """仅供固定复制链接流程：先清空，再读取本次复制；不开放聊天/API 动作。"""
    conf = config()
    command = "import sys,uiautomator2 as u2;d=u2.connect(sys.argv[1]);"
    command += "d.set_clipboard(sys.argv[2])" if text is not None else "sys.stdout.write(d.clipboard or '')"
    args = ["-c", command, conf["serial"]] + ([text] if text is not None else [])
    return adb.run(args, executable=conf.get("ui_python") or sys.executable, timeout=15).decode("utf-8")


def act(action, *, actor="operator", **params):
    if not isinstance(action, str) or action not in (*COMMANDS, "tap", "swipe"):
        raise ValueError("不支持的手机操作")
    expected = {"x", "y"} if action == "tap" else {"direction"} if action == "swipe" else set()
    if set(params) != expected:
        raise ValueError("手机操作参数不匹配")
    if action == "tap" and not all(type(params[k]) in (int, float) and 0 <= params[k] <= 1 for k in ("x", "y")):
        raise ValueError("点按位置应为屏幕宽高的 0–1 比例")
    if action == "swipe" and params["direction"] not in ("up", "down"):
        raise ValueError("只支持向上或向下滑动")
    conf = config()
    if not conf["serial"]:
        raise ValueError("先连接一部手机")
    command = COMMANDS.get(action)
    if action in ("tap", "swipe"):
        size = screen()
        w, h = size["width"], size["height"]
        if action == "tap":
            command = ["shell", "input", "tap", str(min(w - 1, int(params["x"] * w))), str(min(h - 1, int(params["y"] * h)))]
        else:
            y1, y2 = (0.72, 0.28) if params["direction"] == "up" else (0.28, 0.72)
            command = ["shell", "input", "swipe", str(w // 2), str(int(h * y1)), str(w // 2), str(int(h * y2)), "350"]
    entry = ledger.append(ledger.make_entry(who=actor, action="phone." + action,
                                            params={"serial": conf["serial"], **params}))
    try:
        output = adb.run(["-s", conf["serial"], *command], executable=conf["adb_path"])
        # am start 可能退出码为 0 但报告无法解析 Intent。
        if b"Error:" in output or b"Exception" in output:
            raise adb.AdbError(output.decode("utf-8", errors="replace")[:400])
    except adb.AdbError:
        ledger.append({**entry, "state": "failed", "code": "ADB_UNCONFIRMED"})
        raise
    ledger.append({**entry, "state": "done", "code": "COMMAND_ACCEPTED"})
    return {"ok": True, "state": "command_accepted", "ledger_id": entry["ledger_id"],
            "message": "操作已提交，请刷新屏幕确认结果"}
