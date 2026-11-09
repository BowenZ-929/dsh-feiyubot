"""画室素材放在家·仓库，构图草稿放在工作台；不把草稿当生成图或亲历。

参考 dsh-image-studio 的人物/服饰/场景/道具分类。图片内容寻址，旧草稿引用不随素材修改漂移。
"""
import base64
import binascii
import hashlib
import io
import json
import uuid
from datetime import datetime

from PIL import Image, ImageOps, UnidentifiedImageError

from . import residents, store

CATEGORIES = {"characters": "人物", "outfits": "服饰", "scenes": "场景", "props": "道具"}
MAX_BYTES = 8 * 1024 * 1024
FORMATS = {"PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"), "WEBP": (".webp", "image/webp")}


class ArtError(ValueError):
    pass


def library_root(root=None):
    return store.home_root(root) / residents.WAREHOUSE_ROOM / "画室"


def library(root=None):
    path = library_root(root) / "assets.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"assets": [], "changes": []}


def save_asset(body, *, root=None):
    if not isinstance(body, dict) or not isinstance(body.get("category"), str) or body["category"] not in CATEGORIES:
        raise ArtError("选择人物、服饰、场景或道具")
    fields = {"category": body["category"]}
    for key, limit in (("name", 80), ("description", 1200)):
        value = body.get(key, "")
        if not isinstance(value, str) or len(value) > limit or (key == "name" and not value.strip()):
            raise ArtError("名称不能为空，限 80 字；描述限 1200 字")
        fields[key] = value.strip()
    data = library(root)
    ident = body.get("id")
    old = next((row for row in data["assets"] if row["id"] == ident), None)
    if ident and old is None:
        raise ArtError("素材不存在")
    if old and body.get("revision") != old["revision"]:
        raise ArtError("素材已被修改，请刷新后再保存")
    files = body.get("images")
    validated = []
    if files is not None:
        if not isinstance(files, list) or len(files) > 4:
            raise ArtError("每份素材最多 4 张参考图")
        for encoded in files:
            if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES + 2) // 3 * 4:
                raise ArtError("每张图限 8 MB")
            try:
                raw = base64.b64decode(encoded, validate=True)
                if not raw or len(raw) > MAX_BYTES:
                    raise ArtError("图片为空或超过 8 MB")
                with Image.open(io.BytesIO(raw)) as picture:
                    if picture.format not in FORMATS or getattr(picture, "n_frames", 1) != 1:
                        raise ArtError("参考图需要静态 PNG、JPEG 或 WebP")
                    if picture.width * picture.height > 24_000_000:
                        raise ArtError("图片尺寸超过 2400 万像素")
                    picture.load()
                    ext, mime = FORMATS[picture.format]
                    thumb = ImageOps.exif_transpose(picture).convert("RGB")
                    thumb.thumbnail((320, 320))
                    stream = io.BytesIO()
                    thumb.save(stream, format="JPEG", quality=80)
                    digest = hashlib.sha256(raw).hexdigest()
                    meta = {"file": digest + ext, "sha256": digest, "mime": mime,
                            "width": picture.width, "height": picture.height,
                            "thumbnail": digest + ".thumb.jpg"}
                    validated.append((meta, raw, stream.getvalue()))
            except (binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise ArtError("图片无法读取，请重新选择有效图片") from exc
    # 所有上传件通过验证后才落盘，错误的第二张不会留下第一张的半份素材。
    directory = library_root(root) / "images"
    for meta, raw, thumbnail in validated:
        store.ensure_dir(directory)
        for name, content in ((meta["file"], raw), (meta["thumbnail"], thumbnail)):
            path = directory / name
            if not path.exists():
                path.write_bytes(content)
                path.chmod(0o600)
    at = datetime.now().astimezone().isoformat()
    asset = dict(fields, id=ident or uuid.uuid4().hex, revision=(old or {}).get("revision", 0) + 1,
                 updated_at=at, images=[row[0] for row in validated] if files is not None else (old or {}).get("images", []))
    data["assets"] = [asset if row["id"] == ident else row for row in data["assets"]]
    if old is None:
        data["assets"].append(asset)
    data["changes"].append({"at": at, "before": old, "after": asset})
    store.atomic_write_text(library_root(root) / "assets.json", json.dumps(data, ensure_ascii=False, indent=2))
    return asset


def thumbnail(asset, *, root=None, index=0):
    if not asset["images"]:
        return None
    path = library_root(root) / "images" / asset["images"][index]["thumbnail"]
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def drafts(root=None):
    directory = store.home_root(root) / residents.DESK_ROOM / "画室草稿"
    return sorted((json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")),
                  key=lambda row: row["created_at"], reverse=True)


def save_draft(composed, *, root=None):
    draft = dict(composed, id=uuid.uuid4().hex, status="draft", created_at=datetime.now().astimezone().isoformat())
    path = store.home_root(root) / residents.DESK_ROOM / "画室草稿" / (draft["id"] + ".json")
    store.atomic_write_text(path, json.dumps(draft, ensure_ascii=False, indent=2))
    return draft


def painting_jobs(root=None):
    directory = store.home_root(root) / residents.DESK_ROOM / "绘画任务"
    return sorted((json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")),
                  key=lambda row: row["created_at"], reverse=True)


def painting_job(ident, *, root=None):
    if not isinstance(ident, str) or len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
        raise ArtError("画作编号不正确")
    path = store.home_root(root) / residents.DESK_ROOM / "绘画任务" / (ident + ".json")
    if not path.exists():
        raise ArtError("画作不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def save_painting_job(job, *, root=None):
    path = store.home_root(root) / residents.DESK_ROOM / "绘画任务" / (job["id"] + ".json")
    store.atomic_write_text(path, json.dumps(job, ensure_ascii=False, indent=2))
