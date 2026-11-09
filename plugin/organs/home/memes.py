"""家的表情图库：复用旧图和描述，人工修正可追溯；检索不决定是否发送。

参考 dsh-meme 的独立图库、描述/关键词候选接口。没有情绪随机兜底或外部模型调用。
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
from datetime import datetime

from PIL import Image, ImageOps
from . import residents, store

FORMATS = {'PNG':('png','image/png'), 'JPEG':('jpg','image/jpeg'),
           'GIF':('gif','image/gif'), 'WEBP':('webp','image/webp')}


class MemeError(ValueError):
    pass


def library_root(root=None):
    return store.home_root(root) / residents.WAREHOUSE_ROOM / '表情包'


def catalog(root=None):
    path = library_root(root) / 'index.json'
    return json.loads(path.read_text()) if path.exists() else {'items':[], 'changes':[]}


def chat_enabled(root=None):
    try:
        return catalog(root).get('chat_enabled', False) is True
    except store.StoreError:
        return False  # 尚未接入家时没有可发送的图库。


def set_chat_enabled(value, *, root=None):
    if type(value) is not bool:
        raise MemeError('聊天选图开关必须是布尔值')
    data = catalog(root)
    before = data.get('chat_enabled', False)
    if before != value:
        data['chat_enabled'] = value
        data['changes'].append({'at':datetime.now().astimezone().isoformat(), 'kind':'settings',
                               'before':{'chat_enabled':before}, 'after':{'chat_enabled':value}})
        store.atomic_write_text(library_root(root)/'index.json', json.dumps(data, ensure_ascii=False, indent=2))
    return {'chat_enabled':value}


def import_legacy(metadata, *, root=None):
    """本地显式迁移：复制图片，保留旧描述；相同图再次导入不覆盖人工修正。"""
    metadata = Path(metadata).resolve()
    source = json.loads(metadata.read_text())['images']
    data = catalog(root)
    known = {row['id'] for row in data['items']}
    imported, skipped, duplicates = 0, [], 0
    for key, old in source.items():
        relative = old.get('relative_path', '')
        path = (metadata.parent / relative).resolve()
        if metadata.parent not in path.parents or not path.is_file():
            skipped.append({'key':key, 'reason':'路径越界或文件不存在'})
            continue
        raw = path.read_bytes()
        if not raw:
            skipped.append({'key':key, 'reason':'图片为空'})
            continue
        ident = hashlib.sha256(raw).hexdigest()
        if ident in known:
            duplicates += 1
            continue
        try:
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.format not in FORMATS or picture.width * picture.height > 20_000_000:
                    raise MemeError('不支持的图片格式或尺寸')
                extension, mime = FORMATS[picture.format]
                thumb = ImageOps.exif_transpose(picture).convert('RGBA')
                thumb.thumbnail((240, 240))
                background = Image.new('RGBA', thumb.size, 'white')
                background.alpha_composite(thumb)
                output = io.BytesIO()
                background.convert('RGB').save(output, 'JPEG', quality=80)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            skipped.append({'key':key, 'reason':type(exc).__name__})
            continue
        description = str(old.get('caption') or '')
        row = {'id':ident, 'name':path.stem, 'file':ident+'.'+extension, 'mime':mime,
               'description':re.sub(r'^以当前分类.*?所代表的情绪、态度或用途为主体：', '', description),
               'visible_text':str(old.get('visible_text') or ''),
               'keywords':[t for t in old.get('tags', []) if isinstance(t,str) and not t.startswith('category:')],
               'category':str(old.get('category') or ''), 'enabled':True, 'reviewed':False, 'revision':1,
               'source':{'metadata':str(metadata), 'key':key, 'path':relative, 'description':description},
               'updated_at':datetime.now().astimezone().isoformat()}
        directory = library_root(root) / 'images'
        store.ensure_dir(directory)
        for filename, content in ((row['file'], raw), (ident+'.thumb.jpg', output.getvalue())):
            target = directory / filename
            target.write_bytes(content)
            target.chmod(0o600)
        data['items'].append(row)
        known.add(ident)
        imported += 1
    if imported:
        data['changes'].append({'at':datetime.now().astimezone().isoformat(), 'kind':'import',
                                'source':str(metadata), 'imported':imported})
        store.atomic_write_text(library_root(root)/'index.json', json.dumps(data, ensure_ascii=False, indent=2))
    return {'imported':imported, 'duplicates':duplicates, 'skipped':skipped, 'total':len(data['items'])}


def list_items(*, query='', offset=0, limit=20, enabled_only=False, root=None):
    if not isinstance(query,str) or len(query)>200 or type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
        raise MemeError('搜索限200字；分页条数1–100，偏移量不能为负')
    words = query.casefold().split()
    found = []
    for row in catalog(root)['items']:
        if enabled_only and not row['enabled']:
            continue
        text = ' '.join([row['description'], row['visible_text'], *row['keywords']]).casefold()
        score = sum(word in text for word in words)
        if not words or score:
            found.append((score, row))
    found.sort(key=lambda item:(-item[0], item[1]['id']))
    return {'total':len(found), 'offset':offset, 'limit':limit,
            'has_older':offset+limit<len(found), 'items':[r for _,r in found[offset:offset+limit]]}


def candidates(query, *, limit=8, root=None):
    if not query.strip():
        return []
    return [{k:row[k] for k in ('id','description','visible_text','keywords','reviewed')}
            for row in list_items(query=query, limit=limit, enabled_only=True, root=root)['items']]


def edit(body, *, root=None):
    if not isinstance(body,dict):
        raise MemeError('请提交要修改的表情资料')
    data = catalog(root)
    old = next((r for r in data['items'] if r['id']==body.get('id')), None)
    if old is None:
        raise MemeError('找不到这张表情，请核对表情编号')
    if type(body.get('revision')) is not int or body['revision']!=old['revision']:
        raise MemeError('这张表情已被修改，请刷新后再保存')
    for field in ('description','visible_text'):
        if not isinstance(body.get(field),str) or len(body[field])>2000:
            raise MemeError('描述和图片文字各限2000字')
    if any(type(body.get(k)) is not bool for k in ('enabled','reviewed')) or not isinstance(body.get('keywords'),list) or len(body['keywords'])>30 or any(not isinstance(t,str) or not 1<=len(t.strip())<=80 for t in body['keywords']):
        raise MemeError('启用开关须为布尔值，关键词最多30个，每个1–80字')
    updated = dict(old, **{k:body[k] for k in ('description','visible_text','keywords','enabled','reviewed')},
                   revision=old['revision']+1, updated_at=datetime.now().astimezone().isoformat())
    data['items'] = [updated if row['id']==old['id'] else row for row in data['items']]
    data['changes'].append({'at':updated['updated_at'], 'kind':'edit', 'before':old, 'after':updated})
    store.atomic_write_text(library_root(root)/'index.json', json.dumps(data, ensure_ascii=False, indent=2))
    return updated


def image(ident, *, root=None):
    row = next((r for r in catalog(root)['items'] if r['id']==ident), None)
    if row is None:
        raise MemeError('找不到这张表情，请核对表情编号')
    return (library_root(root)/'images'/row['file']).read_bytes(), row


def preview(row, *, root=None):
    raw = (library_root(root)/'images'/(row['id']+'.thumb.jpg')).read_bytes()
    return 'data:image/jpeg;base64,'+base64.b64encode(raw).decode()
