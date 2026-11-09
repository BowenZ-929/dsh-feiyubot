"""YAMNet 声源线索；借鉴 dsh-soundcheck，复用已解码的 16 kHz 单声道。

类别索引来自 Google YAMNet / AudioSet；模型及来源见 SOUND-REFERENCE.md。
只选常见物理声源，不把音乐类别、年龄标签等解释成身份或心情。
"""
from pathlib import Path

LABELS = {0:'说话声', 13:'笑声', 24:'歌声', 35:'口哨声', 48:'脚步声', 62:'掌声',
          70:'狗叫声', 78:'猫叫声', 106:'鸟声', 132:'音乐', 135:'吉他声', 148:'钢琴声',
          159:'鼓声', 195:'铃声', 277:'风声', 281:'雷声', 282:'水声', 283:'雨声',
          294:'车辆声', 337:'引擎声', 348:'门的声音', 378:'打字声', 382:'警报声', 426:'烟花声'}


def classify(audio, model_path):
    data = {'status':'ok', 'source':'local_yamnet', 'events':[],
            'processed_seconds':round(len(audio)/16000, 3)}
    if len(audio) < 15600:
        return dict(data, status='short')
    if not Path(model_path).is_file():
        return dict(data, status='unavailable', reason='model_missing')
    # 可选模型初始化/推理失败不能丢掉已经获得的文字转写。
    try:
        import numpy as np
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        session = ort.InferenceSession(str(model_path), sess_options=options, providers=['CPUExecutionProvider'])
        scores = session.run([session.get_outputs()[0].name],
                             {session.get_inputs()[0].name:np.ascontiguousarray(audio, dtype=np.float32)})[0]
        means = scores.mean(axis=0)
        data['events'] = [{'label':LABELS[i], 'score':round(float(means[i]), 3)}
                          for i in sorted(LABELS, key=lambda i:float(means[i]), reverse=True)
                          if .2 <= means[i] <= 1][:5]
    except Exception:
        return dict(data, status='failed', reason='sound_model_failed')
    return data


def context(audio):
    sounds = audio.get('sound_events', {})
    if sounds.get('status') != 'ok':
        return ''
    labels = '、'.join(event['label'] for event in sounds['events'])
    detail = '可能有' + labels if labels else '没有足够明确的类别线索，不等于没有声音'
    return f"本地声音分类（可能有误，仅前{sounds['processed_seconds']}秒；分数不是正确率）：{detail}"
