"""本地画室：素材、草稿、后台逐笔任务与过程；QQ 命令复用同一任务库。"""
import base64
from organs.hand import strokes, comfy, store as hand_store
from organs.mind import stroke_painting
from organs.home import art
from organs.mind import paint

STUDIO_ROUTES = {"/studio/strokes": "GET", "/studio/strokes/start": "POST",
                 "/studio/strokes/read": "GET","/studio/assets": "GET", "/studio/assets/save": "POST",
                 "/studio/preview": "POST", "/studio/drafts": "GET", "/studio/drafts/save": "POST"}


def dispatch(route, body, soul, params=None):
    try:
        if route == "/studio/strokes":
            return 200, {"jobs": [{k: v for k, v in stroke_painting.state(row).items() if k not in ("program", "raw_response", "draft_program", "review")}
                                 for row in art.painting_jobs()[:30]]}
        if route == "/studio/strokes/read":
            job = stroke_painting.state(art.painting_job((params or {}).get("id")))
            step = (params or {}).get("step")
            if step is not None:
                if not isinstance(step, str) or not step.isdecimal() or "program" not in job or not 0 <= int(step) <= len(job["program"]["strokes"]):
                    raise art.ArtError("笔触位置无效，或画作尚未生成")
                png = strokes.render(job["program"], aspect=job["request"]["aspect"], step=int(step))
                return 200, {"id": job["id"], "step": int(step), "image": "data:image/png;base64," + base64.b64encode(png).decode()}
            if job["status"] == "done":
                job["image"] = "data:image/png;base64," + base64.b64encode(hand_store.read_blob(job["image_path"])).decode()
                if job.get("draft_image_path"):
                    job["draft_image"] = "data:image/png;base64," + base64.b64encode(hand_store.read_blob(job["draft_image_path"])).decode()
            return 200, job
        if route == "/studio/assets":
            data = art.library()
            model_enabled = comfy.config()['enabled']
            return 200, {"assets": [dict(row, previews=[art.thumbnail(row, index=i) for i in range(len(row["images"]))]) for row in data["assets"]],
                         "changes": data["changes"][-30:], "categories": art.CATEGORIES,
                         "generation_available": True, "generation_modes": ["strokes", "image"] if model_enabled else ["strokes"],
                         "generation_note": "两种绘图均可选看图检查；逐笔可改一次，模型图保留原图并说明问题。本地模型生图已配置，需本机生图服务运行，通常需要数分钟。" if model_enabled else
                                            "逐笔绘画使用现有 DeepSeek，可选看图检查并修改一次；本地模型生图尚未启用。"}
        if route == "/studio/assets/save":
            return 200, art.save_asset(body)
        if route == "/studio/drafts":
            return 200, {"drafts": art.drafts()[:50]}
        composed = paint.compose(body, soul=soul)
        if route == "/studio/strokes/start":
            return 202, stroke_painting.start(composed)
        if route == "/studio/preview":
            return 200, composed
        if route == "/studio/drafts/save":
            return 200, art.save_draft(composed)
        return 404, {"ok": False, "message": "没有这个画室端点"}
    except art.ArtError as exc:
        return 400, {"ok": False, "message": str(exc)}
