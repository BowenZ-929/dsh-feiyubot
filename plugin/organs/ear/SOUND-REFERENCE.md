# 声音分类来源

借鉴用户推荐 [dsh-soundcheck](https://github.com/ALittleLov/dsh-soundcheck) 0.9.12（8654993b4db89df6cdb35a256e84f9c5121353f6，MIT）的本地 YAMNet 推理方案，没有复制其音乐分析代码。我们复用耳的解码，按片段平均分选常见物理声源；不使用它的音乐风格/情绪推断。

模型：Google YAMNet，Copyright 2019 The TensorFlow Authors，Apache License 2.0。原项目与类别定义：https://github.com/tensorflow/models/tree/master/research/audioset/yamnet 。许可：https://github.com/tensorflow/models/blob/master/LICENSE 。521 类输出，16 kHz 单声道输入，分数不是校准正确率。

ONNX 转换：https://huggingface.co/niobures/YAMNet ，文件 yamnetonnx/yamnet.onnx；SHA256 04e27fca08e7a3aea2630d1a63a51e6b437c803e0ff6e26399f80870ac251dda，与 soundcheck 公布值一致。模型不随源码提交；本地放在 .scratch/models/yamnet。

分类只提供弱线索；不能据此确定说话者年龄/身份、健康、情绪或所在地。平均分低于 0.2 不输出，最多五项；无标签不证明静音，音频超长时与转写共用前 30 秒。分类异常保留原转写。
