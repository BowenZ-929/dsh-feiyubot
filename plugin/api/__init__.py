"""魂的 Web 层：与框架无关的纯函数端点，加上一个本地验收用的开发服务器。"""
# 器官文件 import core 的公共件，core 末尾再回来汇总它们的路由表：所以 core 必须先落地。
# 这一行让 `import api.soul`（不先经 api.api）也不会撞上"半初始化的模块"。
from . import core  # noqa: F401
