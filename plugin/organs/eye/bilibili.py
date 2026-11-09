"""B 站只读资料获取：适配旧 qqAgent 的 WBI/字幕读取与 dsh-bili-learn 的元数据形状。

不导入旧宿主、不读取浏览器 Cookie、不提供平台写操作。出处见 BILIBILI-REFERENCE.md。
"""
import hashlib
import json
import math
import re
import time
from urllib import error, parse, request

from organs import paths

MIXIN = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


class BiliError(ValueError):
    pass


def video_ref(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise BiliError("请输入 BV 号或完整 B 站视频链接")
    match = re.search(r"\b(BV[0-9A-Za-z]{10})\b", value)
    if not match:
        raise BiliError("需要 BV 号、B 站视频链接或 b23.tv 分享链接")
    page = re.search(r"[?&]p=([^&#\s]*)", value)
    if page and not page[1].isdigit():
        raise BiliError("分 P 编号不正确")
    part = int(page[1]) if page else 1
    if not 1 <= part <= 10000:
        raise BiliError("分 P 编号不正确")
    return match[1], part


def reference_url(value):
    """只规范输入，不联网；短链交给后台读取任务展开。"""
    if isinstance(value, str) and len(value) <= 1000:
        short = re.search(r"(?<![\w./@-])(?:https?://)?b23\.tv/([A-Za-z0-9]{1,32})(?=$|[\s?#/。，！）》])", value)
        if short:
            return "https://b23.tv/" + short[1]
    bvid, page = video_ref(value)
    return "https://www.bilibili.com/video/%s?p=%d" % (bvid, page)


class ShortRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_short(url):
    """只读取 b23 的一次跳转，不访问目标页，也不携带登录资料。"""
    try:
        with request.build_opener(ShortRedirect()).open(
                request.Request(url, headers={"User-Agent": UA}), timeout=15):
            raise BiliError("短链接没有返回视频地址，请改发 BV 号或完整视频链接")
    except error.HTTPError as exc:
        location = exc.headers.get("Location", "")
        code = exc.code
        exc.close()
        address = parse.urlsplit(location)
        if (code not in (301, 302, 303, 307, 308) or address.scheme != "https"
                or address.netloc not in ("www.bilibili.com", "bilibili.com", "m.bilibili.com")
                or not re.fullmatch(r"/video/BV[0-9A-Za-z]{10}/?", address.path)):
            raise BiliError("短链接没有指向 B 站视频，请改发 BV 号或完整视频链接") from None
        bvid, page = video_ref(location)
        return "https://www.bilibili.com/video/%s?p=%d" % (bvid, page)
    except OSError:
        raise BiliError("B 站短链接暂时读不到，本次没有自动重试") from None


def read_cookie():
    path = paths.organ_dir("eye") / "bilibili_cookie"
    return path.read_text().strip() if path.is_file() else ""


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BiliError("B 站请求发生重定向，本次停止读取")


def get_json(url, cookie=""):
    address = parse.urlsplit(url)
    host = address.hostname or ""
    if (address.scheme != "https" or address.username or address.password or address.port not in (None, 443)
            or not (host == "api.bilibili.com" or host.endswith(".hdslb.com"))):
        raise BiliError("字幕地址不属于允许的 B 站服务")
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    # 登录资料只发送到官方 API，不带给字幕 CDN 或重定向目标。
    if cookie and host == "api.bilibili.com":
        headers["Cookie"] = cookie
    try:
        with request.build_opener(NoRedirect()).open(request.Request(url, headers=headers), timeout=15) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise BiliError("资料超过单次读取上限")
        return json.loads(raw)
    except error.HTTPError as exc:
        raise BiliError("B 站返回 HTTP %d，本次停止，没有自动重试" % exc.code) from None
    except (OSError, json.JSONDecodeError):
        raise BiliError("B 站资料暂时读不到，本次没有自动重试") from None


def read_video(value, *, fetch=get_json, cookie=None):
    reference = reference_url(value)
    if reference.startswith("https://b23.tv/"):
        reference = resolve_short(reference)
    bvid, page = video_ref(reference)
    cookie = read_cookie() if cookie is None else cookie
    # 与旧项目一致，请求至少间隔 600ms；本入口顺序读取，不递归遍历平台。
    last = 0.0
    def get(url, *, authenticated=False, allow_anonymous=False):
        nonlocal last
        time.sleep(max(0, .6 - (time.monotonic() - last)))
        last = time.monotonic()
        data = fetch(url, cookie if authenticated else "")
        if not isinstance(data, dict):
            raise BiliError("B 站返回了无法识别的数据")
        if "code" in data and data["code"] not in ((0, -101) if allow_anonymous else (0,)):
            raise BiliError("B 站返回错误码 %s，本次停止读取" % data["code"])
        return data
    nav = get("https://api.bilibili.com/x/web-interface/nav", allow_anonymous=True)
    images = nav.get("data", {}).get("wbi_img", {})
    raw_key = "".join(parse.urlsplit(images.get(k, "")).path.rsplit("/", 1)[-1].split(".")[0]
                      for k in ("img_url", "sub_url"))
    if len(raw_key) != 64:
        raise BiliError("未取得 B 站签名参数")
    key = "".join(raw_key[i] for i in MIXIN)[:32]
    def signed(params):
        params = dict(params, wts=int(time.time()))
        query = parse.urlencode(sorted(params.items()))
        return query + "&w_rid=" + hashlib.md5((query + key).encode()).hexdigest()
    view = get("https://api.bilibili.com/x/web-interface/wbi/view?" + signed({"bvid": bvid}))["data"]
    parts = view.get("pages", [])
    part = next((p for p in parts if p.get("page") == page), None)
    if not part:
        raise BiliError("视频没有这一分 P")
    meta = {"bvid": bvid, "page": page, "cid": part["cid"], "title": view["title"],
            "part": part.get("part", ""), "owner": view.get("owner", {}).get("name", ""),
            "duration": part.get("duration", 0), "description": view.get("desc", ""),
            "url": "https://www.bilibili.com/video/%s?p=%d" % (bvid, page)}
    player = get("https://api.bilibili.com/x/player/wbi/v2?" + signed({"bvid": bvid, "cid": part["cid"]}),
                 authenticated=True)["data"]
    tracks = player.get("subtitle", {}).get("subtitles", [])
    if not tracks:
        return {"meta": meta, "lines": [], "status": "no_subtitles",
                "note": "未取得字幕，可能未登录、登录失效或视频未提供字幕；未据标题生成知识。"}
    # 先人工简体，再其他中文，再外语；AI 轨保留标记，不伪装为原作者校对稿。
    track = min(tracks, key=lambda t: (0 if t.get("lan", "").lower() in ("zh-cn", "zh-hans") else
                                      1 if "zh" in t.get("lan", "").lower() else 2))
    url = track["subtitle_url"]
    if url.startswith("//"):
        url = "https:" + url
    body = get(url).get("body")
    if not isinstance(body, list) or not body:
        raise BiliError("字幕轨没有可读取的内容")
    lines = []
    for item in body:
        if (not isinstance(item, dict) or not isinstance(item.get("content"), str)
                or len(item["content"]) > 2000 or any(type(item.get(k)) not in (int, float)
                or not math.isfinite(item[k]) for k in ("from", "to"))
                or not 0 <= item["from"] <= item["to"]):
            raise BiliError("字幕时间或正文格式不正确")
        if item["content"].strip():
            lines.append({"t": item["from"], "end": item["to"], "text": item["content"].strip()})
    if not lines:
        raise BiliError("字幕没有有效正文")
    meta.update(language=track.get("lan", ""), auto_subtitle=bool(track.get("ai_type")) or track.get("lan", "").startswith("ai-"))
    return {"meta": meta, "lines": sorted(lines, key=lambda row: row["t"]), "status": "read",
            "note": "只读取这一分 P 的字幕，未观看画面；字幕和视频观点仍需核实。"}
