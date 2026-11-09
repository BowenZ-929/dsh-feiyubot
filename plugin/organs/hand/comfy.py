"""手调用本机 ComfyUI；不接触聊天、人格或记忆，不发送 QQ。

节点接线依据已安装 ComfyUI 的 object_info/nodes_qwen，以及参考仓库的
Qwen 2.1 Mac 实测说明。后端独立运行，Python 模型依赖不进入器官核。
"""
import io
import json
import secrets
import threading
import time
from urllib import parse, request
import uuid

from PIL import Image, ImageOps
from organs import paths

BASE_URL = 'http://127.0.0.1:8188'
MODEL = 'qwen_image_2.1_Q4_K_M.gguf'
ENCODER = 'qwen3vl_8b_w4a8.safetensors'
VAE = 'qwen_image_2.1_vae_bf16.safetensors'
SIZES = {'1:1':(768, 768), '3:4':(768, 1024), '4:3':(1024, 768),
         '9:16':(576, 1024), '16:9':(1024, 576)}


class ImageError(ValueError):
    def __init__(self, message, *, kind='execution'):
        super().__init__(message)
        self.kind = kind


def config():
    path = paths.organ_dir('hand') / 'image.json'
    return json.loads((path if path.is_file() else paths.factory_dir('hand') / 'image.json').read_text())


def _read(route, data=None, content_type='application/json'):
    req = request.Request(BASE_URL + route, data=data, headers={'Content-Type':content_type})
    with request.urlopen(req, timeout=20) as response:
        return response.read()


def _api(route, body=None):
    raw = _read(route, json.dumps(body).encode() if body is not None else None)
    return json.loads(raw) if raw else {}


def workflow(prompt, *, width, height, seed, steps, references=()):
    graph = {
        'unet': {'class_type':'UnetLoaderGGUF', 'inputs':{'unet_name':MODEL}},
        'clip': {'class_type':'CLIPLoader', 'inputs':{'clip_name':ENCODER, 'type':'qwen_image'}},
        'vae': {'class_type':'VAELoader', 'inputs':{'vae_name':VAE}},
        'encode': {'class_type':'TextEncodeQwenImage21', 'inputs':{
            'clip':['clip', 0], 'prompt':prompt, 'negative_prompt':'', 'resolution':768}},
        'latent': {'class_type':'EmptyLatentImage', 'inputs':{'width':width, 'height':height, 'batch_size':1}},
        'sample': {'class_type':'KSampler', 'inputs':{
            'model':['unet', 0], 'positive':['encode', 0], 'negative':['encode', 1],
            'latent_image':['latent', 0], 'seed':seed, 'steps':steps, 'cfg':1.0,
            'sampler_name':'euler', 'scheduler':'simple', 'denoise':1.0}},
        'decode': {'class_type':'VAEDecode', 'inputs':{'samples':['sample', 0], 'vae':['vae', 0]}},
        'save': {'class_type':'SaveImage', 'inputs':{'images':['decode', 0], 'filename_prefix':'qqagent'}},
    }
    for index, name in enumerate(references, 1):
        ident = 'ref' + str(index)
        graph[ident] = {'class_type':'LoadImage', 'inputs':{'image':name}}
        graph['encode']['inputs']['images.image_' + str(index)] = [ident, 0]
        graph['encode']['inputs']['vae'] = ['vae', 0]
    return graph


def generate(prompt, *, width, height, references=(), cancel=None, submitted=None, steps=25, seed=None, timeout=1800):
    """输入为构图层已验证的提示/尺寸和素材字节，返回图片字节与实际任务编号。"""
    cancel = cancel if cancel is not None else threading.Event()
    names = []
    for raw in references:
        if cancel.is_set():
            raise ImageError('绘画已取消')
        # 仅传标准 PNG 给固定本机入口，不传原文件路径或素材库目录。
        with Image.open(io.BytesIO(raw)) as picture:
            buffer = io.BytesIO()
            oriented = ImageOps.exif_transpose(picture).convert('RGBA')
            background = Image.new('RGBA', oriented.size, 'white')
            background.alpha_composite(oriented)
            background.convert('RGB').save(buffer, format='PNG')
        boundary = uuid.uuid4().hex
        header = ('--' + boundary + '\r\nContent-Disposition: form-data; name="image"; '
                  'filename="qqagent-' + boundary + '.png"\r\nContent-Type: image/png\r\n\r\n').encode()
        uploaded = json.loads(_read('/upload/image', header + buffer.getvalue() +
                                    ('\r\n--' + boundary + '--\r\n').encode(),
                                    'multipart/form-data; boundary=' + boundary))
        names.append('/'.join(part for part in (uploaded.get('subfolder', ''), uploaded['name']) if part))
    if cancel.is_set():
        raise ImageError('绘画已取消')
    seed = secrets.randbits(32) if seed is None else seed
    graph = workflow(prompt, width=width, height=height, seed=seed, steps=steps, references=names)
    reply = _api('/prompt', {'prompt':graph, 'client_id':'qqagent'})
    if reply.get('node_errors') or not reply.get('prompt_id'):
        raise ImageError('本地生图工作流未被接受')
    ident = reply['prompt_id']
    info = {'prompt_id':ident, 'model':MODEL, 'seed':seed, 'steps':steps, 'reference_count':len(names), 'workflow':graph}
    if submitted is not None:
        submitted(info)
    deadline = time.monotonic() + timeout
    while True:
        if cancel.is_set() or time.monotonic() >= deadline:
            _api('/queue', {'delete':[ident]})
            _api('/interrupt', {'prompt_id':ident})
            raise ImageError('绘画已取消' if cancel.is_set() else '本地生图超时',
                             kind='execution' if cancel.is_set() else 'timeout')
        result = _api('/history/' + ident).get(ident)
        if result is not None:
            if result['status']['status_str'] != 'success':
                raise ImageError('本地模型生成失败，详情保留在本机生图日志')
            image = result['outputs']['save']['images'][0]
            return _read('/view?' + parse.urlencode(image)), info
        cancel.wait(1)
