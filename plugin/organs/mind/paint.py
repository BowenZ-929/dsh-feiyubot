"""生图前的构图装配：魂的形象、家中的参考素材、日子此刻衣着/地点。

移植 dsh-image-studio/compose.js 的参考职责与显式选择优先规则，见 paint-REFERENCE-LICENSE.txt。
这里不调用模型，不生成图片，不发送 QQ。
"""
from organs.home import art
from organs.day import life

REFERENCE_ROLES = {
    "characters": "只参考脸型五官、发型发色、瞳色、体型比例和标志性特征；不照搬姿势、表情、视角、衣着或背景。忽略人设卡上的文字与分格布局。",
    "outfits": "只参考服装的款式、颜色、材质和细节，穿在本次人物身上，随动作自然变化；不引入图中的其他人物。",
    "scenes": "只参考空间、陈设、配色与风格，镜头角度与取景按本次画面安排。",
    "props": "只参考道具外形、材质与配色。",
}


def compose(body, *, soul=None, root=None, day_root=None, now=None):
    if not isinstance(body, dict):
        raise art.ArtError("请填写画面描述")
    prompt = body.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
        raise art.ArtError("画面描述不能为空，限 2000 字")
    mode = body.get("mode", "image")
    if mode not in ("image", "strokes"):
        raise art.ArtError("选择模型生图或逐笔绘画")
    aspect = body.get("aspect", "1:1")
    if aspect not in ("1:1", "3:4", "4:3", "9:16", "16:9"):
        raise art.ArtError("选择支持的画面比例")
    self_portrait, follow = body.get("self_portrait", False), body.get("follow_schedule", False)
    if type(self_portrait) is not bool or type(follow) is not bool:
        raise art.ArtError("画她和跟随日程选项需要 true 或 false")
    visual_review = body.get("visual_review", False)
    if type(visual_review) is not bool:
        raise art.ArtError("看图检查选项需要 true 或 false")
    identifiers = body.get("assets", [])
    if not isinstance(identifiers, list) or len(identifiers) > 12 or any(not isinstance(i, str) for i in identifiers):
        raise art.ArtError("最多选择 12 份素材")
    assets = {row["id"]: row for row in art.library(root)["assets"]}
    if any(ident not in assets for ident in identifiers):
        raise art.ArtError("所选素材已不存在，请刷新后重新选择")
    selected = [assets[ident] for ident in dict.fromkeys(identifiers)]
    if sum(row["category"] == "scenes" for row in selected) > 1:
        raise art.ArtError("一次只选择一个场景，避免背景冲突")
    selected.sort(key=lambda row: list(art.CATEGORIES).index(row["category"]))
    parts = ["【画面】" + prompt.strip(), "【画面比例】" + aspect]
    notes = []
    if self_portrait:
        if soul is None:
            raise art.ArtError("角色设定尚未加载")
        identity = soul.snapshot().identity
        parts.append("【本次人物】" + identity.display + "：" + identity.appearance)
    state = None
    if follow and self_portrait:
        current = life.view(now, root=day_root)
        if current["enabled"] and current["current"]:
            state = {"day": current["day"], "revision": current["plan"]["revision"],
                     "at": current["now"], "current": current["current"]}
            for category, field in (("outfits", "outfit"), ("scenes", "location")):
                value = current["current"][field]
                if value and not any(row["category"] == category for row in selected):
                    parts.append("【日程中的" + art.CATEGORIES[category] + "】" + value)
                    notes.append("沿用此刻" + art.CATEGORIES[category] + "：" + value + "（文字描述）")
                elif any(row["category"] == category for row in selected):
                    notes.append("已选" + art.CATEGORIES[category] + "优先，不套用日程中的" + art.CATEGORIES[category])
        else:
            notes.append("当前没有启用且有效的生活安排，未添加日程衣着或地点")
    elif follow:
        notes.append("未勾选“画她”，未把她的日程带进其他画面")
    refs = []
    # 每份素材轮流选一张，防止人物的多张参考挤掉场景/衣着；上限 6 张。
    if mode == "strokes" and any(asset["images"] for asset in selected):
        notes.append("逐笔绘画只使用素材文字描述，尚未读取参考图片；画布检查由单独选项控制")
    for index in range(4 if mode == "image" else 0):
        for asset in selected:
            if index < len(asset["images"]) and len(refs) < 6:
                image = asset["images"][index]
                refs.append(dict(image, asset_id=asset["id"], asset_revision=asset["revision"],
                                 name=asset["name"], category=asset["category"], role=REFERENCE_ROLES[asset["category"]]))
    for asset in selected:
        category = art.CATEGORIES[asset["category"]]
        chosen = [str(i + 1) for i, ref in enumerate(refs) if ref["asset_id"] == asset["id"]]
        parts.append("【" + category + "·" + asset["name"] + "】" + asset["description"])
        if chosen:
            parts.append("参考图 " + "、".join(chosen) + "：" + REFERENCE_ROLES[asset["category"]])
        else:
            notes.append(asset["name"] + "仅按文字描述" + ("（逐笔模式）" if mode == "strokes" else "（参考图超过 6 张上限）" if asset["images"] else "（没有参考图）"))
    parts.append("画面文字明确指定的衣着、场景优先；选中的服饰和场景其次，日程只补充未指定的部分。")
    if refs:
        parts.append("画一张完整的新画面，不要拼贴参考图、排成三视图、加入参考图中的标题或作为画中画。"
                     "构图、动作、视角和光线以本次画面要求为准。明确指定的衣着和场景优先于人物参考图中的衣着与背景。")
    return {"mode": mode, "prompt": prompt.strip(), "final_prompt": "\n\n".join(parts), "aspect": aspect,
            "self_portrait": self_portrait, "follow_schedule": follow, "visual_review": visual_review,
            "selected": selected, "references": refs, "schedule": state, "notes": notes}
