"""表情图库管理；不暴露导入任意本地路径或发送消息的HTTP入口。"""
import base64
from organs.home import memes

MEME_ROUTES = {'/memes/list':'GET', '/memes/read':'GET', '/memes/save':'POST', '/memes/settings':'POST'}


def dispatch(route, query, body):
    try:
        if route == '/memes/settings':
            return 200, memes.set_chat_enabled(body.get('chat_enabled') if isinstance(body,dict) else None)
        if route == '/memes/save':
            return 200, memes.edit(body)
        if route == '/memes/read':
            raw, row = memes.image(query.get('id'))
            changes = [c for c in memes.catalog()['changes'] if c.get('after',{}).get('id')==row['id']]
            return 200, dict(row, image='data:'+row['mime']+';base64,'+base64.b64encode(raw).decode(), changes=changes)
        page = memes.list_items(query=query.get('q',''), offset=int(query.get('offset','0')), limit=int(query.get('limit','20')))
        page['chat_enabled'] = memes.chat_enabled()
        page['items'] = [{k:r[k] for k in ('id','name','description','enabled','reviewed','revision')} |
                         {'preview':memes.preview(r)} for r in page['items']]
        return 200, page
    except (ValueError, OSError) as exc:
        return 400, {'ok':False, 'message':str(exc) if isinstance(exc,memes.MemeError) else '读取失败或分页参数无效'}
